#!/usr/bin/env python3
"""Turn one Termux device run of the umnet network gate into archived evidence.

CI cannot run the network gate: it needs a real Android phone, a UML kernel, a
static passt and a host that is not traced.  Without a record, the only place a
device verdict lives is the terminal it was printed on -- which is how a green
"it worked on my phone" ends up in a release note with nothing behind it.

This tool runs the gate, then writes what it observed into `docs/evidence/`:

    <date>-umnet-network.json   the machine-readable record (checked by `check`)
    <date>-umnet-network.md     the same facts for a human reviewing the PR
    <date>-umnet-network.log    the guest/host log the verdict was read from

A record is only worth keeping if it cannot lie, so:

* every component is recorded by sha256, size and path -- "it passed" means
  *these* four binaries passed;
* the guest kernel banner is read out of the kernel binary, tying the hash to a
  kernel version;
* the repository commit and its dirty state are recorded, because binaries
  built from uncommitted source are fine to test but not fine to cite;
* `not-verified` is part of the format and must be non-empty: a record that
  claims complete coverage is a record that is wrong;
* verdict=SKIP (exit 77) means the environment made the run impossible.  It is
  archived as SKIP, never as PASS.

Commands:

    record-evidence.sh record [--workdir DIR] [--out docs/evidence]
    record-evidence.sh check  FILE [FILE...] [--verify-hashes]
    record-evidence.sh list
    record-evidence.sh self-test

`check` is offline and is the gate a release record has to pass: it rejects a
record that claims PASS while missing checks, hashes or an honest gap list.
`self-test` proves that by building deliberately dishonest records and
insisting they are rejected -- a checker that has never rejected anything is
not a checker.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import gateutil as gu

EVIDENCE_FORMAT = 1
KIND = "termux-umnet-network-gate"

# The gap list every record carries.  These are the things this gate does not
# measure no matter how well it goes, and saying so once is cheaper than a
# reviewer assuming the opposite.
BASE_GAPS = (
    "IPv6: the guest is only exercised over IPv4 (DHCP, ICMP, DNS, TCP)",
    "ext4/COW rootfs: this gate boots hostfs only; see the ext4 gate",
    "long-run stability and resource leakage: see soak-umnet.sh",
    "fault injection (link drop, guest panic, kernel kill): see inject-faults.sh",
    "device coverage: one device and one Android release, as recorded above",
    "throughput and behaviour under sustained load: not measured by this gate",
    "upstream Linux UML parity: this gate does not compare against upstream",
    "release manifest and SHA256SUMS: verified separately by verify-release.py",
    "CI-side checks (x86_64 build, shell/python syntax, release checker "
    "self-test): those run on GitHub runners, not here",
)

HOST_KEYS = ("host-role", "uname", "kernel-release", "machine")


def evidence_paths(out_dir, label):
    stem = "%s-%s" % (time.strftime("%Y-%m-%d", time.gmtime()), label)
    return (os.path.join(out_dir, stem + ".json"),
            os.path.join(out_dir, stem + ".md"),
            os.path.join(out_dir, stem + ".log"),
            stem)


def unique_path(path, force):
    """Never overwrite another run's evidence by accident.

    Two runs on the same day are two facts; silently replacing the first one
    would lose the only copy of a failure.
    """
    if force or not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    index = 2
    while os.path.exists("%s-%d%s" % (root, index, ext)):
        index += 1
    return "%s-%d%s" % (root, index, ext)


def repo_relative(path):
    """Path as a reviewer sees it; see gateutil.repo_relative()."""
    return gu.repo_relative(path)


def read_log(path, max_bytes=1 << 20):
    """Hash + excerpt the guest log, and say so when it cannot be read."""
    try:
        info = os.stat(path)
    except OSError as exc:
        return None, "guest log unreadable (%s): %s" % (path, exc)
    if info.st_size > max_bytes:
        return {"path": path, "sha256": gu.sha256_file(path),
                "size": info.st_size, "lines": None,
                "excerpt": None,
                "truncated": True}, None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        return None, "guest log unreadable (%s): %s" % (path, exc)
    return {"path": path, "sha256": gu.sha256_file(path),
            "size": info.st_size, "lines": len(lines), "truncated": False,
            "excerpt": {"head": lines[:15], "tail": lines[-15:]}}, None


def collect_context(workdir, assets_dir, banner=True):
    """Everything about the machine and the binaries a verdict depends on."""
    components = {}
    for name, where in gu.COMPONENTS:
        path = os.path.join(assets_dir if where == "assets" else workdir, name)
        record = gu.file_record(path)
        if record is None:
            # Missing is recorded, not hidden: a verdict about a binary that
            # was not there would be a verdict about nothing.
            components[name] = {"path": path, "missing": True}
        else:
            components[name] = record
    return {
        "host": gu.device_info(),
        "git": gu.git_state(),
        "components": components,
        "guest-kernel-banner": (gu.kernel_banner(
            os.path.join(assets_dir, "linux-bionic")) if banner else None),
    }


def build_gaps(record, extra):
    gaps = list(BASE_GAPS) + list(extra)
    verdict = record["verdict"]
    if verdict == "SKIP":
        gaps.insert(0, "the gate did not run in this attempt: %s"
                    % record.get("message", "environment incomplete"))
    if verdict == "FAIL":
        missing = [check.get("name") for check in record["checks"]
                   if check.get("status") != "ok"]
        if missing:
            gaps.insert(0, "checks reported MISSING: %s" % ", ".join(missing))
    if record["gate"]["timed-out"]:
        gaps.insert(0, "the run was cut off by the recorder's timeout; the "
                       "guest log may be incomplete")
    if record["gate"]["verdict-line"] is None:
        # A dry run never asked the gate anything, so "no verdict line" is
        # expected there and must not be reported as if the gate had failed.
        if record["gate"]["exit-code"] is None:
            gaps.insert(0, "the gate was not executed in this attempt")
        else:
            gaps.insert(0, "the gate exited without printing a verdict line, "
                           "so nothing was judged")
    if record["guest-log"] is None:
        gaps.insert(0, "no guest log was archived for this run")
    if not any(record["host"].get(key) for key in HOST_KEYS):
        gaps.insert(0, "host identity could not be probed on this machine")
    if not record["components"]["linux-bionic"].get("sha256"):
        gaps.insert(0, "linux-bionic was not hashed: the verdict cannot be "
                       "tied to a kernel binary")
    return gaps


def make_record(args):
    """Run (or refuse to run) the gate and build the evidence record."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # A record is synthetic whenever it did not come from this repository's
    # own gate: a sample gate can test the tooling, but calling its output
    # "device evidence" would be a lie the checker could not catch.
    synthetic = bool(args.synthetic) or (
        os.path.abspath(args.gate) != os.path.abspath(gu.DEFAULT_GATE))
    record = {
        "evidence-format": EVIDENCE_FORMAT,
        "kind": KIND,
        "synthetic": synthetic,
        "recorded": now,
        "verdict": "SKIP",
        "exit-code": gu.EXIT_SKIP,
        "message": "",
        "gate": {
            "path": repo_relative(args.gate),
            "argv": [],
            "timeout-s": args.timeout,
            "dns": args.dns,
            "seconds": None,
            "exit-code": None,
            "timed-out": False,
            "verdict-line": None,
            "stderr-tail": [],
        },
        "checks": [],
        "components": {},
        "guest-kernel-banner": None,
        "host": {},
        "git": {},
        "guest-log": None,
        "metrics": {},
        "not-verified": [],
        "notes": [],
    }
    if synthetic:
        record["notes"].append("synthetic: produced by %s, not by the "
                               "repository gate %s"
                               % (repo_relative(args.gate),
                                  repo_relative(gu.DEFAULT_GATE)))

    # Record the machine and the binaries even when the run is refused: what
    # was on the phone is part of why the run could not happen.
    record.update(collect_context(args.workdir, args.assets_dir,
                                  banner=not args.no_banner))
    if record["guest-kernel-banner"] is None:
        record["notes"].append("linux-bionic carries no readable "
                               "'Linux version' banner (not a UML kernel?)")

    traced = gu.tracer_pid()
    if traced > 0 and not synthetic:
        # Not a warning: a UML booted under a tracer is a different system, and
        # the harness forbids it outright.
        record["message"] = ("running inside a traced process (TracerPid=%d); "
                             "device gates must run on the Termux host" % traced)
        return record
    if traced > 0:
        # The sample gate does not boot UML, so a traced host is not a
        # problem here -- and the record is marked synthetic, which keeps it
        # out of release evidence either way.
        record["notes"].append("running under a tracer (TracerPid=%d); allowed "
                               "only because this record is synthetic" % traced)

    problems = gu.preflight(args.workdir, args.assets_dir)
    if problems:
        record["message"] = "; ".join(problems)
        record["notes"].append("preflight refused to boot: the gate would "
                               "have failed for want of assets, which proves "
                               "nothing about the build")
        return record

    gate_argv = ["--workdir", args.workdir, "--log", args.log_name,
                 "--timeout", str(args.timeout), "--dns", args.dns]
    gate_argv += list(args.gate_arg)
    record["gate"]["argv"] = gate_argv

    if args.dry_run:
        record["message"] = "dry run: preflight only, no gate was executed"
        return record

    run = gu.run_process(gu.gate_command(args.gate, gate_argv),
                         timeout=args.timeout + args.slack, echo=args.echo)
    record["gate"]["seconds"] = round(run.seconds, 3)
    record["gate"]["exit-code"] = run.exit_code
    record["gate"]["timed-out"] = run.timed_out
    record["gate"]["stderr-tail"] = run.text.splitlines()[-40:]

    report = gu.parse_gate_report(run.text)
    record["gate"]["verdict-line"] = report["verdict"]
    record["checks"] = [{"name": name, "status": "ok" if ok else "MISSING"}
                        for name, ok in report["checks"]]
    record["metrics"] = {
        "icmp-rtt-ms": report["icmp-rtt-ms"],
        "wget-rates": report["wget-rates"],
        "teardown": report["teardown"],
        "clean-shutdown": report["clean-shutdown"],
    }

    log_path = report["guest-log"] or os.path.join(args.workdir, args.log_name)
    if os.path.exists(log_path):
        log_record, log_problem = read_log(log_path, args.max_log_bytes)
    else:
        log_record, log_problem = None, ("guest log not found at %s" % log_path)
    if log_record is not None:
        log_record["source"] = log_path
        log_record["path"] = repo_relative(log_path)
        record["guest-log"] = log_record
    if log_problem:
        record["notes"].append(log_problem)
    if report["verdict"] is None:
        record["notes"].append("gate stderr contained no verdict line")
    if run.timed_out:
        record["notes"].append("recorder killed the gate after %ss"
                               % (args.timeout + args.slack))

    # The verdict is the gate's, not ours.  A missing verdict line is a
    # failure to judge, which is a FAIL, not a PASS and not a SKIP: the
    # environment was fine, the run just did not produce a result.
    if report["verdict"] in gu.VERDICTS:
        record["verdict"] = report["verdict"]
    else:
        record["verdict"] = "FAIL"
        record["message"] = "gate produced no verdict line"
    # An exit code that contradicts the printed verdict is a defect in the
    # gate itself; fail loudly rather than pick a winner.
    expected = gu.exit_for(record["verdict"])
    if run.exit_code != expected:
        record["notes"].append("gate exit code %s contradicts verdict=%s "
                               "(expected %s)"
                               % (run.exit_code, record["verdict"], expected))
        record["verdict"] = "FAIL"
    record["exit-code"] = gu.exit_for(record["verdict"])
    if not record["message"]:
        record["message"] = {
            "PASS": "device gate passed",
            "FAIL": "device gate failed",
            "SKIP": "environment could not run the gate",
        }[record["verdict"]]
    return record


def write_evidence(record, out_dir, label, args):
    os.makedirs(out_dir, exist_ok=True)
    json_path = unique_path(os.path.join(out_dir, "%s-%s.json"
                                         % (time.strftime("%Y-%m-%d",
                                                          time.gmtime()),
                                            label)), args.force)
    stem = os.path.splitext(os.path.basename(json_path))[0]
    md_path = os.path.join(out_dir, stem + ".md")

    # Copy the raw log next to the record so the evidence travels as one unit
    # and a reviewer can read the lines the verdict was read from.
    if record["guest-log"] is not None and not record["guest-log"]["truncated"]:
        source = record["guest-log"]["source"]
        if os.path.isfile(source):
            try:
                shutil.copyfile(source, os.path.join(out_dir, stem + ".log"))
                record["guest-log"]["path"] = stem + ".log"
            except OSError as exc:
                record["notes"].append("could not copy guest log: %s" % exc)

    record["not-verified"] = build_gaps(record, record["notes"])
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=False)
        handle.write("\n")
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(record, os.path.basename(json_path)))
    return json_path, md_path


def _cell(value):
    text = "-" if value in (None, "") else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(record, json_name):
    lines = []
    verdict = record["verdict"]
    lines.append("# Termux device evidence: umnet network gate")
    lines.append("")
    lines.append("| field | value |")
    lines.append("| --- | --- |")
    for key in ("verdict", "exit-code", "recorded", "message"):
        lines.append("| %s | `%s` |" % (key, _cell(record.get(key))))
    lines.append("| synthetic | %s |"
                 % ("yes (a sample gate, not evidence)" if record.get("synthetic")
                    else "no"))
    lines.append("| machine record | `%s` |" % json_name)
    lines.append("| gate | `%s` |" % _cell(
        record["gate"]["path"] + " " + " ".join(record["gate"]["argv"])))
    lines.append("")
    lines.append("## Host")
    lines.append("")
    lines.append("| field | value |")
    lines.append("| --- | --- |")
    for key in sorted(record["host"]):
        lines.append("| %s | %s |" % (key, _cell(record["host"][key])))
    lines.append("")
    lines.append("## Repository")
    lines.append("")
    git = record.get("git", {})
    lines.append("| git | value |")
    lines.append("| --- | --- |")
    for key in sorted(git):
        lines.append("| %s | %s |" % (key, _cell(git[key])))
    lines.append("")
    lines.append("## Components")
    lines.append("")
    lines.append("| component | sha256 | size | path |")
    lines.append("| --- | --- | --- | --- |")
    for name, _ in gu.COMPONENTS:
        entry = record["components"].get(name, {})
        if entry.get("missing"):
            lines.append("| %s | **missing** | - | %s |"
                         % (name, _cell(entry.get("path"))))
        else:
            lines.append("| %s | `%s` | %s | %s |"
                         % (name, _cell(entry.get("sha256")),
                            _cell(entry.get("size")), _cell(entry.get("path"))))
    lines.append("")
    if record.get("guest-kernel-banner"):
        lines.append("Guest kernel banner: `%s`"
                     % record["guest-kernel-banner"])
        lines.append("")
    lines.append("## Gate checks")
    lines.append("")
    if not record["checks"]:
        lines.append("No check was judged in this attempt "
                     "(verdict `%s`)." % verdict)
    else:
        lines.append("| check | result |")
        lines.append("| --- | --- |")
        for check in record["checks"]:
            lines.append("| %s | %s |" % (check["name"], check["status"]))
    lines.append("")
    metrics = record.get("metrics", {})
    lines.append("## Metrics")
    lines.append("")
    lines.append("| metric | value |")
    lines.append("| --- | --- |")
    for key in ("icmp-rtt-ms", "wget-rates", "teardown", "clean-shutdown"):
        if key in metrics:
            value = metrics[key]
            if not isinstance(value, str):
                value = json.dumps(value)
            lines.append("| %s | %s |" % (key, _cell(value)))
    lines.append("| gate seconds | %s |" % _cell(record["gate"]["seconds"]))
    lines.append("")
    log = record.get("guest-log")
    if log:
        lines.append("## Guest log excerpt")
        lines.append("")
        lines.append("`%s` (sha256 `%s`, %s lines)"
                     % (_cell(log["path"]), log["sha256"],
                        _cell(log.get("lines"))))
        lines.append("")
        excerpt = log.get("excerpt") or {}
        for title, caption in (("head", "first lines"), ("tail", "last lines")):
            block = excerpt.get(title) or []
            if block:
                lines.append("_%s_" % caption)
                lines.append("")
                lines.append("```text")
                lines.extend(block)
                lines.append("```")
                lines.append("")
    lines.append("## Not verified by this record")
    lines.append("")
    for gap in record["not-verified"]:
        lines.append("- %s" % gap)
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

REQUIRED_TOP = ("evidence-format", "kind", "recorded", "verdict", "exit-code",
                "gate", "checks", "components", "host", "not-verified")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})$")


def check_record(record, allow_synthetic=False, verify_hashes=False):
    """Return the list of reasons this record is not acceptable evidence."""
    errors = []

    def need(condition, message):
        if not condition:
            errors.append(message)

    if not isinstance(record, dict):
        return ["not a JSON object"]
    for key in REQUIRED_TOP:
        need(key in record, "missing key: %s" % key)
    if errors:
        return errors

    need(record["evidence-format"] == EVIDENCE_FORMAT,
         "evidence-format is %r, expected %d" % (record["evidence-format"],
                                                 EVIDENCE_FORMAT))
    need(record["kind"] == KIND, "unknown kind: %r" % record["kind"])
    need(bool(ISO.match(str(record["recorded"]))),
         "recorded is not ISO 8601 UTC: %r" % record["recorded"])
    verdict = record["verdict"]
    need(verdict in gu.VERDICTS, "unknown verdict: %r" % verdict)
    if verdict in gu.VERDICTS:
        need(record["exit-code"] == gu.exit_for(verdict),
             "exit-code %r does not match verdict %s"
             % (record["exit-code"], verdict))
    if record.get("synthetic") and not allow_synthetic:
        errors.append("synthetic record: it was produced by a sample gate and "
                      "is not evidence of a device run")

    gaps = record.get("not-verified")
    need(isinstance(gaps, list) and len(gaps) > 0,
         "not-verified must list at least one gap: a record claiming complete "
         "coverage is wrong")
    if isinstance(gaps, list):
        need(all(isinstance(gap, str) and gap.strip() for gap in gaps),
             "not-verified contains an empty entry")

    gate = record["gate"]
    need(isinstance(gate, dict), "gate must be an object")
    if isinstance(gate, dict):
        for key in ("path", "argv", "timeout-s"):
            need(key in gate, "gate is missing: %s" % key)
    checks = record["checks"]
    need(isinstance(checks, list), "checks must be a list")
    if isinstance(checks, list):
        for check in checks:
            need(isinstance(check, dict) and "name" in check
                 and check.get("status") in ("ok", "MISSING"),
                 "bad check entry: %r" % (check,))
        missing = [c["name"] for c in checks
                   if isinstance(c, dict) and c.get("status") != "ok"]
    else:
        missing = []

    host = record["host"]
    need(isinstance(host, dict), "host must be an object")
    if isinstance(host, dict):
        for key in HOST_KEYS:
            need(host.get(key) not in (None, ""),
                 "host is missing: %s" % key)

    components = record["components"]
    need(isinstance(components, dict), "components must be an object")
    if isinstance(components, dict):
        for name, _ in gu.COMPONENTS:
            entry = components.get(name)
            need(isinstance(entry, dict), "component not recorded: %s" % name)
            if not isinstance(entry, dict):
                continue
            if entry.get("missing"):
                need(verdict != "PASS",
                     "component %s is missing but the verdict is PASS" % name)
                continue
            digest = entry.get("sha256")
            need(isinstance(digest, str) and bool(HEX64.match(digest)),
                 "component %s has no sha256" % name)
            need(isinstance(entry.get("size"), int) and entry["size"] > 0,
                 "component %s has no size" % name)
            if verify_hashes and isinstance(entry.get("path"), str):
                path = entry["path"]
                if not os.path.isabs(path):
                    path = os.path.join(gu.REPO_ROOT, path)
                if os.path.exists(path):
                    actual = gu.sha256_file(path)
                    need(actual == digest,
                         "component %s does not match its recorded hash "
                         "(%s is now %s)" % (name, digest[:12], actual[:12]))

    if verdict == "PASS":
        need(isinstance(gate, dict) and gate.get("verdict-line") == "PASS",
             "PASS without a PASS verdict line from the gate")
        need(isinstance(gate, dict) and gate.get("exit-code") == gu.EXIT_PASS,
             "PASS without a zero exit code from the gate")
        need(isinstance(gate, dict) and not gate.get("timed-out"),
             "PASS from a run that was killed on timeout")
        need(isinstance(checks, list) and len(checks) > 0,
             "PASS without any judged check")
        need(not missing, "PASS with checks reported MISSING: %s"
             % ", ".join(missing))
    if verdict == "SKIP":
        need(not checks, "SKIP with judged checks: a skipped run judged nothing")
        need(bool(str(record.get("message", "")).strip()),
             "SKIP without a reason")
    if verdict == "FAIL":
        need(bool(missing) or not isinstance(gate, dict)
             or gate.get("exit-code") != gu.EXIT_PASS
             or gate.get("verdict-line") != "PASS"
             or gate.get("timed-out"),
             "FAIL that looks exactly like a pass")
    log = record.get("guest-log")
    if log is not None:
        need(isinstance(log, dict) and HEX64.match(str(log.get("sha256", ""))),
             "guest-log recorded without a sha256")
    return errors


def cmd_check(args):
    failures = 0
    for path in args.files:
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError) as exc:
            print("FAIL %s: cannot read record: %s" % (path, exc),
                  file=sys.stderr)
            failures += 1
            continue
        errors = check_record(record, allow_synthetic=args.allow_synthetic,
                              verify_hashes=args.verify_hashes)
        if errors:
            failures += 1
            for error in errors:
                # Reasons go to stderr with the verdict: a caller that only
                # wants the machine-readable answer reads stderr, and a caller
                # that wants to know *why* gets it on the same stream.
                print("FAIL %s: %s" % (path, error), file=sys.stderr)
        else:
            print("ok   %s verdict=%s%s"
                  % (path, record["verdict"],
                     " (synthetic)" if record.get("synthetic") else ""))
    if failures:
        gu.emit("FAIL", "%d of %d record(s) rejected"
                % (failures, len(args.files)))
        return gu.EXIT_FAIL
    gu.emit("PASS", "%d record(s) valid" % len(args.files))
    return gu.EXIT_PASS


def cmd_list(args):
    if not os.path.isdir(args.out):
        gu.emit("SKIP", "no evidence directory yet: %s" % args.out)
        return gu.EXIT_SKIP
    rows = []
    for name in sorted(os.listdir(args.out)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(args.out, name)
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            rows.append((name, "UNREADABLE", "", ""))
            continue
        rows.append((name, record.get("verdict", "?"),
                     record.get("recorded", ""),
                     "synthetic" if record.get("synthetic") else ""))
    if not rows:
        gu.emit("SKIP", "no evidence records in %s" % args.out)
        return gu.EXIT_SKIP
    for name, verdict, recorded, flag in rows:
        print("%-8s %-20s %s %s" % (verdict, recorded, name, flag))
    return gu.EXIT_PASS


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------

def _fixture(root):
    """A work directory and asset directory that pass preflight.

    The binaries are shell stubs: the sample gate never executes them, and the
    point of the test is the tooling, not the kernel.
    """
    assets = os.path.join(root, "assets")
    work = os.path.join(root, "work")
    os.makedirs(os.path.join(work, "netroot"), exist_ok=True)
    os.makedirs(assets, exist_ok=True)
    for directory, names in ((assets, ("linux-bionic", "stub_exe")),
                             (work, ("umnet", "passt-bionic"))):
        for name in names:
            path = os.path.join(directory, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/sh\n# fixture for the evidence self-test\n")
            os.chmod(path, 0o755)
    with open(os.path.join(assets, "linux-bionic"), "ab") as handle:
        handle.write(b"Linux version 7.2.0-rc4 (fixture)\n\x00")
    gate = gu.write_sample_gate(os.path.join(root, "sample-gate.py"))
    return assets, work, gate


def _run(args):
    return subprocess.run([sys.executable, os.path.abspath(__file__)] + args,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          check=False)


def cmd_self_test(args):
    cases = []

    def case(name, ok, detail=""):
        cases.append((name, ok, detail))
        print("self-test: %-34s %s%s" % (name, "ok" if ok else "FAIL",
                                         " " + detail if detail else ""))

    root = tempfile.mkdtemp(prefix="evidence-selftest-")
    try:
        assets, work, gate = _fixture(root)
        out = os.path.join(root, "evidence")

        def record(mode, extra=(), out_dir=out, assets_dir=None, label="t"):
            # --gate-arg=--mode: the '=' form keeps argparse from reading the
            # sample gate's own option as one of ours.
            argv = ["record", "--workdir", work, "--assets-dir",
                    assets_dir or assets, "--out", out_dir, "--gate", gate,
                    "--gate-arg=--mode", "--gate-arg=" + mode,
                    "--label", label, "--silent"]
            argv += list(extra)
            return _run(argv)

        def record_path(label):
            """The record for one label, whatever date prefix it got.

            Records are named by UTC date, so a run at 06:00 local in +08:00
            lands on the previous UTC day; nothing may assume the date.
            """
            names = [n for n in os.listdir(out)
                     if n.endswith("-%s.json" % label)] if os.path.isdir(out) else []
            if not names:
                raise AssertionError("no record for label %r in %s" % (label, out))
            return os.path.join(out, sorted(names)[-1])

        # 1. a passing sample run: recorded, hashed, honest about its limits.
        run = record("pass")
        pass_path = record_path("t")
        with open(pass_path, encoding="utf-8") as handle:
            sample = json.load(handle)
        case("sample PASS is recorded", run.returncode == 0
             and sample["verdict"] == "PASS" and len(sample["checks"]) == 9,
             "exit=%s verdict=%s checks=%d"
             % (run.returncode, sample["verdict"], len(sample["checks"])))
        case("sample run is marked synthetic", sample["synthetic"] is True)
        case("PASS record has four hashes",
             all(HEX64.match(sample["components"][name]["sha256"])
                 for name, _ in gu.COMPONENTS))
        case("PASS record lists not-verified", bool(sample["not-verified"]))
        case("guest log archived next to record",
             sample["guest-log"] is not None
             and os.path.exists(os.path.join(out, sample["guest-log"]["path"])))
        case("markdown lists the gaps",
             "- " in open(os.path.join(out, os.path.basename(pass_path)[:-5]
                                       + ".md"), encoding="utf-8").read())
        case("synthetic record accepted for format checks",
             _run(["check", pass_path, "--allow-synthetic"]).returncode == 0)
        case("synthetic record is refused as evidence",
             _run(["check", pass_path]).returncode == gu.EXIT_FAIL
             and b"synthetic" in _run(["check", pass_path]).stderr)

        # A record of a real run is the same document with synthetic=false.
        # Everything below mutates this one, so the honesty rules are tested
        # against what would actually be committed.
        real = json.loads(json.dumps(sample))
        real["synthetic"] = False
        real_path = os.path.join(root, "real-record.json")
        with open(real_path, "w", encoding="utf-8") as handle:
            json.dump(real, handle, indent=2)
        case("real record is accepted",
             _run(["check", real_path]).returncode == 0)
        case("--verify-hashes accepts unchanged components",
             _run(["check", real_path, "--verify-hashes"]).returncode == 0)
        with open(os.path.join(work, "umnet"), "a", encoding="utf-8") as handle:
            handle.write("# tampered\n")
        case("--verify-hashes rejects a changed component",
             _run(["check", real_path,
                   "--verify-hashes"]).returncode == gu.EXIT_FAIL)

        # 3. a failing run is recorded as FAIL, with the missing checks named.
        run_fail = record("fail", label="fail")
        fail_path = record_path("fail")
        with open(fail_path, encoding="utf-8") as handle:
            failed = json.load(handle)
        case("sample FAIL is recorded as FAIL",
             run_fail.returncode == gu.EXIT_FAIL and failed["verdict"] == "FAIL",
             "exit=%s verdict=%s" % (run_fail.returncode, failed["verdict"]))
        case("FAIL names the missing checks",
             any("tcp" in gap for gap in failed["not-verified"]))
        case("FAIL record passes check",
             _run(["check", fail_path, "--allow-synthetic"]).returncode == 0)

        # 4. a run whose gate never printed a verdict is FAIL, not PASS/SKIP.
        run_noverdict = record("noverdict", label="noverdict")
        nov_path = record_path("noverdict")
        with open(nov_path, encoding="utf-8") as handle:
            nov = json.load(handle)
        case("missing verdict line is FAIL",
             run_noverdict.returncode == gu.EXIT_FAIL
             and nov["verdict"] == "FAIL",
             "exit=%s verdict=%s" % (run_noverdict.returncode, nov["verdict"]))

        # 5. an empty environment is SKIP 77: it judged nothing and says why.
        empty_assets = os.path.join(root, "empty-assets")
        os.makedirs(empty_assets, exist_ok=True)
        run_skip = record("pass", label="skip", assets_dir=empty_assets)
        skip_path = record_path("skip")
        with open(skip_path, encoding="utf-8") as handle:
            skipped = json.load(handle)
        case("missing assets are SKIP/77",
             run_skip.returncode == gu.EXIT_SKIP
             and skipped["verdict"] == "SKIP"
             and skipped["exit-code"] == 77,
             "exit=%s verdict=%s" % (run_skip.returncode, skipped["verdict"]))
        case("SKIP judged no check", skipped["checks"] == [])
        case("SKIP record passes check",
             _run(["check", skip_path, "--allow-synthetic"]).returncode == 0)

        # 6. the checker rejects the ways a record can lie.
        def mutate(base, name, change):
            candidate = json.loads(json.dumps(base))
            candidate["synthetic"] = False
            change(candidate)
            path_m = os.path.join(root, "mutated-%s.json" % name)
            with open(path_m, "w", encoding="utf-8") as handle:
                json.dump(candidate, handle, indent=2)
            result = _run(["check", path_m])
            case("reject %s" % name, result.returncode == gu.EXIT_FAIL,
                 "" if result.returncode == gu.EXIT_FAIL
                 else "the checker accepted a dishonest record")

        mutate(real, "pass-with-missing-check",
               lambda r: r["checks"].append({"name": "tcp",
                                             "status": "MISSING"}))
        mutate(real, "pass-without-hash",
               lambda r: r["components"]["linux-bionic"].update(
                   {"sha256": None}))
        mutate(real, "pass-without-component",
               lambda r: r["components"].update(
                   {"umnet": {"path": "/nowhere", "missing": True}}))
        mutate(real, "empty-not-verified",
               lambda r: r.update({"not-verified": []}))
        mutate(real, "bad-timestamp",
               lambda r: r.update({"recorded": "today"}))
        mutate(real, "exit-code-mismatch",
               lambda r: r.update({"exit-code": 3}))
        mutate(real, "timed-out-pass",
               lambda r: r["gate"].update({"timed-out": True}))
        mutate(real, "missing-host",
               lambda r: r["host"].pop("uname", None))
        mutate(real, "no-gate-fields", lambda r: r.pop("gate", None))
        mutate(skipped, "skip-relabelled-pass",
               lambda r: r.update({"verdict": "PASS", "exit-code": 0}))
        mutate(real, "pass-relabelled-skip",
               lambda r: r.update({"verdict": "SKIP", "exit-code": 77}))

        # 7. a dry run never invents a verdict.
        run_dry = record("pass", extra=["--dry-run"], label="dry")
        dry_path = record_path("dry")
        with open(dry_path, encoding="utf-8") as handle:
            dry = json.load(handle)
        case("dry run is SKIP and did not execute the gate",
             run_dry.returncode == gu.EXIT_SKIP and dry["verdict"] == "SKIP"
             and dry["gate"]["exit-code"] is None)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    failed = [name for name, ok, _ in cases if not ok]
    if failed:
        gu.emit("FAIL", "self-test failures: %s" % ", ".join(failed),
                cases=len(cases), failed=len(failed))
        return gu.EXIT_FAIL
    gu.emit("PASS", "evidence recorder self-test", cases=len(cases))
    return gu.EXIT_PASS


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    rec = sub.add_parser("record", help="run the device gate and archive it")
    rec.add_argument("--workdir", default=gu.DEFAULT_WORKDIR,
                     help="gate work directory (umnet, passt-bionic, netroot)")
    rec.add_argument("--assets-dir", default=gu.DEFAULT_ASSETS_DIR,
                     help="directory holding linux-bionic and stub_exe")
    rec.add_argument("--out", default=os.path.join(gu.REPO_ROOT,
                                                   "docs/evidence"))
    rec.add_argument("--label", default="umnet-network")
    rec.add_argument("--gate", default=gu.DEFAULT_GATE)
    rec.add_argument("--gate-arg", action="append", default=[],
                     help="extra argument appended to the gate command "
                          "(used by the offline self-test)")
    rec.add_argument("--timeout", type=float, default=300,
                     help="gate timeout in seconds (passed to the gate)")
    rec.add_argument("--slack", type=float, default=60,
                     help="extra seconds before the recorder kills the gate")
    rec.add_argument("--dns", default="1.1.1.1",
                     help="nameserver the bridge announces to the guest")
    rec.add_argument("--log-name", default="net-gate.log")
    rec.add_argument("--max-log-bytes", type=int, default=1 << 20)
    rec.add_argument("--no-banner", action="store_true",
                     help="skip reading the guest kernel banner")
    rec.add_argument("--dry-run", action="store_true",
                     help="probe the environment and hash assets, do not boot")
    rec.add_argument("--echo", action="store_true",
                     help="stream the gate's stderr while it runs")
    rec.add_argument("--silent", action="store_true",
                     help="do not print the human summary")
    rec.add_argument("--synthetic", action="store_true",
                     help="mark the record as produced by a sample gate "
                          "(also set automatically when --gate is not the "
                          "repository gate)")
    rec.add_argument("--force", action="store_true",
                     help="overwrite today's record instead of adding -2")
    rec.set_defaults(func=cmd_record)

    chk = sub.add_parser("check", help="validate archived evidence records")
    chk.add_argument("files", nargs="+")
    chk.add_argument("--allow-synthetic", action="store_true",
                     help="accept records from the sample gate (format tests)")
    chk.add_argument("--verify-hashes", action="store_true",
                     help="re-hash components that still exist on disk")
    chk.set_defaults(func=cmd_check)

    lst = sub.add_parser("list", help="list archived records")
    lst.add_argument("--out", default=os.path.join(gu.REPO_ROOT,
                                                   "docs/evidence"))
    lst.set_defaults(func=cmd_list)

    st = sub.add_parser("self-test", help="prove the recorder and checker work")
    st.set_defaults(func=cmd_self_test)
    return parser


def cmd_record(args):
    record = make_record(args)
    if args.dry_run:
        # A dry run still writes a record: "these assets, hashed, on this
        # date, gate not executed" is exactly what someone rebuilding the
        # assets wants to compare against later.
        for name, _ in gu.COMPONENTS:
            entry = record["components"].get(name, {})
            print("%-14s %s" % (name, "present" if entry.get("sha256")
                                else "MISSING"), file=sys.stderr)
    json_path, md_path = write_evidence(record, args.out, args.label, args)
    if not args.silent:
        fields = {
            "evidence": repo_relative(json_path),
            "document": repo_relative(md_path),
            "checks": "%d/%d" % (sum(1 for check in record["checks"]
                                     if check["status"] == "ok"),
                                 len(record["checks"])),
        }
        if record["gate"]["seconds"] is not None:
            fields["seconds"] = record["gate"]["seconds"]
        gu.emit(record["verdict"], record["message"], **fields)
    return gu.exit_for(record["verdict"])

    json_path, md_path = write_evidence(record, args.out, args.label, args)
    if not args.silent:
        gu.emit(record["verdict"], record["message"],
                evidence=repo_relative(json_path),
                document=repo_relative(md_path),
                checks="%d/%d" % (sum(1 for c in record["checks"]
                                      if c["status"] == "ok"),
                                  len(record["checks"])),
                seconds=record["gate"]["seconds"])
    return gu.exit_for(record["verdict"])


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "command", None):
        args = parser.parse_args(["record"] + sys.argv[1:])
    if args.command == "record" and args.slack < 0:
        parser.error("--slack must not be negative")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
