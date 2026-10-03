# Build Guide

The repository has two build targets with different purposes:

- x86/x86_64 glibc UML is the upstream compatibility and CI reference.
- ARM64 bionic UML is the Android/Termux downstream target.

## x86_64 Reference Build

On an x86_64 Linux host:

```sh
make ARCH=um SUBARCH=x86 LLVM=1 defconfig
make ARCH=um SUBARCH=x86 LLVM=1 -j"$(nproc)"
```

`SUBARCH=x86` selects the x86_64 UML defconfig on an x86_64 host. Use the
normal Linux kernel build dependencies and keep build output in a separate
`O=` directory for clean comparisons.

## Android ARM64 Build

The supported Termux harness uses Android NDK clang, API 30, static bionic
linking and an embedded UML stub executable:

```sh
NDK=/path/to/android-ndk-r29 \
OUT="$PWD/out/android-arm64" \
JOBS=4 \
harness/build-bionic-termux.sh
```

The harness emits `linux-bionic` and `stub_exe` under the artifact directory.
Run the resulting binary on the untraced Termux host, never inside a neoproot
container.

## Reproducibility

Record the source commit, NDK release, Android API level, compiler version,
kernel configuration, rootfs checksum and artifact SHA256 values. Do not put
build output, rootfs images or credentials into the source tree.
