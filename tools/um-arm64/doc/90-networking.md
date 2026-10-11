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

* netlink link enumeration is denied to ordinary uids -- passt reports
  `Invalid interface name wlan0: Permission denied`;
* the routing table is per-uid, so several default routes are visible and passt
  warns `Multiple default IPv4 routes, picked first`;
* there is no `/etc/resolv.conf` at all (DNS goes through netd), so passt gives
  up with `No IPv4 nameserver available for DHCP` and the guest gets no lease.

So umnet passes the whole configuration explicitly -- `-4 -a 10.0.2.15 -n 24
-g 10.0.2.2 -D 1.1.1.1` -- and passt then needs to discover nothing. It still
reaches the network by opening ordinary sockets, which is what makes this work
without root.

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
