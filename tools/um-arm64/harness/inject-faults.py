#!/usr/bin/env python3
"""Inject the failures the network gate has never been tested against.

The gate exercises the happy path and a few teardown paths (timeout, SIGTERM,
passt crash).  What it has never been asked is what happens when the link
disappears *while the guest is using it*, when the guest kernel dies, or when
the bridge is left holding the pieces.  Those are the failures a phone user
actually hits -- a radio handover, an Android low-memory kill, a kernel oops.

Every scenario states its criteria up front and writes them down with the
evidence, so "it ran" can never be mistaken for "it passed":

    link-drop    SIGKILL passt mid-run: the run must end by itself, must not
                 report PASS, and must leave no umnet/passt/UML behind
    kernel-kill  SIGKILL the UML process mid-run: same three requirements
    guest-panic  the guest panics (sysrq-c) behind a scratch rootfs: the panic
                 must be visible, the run must end by itself, nothing may
                 survive, and it must not report a clean guest exit

SKIP (exit 77) means the environment could not run the scenario at all.  It
never means the scenario passed.  A criterion that could not be evaluated is
printed as not-ok with the reason, never quietly dropped.

    inject-faults.sh --scenario link-drop
    inject-faults.sh --scenario guest-panic --panic-root <dir>
    inject-faults.sh --self-test          # no device needed
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types

import gateutil as gu

FAULT_FORMAT = 1
KIND = "umnet-network-fault-injection"
SCENARIOS = ("link-drop", "kernel-kill", "guest-panic")

# Marks a directory this tool created, and nothing else.  It is what makes
# "delete the previous copy" safe.
PANIC_ROOT_MARKER = ".inject-faults-panic-root"

# What the injected guest script does.  sysrq-c asks the kernel to crash; the
# script then keeps the guest alive long enough for the panic to be observed,
# and prints a marker if it never happened (which is itself evidence: the
# kernel may have been built without CONFIG_MAGIC_SYSRQ).
PANIC_SCRIPT = """#!/bin/sh
# Written by inject-faults.sh as the guest's net-command.sh.
set +e
echo 1 > /proc/sys/kernel/sysrq 2>/dev/null
echo PANIC_TRIGGER
echo c > /proc/sysrq-trigger 2>/dev/null
sleep 15
echo PANIC_NOT_TRIGGERED
exit 0
"""

PANIC_MARKER = re.compile(
    r"Kernel panic|end Kernel panic|panic - not syncing|BUG: unable to handle")

BASE_GAPS = (
    "a fault injection exercises one failure at one moment; it does not prove "
    "the bridge handles every failure mode",
    "link-drop kills passt, which is the closest a non-root Termux process "
    "gets to dropping the host link; radio handover and interface-down are "
    "not reproducible here",
    "guest-panic needs CONFIG_MAGIC_SYSRQ and a writable /proc/sysrq-trigger "
    "in the guest; PANIC_NOT_TRIGGERED in the log means the trigger never "
    "reached the kernel",
    "the panic scenario boots a copy of the rootfs and its own command line; "
    "it does not prove the release gate's own arguments are right",
    "timing is wall-clock, not deterministic: the fault is injected a fixed "
    "delay after the UML process appears",
    "the survivor check is global: another process on the phone whose command "
    "line matches a watched token (a second harness run, say) is reported as "
    "a survivor too; run device gates one at a time",
)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def criterion(cid, description, ok, evidence, required=True):
    return {"id": cid, "description": description, "ok": bool(ok),
            "evidence": evidence, "required": bool(required)}


class Attack(object):
    """A gate (or bridge) started in the background so that it can be hit."""

    def __init__(self, argv, tokens, interval=0.3):
        self.argv = list(argv)
        self.tokens = tuple(tokens)
        self.interval = interval
        self.proc = None
        self.sampler = None
        self.killed = []
        self._sink = []
        self._reader = None
        self._started = None

    def start(self):
        self._started = time.monotonic()
        self.proc = subprocess.Popen(
            self.argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, start_new_session=True)
        self.sampler = gu.Sampler(self.proc.pid, tokens=self.tokens,
                                  interval=self.interval).start()
        self._reader = threading.Thread(
            target=gu.pump_stream, args=(self.proc.stderr, self._sink, False))
        self._reader.daemon = True
        self._reader.start()
        return self

    @property
    def seconds(self):
        return None if self._started is None else time.monotonic() - self._started

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def find(self, token):
        """The processes **named** `token` inside this run's own process tree.

        Matched on the executable name, never on the whole command line.  A
        substring match is too loose here twice over.  "passt-bionic" appears in
        umnet's arguments (`umnet --passt .../passt-bionic`), so the link-drop
        scenario killed the bridge and then recorded, in its evidence, that it
        had killed passt.  And the watcher's own tooling has the token in its
        command line, so a fuzzy match turns the observer into an observed.
        """
        table = gu.proc_table()
        mine = {proc.pid for proc in gu.descendants(table, self.proc.pid)}
        hits = gu.matching(table, (token,), pids=mine,
                           exclude=gu.self_tree_pids(table))
        return [
            proc for proc in hits
            if proc.comm == token
            or os.path.basename(proc.argv[0] if proc.argv else "") == token
        ]

    def wait_for(self, token, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hits = self.find(token)
            if hits:
                return hits
            if not self.alive():
                return []
            time.sleep(0.2)
        return []

    def kill(self, pid, sig=signal.SIGKILL):
        os.kill(pid, sig)

    def wait(self, deadline):
        """Wait for the run to end on its own.  Returns True if it had to be
        killed after the deadline."""
        try:
            self.proc.wait(timeout=deadline)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            self.killed = gu.kill_tree(self.proc.pid)
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
        self.sampler.stop()
        if self._reader is not None:
            self._reader.join(timeout=5)
        return timed_out

    def text(self):
        return "".join(self._sink)

    def survivors(self):
        """Watched processes that are still alive after the run ended.

        Global by necessity: an orphan is reparented, so the process tree is
        gone by the time the leak is measurable.  This process and its
        ancestors are excluded, since their command lines name the tokens.
        """
        table = gu.proc_table()
        return [proc.as_dict() for proc in
                gu.matching(table, self.tokens,
                            exclude=gu.self_tree_pids(table))]


def settle(attack, delay):
    """Give the guest `delay` seconds to get going, but never past the run.

    Returns False if the run ended first, which is a real answer: the injection
    point was not reached, and the caller says that instead of blaming a
    process that had simply finished.

    A fixed sleep measured from the start of the run cannot work: a gate run on
    this hardware is over in about five seconds, so the original 20s default was
    a delay measured from *after* the run, and every scenario reported that its
    target "was already gone" while nothing had been injected at all.
    """
    deadline = time.monotonic() + delay
    while time.monotonic() < deadline:
        if not attack.alive():
            return False
        time.sleep(0.1)
    return attack.alive()


def gate_argv(args):
    return gu.gate_command(args.gate, [
        "--workdir", args.workdir, "--log", args.log_name,
        "--timeout", str(args.gate_timeout), "--dns", args.dns]
        + list(args.gate_arg))


def read_log(path, tail=25):
    """Hash + excerpt the guest log; None when there is nothing to read."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    lines = text.splitlines()
    return {"path": path, "sha256": gu.sha256_file(path),
            "size": os.path.getsize(path), "lines": len(lines),
            "panic-observed": bool(PANIC_MARKER.search(text)),
            "panic-not-triggered": "PANIC_NOT_TRIGGERED" in text,
            "clean-guest-exit": "UM_NET_COMMAND_EXIT=0" in text,
            "excerpt": {"head": lines[:12], "tail": lines[-tail:]}}


def finish_run(scenario, args, attack, criteria, notes, log_path,
               no_false_pass=True):
    """Common tail: wait for the run, judge survivors, build the record."""
    ended_clean = attack.wait(args.deadline)
    if args.settle > 0:
        time.sleep(args.settle)
    survivors = attack.survivors()
    report = gu.parse_gate_report(attack.text())

    criteria.append(criterion(
        "c2-run-ended",
        "the run ended by itself within %ss of the injection" % args.deadline,
        not ended_clean,
        ("still running after %ss; killed %s" % (args.deadline, attack.killed))
        if ended_clean else "ended %.1fs into the run" % (attack.seconds or 0)))
    if no_false_pass:
        criteria.append(criterion(
            "c3-no-false-pass",
            "a run whose link or guest was destroyed does not report PASS",
            report.get("verdict") != "PASS",
            "gate verdict=%s" % report.get("verdict")))
    criteria.append(criterion(
        "c4-no-survivors",
        "no umnet/passt/UML process survived the run",
        not survivors,
        "survivors: %s" % survivors if survivors else "none after %ss settle"
        % args.settle))
    if ended_clean:
        notes.append("the injector had to kill the run after the deadline; "
                     "whatever survived was then killed too")

    record = {
        "fault-format": FAULT_FORMAT,
        "kind": KIND,
        "scenario": scenario,
        "synthetic": bool(getattr(args, "synthetic", False)),
        "recorded": now_iso(),
        "verdict": "PASS" if all(c["ok"] for c in criteria
                                 if c["required"]) else "FAIL",
        "criteria": criteria,
        "run": {
            "argv": attack.argv,
            "exit-code": attack.proc.returncode,
            "timed-out": ended_clean,
            "killed-tree": attack.killed,
            "seconds": round(attack.seconds or 0, 3),
            "verdict-line": report.get("verdict"),
            "checks": [{"name": name, "status": "ok" if ok else "MISSING"}
                       for name, ok in report.get("checks", [])],
            "teardown": report.get("teardown"),
            "stderr-tail": attack.text().splitlines()[-30:],
        },
        "survivors": survivors,
        "guest-log": read_log(log_path),
        "host": gu.device_info(),
        "not-verified": [],
        "notes": notes,
    }
    record["exit-code"] = gu.exit_for(record["verdict"])
    record["message"] = ("%s: all required criteria met" % scenario
                         if record["verdict"] == "PASS"
                         else "%s: %s" % (scenario, "; ".join(
                             c["id"] for c in criteria
                             if c["required"] and not c["ok"])))
    gaps = list(BASE_GAPS)
    record["not-verified"] = gaps
    return record


def scenario_link_drop(args):
    """Kill passt while the guest is using the link."""
    attack = Attack(gate_argv(args), tuple(args.watch), args.interval).start()
    criteria, notes = [], []
    uml = attack.wait_for("linux-bionic", args.start_timeout)
    injection = {"target": "passt-bionic", "signal": "SIGKILL", "injected": False,
                 "pid": None, "at-seconds": None, "cmdline": None}
    if not uml:
        criteria.append(criterion(
            "c1-injected-live",
            "the fault was injected while the run was live",
            False,
            "no UML process appeared within %ss (run alive=%s)"
            % (args.start_timeout, attack.alive())))
    else:
        passt = attack.wait_for("passt-bionic", args.start_timeout)
        if passt and settle(attack, args.delay):
            injection.update({"injected": True, "pid": passt[0].pid,
                              "at-seconds": round(attack.seconds, 1),
                              "cmdline": passt[0].cmdline})
            attack.kill(passt[0].pid)
            criteria.append(criterion(
                "c1-injected-live",
                "the fault was injected while the run was live",
                True,
                "SIGKILL to pid %d (%s) %.1fs into the run, while the UML "
                "process was alive" % (passt[0].pid, passt[0].cmdline,
                                       attack.seconds)))
        else:
            criteria.append(criterion(
                "c1-injected-live",
                "the fault was injected while the run was live",
                False,
                "no passt-bionic process in the run's tree while the run "
                "was alive (run alive=%s, waited %ss)" 
                % (attack.alive(), args.delay)))
    args.injection = injection
    record = finish_run("link-drop", args, attack, criteria, notes,
                        os.path.join(args.workdir, args.log_name))
    record["injection"] = injection
    if not injection["injected"]:
        record["not-verified"].insert(
            0, "the link was never dropped: the injection point was not "
               "reached, so nothing about link loss was measured")
    link_loss = re.search(r"passt (killed|exited|died|gone)[^\n]*",
                          attack.text())
    record["criteria"].append(criterion(
        "c5-link-loss-visible",
        "the run's own output shows the bridge noticing the link going away "
        "(informational: absence is worth investigating, not automatically a "
        "failure)",
        bool(link_loss), link_loss.group(0) if link_loss
        else "no passt-death line in the run output", required=False))
    return record


def scenario_kernel_kill(args):
    """Kill the UML process: what an Android low-memory kill looks like."""
    attack = Attack(gate_argv(args), tuple(args.watch), args.interval).start()
    criteria, notes = [], []
    uml = attack.wait_for("linux-bionic", args.start_timeout)
    injection = {"target": "linux-bionic", "signal": "SIGKILL",
                 "injected": False, "pid": None, "at-seconds": None,
                 "cmdline": None}
    if not uml:
        criteria.append(criterion(
            "c1-injected-live",
            "the UML process was killed while the run was live",
            False,
            "no UML process appeared within %ss (run alive=%s)"
            % (args.start_timeout, attack.alive())))
    else:
        live = attack.wait_for("linux-bionic", args.start_timeout)
        if live and settle(attack, args.delay):
            injection.update({"injected": True, "pid": live[0].pid,
                              "at-seconds": round(attack.seconds, 1),
                              "cmdline": live[0].cmdline})
            attack.kill(live[0].pid)
            criteria.append(criterion(
                "c1-injected-live",
                "the UML process was killed while the run was live",
                True,
                "SIGKILL to pid %d (%s) %.1fs into the run"
                % (live[0].pid, live[0].cmdline, attack.seconds)))
        else:
            criteria.append(criterion(
                "c1-injected-live",
                "the UML process was killed while the run was live",
                False,
                "the run ended before the injection could land "
                "(run alive=%s, waited %ss); a gate run here lasts about 5s"
                % (attack.alive(), args.delay)))
    args.injection = injection
    record = finish_run("kernel-kill", args, attack, criteria, notes,
                        os.path.join(args.workdir, args.log_name))
    record["injection"] = injection
    return record


class SetupRefused(Exception):
    """The tool refuses to touch a path it does not own.

    A refusal is a SKIP, not a FAIL: nothing was measured, so nothing can be
    claimed about the bridge.
    """


def prepare_panic_root(args):
    """A copy of the hostfs root whose net-command.sh panics the guest.

    The gate rewrites `net-command.sh` on every run, and the assets under the
    work directory belong to it; so the panic scenario gets its own root
    instead of editing a file the gate owns.

    This function deletes a directory, so it is written to be incapable of
    deleting the real rootfs: it may only create and remove a copy it marked
    itself, it refuses a target that overlaps the source, and it refuses to
    remove anything that does not carry its marker.
    """
    source = os.path.abspath(os.path.join(args.workdir, "netroot"))
    target = os.path.abspath(args.panic_root)
    if not os.path.isdir(source):
        raise SetupRefused("no hostfs root to copy: %s" % source)
    if not os.path.isfile(os.path.join(source, "um-init")):
        # The bridge boots `init=/um-init`, which has to exist inside the
        # root.  Without it the kernel panics with "Requested init /um-init
        # failed", which reads like a kernel fault and is a missing file.
        raise SetupRefused("%s has no um-init; the root needs the hostfs "
                           "init from tools/um-arm64/harness/netroot-um-init.sh"
                           % source)
    if (target == source
            or target.startswith(source + os.sep)
            or source.startswith(target + os.sep)):
        raise SetupRefused("--panic-root %s overlaps the gate's own rootfs %s: "
                           "inject-faults.sh copies a root, it never modifies "
                           "the assets the gate owns" % (target, source))
    if os.path.exists(target):
        if not os.path.isfile(os.path.join(target, PANIC_ROOT_MARKER)):
            raise SetupRefused("refusing to delete %s: inject-faults.sh did "
                               "not create it (no %s marker); remove it "
                               "yourself if it is really disposable"
                               % (target, PANIC_ROOT_MARKER))
        shutil.rmtree(target)
    shutil.copytree(source, target, symlinks=True)
    with open(os.path.join(target, PANIC_ROOT_MARKER), "w",
              encoding="utf-8") as handle:
        handle.write("created by inject-faults.sh; safe to delete\n")
    script = os.path.join(target, "net-command.sh")
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(PANIC_SCRIPT)
    os.chmod(script, 0o755)
    return target


def scenario_guest_panic(args):
    """Panic the guest kernel and watch the bridge clean up after it."""
    criteria, notes = [], []
    if args.gate != gu.DEFAULT_GATE:
        # The stand-in gate writes the log it was told to write.
        log_path = os.path.join(args.workdir, args.log_name)
    else:
        log_path = os.path.join(args.workdir, args.panic_log or "panic-gate.log")
    if args.gate != gu.DEFAULT_GATE:
        # Sample mode: the stand-in gate produces a panic in its log, which
        # exercises the criteria without a device.
        attack = Attack(gate_argv(args), tuple(args.watch), args.interval).start()
        attack.wait(args.gate_timeout + args.slack)
        if args.settle > 0:
            time.sleep(args.settle)
    else:
        root = prepare_panic_root(args)
        cmd = gu.build_bridge_command(
            args.workdir, args.assets_dir, dns=args.dns, netroot=root,
            extra=list(args.extra_arg))
        # The bridge writes the guest console to its stdout/stderr; capture it
        # into the same file the other scenarios read.
        attack = Attack(cmd, tuple(args.watch), args.interval)
        attack._sink = []
        attack.start()
        attack.wait(args.deadline)
        if args.settle > 0:
            time.sleep(args.settle)
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(attack.text())

    log = read_log(log_path)
    criteria.append(criterion(
        "c1-panic-observed",
        "the guest log shows a kernel panic",
        bool(log and log["panic-observed"]),
        "panic marker found" if log and log["panic-observed"]
        else "no 'Kernel panic' in %s" % log_path))
    criteria.append(criterion(
        "c2-trigger-reached-kernel",
        "the sysrq trigger reached the kernel (no PANIC_NOT_TRIGGERED)",
        bool(log and not log["panic-not-triggered"]),
        "PANIC_NOT_TRIGGERED present: the guest survived the trigger; "
        "CONFIG_MAGIC_SYSRQ or a read-only /proc/sysrq-trigger may be why"
        if log and log["panic-not-triggered"] else "trigger took effect"))
    criteria.append(criterion(
        "c3-no-clean-guest-exit",
        "a panicked guest does not report a clean command exit",
        bool(log and not log["clean-guest-exit"]),
        "UM_NET_COMMAND_EXIT=0 in the log despite the panic"
        if log and log["clean-guest-exit"] else "no clean-exit marker"))
    # The gate's verdict is meaningless for a bridge run with an injected
    # guest script, so the panic scenario replaces it with the guest facts.
    record = finish_run("guest-panic", args, attack, criteria, notes, log_path,
                        no_false_pass=False)
    record["guest-log"] = read_log(log_path)
    if args.gate == gu.DEFAULT_GATE:
        notes.append("the panic root is left at %s for inspection; remove it "
                     "with rm -rf when finished"
                     % (args.panic_root or
                        os.path.join(args.workdir, "panic-root")))
    return record


def write_record(record, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    stem = "%s-%s" % (record["scenario"],
                      time.strftime("%Y-%m-%dT%H%M%SZ", time.gmtime()))
    path = os.path.join(out_dir, stem + ".json")
    index = 2
    while os.path.exists(path):
        path = os.path.join(out_dir, "%s-%d.json" % (stem, index))
        index += 1
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=False)
        handle.write("\n")
    return path


def print_record(record):
    print("scenario=%s verdict=%s" % (record["scenario"], record["verdict"]))
    for item in record["criteria"]:
        print("  %-26s %-4s %s%s"
              % (item["id"], "ok" if item["ok"] else "FAIL", item["evidence"],
                 "" if item["required"] else "  [informational]"))
    run = record["run"]
    print("  run: exit=%s seconds=%s verdict-line=%s timed-out=%s"
          % (run["exit-code"], run["seconds"], run["verdict-line"],
             run["timed-out"]))
    if record["survivors"]:
        print("  survivors:")
        for proc in record["survivors"]:
            print("    pid %s %s" % (proc["pid"], proc["cmdline"]))


def cmd_run(args):
    problems = gu.preflight(args.workdir, args.assets_dir)
    if args.gate == gu.DEFAULT_GATE and problems:
        # The real scenarios need the real assets; without them the honest
        # answer is SKIP, and nothing is written that looks like a result.
        gu.emit("SKIP", "; ".join(problems), scenario=args.scenario)
        return gu.EXIT_SKIP
    if args.gate != gu.DEFAULT_GATE:
        args.synthetic = True
    handler = {"link-drop": scenario_link_drop,
               "kernel-kill": scenario_kernel_kill,
               "guest-panic": scenario_guest_panic}[args.scenario]
    try:
        record = handler(args)
    except SetupRefused as exc:
        gu.emit("SKIP", str(exc), scenario=args.scenario)
        return gu.EXIT_SKIP
    print_record(record)
    path = write_record(record, args.out)
    print("fault: record written to %s" % path, file=sys.stderr)
    gu.emit(record["verdict"], record["message"], scenario=args.scenario,
            evidence=path)
    return gu.exit_for(record["verdict"])


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
                handle.write("#!/bin/sh\n# fault self-test fixture\n")
            os.chmod(path, 0o755)
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
        print("self-test: %-40s %s%s" % (name, "ok" if ok else "FAIL",
                                         " " + detail if detail else ""))

    root = tempfile.mkdtemp(prefix="fault-selftest-")
    strays = []
    try:
        assets, work, gate = _fixture(root)
        marker = "umfault-selftest"

        def inject(scenario, mode, out_label, extra=(), deadline=20,
                   start_timeout=10, delay=1, assets_dir=None,
                   gate_timeout=30, use_default_gate=False):
            out = os.path.join(root, "out-" + out_label)
            argv = [sys.executable, os.path.abspath(__file__),
                    "--scenario", scenario,
                    "--workdir", work, "--assets-dir", assets_dir or assets,
                    "--gate-arg=--mode", "--gate-arg=" + mode,
                    "--gate-arg=--marker", "--gate-arg=" + marker,
                    "--watch", marker, "--interval", "0.1",
                    "--settle", "0.3", "--delay", str(delay),
                    "--deadline", str(deadline),
                    "--gate-timeout", str(gate_timeout), "--slack", "5",
                    "--start-timeout", str(start_timeout),
                    "--out", out]
            if not use_default_gate:
                argv += ["--gate", gate]
            argv += list(extra)
            result = subprocess.run(argv, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, check=False)
            record = None
            if os.path.isdir(out):
                names = sorted(n for n in os.listdir(out) if n.endswith(".json"))
                if names:
                    with open(os.path.join(out, names[-1]),
                              encoding="utf-8") as handle:
                        record = json.load(handle)
            return result, record

        # 1. link drop: injected mid-run, run ends by itself, nothing survives.
        result, record = inject("link-drop", "children", "linkdrop")
        criteria = {item["id"]: item for item in (record or {}).get("criteria", [])}
        case("link-drop scenario passes on a well-behaved bridge",
             result.returncode == 0 and record and record["verdict"] == "PASS",
             "exit=%s verdict=%s" % (result.returncode,
                                     record and record["verdict"]))
        case("link-drop recorded a real injection",
             record and record["injection"]["injected"] is True
             and record["injection"]["pid"])
        case("link-drop saw the run end by itself",
             criteria.get("c2-run-ended", {}).get("ok") is True)
        case("link-drop refused to call it a pass",
             criteria.get("c3-no-false-pass", {}).get("ok") is True)
        case("link-drop found no survivors",
             criteria.get("c4-no-survivors", {}).get("ok") is True
             and record["survivors"] == [])

        # 2. a bridge that leaks its children must fail the same scenario.
        result, record = inject("link-drop", "children-leak", "linkdrop-leak")
        criteria = {item["id"]: item for item in (record or {}).get("criteria", [])}
        case("link-drop fails when processes survive",
             result.returncode == gu.EXIT_FAIL and record
             and record["verdict"] == "FAIL"
             and criteria.get("c4-no-survivors", {}).get("ok") is False,
             "survivors=%s" % (record and len(record["survivors"])))
        for proc in (record or {}).get("survivors", []):
            strays.append(proc["pid"])
            # Kill them now: the next scenario judges survivors globally, and
            # a fixture leaked by this one would look like its own failure.
            gu.kill_tree(proc["pid"])
        gu.wait_gone(strays, 3)

        # 3. kernel kill: the UML process dies mid-run.
        result, record = inject("kernel-kill", "kernel", "kernelkill")
        criteria = {item["id"]: item for item in (record or {}).get("criteria", [])}
        case("kernel-kill scenario passes",
             result.returncode == 0 and record and record["verdict"] == "PASS",
             "exit=%s verdict=%s" % (result.returncode,
                                     record and record["verdict"]))
        case("kernel-kill killed the UML process",
             record and record["injection"]["target"] == "linux-bionic"
             and record["injection"]["injected"] is True)

        # 4. guest panic: the log shows a panic and the run ends.
        # The sample gate waits out its own timeout before reporting the
        # panic, so keep that timeout short for the offline test.
        result, record = inject("guest-panic", "kernel", "panic",
                                gate_timeout=3)
        criteria = {item["id"]: item for item in (record or {}).get("criteria", [])}
        case("guest-panic scenario passes on a panicking guest",
             result.returncode == 0 and record and record["verdict"] == "PASS",
             "exit=%s verdict=%s" % (result.returncode,
                                     record and record["verdict"]))
        case("guest-panic saw the panic marker",
             record and record["guest-log"]
             and record["guest-log"]["panic-observed"] is True)

        # 5. no injection point: the scenario must not report a pass.
        result, record = inject("link-drop", "pass", "nopasst")
        criteria = {item["id"]: item for item in (record or {}).get("criteria", [])}
        case("a run with no passt process cannot pass the scenario",
             result.returncode == gu.EXIT_FAIL and record
             and criteria.get("c1-injected-live", {}).get("ok") is False,
             "exit=%s" % result.returncode)
        case("no-injection run says nothing was measured",
             record and any("never dropped" in gap
                            for gap in record["not-verified"]))

        # 6. a run that will not end is killed, called FAIL, and cleaned up.
        result, record = inject("link-drop", "hang", "hang", deadline=3,
                                start_timeout=2)
        criteria = {item["id"]: item for item in (record or {}).get("criteria", [])}
        case("a run that never ends fails the scenario",
             result.returncode == gu.EXIT_FAIL and record
             and record["verdict"] == "FAIL")
        case("the killed run is recorded with its process tree",
             record and record["run"]["timed-out"] is True
             and record["run"]["killed-tree"])
        case("nothing survived the kill",
             record and record["survivors"] == [],
             str(record and record["survivors"]))

        # 7. an incomplete environment is SKIP, not a scenario result.
        empty = os.path.join(root, "empty-assets")
        os.makedirs(empty, exist_ok=True)
        result, record = inject("link-drop", "children", "skip",
                                assets_dir=empty, use_default_gate=True)
        case("missing assets are SKIP/77",
             result.returncode == gu.EXIT_SKIP and record is None
             and b"verdict=SKIP" in result.stderr,
             "exit=%s" % result.returncode)
        # 8. the panic-root guard.  This is the one code path in the tool
        #    that deletes a directory, so prove it cannot delete anything it
        #    does not own -- the device assets are not ours to remove.
        netroot = os.path.join(work, "netroot")
        um_init = os.path.join(netroot, "um-init")
        with open(um_init, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n# fixture hostfs init\n")
        os.chmod(um_init, 0o755)

        def panic_args(root):
            return types.SimpleNamespace(workdir=work, panic_root=root)

        def refuses(root):
            try:
                prepare_panic_root(panic_args(root))
            except SetupRefused:
                return True
            return False

        case("refuses --panic-root aimed at the gate's rootfs",
             refuses(netroot))
        case("gate rootfs is untouched after the refusal",
             os.path.isfile(um_init))

        unmarked = os.path.join(root, "unmarked-root")
        os.makedirs(unmarked, exist_ok=True)
        with open(os.path.join(unmarked, "keep-me"), "w",
                  encoding="utf-8") as handle:
            handle.write("not ours\n")
        case("refuses to delete a directory it did not create",
             refuses(unmarked) and os.path.isfile(os.path.join(unmarked,
                                                               "keep-me")))

        copy = os.path.join(root, "panic-copy")
        prepare_panic_root(panic_args(copy))
        case("copies the root, marks it and injects the panic script",
             os.path.isfile(os.path.join(copy, PANIC_ROOT_MARKER))
             and "sysrq-trigger" in open(
                 os.path.join(copy, "net-command.sh"), encoding="utf-8").read()
             and os.path.isfile(um_init))
        prepare_panic_root(panic_args(copy))
        case("re-copies its own marked directory safely",
             os.path.isfile(os.path.join(copy, "net-command.sh")))

    finally:
        for pid in strays:
            gu.kill_tree(pid)
        leftover = [proc.as_dict() for proc in
                    gu.matching(gu.proc_table(), (marker,),
                                exclude=gu.self_tree_pids())]
        for proc in leftover:
            gu.kill_tree(proc["pid"])
        shutil.rmtree(root, ignore_errors=True)

    failed = [name for name, ok, _ in cases if not ok]
    if failed:
        gu.emit("FAIL", "self-test failures: %s" % ", ".join(failed),
                cases=len(cases), failed=len(failed))
        return gu.EXIT_FAIL
    gu.emit("PASS", "fault-injection self-test", cases=len(cases))
    return gu.EXIT_PASS


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=SCENARIOS, default="link-drop")
    parser.add_argument("--workdir", default=gu.DEFAULT_WORKDIR)
    parser.add_argument("--assets-dir", default=gu.DEFAULT_ASSETS_DIR)
    parser.add_argument("--gate", default=gu.DEFAULT_GATE,
                        help="gate to attack (default: the repository gate; "
                             "any other value marks the record synthetic)")
    parser.add_argument("--gate-arg", action="append", default=[],
                        help="extra argument for the gate (use = for options)")
    parser.add_argument("--extra-arg", action="append", default=[],
                        help="extra kernel/bridge argument (guest-panic only)")
    parser.add_argument("--watch", default=",".join(gu.WATCH_TOKENS),
                        help="comma-separated process tokens to watch")
    parser.add_argument("--interval", type=float, default=0.3)
    parser.add_argument("--start-timeout", type=float, default=120,
                        help="seconds to wait for the UML process to appear")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="seconds between the UML process appearing and "
                             "the fault being injected")
    parser.add_argument("--deadline", type=float, default=90,
                        help="seconds the run may take to end after the fault")
    parser.add_argument("--settle", type=float, default=3,
                        help="seconds before judging survivors")
    parser.add_argument("--gate-timeout", type=float, default=300)
    parser.add_argument("--slack", type=float, default=60)
    parser.add_argument("--dns", default="1.1.1.1")
    parser.add_argument("--log-name", default="net-gate.log")
    parser.add_argument("--panic-root", default="",
                        help="scratch hostfs root for the panic scenario "
                             "(default: <workdir>/../um-faults/panic-root, so "
                             "the gate's asset directory is left alone)")
    parser.add_argument("--panic-log", default="",
                        help="where the panic run's console log is written "
                             "(default: <workdir>/../um-faults/panic-gate.log)")
    parser.add_argument("--keep-panic-root", action="store_true")
    parser.add_argument("--out", default=os.path.join(gu.REPO_ROOT,
                                                      "docs/evidence/faults"))
    parser.add_argument("--self-test", action="store_true")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.watch = tuple(token.strip() for token in args.watch.split(",")
                       if token.strip())
    # Keep the panic scenario's scratch root and log outside the gate's work
    # directory: that directory holds assets the gate owns, and a fault tool
    # has no business writing into it.
    faults_dir = os.path.join(os.path.dirname(os.path.abspath(args.workdir)),
                              "um-faults")
    if not args.panic_root:
        args.panic_root = os.path.join(faults_dir, "panic-root")
    if not args.panic_log:
        args.panic_log = os.path.join(faults_dir, "panic-gate.log")
    args.synthetic = False
    args.injection = {}
    if args.self_test:
        return cmd_self_test(args)
    if not args.watch:
        parser.error("--watch needs at least one token")
    if args.delay < 0 or args.deadline <= 0 or args.start_timeout <= 0:
        parser.error("--delay, --deadline and --start-timeout must be sane")
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
