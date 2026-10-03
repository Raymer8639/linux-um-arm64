# ARM64 Android/Termux harness

These scripts provide the maintained reproducible bionic UML build path.

## Build

```sh
NDK=$HOME/android-sdk/ndk/29.0.14206865 \
  OUT=$PWD/out/android-arm64 \
  JOBS=4 \
  harness/build-bionic-termux.sh
```

Build on a glibc Linux host with gcc/g++, make, bison, flex and bc. NDK may be
the NDK root or its LLVM prebuilt directory. Output artifacts are under
`out/android-arm64/artifacts`. `stub_exe` is generated from existing kernel
source, not checked in as a binary. Use a dedicated OUT directory: this script
resets defconfig.

## Rootfs

On Termux, install bash, curl, tar, coreutils, python and e2fsprogs.
The script verifies the Alpine SHA256 and refuses to overwrite existing roots.
It installs `um-init` and a default `/um-command.sh` which prints uname.
Edit the command script in the directory root to test other commands.

```sh
harness/rootfs-alpine-termux.sh $PREFIX/tmp/um-arm64/rootfs
```

## Boot

Run from the Termux host, not inside a neoproot container:

```sh
python3 harness/boot.py \
  out/android-arm64/artifacts/linux-bionic \
  out/android-arm64/artifacts/stub_exe \
  $PREFIX/tmp/um-arm64/rootfs/alpine.ext4
```

Android refuses embedded memfd execution for the UML stub, so `stub_exe` is required.

Add `--hostfs` and pass the generated `root` directory to boot a directory root.
The init wrapper mounts proc/sys/dev, executes `/um-command.sh`, reports its
exit code and powers off. The gate requires the exact `UMARM_BOOT_OK` line,
clean shutdown and no BUG/WARNING/panic. It refuses traced containers and
kills only its own process group on timeout (default 120 seconds).
Hostfs exposes writable host storage; do not treat it as an untrusted-guest
security boundary.
