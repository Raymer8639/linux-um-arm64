#!/usr/bin/env bash
set -euo pipefail

SRC=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
OUT=${OUT:-$SRC/out/android-arm64}
NDK=${NDK:-${ANDROID_NDK_HOME:-$HOME/android-sdk/ndk/29.0.14206865}/toolchains/llvm/prebuilt/linux-x86_64}
API=${API:-30}
JOBS=${JOBS:-4}
HOSTCC=${HOSTCC:-/usr/bin/gcc}
HOSTCXX=${HOSTCXX:-/usr/bin/g++}

if [[ ! -x "$NDK/bin/clang" ]]; then echo "missing NDK clang: $NDK/bin/clang" >&2; exit 1; fi
mkdir -p "$OUT"
cd "$SRC"

common=(
  ARCH=um SUBARCH=arm64 O="$OUT"
  HOSTCC="$HOSTCC" HOSTCXX="$HOSTCXX"
  "CC=$NDK/bin/clang --target=aarch64-linux-android$API"
  "LD=$NDK/bin/ld.lld" "AR=$NDK/bin/llvm-ar" "NM=$NDK/bin/llvm-nm"
  "OBJCOPY=$NDK/bin/llvm-objcopy" "OBJDUMP=$NDK/bin/llvm-objdump"
  "READELF=$NDK/bin/llvm-readelf" "STRIP=$NDK/bin/llvm-strip"
  LLVM_IAS=1 "CLANG_TARGET_FLAGS=aarch64-linux-android$API"
)

make "${common[@]}" defconfig
scripts/config --file "$OUT/.config" -e STATIC_LINK -e UML_NET_VECTOR -e NET_9P -e 9P_FS
make "${common[@]}" olddefconfig
make "${common[@]}" -j"$JOBS"

mkdir -p "$OUT/artifacts"
cp "$OUT/linux" "$OUT/artifacts/linux-bionic"
cp "$OUT/arch/um/kernel/skas/stub_exe" "$OUT/artifacts/stub_exe"
"$NDK/bin/llvm-strip" --strip-debug "$OUT/artifacts/linux-bionic"
chmod 755 "$OUT/artifacts/linux-bionic" "$OUT/artifacts/stub_exe"
sha256sum "$OUT/artifacts/linux-bionic" "$OUT/artifacts/stub_exe"
file "$OUT/artifacts/linux-bionic" "$OUT/artifacts/stub_exe"
