#!/usr/bin/env bash
set -euo pipefail

OUT=${1:?usage: $0 OUTPUT_DIR}
ALPINE_VERSION=${ALPINE_VERSION:-3.21.3}
ARCH=${ARCH:-aarch64}
URL="https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/$ARCH/alpine-minirootfs-$ALPINE_VERSION-$ARCH.tar.gz"
mkdir -p "$OUT"
TAR="$OUT/alpine-minirootfs.tar.gz"
ROOT="$OUT/root"
IMAGE="$OUT/alpine.ext4"
curl -fL --retry 3 -o "$TAR" "$URL"
rm -rf "$ROOT"
mkdir -p "$ROOT"
tar -xzf "$TAR" -C "$ROOT"
truncate -s 1G "$IMAGE"
mke2fs -q -t ext4 -d "$ROOT" -F "$IMAGE"
echo "$IMAGE"
