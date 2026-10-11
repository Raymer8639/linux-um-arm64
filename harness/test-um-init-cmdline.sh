#!/bin/sh

set -eu

# Functional test for um-init's /proc/cmdline scanner and session validation.
#
# The companion test-um-init.sh only greps for strings; this one runs the real
# parsing and validation code against crafted cmdlines, which is where the
# interesting cases live: the scanner has to survive tabs, repeated keys and
# missing keys, and the validator has to reject every session path that could
# escape /run/neoproot or confuse the hostfs mount.

script=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/um-init
temporary_directory=$(mktemp -d "${TMPDIR:-/tmp}/um-init-cmdline.XXXXXX")
trap 'rm -rf "$temporary_directory"' EXIT HUP INT TERM

sh -n "$script"

# Lift the cmdline scanner (from the first read to the mount of /run) out of
# um-init. power_down() would power off this host, so the extract replaces it
# with a marker and stops before the mount.
python3 - "$script" "$temporary_directory/scan.sh" "$temporary_directory/validate.sh" <<'PYTHON'
import sys

source = open(sys.argv[1], encoding="utf-8").read()
head = "session=\nguest_cwd=/\nprotocol=\n"
start = source.index("cmdline=$(cat /proc/cmdline")
scan_end = source.index('if [ -z "$session" ]; then')
validate_end = source.index("mount -t tmpfs tmpfs /run")

scanner = head + source[start:scan_end]
scanner += ('printf "protocol=%s\\nsession=%s\\ncwd=%s\\n" '
            '"$protocol" "$session" "$guest_cwd"\n')
open(sys.argv[2], "w", encoding="utf-8").write(scanner)

validator = head + source[start:validate_end]
validator = validator.replace("power_down", "echo REJECTED; exit 1")
open(sys.argv[3], "w", encoding="utf-8").write(validator + "echo ACCEPTED\n")
PYTHON

# Run a script with /proc/cmdline replaced by the given string.
run_with_cmdline() {
	script_path=$1
	cmdline=$2

	sed "s|cat /proc/cmdline 2>/dev/null|printf '%s' \"$cmdline\"|" \
		"$script_path" >"$temporary_directory/run.sh"
	sh "$temporary_directory/run.sh"
}

failures=0

expect_parse() {
	name=$1
	cmdline=$2
	expected=$3

	actual=$(run_with_cmdline "$temporary_directory/scan.sh" "$cmdline" |
		tr '\n' ' ' | sed 's/[[:space:]]*$//')
	expected_normalised=$(printf '%s' "$expected" | tr '\n' ' ' |
		sed 's/[[:space:]]*$//')
	if [ "$actual" != "$expected_normalised" ]; then
		printf 'FAIL: %s\n  expected: %s\n  actual:   %s\n' \
			"$name" "$expected_normalised" "$actual" >&2
		failures=$((failures + 1))
	fi
}

expect_verdict() {
	name=$1
	cmdline=$2
	expected=$3

	actual=$(run_with_cmdline "$temporary_directory/validate.sh" "$cmdline" |
		tail -1)
	if [ "$actual" != "$expected" ]; then
		printf 'FAIL: %s\n  expected: %s\n  actual:   %s\n' \
			"$name" "$expected" "$actual" >&2
		failures=$((failures + 1))
	fi
}

# Scanner: values, defaults, tabs, repeated keys, missing keys.
expect_parse "plain session" \
	"rw init=/um-init neoproot_protocol=session-v1 neoproot_session=/.neoproot-um-abc neoproot_cwd=/work" \
	"protocol=session-v1 session=/.neoproot-um-abc cwd=/work"
expect_parse "no session keys at all" \
	"rw root=/dev/ubda console=tty0" \
	"protocol= session= cwd=/"
expect_parse "tab separated" \
	"$(printf 'rw\tneoproot_session=/tabbed\tneoproot_cwd=/tmp')" \
	"protocol= session=/tabbed cwd=/tmp"
expect_parse "repeated key takes the last" \
	"neoproot_session=/first neoproot_session=/second" \
	"protocol= session=/second cwd=/"
expect_parse "value with an equals sign" \
	"neoproot_cwd=/a=b" \
	"protocol= session= cwd=/a=b"
expect_parse "session but no cwd" \
	"neoproot_session=/.neoproot-um-abc" \
	"protocol= session=/.neoproot-um-abc cwd=/"

# Validator: only an absolute, plain session path under a plain cwd passes.
expect_verdict "valid absolute session" \
	"neoproot_session=/.neoproot-um-abc neoproot_cwd=/work" \
	"ACCEPTED"
expect_verdict "relative session" \
	"neoproot_session=relative neoproot_cwd=/work" \
	"REJECTED"
expect_verdict "relative cwd" \
	"neoproot_session=/.neoproot-um-abc neoproot_cwd=relative" \
	"REJECTED"
expect_verdict "session is the root itself" \
	"neoproot_session=/ neoproot_cwd=/work" \
	"REJECTED"
expect_verdict "session escapes with .." \
	"neoproot_session=/a/../b neoproot_cwd=/work" \
	"REJECTED"
expect_verdict "cwd escapes with .." \
	"neoproot_session=/.ok neoproot_cwd=/a/../b" \
	"REJECTED"
expect_verdict "session contains a comma" \
	"neoproot_session=/a,b neoproot_cwd=/work" \
	"REJECTED"
expect_verdict "cwd contains a comma" \
	"neoproot_session=/.ok neoproot_cwd=/a,b" \
	"REJECTED"

# A space cannot survive the kernel cmdline in the first place: the shell sees
# it as an argument separator, so the value is truncated before validation.
# Assert the documented consequence rather than pretending it is rejected.
expect_parse "space splits the value" \
	"neoproot_session=/a b neoproot_cwd=/work" \
	"protocol= session=/a cwd=/work"

if [ "$failures" -ne 0 ]; then
	printf 'um-init cmdline test: %d failure(s)\n' "$failures" >&2
	exit 1
fi

printf '%s\n' 'um-init cmdline scanner and validator test: PASS'
