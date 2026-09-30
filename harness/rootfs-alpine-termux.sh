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
if [[ -e "$ROOT" || -L "$ROOT" || -e "$IMAGE" || -L "$IMAGE" ]]; then
  echo "refusing to overwrite existing rootfs: $OUT" >&2
  exit 1
fi
curl -fL --retry 3 -o "$TAR" "$URL"
curl -fL --retry 3 -o "$TAR.sha256" "$URL.sha256"
(cd "$OUT"; printf '%s  alpine-minirootfs.tar.gz\n' "$(awk 'NR == 1 { print $1 }' alpine-minirootfs.tar.gz.sha256)" | sha256sum -c -)
mkdir -p "$ROOT"
tar -xzf "$TAR" -C "$ROOT"
HARNESS=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cp "$HARNESS/um-init" "$ROOT/um-init"
chmod 755 "$ROOT/um-init"
printf '%s\n' '#!/bin/sh' 'uname -a' > "$ROOT/um-command.sh"
truncate -s 1G "$IMAGE"
mke2fs -q -t ext4 -d "$ROOT" -F "$IMAGE"
echo "$IMAGE"
