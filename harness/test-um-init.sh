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

# init protocol 2: the declaration is written into the session before init.sh
# runs, so the supervisor's wrapper can refuse a command whose binds this init
# cannot mount instead of running it without them.
for declaration in \
    'neoproot-init-protocol=2' \
    'feature=binds' \
    '>/run/neoproot/init-features' \
    'if [ -f /run/neoproot/binds ]; then'
do
    if ! grep -Fq "$declaration" "$script"; then
        printf 'um-init is missing init protocol 2: %s\n' "$declaration" >&2
        exit 1
    fi
done

# binds: the five tab-separated fields, both record types, and the scratch
# mount a file bind is bound out of.
for bind_text in \
    'while IFS=$bind_tab read -r bind_source bind_target bind_type bind_mode bind_name' \
    'done </run/neoproot/binds' \
    'mount -t hostfs -o "$bind_source" none "$bind_target"' \
    'mount -o remount,ro "$bind_target"' \
    'mount_bind_file "$bind_index" "$bind_source" "$bind_target"' \
    'mount -t hostfs -o "$bind_source" none "$bind_scratch"' \
    'mount -o remount,ro "$bind_scratch"' \
    'mount --bind "$bind_scratch/$bind_name" "$bind_target"'
do
    if ! grep -Fq "$bind_text" "$script"; then
        printf 'um-init is missing binds support: %s\n' "$bind_text" >&2
        exit 1
    fi
done

# A failed bind must be readable by the supervisor, report 125, and never reach
# init.sh.
for failure_text in \
    'if ! mount_binds; then' \
    '>/run/neoproot/protocol-error' \
    'UM_BIND_ERROR=$bind_target:mount-failed' \
    "'125' >/run/neoproot/status" \
    'UM_SESSION_ERROR=125'
do
    if ! grep -Fq "$failure_text" "$script"; then
        printf 'um-init is missing the binds failure path: %s\n' "$failure_text" >&2
        exit 1
    fi
done

# The test host has no hostfs, so the mounts themselves cannot run here.  Lift
# the two bind functions out of um-init, redirect /run into a temporary
# directory, and record every mount call with a stub; that checks the five-field
# parse and the exact mount sequence without touching the host.
temporary_directory=$(mktemp -d "${TMPDIR:-/tmp}/um-init-binds.XXXXXX")
um_root=$temporary_directory
trap 'rm -rf "$temporary_directory"' EXIT HUP INT TERM

python3 - "$script" "$temporary_directory/functions.sh" <<'PYTHON'
import sys

source = open(sys.argv[1], encoding="utf-8").read()
start = source.index("mount_bind_file() {")
end = source.index("if [ -f /run/neoproot/binds ]")
block = source[start:end].replace("/run/", '"$um_root"/run/')
open(sys.argv[2], "w", encoding="utf-8").write(block)
PYTHON

mkdir -p "$temporary_directory/run/neoproot"

# A shell function stands in for mount: the sourced bind functions run in this
# shell, so this needs no executable and does not depend on /tmp being executable.
mount() {
    printf '%s\n' "$*" >>"$MOUNT_LOG"
    if [ -n "${MOUNT_FAIL_MATCH:-}" ]; then
        case "$*" in
            *"$MOUNT_FAIL_MATCH"*) return 1 ;;
        esac
    fi
    return 0
}

MOUNT_LOG=$temporary_directory/mount.log
bind_list=$temporary_directory/run/neoproot/binds
bind_error=
bind_rc=0
MOUNT_FAIL_MATCH=
. "$temporary_directory/functions.sh"

# Both record types, both modes, a comment, and a blank line.  The file bind
# targets sit in a directory that does not exist yet, so the handler has to
# create it; a ro file bind remounts its scratch mount, a rw one does not.
{
    printf '%s\n' '# neoproot binds v1'
    printf '%s\n' ''
    printf '%s\t%s\tdir\tro\t-\n' '../host/dir-ro' \
        "$temporary_directory/guest/dir-ro"
    printf '%s\t%s\tdir\trw\t-\n' '../host/dir-rw' \
        "$temporary_directory/guest/dir-rw"
    printf '%s\t%s\tfile\tro\tfile-ro.txt\n' '../host/file-ro.txt' \
        "$temporary_directory/guest/nested/file-ro.txt"
    printf '%s\t%s\tfile\trw\tfile-rw.txt\n' '../host/file-rw.txt' \
        "$temporary_directory/guest/nested/file-rw.txt"
} >"$bind_list"

: >"$MOUNT_LOG"
bind_error=
mount_binds || bind_rc=$?
if [ "$bind_rc" -ne 0 ] || [ -n "$bind_error" ]; then
    printf 'FAIL: valid binds were rejected (rc=%s, %s)\n' \
        "$bind_rc" "$bind_error" >&2
    exit 1
fi

expected=$(printf '%s\n' \
    "-t hostfs -o ../host/dir-ro none $temporary_directory/guest/dir-ro" \
    "-o remount,ro $temporary_directory/guest/dir-ro" \
    "-t hostfs -o ../host/dir-rw none $temporary_directory/guest/dir-rw" \
    "-t hostfs -o ../host/file-ro.txt none $temporary_directory/run/.neoproot-bind-3" \
    "-o remount,ro $temporary_directory/run/.neoproot-bind-3" \
    "--bind $temporary_directory/run/.neoproot-bind-3/file-ro.txt $temporary_directory/guest/nested/file-ro.txt" \
    "-t hostfs -o ../host/file-rw.txt none $temporary_directory/run/.neoproot-bind-4" \
    "--bind $temporary_directory/run/.neoproot-bind-4/file-rw.txt $temporary_directory/guest/nested/file-rw.txt")
actual=$(cat "$MOUNT_LOG")
if [ "$actual" != "$expected" ]; then
    printf 'FAIL: binds mount sequence\n--- expected\n%s\n--- actual\n%s\n' \
        "$expected" "$actual" >&2
    exit 1
fi
for target in dir-ro dir-rw nested/file-ro.txt nested/file-rw.txt; do
    if [ ! -e "$temporary_directory/guest/$target" ]; then
        printf 'FAIL: bind target was not created: %s\n' "$target" >&2
        exit 1
    fi
done

# A refused mount stops the list, names the target, and leaves the rest
# unmounted.
printf '%s\t%s\tdir\trw\t-\n' '../host/missing' \
    "$temporary_directory/guest/missing" >"$bind_list"
: >"$MOUNT_LOG"
bind_error=
bind_rc=0
MOUNT_FAIL_MATCH=../host/missing
mount_binds || bind_rc=$?
MOUNT_FAIL_MATCH=
if [ "$bind_rc" -ne 1 ] ||
    [ "$bind_error" != "UM_BIND_ERROR=$temporary_directory/guest/missing:mount-failed" ] ||
    [ "$(wc -l <"$MOUNT_LOG")" -ne 1 ]; then
    printf 'FAIL: a refused mount must stop the bind list (rc=%s, %s, %s mounts)\n' \
        "$bind_rc" "$bind_error" "$(wc -l <"$MOUNT_LOG")" >&2
    exit 1
fi

# A relative target and a target that would land on the session channel are
# rejected before anything is mounted.
expect_bind_error() {
    name=$1
    target=$2
    expected_error=$3

    printf '%s\t%s\tdir\trw\t-\n' '../host/dir' "$target" >"$bind_list"
    : >"$MOUNT_LOG"
    bind_error=
    bind_rc=0
    mount_binds || bind_rc=$?
    if [ "$bind_rc" -ne 1 ] || [ "$bind_error" != "$expected_error" ] ||
        [ -s "$MOUNT_LOG" ]; then
        printf 'FAIL: %s (rc=%s, %s)\n' "$name" "$bind_rc" "$bind_error" >&2
        exit 1
    fi
}

expect_bind_error 'relative target' 'guest/relative' \
    'UM_BIND_ERROR=guest/relative:relative-target'
expect_bind_error 'target inside the session' \
    "$temporary_directory/run/neoproot/status" \
    "UM_BIND_ERROR=$temporary_directory/run/neoproot/status:target-inside-session"

printf '%s\n' 'um-init syntax/protocol parser test: PASS'
