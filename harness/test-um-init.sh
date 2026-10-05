#!/bin/sh

set -eu

script=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/um-init

sh -n "$script"

if grep -Fq 'for argument in $cmdline' "$script"; then
    printf '%s\n' 'um-init still uses unsafe cmdline expansion' >&2
    exit 1
fi
if ! grep -Fq 'while [ -n "$remaining" ]' "$script"; then
    printf '%s\n' 'um-init is missing the quoted cmdline scanner' >&2
    exit 1
fi
if ! grep -Fq 'cmdline=$(cat /proc/cmdline' "$script"; then
    printf '%s\n' 'um-init is missing post-mount cmdline parsing' >&2
    exit 1
fi
for protocol in \
    'neoproot_session=*)' \
    'neoproot_protocol=*)' \
    'neoproot_cwd=*)' \
    '/bin/sh /um-command.sh' \
    '/bin/sh /run/neoproot/init.sh'
do
    if ! grep -Fq "$protocol" "$script"; then
        printf 'um-init is missing protocol compatibility: %s\n' "$protocol" >&2
        exit 1
    fi
done

printf '%s\n' 'um-init syntax/protocol parser test: PASS'
