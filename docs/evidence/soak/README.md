# Soak records

One JSON record per `harness/soak-umnet.sh` run. A soak repeats the network
gate and writes down, per round, the gate's verdict, how long the round took,
the watched process peaks and anything that survived, plus available memory
before and after. The summary adds counts, coverage (`--rounds` and
`--minutes` against what was asked), leaked processes, timing min/max/avg, the
memory delta, and -- like every device record in this directory -- the
components by sha256, the host, and a `not-verified` list.

`verdict` is `PASS` only when every round passed, nothing leaked, and the
requested coverage was actually reached. `--minutes 180 --rounds 5000` stops at
whichever bound comes first, so a run that used up its rounds in 40 minutes is
not an hour-scale soak and says so (`coverage.met = false`).

## Reference set

| record | request | result |
| --- | --- | --- |
| `soak-2026-10-16T235555Z.json` | `--rounds 6` | PASS: 6/6 rounds, 0 leaked |
| `soak-2026-10-17T074023Z.json` | `--minutes 180 --rounds 5000` | **793 rounds over 180.17 minutes, 792 PASS / 1 FAIL, 0 leaked, no crashes.** The one failure was the gate's *external* TCP check (round 650, 30.4 s: the fetch did not return the page) during a period when the phone's network was known to be bad. That round's bridge checks -- DHCP, address, route, resolver, ICMP, concurrency, clean exit -- were all green. The soak's verdict is `FAIL` because one round failed; it is kept as-is rather than rounded up. |

## The 20-round record that failed

`soak-2026-10-16T235017Z.json` failed with 8 "leaked" processes. They were the
operator's own monitoring shells, whose command lines contained a watched token
(`umnet`, `passt-bionic`, `linux-bionic`), not processes leaked by the run.
That is exactly the failure mode the record's `not-verified` list warns about:
the leak check matches command lines, so anything else on the phone that
mentions one of those tokens during a round is indistinguishable from a
survivor. Run one device tool at a time. The 6-round PASS next to it is the
clean rerun.

## What a clean soak proves, and what it does not

It proves the gate path -- UML boot, bridge, passt, DHCP/DNS/TCP, teardown --
repeats without leaking a process, without exhausting memory, and without a
failed round, for the window that was requested. It does not vary the network,
does not cover IPv6 or ext4, and says nothing about device states (doze,
thermal throttling, low battery) that the sample did not happen to include.
Every record states its own gaps in `not-verified`.
