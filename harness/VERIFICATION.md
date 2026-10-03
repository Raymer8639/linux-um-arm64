# Harness verification

Validated on the untraced Termux ARM64 host on 2026-09-27. No additional
packages were installed in this verification round.

- Shell syntax and Python compilation: PASS.
- Fresh Alpine 3.21.3 archive SHA256 verification and ext4 creation: PASS.
- Hostfs boot using um-init and /um-command.sh: verdict=PASS.
- Ext4 boot using the same init wrapper: verdict=PASS.
- Guest exit 7: UM_COMMAND_EXIT=7 and verdict=FAIL (expected).
- Existing rootfs: refused before download or modification.
- Timeout with a deliberately stalled test child: exit 124, cleanup returned.

These gates reused the previously built bionic kernel (Linux 7.2.0-rc4, NDK
r29, API 30, vector/9P enabled) and its matching stub. The revised build script
was syntax-checked but a full kernel rebuild was not repeated.
