# Device evidence

## Why this directory exists

CI runs on GitHub runners. The network gate cannot: it needs a real Android
phone, a UML kernel built for bionic, a static passt, and a host that is not
traced. So the most valuable result in this repository -- "the network really
works on the device" -- used to live only in the terminal that printed it, and
a release note could claim it with nothing behind the claim.

This directory holds records of device runs. One run of
`tools/um-arm64/harness/test-umnet-network.sh` produces three files:

```text
<date>-umnet-network.json   the machine-readable record (canonical)
<date>-umnet-network.md     the same facts rendered for a reviewer
<date>-umnet-network.log    the guest log the verdict was read from
```

The JSON is what `check` validates and what a release cites; the markdown is
the same document, rendered so a reviewer does not have to read JSON. An
`--ipv6` gate run uses `--label` to get its own file stem
(`<date>-umnet-network-ipv6.*`).

`SAMPLE-synthetic-umnet-network.*` is a sample produced by the harness's
stand-in gate. It exists to show the format, it is marked `synthetic` inside,
and `check` refuses it as evidence.

## Recording a run

Run this on the Termux host, never inside a traced container:

```sh
tools/um-arm64/harness/record-evidence.sh record \
    --workdir "$PREFIX/tmp/um-network" \
    --assets-dir "$PREFIX/tmp/um-arm64/new" \
    --out docs/evidence
```

If the repository is not visible from the phone, write the record into
`$PREFIX/tmp` and copy it into `docs/evidence/` afterwards; the record is
self-contained (the guest log is copied next to it).

The gate rewrites `net-command.sh` inside the work directory, so that root has
to carry the init the kernel is told to run: `init=/um-init` must exist in the
hostfs root before the gate starts. The gate does not create it -- the file to
put there is `tools/um-arm64/harness/netroot-um-init.sh`. Without it the run
dies with `Kernel panic - not syncing: Requested init /um-init failed`, which
reads like a kernel fault and is a missing file in the rootfs.

Useful variations:

```sh
# probe the environment and hash the assets without booting anything
record-evidence.sh record --dry-run

# the IPv6 gate: extra gate arguments go through --gate-arg, and --label keeps
# the record from colliding with the IPv4 one
record-evidence.sh record --dns 8.8.8.8 \
    --gate-arg=--ipv6 --gate-arg=--ipv6-dns=2001:4860:4860::8888 \
    --label umnet-network-ipv6

# validate a record before committing it (and re-hash what is still on disk)
record-evidence.sh check docs/evidence/<file>.json --verify-hashes

# what has been recorded so far
record-evidence.sh list

# prove the recorder and the checker still work (offline, no phone)
record-evidence.sh self-test
```

## Verdicts

| verdict | exit | meaning |
| --- | --- | --- |
| `PASS` | 0 | the gate ran and every check passed |
| `FAIL` | 1 | the gate ran and something failed, or it never judged anything |
| `SKIP` | 77 | the environment made the run impossible; nothing was judged |

`SKIP` is not a soft pass. A record with no device, or with a component that
was never hashed, cannot be `PASS` -- the checker enforces that. A gate that
printed checks but no `verdict=` line is recorded as `FAIL`, because the run
happened and produced no result.

## The record format

Top-level fields of the JSON record (format 1):

| field | what it holds |
| --- | --- |
| `evidence-format` | format version; `check` rejects anything it does not know |
| `kind` | `termux-umnet-network-gate` |
| `synthetic` | true when the run used a gate other than the repository's own |
| `recorded` | ISO 8601 UTC timestamp (file names use the same UTC date) |
| `verdict`, `exit-code`, `message` | the result and the one-line reason for it |
| `gate` | path, argv, timeout, dns, seconds, exit-code, `timed-out`, `verdict-line`, last 40 stderr lines |
| `checks` | one `{name, status}` entry per gate check (`ok` / `MISSING`) |
| `components` | `linux-bionic`, `stub_exe`, `umnet`, `passt-bionic`: path, sha256, size, executable -- or an explicit `missing: true` |
| `guest-kernel-banner` | the `Linux version ...` string read out of the kernel binary, tying a hash to a version |
| `host` | uname, kernel release, machine, Termux prefix, python, TracerPid, and the Android properties when `getprop` exists |
| `git` | repository, commit, branch, and whether the recorded scopes were dirty |
| `guest-log` | path, sha256, size, line count and a head/tail excerpt of the guest log |
| `metrics` | ICMP min/avg/max, guest wget rates, teardown line, clean-shutdown flag |
| `not-verified` | what this record does **not** cover (required, never empty) |
| `notes` | the recorder's own remarks, including anything it could not read |

## What the checker refuses

`check` is offline and is the gate a release record has to pass. It rejects:

* a `PASS` with no `verdict=PASS` line, a nonzero exit code, a timeout, no
  judged check, or any check `MISSING`;
* a `PASS` whose components have no sha256, or that names a missing component;
* a `SKIP` that judged checks (a skipped run judges nothing) or gives no reason;
* a record with an empty `not-verified` list: a record claiming complete
  coverage is a record that is wrong;
* an unknown format version, an unparseable timestamp, a verdict/exit-code
  mismatch, or a missing host/gate block;
* any `synthetic` record, unless `--allow-synthetic` is passed for format
  testing.

`--verify-hashes` additionally re-hashes every component that still exists on
disk, so a record cannot outlive the binaries it describes silently.

The gate itself uses the same vocabulary: missing assets or a traced host make
it print `verdict=SKIP` and exit 77 rather than exit 1, because "broken" and
"never tested" are different facts.

## Related device tools

| tool | question it answers |
| --- | --- |
| `record-evidence.sh` | what did one device run measure, and about which binaries? |
| `soak-umnet.sh` | does it still pass after N rounds, and does anything leak? |
| `inject-faults.sh` | what happens when the link drops, the guest panics, or the kernel dies? |

Soak and fault records live in `docs/evidence/soak/` and
`docs/evidence/faults/`. They carry the same rule: a run that could not be
attempted is `SKIP`, and any leak or unmet criterion is a `FAIL` even when
every other round was clean. [soak/README.md](soak/README.md) names the
reference soak (an hour-scale run, and the one round it failed on the external
network) and [faults/README.md](faults/README.md) the three fault scenarios,
including why the older `FAIL` records in those directories are part of the
evidence rather than noise.

Run one device tool at a time. The soak and fault tools decide "did anything
leak?" by process name and start time, so a second harness run on the same
phone at the same time looks like a survivor; both tools say so in their own
`not-verified` list.

## What this mechanism solves, and what it does not

It solves traceability: a device verdict now names the four binaries by
sha256, the kernel version, the device, the commit (and whether it was dirty),
the exact gate arguments and the raw log, and it states its own gaps. A
reviewer can check any of it without a phone, and CI can validate the record's
shape without a device.

It does not replace CI (the x86_64 build, the shell/python checks and the
release checker still run there), it does not make the device run reproducible
on another machine, and it does not extend coverage: what the gate does not
test stays untested, and the record says so in `not-verified` instead of
implying otherwise.
