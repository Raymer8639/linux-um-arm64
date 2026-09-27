# ARM64 Android/Termux harness

These scripts provide the maintained reproducible bionic UML build path.

## Build

```sh
NDK=$HOME/android-sdk/ndk/29.0.14206865 \
  OUT=$PWD/out/android-arm64 \
  JOBS=4 \
  harness/build-bionic-termux.sh
```

The outputs are `linux-bionic` and `stub_exe`.

## Rootfs

On Termux:

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
