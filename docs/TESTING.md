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
