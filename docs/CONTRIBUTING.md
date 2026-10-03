# Contributing

This repository carries an ARM64 Android/Termux downstream port of UML.

## Patch Classification

- Generic UML or x86 fixes should be prepared for the official UML tree.
- Android, bionic, NDK and ARM64-only changes remain downstream unless an
  upstream-compatible form can be demonstrated.
- Do not merge the official x86 UML tree wholesale into `um-arm64`.

## Change Requirements

- Keep one logical change per commit.
- Include a `Signed-off-by` line on commits intended for kernel upstreaming.
- State the upstream baseline and whether the change is downstream-only.
- Add or update a deterministic harness gate for behavior changes.
- Run shell syntax checks and the relevant hostfs, ext4, console, interception
  and lifecycle tests.
- Do not commit credentials, phone-specific rootfs images or build artifacts.

The protected `um-arm64` branch accepts changes through pull requests. Changes
to upstream-facing code should include the relevant UML mailing-list or
Patchwork reference when one exists.
