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
| Console | pty input/output and non-pollable stdin fallback |
| Memory | 4 KB and 16 KB guest page configurations where supported |
| Networking | loopback, passt vector fd, DHCP, DNS and TCP |
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
