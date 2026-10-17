# Networking for an unrooted guest

The guest gets a working network -- DHCP, DNS, TCP, ICMP -- on a phone where the
process running it has uid 2000, no capabilities, no root and no user
namespaces.

## Why the obvious routes are all closed

UML's network transports, and why each one is unavailable here:

| transport | needs | on Android as `shell` |
| --- | --- | --- |
| `tap` | `/dev/net/tun`, CAP_NET_ADMIN | no |
| `raw` | raw sockets, CAP_NET_RAW | no |
| `gre`, `l2tpv3` | raw sockets | no |
| `bess`, `vde` | a switch daemon, which still needs a tap to reach the world | no |
| **`fd`** | **a descriptor somebody else set up** | **yes** |

Modern UML has also dropped the old `slirp` and `slip` transports; only the
vector driver remains. So the only door left is `fd`, which reads and writes
Ethernet frames on a descriptor and asks no questions about where it came from.

On the other side of that descriptor has to be a userspace TCP/IP stack: a
program that accepts the guest's Ethernet frames, terminates the connections
itself, and reaches the real network the only way an unprivileged process can --
by opening ordinary sockets.

## The pieces

```
   guest            UML kernel              umnet                  passt
  vec0  <--frames--> vector fd  <--SOCK_DGRAM--> pump <--SOCK_STREAM--> tap
                                             (adds/strips              |
                                              length prefix)      ordinary
                                                                  sockets
                                                                      |
                                                                  the network
```

**passt** is the stack. It was chosen over libslirp for one practical reason:
libslirp depends on glib, and cross-compiling glib statically for Android is a
project in itself. passt is plain C with nothing but libc, so
`make CC=aarch64-linux-gnu-gcc LDFLAGS=-static` produces a 1.4 MB binary that
runs on the phone as-is.

**umnet** (`harness/umnet.c`) exists because of a framing mismatch, and only
that:

* UML's `fd` transport does one frame per datagram -- boundaries come from the
  socket.
* passt speaks qemu's socket protocol on a stream -- each frame preceded by its
  length as a 4-byte big-endian integer.

So umnet holds a `SOCK_DGRAM` socketpair towards UML and a `SOCK_STREAM`
socketpair towards passt, adds the prefix in one direction and strips it in the
other, and execs the UML kernel with the guest end inherited, appending
`vec0:transport=fd,fd=N,mac=...` to its command line.

## One kernel change

`CONFIG_UML_NET_VECTOR` used to `select MAY_HAVE_RUNTIME_DEPS`, and
`CONFIG_STATIC_LINK` depends on that symbol being unset -- so enabling vector
networking made a static build impossible. On Android the binary *must* be
static, because there is no glibc to link against. The combination was therefore
unbuildable, which is to say: no networking at all.

The select was over-broad. `getaddrinfo()` appears exactly twice in the driver,
both inside `user_init_socket_fds()`, reached only by the `gre` and `l2tpv3`
transports. `tap`, `raw`, `bess`, `vde` and `fd` never call it, and where it is
called a static binary degrades rather than breaks -- numeric addresses resolve
without NSS. Dropped it, with the reasoning recorded in the Kconfig.

## How passt is made to run on Android

passt self-isolates aggressively at startup. Every one of those steps assumes a
capability an Android app process does not have. Measured on Termux (Android 13,
kernel 5.15.194, ordinary app uid):

| call | result |
| --- | --- |
| `socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE)` | `EPERM` |
| `setgroups(0, NULL)` | `SIGSYS` (app seccomp kills the process) |
| `clone(CLONE_NEWUSER)` | `EINVAL` |
| `unshare(CLONE_NEWNS\|CLONE_NEWIPC\|CLONE_NEWUTS)` | `EPERM` |

A passt that only has to serve one prearranged interface -- one end of a
socketpair handed over by umnet -- needs none of them. passt therefore detects
that case and skips the whole dance. The check is `fd_prepatched()`: the `--fd`
descriptor is a socket, so a parent already prepared the interface.

* `conf()`: with an explicit configuration there is nothing to discover, so
  `nl_sock_init()` is skipped and the local-mode IPv4 values are used directly.
* `isolate_user()`: returns early -- no `setgroups()`, no user namespace.
* `isolate_prefork()`: skips `unshare()` and the mount/`pivot_root` sandbox,
  still dropping capabilities.
* `passt.c`: skips `nl_neigh_notify_init()` for the same reason.
* `make_ugid_map()`: writing `deny` to `/proc/<pid>/setgroups` is only needed to
  make the gid map writable on kernels that have the file; failure is no longer
  fatal.

This is upstream in `Raymer8639/passt-android` as commit `25f9c1f`
(PR #1). The `--fd`-less and non-socket-`--fd` paths -- ordinary passt and pasta
-- are untouched and still take the full isolation sequence.

The trade is stated plainly: in this mode passt runs as an ordinary
unprivileged uid with no namespace isolation. Its **seccomp filter still
applies**, so the syscall surface is still restricted, and the process never had
privileges to lose in the first place. The alternative was no networking.

Worth sending upstream: "run correctly when handed an interface socket and
denied namespace creation" is a reasonable thing for passt to support.

## Configuration passt cannot discover

passt normally reads the host's addresses, routes and `/etc/resolv.conf`. On
Android all three fail:

* route netlink is denied to ordinary uids. Measured on the Android 15 device:
  `socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE)` succeeds but `bind((0, 0))`
  returns `EACCES`; passt without the prepatched path fails with `Failed to
  get netlink socket` (the older Android 13 measurement in this project's
  history was `EPERM`);
* the routing table is per-uid, so several default routes are visible and passt
  warns `Multiple default IPv4 routes, picked first`;
* there is no `/etc/resolv.conf` at all (DNS goes through netd), so passt gives
  up with `No IPv4 nameserver available for DHCP` and the guest gets no lease.

So umnet passes the whole configuration explicitly -- `-4 -a 10.0.2.15 -n 24
-g 10.0.2.2 -D 1.1.1.1` for IPv4 and `-6 -a fd00::2/64 -g fe80::1 -D ...` for
IPv6 -- and passt then needs to discover nothing and opens no netlink socket. It
still reaches the network by opening ordinary sockets, which is what makes this
work without root. A passt build without the prepatched-IPv6 fix needs netlink
for IPv6 though; that is why IPv6 can degrade to "no IPv6" while IPv4 keeps
running (see [IPv6](#ipv6)).

## Result

From the guest, on the phone:

```
udhcpc: lease of 10.0.2.15 obtained from 10.0.2.2, lease time 4294967295
    inet 10.0.2.15/24 scope global vec0
default via 10.0.2.2 dev vec0
nslookup example.com  ->  104.20.23.154, 172.66.147.243
ping -c 2 10.0.2.2    ->  2 packets transmitted, 2 packets received (5.6-6.4 ms)
wget http://example.com/  ->  OK, 577 bytes
<!doctype html><html lang=en><head><title>Example Domain</title>...
```

ICMP works too: passt implements ping with `SOCK_DGRAM` ICMP sockets, which
Android permits via `ping_group_range`.

`./deb` wires this up automatically when `umnet` and `passt` are present next to
the kernel; `UMDEB_NET=0` turns it off.

## IPv6

The guest can have IPv6 too. It is served by a *second* passt, and the reason is
failure isolation rather than a netlink limitation any more.

passt supports both families in one process. Over an inherited socket on
Android the "prepatched" path (`--fd`, no namespaces) used to configure IPv4
locally and then still call `conf_ip6()`, which needed the netlink socket that
path deliberately never opened -- one dual-stack passt died at start-up with

    netlink: Failed to send(): Bad file descriptor

That is fixed in passt-android (PR #2, commit `1a0c657`): the prepatched branch
now configures both families locally (`conf_ip4_local()`/`conf_ip6_local()`) and
never calls `nl_sock_init()`, taking the addresses, prefixes, gateway and
resolver from `-a`/`-g`/`-D`. A single dual-stack passt is therefore possible;
this bridge keeps two helpers so that a lost IPv6 helper cannot affect IPv4 and
`--passt-pid-file` keeps naming the IPv4 helper whose loss ends the run.

So `umnet --ipv6` forks an IPv4-only passt *and* an IPv6-only passt, and routes
each guest frame by its EtherType: `0x86dd` to the IPv6 helper, everything else
-- IPv4 and ARP -- to the IPv4 helper. On the wire that is a router with one
address per family; each helper is an independent point-to-point link and
neither hears the other's frames. The IPv4 helper's loss still ends the run;
the IPv6 helper's loss does not.

The helper needs a passt built with the prepatched-IPv6 support. An older passt
opens `NETLINK_ROUTE` for IPv6, and Android denies `bind((0, 0))` on a route
socket to an app uid (`EACCES`), so the helper exits before its event loop;
umnet prints

    umnet: ipv6 helper exited (N); continuing without IPv6

and IPv4 keeps working. That is a degraded environment, not a broken bridge, and
the gate reports it as `verdict=SKIP` rather than FAIL.

The guest also needs a kernel built with IPv6. `arch/um/configs/arm64_defconfig`
sets `CONFIG_IPV6=y`; a `linux-bionic` built before that change has no IPv6
stack at all, so no userspace command can bring it up and the gate skips with
that reason.

Verified on the device (2026-10-17): IPv4 gate 9/9 PASS and the `--ipv6` gate
14/14 PASS, with `inet6 fd00::2/64 scope global`, `default via fe80::1 dev vec0`,
`NET_PING6_RC=0`, `NET_IPV6_AAAA=2606:4700:10::ac42:93f3` and
`NET_IPV6_TCP_OK=1` in the guest log. The record is
`docs/evidence/2026-10-17-umnet-network-ipv6.json` and
`record-evidence.sh check --verify-hashes` passes on it. Swapping the old and
the new `passt-bionic` on the same phone is an A/B: the old one exits with
`Failed to get netlink socket`, the new one answers router solicitations.

### What the guest gets

| item | value | option |
| --- | --- | --- |
| guest address | `fd00::2/64` | `--ipv6-addr` |
| gateway | `fe80::1` | `--ipv6-gw` |
| resolver | `2001:4860:4860::8888` | `--ipv6-dns` (or `none`) |

The prefix is a ULA on purpose: passt terminates the link and acts as the
router, and outbound traffic leaves through the phone's real IPv6 address. A
global prefix copied from `wlan0` would instead make passt proxy NDP for the
guest on the home LAN, which needs router cooperation and is not the job of
this bridge.

passt answers router solicitation with an RA carrying that prefix (A flag),
offers the address over DHCPv6, answers neighbour solicitations, and answers
ICMPv6 echo requests to the gateway by mapping it to the host's `::1` -- the
same trick it uses for `10.0.2.2` in IPv4. UDP and TCP reach the real network
through ordinary host sockets.

### Limits

* **Two helpers, one per family.** With the prepatched-IPv6 passt a single
  dual-stack process would work too; the two-process split is kept for failure
  isolation and because it leaves the IPv4 helper's pid semantics unchanged. It
  costs one extra passt (a few MB of RSS) plus one fork.
* **No NAT64/DNS64.** An IPv6-only destination has to be reachable over the
  phone's IPv6 uplink; an IPv4-only destination stays an IPv4 destination.
* **Inbound is still closed.** Both helpers are started with `-t none -u none`.
* **`--passt-pid-file` names the IPv4 helper**, the one whose loss ends the
  run. The IPv6 helper's pid is not written anywhere.
* **It needs a `passt-bionic` built with the prepatched-IPv6 support.** An
  older build falls back to netlink, which Android denies, and the run degrades
  to IPv4 only (see above). The fix is in `Raymer8639/passt-android` `main`
  from commit `1a0c657`.

### Gate

**`harness/test-umnet-network.sh --ipv6`** runs the same guest with both
helpers and adds five checks to the nine IPv4 ones: `ipv6-addr` (the static
address is on `vec0`), `ipv6-route` (default via the passt gateway),
`ipv6-icmp` (`ping6` to the gateway, exit 0), `ipv6-dns` (AAAA lookup through
the IPv6 resolver) and `ipv6-tcp` (a fetch to the resolved IPv6 literal, so the
guest cannot fall back to IPv4). `--link-only` keeps only the three link-level
checks and is the mode for a host with no IPv6 uplink.

The gate checks the host's own IPv6 egress before booting. Three environment
facts make a judged verdict impossible, and all three produce
`verdict=SKIP` (exit 77), never a PASS or a FAIL:

1. the host has no IPv6 egress and `--link-only` was not given;
2. the guest's kernel has no IPv6 (`NET_IPV6_KERNEL=no`, i.e. the old
   `linux-bionic` built without `CONFIG_IPV6`);
3. the IPv6 helper exited at start-up (`umnet: ipv6 helper exited`).

On the device this was developed on, the uplink does have IPv6: `wlan0` holds a
global `2409:...` address, a default route via `fe80::a:1`, and both `curl -6`
and a DNS query to `2001:4860:4860::8888` succeed from the app uid. A run needs
a `linux-bionic` built with `CONFIG_IPV6=y` and a `passt-bionic` with the
prepatched-IPv6 fix. Archive it with the extra gate arguments (the recorder's
`--gate-arg` carries them):

```sh
tools/um-arm64/harness/record-evidence.sh record \
    --workdir "$PREFIX/tmp/um-network" --assets-dir "$PREFIX/tmp/um-arm64/new" \
    --dns 8.8.8.8 \
    --gate-arg=--ipv6 --gate-arg=--ipv6-dns=2001:4860:4860::8888 \
    --label umnet-network-ipv6
```

The record from the first full IPv6 run is
`docs/evidence/2026-10-17-umnet-network-ipv6.json`.

## Gates

Two gates cover this path, and neither replaces the other.

**`harness/test-umnet.sh`** is the protocol gate and runs anywhere. It compiles
`harness/umnet.c` and exercises its real process boundary with mock UML and
passt binaries: the vector fd argument, passt configuration, bidirectional
framing, fragmented stream input, and clean child reaping.

**`harness/test-umnet-network.sh`** is the Termux host gate and needs a real
kernel, rootfs and passt build. Run it on the phone, not inside a container:

```sh
python3 harness/test-umnet-network.sh --workdir "$PREFIX/tmp/um-network"
```

It boots the guest through `umnet`, then judges nine things from the guest's own
output: DHCP lease, address, default route, announced resolver, ICMP, DNS
resolution, IPv4 TCP fetch, six concurrent fetches, and clean command exit. It
prints `verdict=PASS|FAIL` and writes the full console log next to the workdir.
Setup it expects:

| path | what |
| --- | --- |
| `$PREFIX/tmp/um-arm64/new/linux-bionic` | UML kernel |
| `$PREFIX/tmp/um-arm64/new/stub_exe` | stub for the kernel |
| `<workdir>/passt-bionic` | passt built for Android 30 |
| `<workdir>/umnet` | bridge built from `harness/umnet.c` |
| `<workdir>/netroot` | Alpine hostfs root |
| `<workdir>/netroot/um-init` | the guest's init; copy `harness/netroot-um-init.sh` in |

The last row is easy to miss, because the kernel does not say "a file is
missing from the rootfs". It says:

```
Kernel panic - not syncing: Requested init /um-init failed (error -2).
```

which reads like a kernel fault and is `ENOENT` for a path inside the root the
gate handed to `rootfstype=hostfs`. The gate writes `net-command.sh` itself but
expects the init that calls it to be part of the root, so a root assembled from
a stock Alpine minirootfs boots straight into that panic. Copy the script in and
make it executable:

```sh
install -m 755 harness/netroot-um-init.sh <workdir>/netroot/um-init
```

The guest command script is written by the gate itself, so the checks and the
commands that produce them cannot drift apart. Five consecutive runs pass with
no leftover processes.

One trap worth knowing: UML needs a **controlling terminal**. With
`con0=fd:0,fd:1` on a plain pipe the guest hangs before userspace, so the gate
runs the bridge on a pty, exactly as `boot.py` does.

## Throughput

Guest and host fetching the same 10 MB payload from the public internet, so the
number includes the phone's uplink:

| where | time | rate |
| --- | --- | --- |
| Termux host (no UML, no bridge) | 14.2 s | 5.9 Mbit/s |
| guest through UML + umnet + passt | 15.9-17.9 s | 4.7-5.3 Mbit/s |

So the bridge costs roughly 10-20% on a transfer that is already bounded by the
uplink. It is not the bottleneck, and a faster phone uplink would be the first
thing to change before this measurement says anything about the bridge itself.
`vm.net.vector` work is not worth starting on the strength of these numbers.

Latency to the emulated gateway is single-digit milliseconds (ICMP RTT
5.6-6.4 ms, measured in the host gate).

## Gotchas


* **The guest interface is `vec0`, not `eth0`.** UML's vector driver names them
  that way. Half an hour went into a test script that configured an interface
  that did not exist.
* **The disk image is flock'd.** An interactive `./deb` session holds it, and any
  other run then aborts during root mount -- which looks exactly like a corrupt
  filesystem until you read the message. Also worth remembering when running
  benchmarks while somebody is using the guest.
* **Binding a UNIX socket under `/data/local/tmp` is denied** by SELinux, so
  passt's `-s` mode cannot be used. `-F` with an inherited descriptor can, which
  is what umnet does anyway.
* **A guest that powers off does not end the run by itself.** UML closes its end
  of the datagram pair, but passt holds its own end open forever -- it has no way
  to learn the guest is gone -- so a pump that only waits for the link to close
  blocks in `poll()` indefinitely. `umnet` therefore waits on the kernel with
  `WNOHANG` between poll rounds as well; otherwise it never reaps the kernel or
  reports its exit status, and an automated gate hangs instead of failing.
