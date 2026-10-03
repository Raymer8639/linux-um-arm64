# Release Policy

Downstream release tags must not reuse Linux kernel version tags. Use a name
that identifies this project and the downstream line, for example:

```text
um-arm64-v0.1.0
```

Each release records:

- downstream Git commit and branch;
- official UML upstream baseline and selected patch list;
- NDK, Android API and compiler versions;
- host kernel and device test matrix;
- rootfs name and checksum;
- `linux-bionic`, `stub_exe`, configuration and SHA256SUMS assets.

Documentation-only and repository-settings changes merge without a tag or
Release. A tag and Release are reserved for source, runtime behavior or
published artifacts that changed and passed the release gates.
