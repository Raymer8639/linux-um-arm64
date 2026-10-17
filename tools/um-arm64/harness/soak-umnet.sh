#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
#
# runs the network gate many times and reports leaks and drift.
#
# The entry point stays a .sh because docs/RELEASE.md, docs/evidence/README.md
# and docs/TESTING.md refer to it by this name; the implementation is python,
# where sha256, ISO timestamps, /proc scanning and JSON are one-liners instead
# of quoting traps.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$here/soak-umnet.py" "$@"
