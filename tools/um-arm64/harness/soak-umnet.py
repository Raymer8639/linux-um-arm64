#!/usr/bin/env python3
"""Soak the umnet network gate: many rounds, watching for leaks and drift.

One green run answers "does the network work".  It does not answer "does it
still work after an hour", "does every round leave a stray UML behind", or
"does the phone run out of memory".  Those are the questions a soak answers,
and they are the ones a single end-to-end run is worst at.

Each round records:

* the gate verdict and how long it took;
* how many umnet / passt / UML processes were alive at the peak;
* whether anything from the round was still alive after a settle period;
* MemAvailable before and after, as evidence rather than as a criterion.

The leak rule is deliberately concrete: a process that the sampler saw during
a round and is still alive (and not a zombie) after the round ended is a leak.
Processes that were already on the phone and merely have "umnet" in their
command line are excluded, and so is this tool's own command line.

    soak-umnet.sh --rounds 20
    soak-umnet.sh --minutes 180 --rounds 1000      # until either bound is hit
    soak-umnet.sh --self-test                      # no device needed

The overall verdict is not the last round's: it is FAIL if any round failed,
if any process leaked, or if the requested window was not covered.  A soak
that stopped early is a soak that proved less than it claims.  An environment
that cannot run even one round is SKIP (77), never PASS.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import gateutil as gu

SOAK_FORMAT = 1
KIND = "umnet-network-soak"

BASE_GAPS = (
    "a clean soak is not proof of no leak: it covers the requested window only",
    "MemAvailable moves with every other app on the phone; the delta is "
    "evidence, not a criterion",
    "the soak repeats one gate with one work directory; it does not vary "
    "network conditions, DNS or link speed",
    "IPv6, ext4/COW rootfs and fault injection are covered elsewhere (or not "
    "at all) -- see docs/evidence/README.md",
    "the round list is a sample of wall-clock time, not of device states "
    "(doze, thermal throttling, low battery)",
    "a leak is judged by process name and start time: another process on the "
    "phone whose command line matches a watched token and that started during "
    "a round would be counted too",
)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class RoundWatch(object):
    """Attach a process sampler to one gate round."""

    def __init__(self, tokens, interval, marker):
        self.tokens = tokens
        self.interval = interval
        self.marker = marker
        self.sampler = None

    def on_spawn(self, pid):
        self.sampler = gu.Sampler(pid, tokens=self.tokens,
                                  interval=self.interval,
                                  marker=self.marker).start()

    def stop(self):
        if self.sampler is not None:
            self.sampler.stop()
        return self.sampler


def run_round(index, args, marker, log_path):
    """Run one gate round and describe what happened."""
    watch = RoundWatch(tuple(args.watch), args.interval, marker)
    argv = gu.gate_command(args.gate, [
        "--workdir", args.workdir, "--log", args.log_name,
        "--timeout", str(args.gate_timeout), "--dns", args.dns] + args.gate_arg)
    before = None
    if os.path.exists(log_path):
        before = (os.stat(log_path).st_mtime, os.stat(log_path).st_size)
    mem_before = gu.meminfo()
    round_marker = gu.watch_starttime()
    run = gu.run_process(argv, timeout=args.gate_timeout + args.slack,
                         echo=args.echo, on_spawn=watch.on_spawn)
    sampler = watch.stop()
    if args.settle > 0:
        time.sleep(args.settle)
    mem_after = gu.meminfo()

    leaks = list(sampler.leaks()) if sampler is not None else []
    seen_pids = set(sampler.seen) if sampler is not None else set()
    # The in-tree sampler can miss a process that called setsid(), or one that
    # was born and orphaned between two samples.  Anything carrying a watched
    # token that was born during this round and is still alive is a leak too;
    # `in-gate-tree` records how strong that evidence is.
    table = gu.proc_table()
    for proc in gu.matching(table, tuple(args.watch),
                            exclude=gu.self_tree_pids(table)):
        if proc.is_zombie or proc.starttime < round_marker:
            continue
        if any(leak.pid == proc.pid for leak in leaks):
            continue
        leaks.append(proc)
    summary = sampler.summary() if sampler is not None else {"peaks": {},
                                                             "seen": []}
    record = {
        "round": index,
        "verdict": run.verdict,
        "exit-code": run.exit_code,
        "timed-out": run.timed_out,
        "seconds": round(run.seconds, 3),
        "peaks": summary["peaks"],
        "leaked": [dict(proc.as_dict(),
                        **{"in-gate-tree": proc.pid in seen_pids})
                   for proc in leaks],
        "killed-tree": run.killed_tree,
        "mem-available-kb": {"before": mem_before, "after": mem_after},
        "log": None,
        "stderr-tail": run.text.splitlines()[-20:],
    }
    # A round that was killed on timeout never rewrote the log, so the file on
    # disk belongs to an earlier round.  Saying so beats hashing the wrong file.
    if os.path.exists(log_path):
        after = (os.stat(log_path).st_mtime, os.stat(log_path).st_size)
        stale = before is not None and after == before
        record["log"] = {"path": log_path, "sha256": gu.sha256_file(log_path),
                         "size": after[1], "stale": stale}
        if stale and run.timed_out:
            # The gate was killed before it could write a fresh log, so this
            # round produced no usable evidence of its own.
            record["verdict"] = "FAIL"
            record["stderr-tail"].append(
                "soak: gate produced no new log for this round (killed before "
                "it could write one); counting the round as FAIL")
        elif stale:
            record["stderr-tail"].append(
                "soak: gate did not rewrite the log (it never booted); the "
                "file on disk is from an earlier round and is not evidence "
                "for this one")
    elif before is not None:
        record["log"] = {"path": log_path, "removed": True}
    return record


def summarize(rounds, args, started_at, elapsed, marker, preflight_problems):
    counts = {verdict: 0 for verdict in gu.VERDICTS}
    for record in rounds:
        counts[record["verdict"]] = counts.get(record["verdict"], 0) + 1
    leaked = [{"round": record["round"], "process": proc}
              for record in rounds for proc in record["leaked"]]
    times = [record["seconds"] for record in rounds]
    memory = [record["mem-available-kb"]["after"] for record in rounds
              if record["mem-available-kb"]["after"] is not None]
    requested_minutes = args.minutes
    coverage = {
        "rounds-requested": args.rounds,
        "rounds-run": len(rounds),
        "minutes-requested": requested_minutes,
        "minutes-run": round(elapsed / 60.0, 3),
        "rounds-met": len(rounds) >= args.rounds,
        "minutes-met": bool(requested_minutes
                            and elapsed >= requested_minutes * 60),
    }
    coverage["met"] = coverage["rounds-met"] or coverage["minutes-met"]

    if preflight_problems:
        verdict, message = "SKIP", "; ".join(preflight_problems)
    elif not rounds:
        verdict, message = "SKIP", "no round was run"
    elif leaked:
        verdict, message = "FAIL", ("%d process(es) survived their round"
                                    % len(leaked))
    elif counts.get("FAIL"):
        verdict, message = "FAIL", ("%d of %d rounds failed"
                                    % (counts["FAIL"], len(rounds)))
    elif counts.get("SKIP"):
        # A soak that skipped rounds did not cover the window it was asked to
        # cover, so the result is not a pass no matter how good the other
        # rounds looked.
        verdict, message = "FAIL", ("%d of %d rounds could not run"
                                    % (counts["SKIP"], len(rounds)))
    elif not coverage["met"]:
        verdict, message = "FAIL", ("stopped after %d rounds in %.1f minutes "
                                    "without covering the requested window"
                                    % (len(rounds), elapsed / 60.0))
    else:
        verdict, message = "PASS", ("%d round(s) clean" % len(rounds))

    summary = {
        "soak-format": SOAK_FORMAT,
        "kind": KIND,
        "recorded": now_iso(),
        "verdict": verdict,
        "exit-code": gu.exit_for(verdict),
        "message": message,
        "rounds": rounds,
        "counts": counts,
        "coverage": coverage,
        "leaked-processes": leaked,
        "seconds": {"min": min(times) if times else None,
                    "max": max(times) if times else None,
                    "avg": round(sum(times) / len(times), 3) if times else None},
        "memory-available-kb": {"first": memory[0] if memory else None,
                                "last": memory[-1] if memory else None,
                                "delta": ((memory[-1] - memory[0])
                                          if len(memory) > 1 else None)},
        "started": started_at,
        "gate": {"path": gu.repo_relative(args.gate), "argv": list(args.gate_arg),
                 "timeout-s": args.gate_timeout, "dns": args.dns},
        "watch-tokens": list(args.watch),
        "settle-s": args.settle,
        "host": gu.device_info(),
        "components": {name: (gu.file_record(os.path.join(
            args.assets_dir if where == "assets" else args.workdir, name))
            or {"path": os.path.join(
                args.assets_dir if where == "assets" else args.workdir, name),
                "missing": True})
            for name, where in gu.COMPONENTS},
        "not-verified": [],
    }
    gaps = list(BASE_GAPS)
    if verdict == "SKIP":
        gaps.insert(0, "no round ran: %s" % message)
    if leaked:
        gaps.insert(0, "processes leaked during the soak; see leaked-processes")
    if counts.get("SKIP"):
        gaps.insert(0, "%d round(s) could not run" % counts["SKIP"])
    if not coverage["met"]:
        gaps.insert(0, "the requested soak window was not covered")
    summary["not-verified"] = gaps
    return summary


def print_summary(summary):
    print("soak: rounds=%d  pass=%d fail=%d skip=%d"
          % (len(summary["rounds"]), summary["counts"].get("PASS", 0),
             summary["counts"].get("FAIL", 0), summary["counts"].get("SKIP", 0)))
    seconds = summary["seconds"]
    print("soak: round seconds min=%s max=%s avg=%s"
          % (seconds["min"], seconds["max"], seconds["avg"]))
    memory = summary["memory-available-kb"]
    print("soak: MemAvailable first=%s last=%s delta=%s kB"
          % (memory["first"], memory["last"], memory["delta"]))
    print("soak: coverage rounds=%s minutes=%s met=%s"
          % (summary["coverage"]["rounds-met"],
             summary["coverage"]["minutes-met"], summary["coverage"]["met"]))
    if summary["leaked-processes"]:
        print("soak: LEAKED %d process(es):"
              % len(summary["leaked-processes"]))
        for leak in summary["leaked-processes"]:
            print("soak:   round %s pid %s %s"
                  % (leak["round"], leak["process"]["pid"],
                     leak["process"]["cmdline"]))
    else:
        print("soak: no leaked umnet/passt/UML process after any round")
    for record in summary["rounds"]:
        print("round %-4d verdict=%-4s exit=%-5s seconds=%-7.1f peaks=%s "
              "leaked=%d"
              % (record["round"], record["verdict"], record["exit-code"],
                 record["seconds"],
                 ",".join("%s=%s" % (key, value)
                          for key, value in sorted(record["peaks"].items())),
                 len(record["leaked"])))


def write_summary(summary, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    stem = "soak-%s" % time.strftime("%Y-%m-%dT%H%M%SZ", time.gmtime())
    path = os.path.join(out_dir, stem + ".json")
    index = 2
    while os.path.exists(path):
        path = os.path.join(out_dir, "%s-%d.json" % (stem, index))
        index += 1
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=False)
        handle.write("\n")
    return path


def cmd_soak(args):
    started_at = now_iso()
    started = time.monotonic()
    marker = gu.watch_starttime()
    problems = gu.preflight(args.workdir, args.assets_dir)
    rounds = []
    log_path = os.path.join(args.workdir, args.log_name)
    if problems:
        # Refuse before booting anything: a soak of an incomplete environment
        # would spend an hour producing SKIPs.
        print("soak: environment incomplete: %s" % "; ".join(problems),
              file=sys.stderr)
    else:
        index = 1
        while index <= args.rounds:
            if args.minutes and (time.monotonic() - started) >= args.minutes * 60:
                break
            record = run_round(index, args, marker, log_path)
            rounds.append(record)
            print("round %-4d verdict=%-4s exit=%-5s seconds=%-7.1f leaked=%d"
                  % (index, record["verdict"], record["exit-code"],
                     record["seconds"], len(record["leaked"])), file=sys.stderr)
            if args.fail_fast and record["verdict"] != "PASS":
                break
            index += 1

    elapsed = time.monotonic() - started
    summary = summarize(rounds, args, started_at, elapsed, marker, problems)
    print_summary(summary)
    if args.out:
        path = write_summary(summary, args.out)
        print("soak: summary written to %s" % path, file=sys.stderr)
    gu.emit(summary["verdict"], summary["message"],
            rounds=len(rounds),
            leaked=len(summary["leaked-processes"]),
            seconds=round(elapsed, 1))
    return gu.exit_for(summary["verdict"])


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------

def _fixture(root):
    assets = os.path.join(root, "assets")
    work = os.path.join(root, "work")
    os.makedirs(os.path.join(work, "netroot"), exist_ok=True)
    os.makedirs(assets, exist_ok=True)
    for directory, names in ((assets, ("linux-bionic", "stub_exe")),
                             (work, ("umnet", "passt-bionic"))):
        for name in names:
            path = os.path.join(directory, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/sh\n# soak self-test fixture\n")
            os.chmod(path, 0o755)
    return assets, work, gu.write_sample_gate(os.path.join(root, "sample-gate.py"))


def _run(args):
    return subprocess.run([sys.executable, os.path.abspath(__file__)] + args,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          check=False)


def cmd_self_test(args):
    cases = []

    def case(name, ok, detail=""):
        cases.append((name, ok, detail))
        print("self-test: %-38s %s%s" % (name, "ok" if ok else "FAIL",
                                         " " + detail if detail else ""))

    root = tempfile.mkdtemp(prefix="soak-selftest-")
    strays = []
    try:
        assets, work, gate = _fixture(root)
        marker = "umsoak-selftest"

        def soak(mode, rounds=3, extra=(), label="t", assets_dir=None,
                 counter=None, gate_timeout=10, slack=5):
            # Generous timeouts by default: a self-test that fails when the
            # machine is busy teaches people to rerun instead of to read.
            out = os.path.join(root, "out-" + label)
            argv = [sys.executable, os.path.abspath(__file__),
                    "--workdir", work, "--assets-dir", assets_dir or assets,
                    "--gate", gate, "--gate-arg=--mode", "--gate-arg=" + mode,
                    "--gate-arg=--marker", "--gate-arg=" + marker,
                    "--rounds", str(rounds), "--watch", marker,
                    "--settle", "0.2", "--interval", "0.1",
                    "--gate-timeout", str(gate_timeout), "--slack", str(slack),
                    "--out", out]
            if counter:
                argv += ["--gate-arg=--counter", "--gate-arg=" + counter]
            argv += list(extra)
            result = subprocess.run(argv, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, check=False)
            summaries = sorted(os.listdir(out)) if os.path.isdir(out) else []
            summary = None
            if summaries:
                with open(os.path.join(out, summaries[-1]),
                          encoding="utf-8") as handle:
                    summary = json.load(handle)
            return result, summary

        # 1. three clean rounds are a pass, with no leaks and full coverage.
        result, summary = soak("pass", rounds=3, label="clean")
        case("clean soak is PASS", result.returncode == 0
             and summary and summary["verdict"] == "PASS",
             "exit=%s verdict=%s rounds=%s"
             % (result.returncode, summary and summary["verdict"],
                [(record["round"], record["verdict"], record["exit-code"],
                  record["leaked"]) for record in (summary or {}).get("rounds",
                                                                     [])]))
        case("clean soak counts 3/0/0", summary
             and summary["counts"] == {"PASS": 3, "FAIL": 0, "SKIP": 0})
        case("clean soak records peaks per round", summary
             and all(record["peaks"] for record in summary["rounds"]))
        case("clean soak reports coverage", summary
             and summary["coverage"]["met"] is True
             and summary["coverage"]["rounds-run"] == 3)
        case("clean soak lists not-verified", summary
             and summary["not-verified"])

        # 2. a leaked child process is found and fails the soak.
        result, summary = soak("leak", rounds=2, label="leak")
        case("leaked process fails the soak", result.returncode == gu.EXIT_FAIL
             and summary and summary["verdict"] == "FAIL"
             and len(summary["leaked-processes"]) > 0,
             "exit=%s leaked=%s" % (result.returncode,
                                    summary and len(summary["leaked-processes"])))
        for leak in (summary or {}).get("leaked-processes", []):
            strays.append(leak["process"]["pid"])
        case("leak is attributed to its round",
             summary and all("round" in leak
                             for leak in summary["leaked-processes"]))

        # 3. a flaky round is a failed soak, not an average.
        counter = os.path.join(root, "counter")
        result, summary = soak("flake", rounds=3, label="flake", counter=counter)
        case("one failing round fails the soak",
             result.returncode == gu.EXIT_FAIL and summary
             and summary["counts"]["FAIL"] == 1
             and summary["counts"]["PASS"] == 2,
             "counts=%s" % (summary and summary["counts"]))

        # 4. --fail-fast stops at the first bad round and says so.
        counter2 = os.path.join(root, "counter2")
        result, summary = soak("flake", rounds=5, label="fast",
                               counter=counter2, extra=["--fail-fast"])
        case("--fail-fast stops early and reports it",
             result.returncode == gu.EXIT_FAIL and summary
             and summary["coverage"]["met"] is False
             and summary["coverage"]["rounds-run"] == 2
             and summary["counts"]["FAIL"] == 1,
             "rounds=%s" % (summary and summary["coverage"]["rounds-run"]))

        # 5. a hung round is killed, called FAIL, and leaves nothing behind.
        result, summary = soak("hang", rounds=1, label="hang",
                               gate_timeout=2, slack=1)
        case("hung round is FAIL, not a hang",
             result.returncode == gu.EXIT_FAIL and summary
             and summary["verdict"] == "FAIL"
             and summary["rounds"][0]["timed-out"] is True,
             "exit=%s verdict=%s" % (result.returncode,
                                     summary and summary["verdict"]))
        case("hung round records the killed tree",
             summary and summary["rounds"][0]["killed-tree"])
        case("hung round leaves no watched process",
             summary and summary["rounds"][0]["leaked"] == [])
        leftovers = [proc.as_dict() for proc in
                     gu.matching(gu.proc_table(), (marker + "-hang",))]
        case("sample gate's children were really killed", not leftovers,
             str(leftovers))

        # 6. an environment with no assets is SKIP, and it says why.
        empty = os.path.join(root, "empty-assets")
        os.makedirs(empty, exist_ok=True)
        result, summary = soak("pass", rounds=2, label="skip", assets_dir=empty)
        case("incomplete environment is SKIP/77",
             result.returncode == gu.EXIT_SKIP and summary
             and summary["verdict"] == "SKIP" and not summary["rounds"],
             "exit=%s verdict=%s" % (result.returncode,
                                     summary and summary["verdict"]))
        case("SKIP soak explains itself",
             summary and any("missing" in gap for gap in summary["not-verified"]))

        # 7. a gate that always skips makes the soak incomplete, not green.
        result, summary = soak("skip", rounds=2, label="gateskip")
        case("rounds that skip do not add up to a pass",
             result.returncode == gu.EXIT_FAIL and summary
             and summary["counts"]["SKIP"] == 2,
             "exit=%s counts=%s" % (result.returncode,
                                    summary and summary["counts"]))
    finally:
        for pid in strays:
            gu.kill_tree(pid)
        shutil.rmtree(root, ignore_errors=True)

    failed = [name for name, ok, _ in cases if not ok]
    if failed:
        gu.emit("FAIL", "self-test failures: %s" % ", ".join(failed),
                cases=len(cases), failed=len(failed))
        return gu.EXIT_FAIL
    gu.emit("PASS", "soak self-test", cases=len(cases))
    return gu.EXIT_PASS


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workdir", default=gu.DEFAULT_WORKDIR)
    parser.add_argument("--assets-dir", default=gu.DEFAULT_ASSETS_DIR)
    parser.add_argument("--gate", default=gu.DEFAULT_GATE)
    parser.add_argument("--gate-arg", action="append", default=[],
                        help="extra argument for the gate (use = for options)")
    parser.add_argument("--rounds", type=int, default=3,
                        help="maximum number of rounds (default 3)")
    parser.add_argument("--minutes", type=float, default=0,
                        help="also stop after this many minutes (0: no bound)")
    parser.add_argument("--gate-timeout", type=float, default=300,
                        help="per-round gate timeout in seconds")
    parser.add_argument("--slack", type=float, default=60,
                        help="extra seconds before the soak kills the round")
    parser.add_argument("--settle", type=float, default=3,
                        help="seconds to wait after a round before judging "
                             "leaks")
    parser.add_argument("--interval", type=float, default=0.5,
                        help="process sampling interval in seconds")
    parser.add_argument("--watch", default=",".join(gu.WATCH_TOKENS),
                        help="comma-separated process-name tokens to watch")
    parser.add_argument("--dns", default="1.1.1.1")
    parser.add_argument("--log-name", default="net-gate.log")
    parser.add_argument("--out", default=os.path.join(gu.REPO_ROOT,
                                                      "docs/evidence/soak"),
                        help="directory for the JSON summary")
    parser.add_argument("--fail-fast", action="store_true",
                        help="stop at the first round that is not a pass")
    parser.add_argument("--echo", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.watch = tuple(token.strip() for token in args.watch.split(",")
                       if token.strip())
    if args.self_test:
        return cmd_self_test(args)
    if args.rounds < 1:
        parser.error("--rounds must be at least 1")
    if args.minutes < 0 or args.gate_timeout <= 0 or args.settle < 0:
        parser.error("--minutes, --gate-timeout and --settle must be sane")
    return cmd_soak(args)


if __name__ == "__main__":
    sys.exit(main())
