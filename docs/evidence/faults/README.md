# Fault-injection records

One JSON record per `harness/inject-faults.sh` run, named
`<scenario>-<UTC timestamp>.json`. Each record carries `fault-format`, the
scenario, one `{id, requirement, ok, evidence}` entry per criterion (the verdict
is `PASS` only when every required criterion is ok), the run's argv, exit code
and seconds, and the survivor list. Records are produced on the phone; CI can
only check their shape.

## The reference set

The authoritative records are the last three, produced by one sequential run on
2026-10-17 (UTC), on the same device and the same binaries:

| scenario | record | result |
| --- | --- | --- |
| `link-drop` | `link-drop-2026-10-17T003424Z.json` | PASS — the helper pid was killed 1.3 s into a live run; the run ended by itself; it did not report PASS; no survivors |
| `kernel-kill` | `kernel-kill-2026-10-17T003429Z.json` | PASS — the UML pid was killed 1.3 s in; the run ended by itself; no survivors |
| `guest-panic` | `guest-panic-2026-10-17T003437Z.json` | PASS — the panic is in the guest log, the sysrq trigger reached the kernel, no clean guest exit, no survivors |

## The older records are evidence too

Every record in this directory is kept, failures included. Most of the older
ones are `FAIL` on `c1-injected-live` (or, for `guest-panic`,
`c1-panic-observed`): the injection point was never reached, because the tool
was late (the delay outlasted the whole ~5 s run) or looked at the wrong process
(it matched `passt-bionic` as a substring of the bridge's own command line).
That distinction is the point of recording criteria separately from the run:
"the product is broken" and "the tool could not measure" must not read the same.

## What is not covered

- `link-drop` kills passt, which is the closest a non-root Termux process gets
  to dropping the host link. Radio handover and interface-down are not
  reproducible here.
- `guest-panic` needs `CONFIG_MAGIC_SYSRQ` and a writable
  `/proc/sysrq-trigger` in the guest; `PANIC_NOT_TRIGGERED` in the log means the
  trigger never reached the kernel and the panic criterion cannot be judged.
- Every record's `not-verified` list is part of the record; read it before
  citing a scenario.
