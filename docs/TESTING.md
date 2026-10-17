# Testing Matrix

The harness verdict is the boot marker and log scan, not the wrapper process
exit code alone. A passing gate must report `UMARM_BOOT_OK`, shut down cleanly,
and contain no `BUG:`, `WARNING:` or kernel panic.

## Required Coverage

| Area | Minimum coverage |
|---|---|
| Build | x86_64 glibc UML and Android ARM64 bionic UML |
| Interception | `seccomp=on`, `seccomp=off`, `nosysemu`, `nocancel` |
| Storage | hostfs directory root, ext4 image, bind mounts, read/write and exit status |
| Neoproot session | `neoproot_protocol=session-v1`, hostfs session mount, cwd, status and exit code |
| Neoproot init protocol | protocol 2 and `feature=binds` declared before `init.sh`; `binds` records parsed and mounted in order; a refused bind reports `protocol-error` and 125 without running the command |
| Console | pty input/output and non-pollable stdin fallback |
| Memory | 4 KB and 16 KB guest page configurations where supported |
| Networking | loopback, passt vector fd, DHCP, DNS and TCP; IPv6 link, NDP/ICMPv6, AAAA and IPv6 TCP with the gate's `--ipv6` mode |
| Lifecycle | fork/exec, signals, timeout cleanup and process-group cleanup |
| Android | bionic static link, `stub_exe`, API 30 or newer, Termux host execution |

## Local Gates

Run these from an untraced host:

```sh
GATE=g3 MARKER=UMARM_BOOT_OK INIT=/gate3-init \
  UBD0=/path/to/alpine.ext4 EXTRA_ARGS="rw seccomp=on" harness/boot.sh

N=20 harness/loop.sh
```

The Android harness captures logs on the device and pulls them after the gate;
streaming logs can hide the failure tail.

UML must not be launched from inside a neoproot container. The harness should
fail early when `TracerPid` is nonzero.

## Device Gates and Evidence

The gates under `tools/um-arm64/harness/` run on the Termux host, so CI cannot
run them and their verdicts have to be archived by hand. The format and the
honesty rules are in [docs/evidence/README.md](evidence/README.md):

```sh
# one device run of the network gate, archived with hashes and gaps
tools/um-arm64/harness/record-evidence.sh record --out docs/evidence
tools/um-arm64/harness/record-evidence.sh check docs/evidence/<file>.json --verify-hashes

# repetition, resource leaks and drift
tools/um-arm64/harness/soak-umnet.sh --rounds 20

# link loss, guest kernel panic, UML process killed mid-run
tools/um-arm64/harness/inject-faults.sh --scenario link-drop
```

Each tool has an offline `self-test` that needs no phone; those self-tests are
what CI can run. `verdict=SKIP` (exit 77) means the environment made the run
impossible -- missing assets, no passt, a traced host -- and is never a pass.

The network gate also has an IPv6 mode: `--ipv6` boots the same guest with a
second, IPv6-only passt and judges address, route, `ping6` to passt's gateway,
AAAA resolution and an IPv6 TCP fetch. `--link-only` keeps the three
link-level checks for a host without an IPv6 uplink. It skips, rather than
fails, when the guest kernel has no IPv6 (`CONFIG_IPV6`), the host has no
IPv6 egress, or passt's IPv6 helper cannot start; see
[tools/um-arm64/doc/90-networking.md](../tools/um-arm64/doc/90-networking.md#ipv6).

## Fault injection

A gate that passes proves the happy path. It does not say what happens when the
link disappears *while the guest is using it*, when the guest kernel dies, or
when the bridge is left holding the pieces -- the failures a phone user actually
hits (a radio handover, an Android low-memory kill, a kernel oops).
`harness/inject-faults.sh` destroys one of those mid-run and states its criteria
up front, so "it ran" cannot be mistaken for "it passed":

| scenario | destroyed mid-run | required criteria |
|---|---|---|
| `link-drop` | the passt helper (`SIGKILL`) | the injection landed while the run was live; the run ended by itself; it did not report `PASS`; no umnet/passt/UML survived; the bridge's own output shows the link going away |
| `kernel-kill` | the UML process (`SIGKILL`) | the injection landed while live; the run ended by itself; it did not report `PASS`; no survivors |
| `guest-panic` | the guest kernel (`sysrq-c`) | the panic is visible in the guest log; the trigger reached the kernel; the guest did not report a clean exit; the run ended by itself; no survivors |

All three judge and pass on the device (2026-10-17); the records are in
`docs/evidence/faults/`, and its README names the reference set. Two device
facts are what turned the scenarios from misleading into judged, and any future
scenario inherits them:

- **A killed helper cannot be found by scanning `/proc`.** passt is alive, has
  the same uid, answers `kill(pid, 0)`, and does not appear under `/proc/<pid>`
  on this platform. The bridge therefore writes the pid it forked into
  `--passt-pid-file`, and the injector signals exactly that pid. A test that
  looks for passt by name finds nothing and kills the wrong process.
- **UML needs a controlling terminal even when a script starts it.** With
  `con0=fd:0,fd:1` on a plain pipe the kernel dies before userspace and prints
  nothing at all. The panicking scenario boots through the gate (which allocates
  a pty) for that reason.

```sh
tools/um-arm64/harness/inject-faults.sh --scenario link-drop
tools/um-arm64/harness/inject-faults.sh --scenario kernel-kill
tools/um-arm64/harness/inject-faults.sh --scenario guest-panic
```

`harness/soak-umnet.sh` is the repetition counterpart: it runs the network gate
in a loop and reports per-round verdicts, leaked processes and memory drift.
`--minutes` bounds the run by wall clock as well as `--rounds` bounds the round
count, so an hour-scale run is
`soak-umnet.sh --minutes 180 --rounds 5000` (stop at whichever bound is reached
first). Records land in `docs/evidence/soak/`. The gate's two external-network
checks are sized for a phone: the announced resolver sits behind the phone's own
UDP path (measured here: ~10% of plain queries time out), so name resolution is
tried up to eight times and the TCP fetch is retried whole, with 30 s per
transfer. An hour-scale run therefore measures the bridge, not the current state
of the public network; a bridge that does not carry frames still fails them.

Run one device tool at a time: the leak criteria count processes by name and
start time, so a second harness running on the same phone is indistinguishable
from a survivor. Both tools say so in their own `not-verified` list.
