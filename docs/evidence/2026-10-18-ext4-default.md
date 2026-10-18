# ext4 default root through `neoproot-um` (2026-10-18)

Device runs of the neoproot UM backend after v5.13.0 made **ext4 + COW the
default root for `--backend=um`** (hostfs stays available with `--hostfs` and
remains the only option for the PRoot tracer backend). The point is the storage
path the release claims: the guest boots from an ext4 image through the
kernel's `ubd` device, the base image is untouched, and the supervisor still
returns the guest's exit code and leaves nothing behind.

Device: Termux host (Xiaomi 23124RN87C, Android 15 / SDK 35, host kernel
5.15.194); kernel `linux-bionic` `b778f4f7…`, `stub_exe` `731c48d5…`,
`umnet` `7683b4f8…`, `passt-bionic` `502d980c…` (all from this release's
assets), `neoproot-um` `914ba9e2…` (the merged v5.13.0 binary), guest
`um-init` from the bench image.

Image: 1 GiB ext4 (`rootfs.img`, sha256 `2cc8ff7b…ca422d8`) with a throwaway
COW file per run. Every UML invocation below printed the kernel command line
with `ubd0=<cow>,<image> rootfstype=ext4` and `EXT4-fs (ubda): mounted
filesystem … r/w`, i.e. it really was the ext4 path, not hostfs.

| run | what | result |
| --- | --- | --- |
| A | `./start.sh um -- true` (default root) | exit 0; ext4 mounted r/w from `ubd0` + COW; clean shutdown; `UM_COMMAND_EXIT=0` |
| B | `./start.sh um -- sh -c 'exit 7'` | exit 7; `UM_COMMAND_EXIT=7` — the guest status is returned, not swallowed |
| C | `./start.sh um --hostfs -- true` | exit 0; hostfs still selectable (kernel falls back to `rootfstype=hostfs`), so the fast/directory path did not regress |
| D | `./start.sh um --net -- sh -c '…'` | DHCP ack; `ping 127.0.0.1` OK (`LOOPBACK_OK`); `wget http://example.com/` OK (`TCP_OK`); rerun: resolver 8.8.8.8, `ping -4 baidu.com` 2/2 packets, 0% loss, 32.5/32.8/33.1 ms (`PING_OK`) |

Notes, recorded rather than smoothed over:

* The phone's UDP path drops plain DNS queries occasionally (measured at
  roughly 10% on this network; the repository's network gate retries a lookup
  up to eight times for exactly this reason). One earlier attempt in this
  session saw `ping: bad address` / `wget: bad address` once and then
  succeeded on retry; every retried check in the logs above passed.
* The base image sha256 was identical before and after all runs; the COW file
  carries the changes, as intended.
* Leftover check after the runs: `~/.uml` empty, no `linux-bionic` / `umnet` /
  `passt-bionic` processes left.

Raw logs: `2026-10-18-ext4-default-run1.log` (runs A–D) and
`2026-10-18-ext4-default-run2.log` (resolver probe and DNS/ICMP/TCP retry
run) in this directory. Together with the network records
`2026-10-18-umnet-network.json` / `-ipv6.json`, this is what the
`um-arm64-v0.2.0` release's `ext4-gate=pass` rests on.
