#!/usr/bin/env python3
"""Shared helpers for the Termux-host gates that drive UML + umnet + passt.

`test-umnet-network.sh` is the judge; this module is the plumbing the tools
around it need: running the gate and reading its verdict, hashing the exact
binaries a run used, probing the device, and watching the process table so a
killed or hung run cannot leave a UML behind.

Everything here is deliberately stdlib-only and reads /proc directly.  Termux
does not guarantee procps (no `pgrep`), and the gates must not depend on tools
the phone may not have.

The verdict vocabulary follows the rest of the harness:

    verdict=PASS  exit 0    the thing was exercised and worked
    verdict=FAIL  exit 1    the thing was exercised and did not work
    verdict=SKIP  exit 77   the environment made it impossible to attempt

SKIP is not a soft pass.  A gate that cannot run is a gate that has no result.
"""

import hashlib
import os
import pty
import re
import select
import signal
import subprocess
import sys
import threading
import time

# Exit code convention shared with harness/boot.py and verify-release.py.
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_SKIP = 77

VERDICTS = ("PASS", "FAIL", "SKIP")

PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")

# Layout produced by the other Termux harness scripts; the network gate reads
# the same paths, so the tools here default to them too.
DEFAULT_ASSETS_DIR = os.path.join(PREFIX, "tmp/um-arm64/new")
DEFAULT_WORKDIR = os.path.join(PREFIX, "tmp/um-network")

# The four binaries every network verdict is about.  The first two come from
# the asset directory (built by build-bionic.sh / build-net-bionic.sh), the
# last two live in the network work directory the gate drives.
COMPONENTS = (
    ("linux-bionic", "assets"),
    ("stub_exe", "assets"),
    ("umnet", "workdir"),
    ("passt-bionic", "workdir"),
)

# Process names to watch for leaks.  Matching is a substring test on the full
# command line, so it works for both a bare binary and a wrapped invocation.
WATCH_TOKENS = ("umnet", "passt-bionic", "linux-bionic")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
DEFAULT_GATE = os.path.join(HERE, "test-umnet-network.sh")


class Result(object):
    """One finished gate/bridge run."""

    def __init__(self, exit_code, text, seconds, timed_out=False,
                 killed_tree=None):
        self.exit_code = exit_code
        self.text = text
        self.seconds = seconds
        self.timed_out = timed_out
        # PIDs the caller had to kill after the timeout; empty on a normal end.
        self.killed_tree = list(killed_tree or ())

    @property
    def verdict(self):
        """Map the process exit code onto the shared verdict vocabulary."""
        if self.timed_out:
            return "FAIL"
        if self.exit_code == EXIT_PASS:
            return "PASS"
        if self.exit_code == EXIT_SKIP:
            return "SKIP"
        return "FAIL"


def exit_for(verdict):
    return {"PASS": EXIT_PASS, "FAIL": EXIT_FAIL, "SKIP": EXIT_SKIP}[verdict]


def repo_relative(path):
    """The path as a reviewer sees it, when it is inside the repository.

    Evidence files name their inputs the way the repository names them; a
    Termux path is recorded verbatim because it exists only on the phone.
    """
    try:
        rel = os.path.relpath(path, REPO_ROOT)
    except ValueError:
        return path
    return rel if not rel.startswith("..") else path


def emit(verdict, message="", **fields):
    """Print one machine-readable result line, in the harness's format."""
    parts = ["verdict=" + verdict]
    if message:
        parts.append("message=" + message.replace("\n", " "))
    for key in sorted(fields):
        parts.append("%s=%s" % (key, fields[key]))
    print(" ".join(parts), file=sys.stderr)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def file_record(path):
    """Hash/size a file, or return None when it is not a readable file.

    None is information, not an error: the evidence writer records "this
    component was missing" rather than inventing a hash for it.
    """
    try:
        info = os.stat(path)
    except OSError:
        return None
    if not os.path.isfile(path):
        return None
    try:
        digest = sha256_file(path)
    except OSError:
        return None
    return {"path": path, "sha256": digest, "size": info.st_size,
            "executable": os.access(path, os.X_OK)}


def kernel_banner(path):
    """Pull `Linux version ...` out of a UML kernel binary, if it is one.

    The banner is how the evidence file ties a hash to an actual kernel
    version; a hash alone says nothing about what was measured.
    """
    try:
        with open(path, "rb") as handle:
            blob = handle.read()
    except OSError:
        return None
    marker = b"Linux version "
    start = blob.find(marker)
    if start < 0:
        return None
    end = blob.find(b"\x00", start)
    if end < 0:
        end = min(len(blob), start + 200)
    return blob[start:end].decode("utf-8", "replace").strip()


def tracer_pid():
    """Nonzero when this process is traced: the host gate cannot run there.

    UML under a proot/neoproot tracer is not the configuration this harness
    judges, and booting one inside a traced container is forbidden outright.
    """
    try:
        with open("/proc/self/status", encoding="ascii") as status_file:
            for line in status_file:
                if line.startswith("TracerPid:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return -1
    return -1


def getprop(name):
    try:
        out = subprocess.run(["getprop", name], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=5,
                             check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.decode("utf-8", "replace").strip()
    return value or None


def device_info():
    """Describe the machine a verdict came from.

    A verdict without the device is not reproducible evidence: the same
    binaries behave differently across Android releases and kernels.
    """
    uname = os.uname()
    info = {
        "host-role": "termux-host",
        "uname": " ".join(uname),
        "kernel-release": uname.release,
        "machine": uname.machine,
        "termux-prefix": PREFIX,
        "python": sys.version.split()[0],
        "tracer-pid": tracer_pid(),
    }
    props = (
        ("ro.product.manufacturer", "device-manufacturer"),
        ("ro.product.model", "device-model"),
        ("ro.build.version.release", "android-release"),
        ("ro.build.version.sdk", "android-sdk"),
        ("ro.product.cpu.abi", "android-abi"),
        ("ro.build.fingerprint", "android-fingerprint"),
    )
    for prop, key in props:
        info[key] = getprop(prop)
    if not any(info[key] for _, key in props):
        # No getprop is normal on a non-Android host and worth saying out loud
        # rather than recording empty strings that look like a failed probe.
        info["device-probe"] = "getprop unavailable"
    return info


def git_state(scopes=("tools/um-arm64/harness", "harness")):
    """Record the commit the gate ran from, plus whether it was modified.

    A verdict about binaries built from uncommitted source is still useful,
    but only if the record says so.  Path-limited diffs keep this fast on a
    kernel-sized tree (a full `git status` takes minutes on a phone).
    """
    def run(argv):
        try:
            out = subprocess.run(argv, cwd=REPO_ROOT, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=20,
                                 check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0:
            return None
        return out.stdout.decode("utf-8", "replace").strip()

    state = {"repo": REPO_ROOT}
    head = run(["git", "rev-parse", "HEAD"])
    if head is None:
        state["git"] = "unavailable"
        return state
    state["commit"] = head
    state["branch"] = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    # Scoped on purpose: see the docstring.  Recorded so the reader knows the
    # dirty flag does not cover the whole tree.
    state["dirty-scope"] = list(scopes)
    dirty = False
    for scope in scopes:
        if run(["git", "status", "--porcelain", "--untracked-files=no",
                "--", scope]):
            dirty = True
            break
    state["dirty"] = dirty
    return state


# ---------------------------------------------------------------------------
# process table
# ---------------------------------------------------------------------------

class Proc(object):
    __slots__ = ("pid", "ppid", "sid", "state", "comm", "argv", "starttime",
                 "rss_kb")

    def __init__(self, pid, ppid, sid, state, comm, argv, starttime, rss_pages):
        self.pid = pid
        self.ppid = ppid
        self.sid = sid
        self.state = state
        self.comm = comm
        self.argv = argv
        self.starttime = starttime
        self.rss_kb = rss_pages * (os.sysconf("SC_PAGE_SIZE") // 1024)

    @property
    def cmdline(self):
        return " ".join(self.argv) if self.argv else "[%s]" % self.comm

    @property
    def is_zombie(self):
        """A zombie holds no resources; counting it as a leak would be wrong."""
        return self.state == "Z"

    def as_dict(self):
        return {"pid": self.pid, "ppid": self.ppid, "comm": self.comm,
                "cmdline": self.cmdline, "starttime": self.starttime,
                "rss-kb": self.rss_kb}

    def __repr__(self):
        return "Proc(%d, %r)" % (self.pid, self.cmdline)


def proc_table():
    """Read /proc into {pid: Proc}, skipping entries that vanish mid-read."""
    table = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return table
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open("/proc/%d/stat" % pid, "rb") as handle:
                stat = handle.read().decode("utf-8", "replace")
            # comm is parenthesised and may itself contain spaces or parens,
            # so split on the *last* ')' instead of tokenising from the left.
            left = stat.find("(")
            right = stat.rfind(")")
            if left < 0 or right < 0:
                continue
            comm = stat[left + 1:right]
            fields = stat[right + 2:].split()
            state = fields[0]
            ppid = int(fields[1])
            sid = int(fields[3])
            starttime = int(fields[19])
            rss_pages = int(fields[21])
            with open("/proc/%d/cmdline" % pid, "rb") as handle:
                raw = handle.read()
            argv = [part.decode("utf-8", "replace")
                    for part in raw.split(b"\x00") if part]
        except (OSError, ValueError, IndexError):
            continue
        table[pid] = Proc(pid, ppid, sid, state, comm, argv, starttime,
                          rss_pages)
    return table


def descendants(table, root):
    """Every live descendant of root, children first (safe kill order)."""
    children = {}
    for proc in table.values():
        children.setdefault(proc.ppid, []).append(proc.pid)
    out = []
    stack = list(children.get(root, ()))
    seen = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        proc = table.get(pid)
        if proc is not None:
            out.append(proc)
        stack.extend(children.get(pid, ()))
    return out


def matching(table, tokens=WATCH_TOKENS, pids=None, exclude=()):
    """Processes whose command line mentions one of the tokens.

    `pids` restricts the search to a known set (for example the descendants of
    one gate run).  `exclude` drops PIDs that merely *talk* about the tokens --
    the tool's own command line contains the word "umnet", and reporting the
    watcher as a leak would make every leak check useless.
    """
    exclude = set(exclude)
    out = []
    for proc in table.values():
        if proc.pid in exclude:
            continue
        if pids is not None and proc.pid not in pids:
            continue
        haystack = proc.cmdline
        if any(token in haystack or token == proc.comm for token in tokens):
            out.append(proc)
    return out


def self_tree_pids(table=None):
    """This process and its ancestors: never a leak, however it is named."""
    table = proc_table() if table is None else table
    out = []
    pid = os.getpid()
    while pid and pid in table:
        out.append(pid)
        pid = table[pid].ppid
    return out


def count_tokens(table, tokens=WATCH_TOKENS, pids=None, exclude=()):
    """How many live processes carry each token right now."""
    return {token: len(matching(table, (token,), pids=pids, exclude=exclude))
            for token in tokens}


class Sampler(object):
    """Sample the process table while a gate runs.

    Sampling *during* the run is the only way to know which processes belonged
    to it: once the gate exits, its children are reparented and the tree is
    lost.  The recorded PIDs are then checked after teardown, and anything
    that appeared after the run started is a candidate leak.
    """

    def __init__(self, root_pid, tokens=WATCH_TOKENS, interval=0.5,
                 marker=None):
        self.root_pid = root_pid
        self.tokens = tuple(tokens)
        self.interval = interval
        # A soak wants one marker for the whole run, so a process leaked by
        # round 1 is still attributed to the run when round 5 starts.
        self.marker = watch_starttime() if marker is None else marker
        self.seen = {}
        self._peaks = {token: 0 for token in self.tokens}
        self._stop = threading.Event()
        self._thread = None
        self.samples = 0

    def _tick(self):
        table = proc_table()
        mine = {proc.pid for proc in descendants(table, self.root_pid)}
        exclude = self_tree_pids(table)
        hits = matching(table, self.tokens, pids=mine, exclude=exclude)
        # A child that called setsid() leaves the tree; anything that started
        # after this run began is still ours to account for.
        detached = [proc for proc in matching(table, self.tokens,
                                              exclude=exclude)
                    if proc.starttime >= self.marker]
        for proc in hits + detached:
            self.seen.setdefault(proc.pid, proc)
        live = count_tokens(table, self.tokens, exclude=exclude)
        for token, value in live.items():
            self._peaks[token] = max(self._peaks[token], value)
        self.samples += 1

    def start(self):
        def loop():
            while not self._stop.is_set():
                try:
                    self._tick()
                except Exception:  # a sampler must not kill the run it watches
                    pass
                self._stop.wait(self.interval)
        self._thread = threading.Thread(target=loop)
        self._thread.daemon = True
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        return self

    def summary(self):
        return {
            "samples": self.samples,
            "peaks": dict(self._peaks),
            "seen": [proc.as_dict() for proc in self.seen.values()],
        }

    def leaks(self, table=None):
        """Processes seen during the run that are still alive now."""
        table = proc_table() if table is None else table
        out = []
        for pid, proc in self.seen.items():
            live = table.get(pid)
            if live is not None and not live.is_zombie:
                out.append(live)
        return out


def kill_tree(pid, sig=signal.SIGKILL, table=None):
    """Kill a process and its descendants; returns the PIDs that were signalled.

    A gate normally cleans up after itself, but a gate that was killed or that
    hung never gets to run its own `finally` block.  Whoever killed it owns the
    survivors, and a leftover UML on a phone is not acceptable.
    """
    table = proc_table() if table is None else table
    victims = descendants(table, pid)
    victims.append(table.get(pid) or Proc(pid, 0, 0, "?", "?", [], 0, 0))
    signalled = []
    for proc in victims:
        try:
            os.kill(proc.pid, sig)
            signalled.append(proc.pid)
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return signalled


def alive_pids(pids):
    """PIDs that still exist and are not zombies."""
    table = proc_table()
    out = []
    for pid in pids:
        proc = table.get(pid)
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            continue
        if proc is None or not proc.is_zombie:
            out.append(pid)
    return out


def wait_gone(pids, timeout):
    """Wait until every PID is gone; returns the ones still alive."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = alive_pids(pids)
        if not alive:
            return []
        time.sleep(0.2)
    return alive_pids(pids)


def meminfo():
    """MemAvailable in kB, or None when /proc/meminfo is unreadable."""
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


# ---------------------------------------------------------------------------
# gate output
# ---------------------------------------------------------------------------

_CHECK_RE = re.compile(r"^gate:[ \t]+(\S+)[ \t]+(ok|MISSING)[ \t]*$", re.M)
_VERDICT_RE = re.compile(r"^verdict=(PASS|FAIL|SKIP)[ \t]*$", re.M)
_TEARDOWN_RE = re.compile(r"^gate: teardown: (.+)$", re.M)
_RTT_RE = re.compile(
    r"^gate: icmp rtt ms: min=([\d.]+) avg=([\d.]+) max=([\d.]+) \(n=(\d+)\)",
    re.M)
_RATES_RE = re.compile(r"^gate: guest wget rates: (.+)$", re.M)
_LOG_RE = re.compile(r"^log=(.+)$", re.M)


def parse_gate_report(text):
    """Turn the gate's stderr into the facts a verdict is made of.

    The gate prints one `gate: <check> ok|MISSING` line per check and one
    `verdict=` line; a missing verdict line means the gate never judged
    anything, which is different from judging it wrong.
    """
    report = {
        "checks": [(name, status == "ok")
                   for name, status in _CHECK_RE.findall(text)],
        "verdict": None,
        "teardown": None,
        "icmp-rtt-ms": None,
        "wget-rates": [],
        "guest-log": None,
        "clean-shutdown": "deadline expired" not in text,
    }
    verdict = _VERDICT_RE.search(text)
    if verdict:
        report["verdict"] = verdict.group(1)
    teardown = _TEARDOWN_RE.search(text)
    if teardown:
        report["teardown"] = teardown.group(1).strip()
    rtt = _RTT_RE.search(text)
    if rtt:
        report["icmp-rtt-ms"] = {
            "min": float(rtt.group(1)), "avg": float(rtt.group(2)),
            "max": float(rtt.group(3)), "n": int(rtt.group(4)),
        }
    rates = _RATES_RE.search(text)
    if rates:
        report["wget-rates"] = [v.strip()
                                for v in rates.group(1).split(",") if v.strip()]
    log = _LOG_RE.search(text)
    if log:
        report["guest-log"] = log.group(1).strip()
    return report


def gate_command(gate, argv=()):
    """Build the argv that runs a gate script.

    The gates are executable python files with a `.sh` name (an old convention
    of this harness); when the executable bit is missing -- a checkout on a
    filesystem that dropped it -- fall back to `python3 <script>`.
    """
    argv = list(argv)
    if os.access(gate, os.X_OK):
        return [gate] + argv
    return [sys.executable, gate] + argv


def pump_stream(stream, sink, echo=False):
    """Read a pipe into a list of decoded chunks, optionally echoing it.

    Public because tools that keep a child running in order to attack it need
    the same non-blocking capture that run_process() uses internally.
    """
    for chunk in iter(lambda: stream.read(4096), b""):
        text = chunk.decode("utf-8", "replace")
        sink.append(text)
        if echo:
            sys.stderr.write(text)
            sys.stderr.flush()


def run_process(argv, timeout, echo=False, new_session=True, on_spawn=None):
    """Run a command, capture its stderr, and never leave it running.

    Returns a Result.  On timeout the whole process tree is killed: a gate
    that ignores its own deadline must not be allowed to keep a UML instance
    (and a passt) alive on the phone.

    `on_spawn` is called with the child PID right after it starts, which is
    the only moment at which a watcher can attach to the process tree it is
    supposed to observe.
    """
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, start_new_session=new_session)
    except OSError as exc:
        text = "gate: cannot execute %s: %s" % (argv[0], exc)
        return Result(127, text, 0.0)
    if on_spawn is not None:
        try:
            on_spawn(proc.pid)
        except Exception:  # a broken watcher must not break the run
            pass

    sink = []
    reader = threading.Thread(target=pump_stream,
                                  args=(proc.stderr, sink, echo))
    reader.daemon = True
    reader.start()

    timed_out = False
    killed = []
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        killed = kill_tree(proc.pid)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
    reader.join(timeout=10)
    try:
        proc.stderr.close()
    except OSError:
        pass
    return Result(proc.returncode, "".join(sink), time.monotonic() - started,
                  timed_out=timed_out, killed_tree=killed)


def watch_starttime():
    """A monotonic marker in the same clock as /proc starttime.

    Processes started after this value are the ones this run owns; anything
    older was already on the phone.
    """
    now = proc_table()
    return max((p.starttime for p in now.values()), default=0)


# ---------------------------------------------------------------------------
# bare bridge runs
# ---------------------------------------------------------------------------

def build_bridge_command(workdir, assets_dir, dns="1.1.1.1", netroot=None,
                         extra=(), ipv6=False, ipv6_addr="fd00::2/64",
                         ipv6_gw="fe80::1",
                         ipv6_dns="2001:4860:4860::8888"):
    """The umnet + UML command line the network gate boots.

    Kept in sync with `run_gate()` in test-umnet-network.sh.  Scenarios that
    must supply their own guest script (a guest-side kernel panic, say) cannot
    go through the gate, because the gate rewrites `net-command.sh` on every
    run; they drive the bridge directly with the identical arguments.

    `ipv6=True` asks umnet for the second, IPv6-only passt; the guest script
    the caller writes then has to configure the address itself (the gate's
    `--ipv6` mode writes one, but a custom script has to do the same).
    """
    umnet = os.path.join(workdir, "umnet")
    passt = os.path.join(workdir, "passt-bionic")
    kernel = os.path.join(assets_dir, "linux-bionic")
    stub = os.path.join(assets_dir, "stub_exe")
    root = netroot or os.path.join(workdir, "netroot")
    cmd = [umnet, "--passt", passt]
    if ipv6:
        cmd += ["--ipv6", "--ipv6-addr", ipv6_addr, "--ipv6-gw", ipv6_gw]
        if ipv6_dns != "none":
            cmd += ["--ipv6-dns", ipv6_dns]
    return cmd + ["--dns", dns, "--", kernel,
                  "mem=512M", "rw", "init=/um-init", "con=null", "con0=fd:0,fd:1",
                  "stub_exe=" + stub, "rootfstype=hostfs", "rootflags=" + root,
                  "panic=1"] + list(extra)


def run_on_pty(cmd, timeout):
    """Run cmd with a pty as its controlling terminal, bounded in time.

    UML with `con0=fd:0,fd:1` hangs before userspace on a plain pipe, so the
    bridge needs a pty exactly as the gate gives it one.  Returns
    (completed, text): completed is False when the deadline expired and the
    process group had to be killed.
    """
    log = bytearray()
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - child
        try:
            os.execv(cmd[0], cmd)
        except OSError as exc:
            sys.stderr.write("%s\n" % exc)
            sys.stderr.flush()
            os._exit(127)

    completed = False
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select(
                [fd], [], [], min(0.2, max(0.0, deadline - time.monotonic())))
            if not ready:
                child, _ = os.waitpid(pid, os.WNOHANG)
                if child:
                    completed = True
                    break
                continue
            try:
                data = os.read(fd, 65536)
            except OSError as exc:
                if exc.errno == 5:  # EIO: the slave side closed
                    completed = True
                    break
                raise
            if not data:
                completed = True
                break
            log.extend(data)
        else:
            completed = False
    finally:
        # pty.fork puts the child in its own session; kill only that group.
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        os.close(fd)
    return completed, bytes(log)


def preflight(workdir, assets_dir):
    """Why the gate cannot run here, as a list of human-readable reasons.

    An empty list means the environment is complete.  This is what turns
    "missing assets" into SKIP instead of a failed run that proves nothing.
    """
    problems = []
    for name, where in COMPONENTS:
        path = os.path.join(assets_dir if where == "assets" else workdir, name)
        if not os.path.isfile(path):
            problems.append("%s missing: %s" % (name, path))
        elif not os.access(path, os.X_OK):
            problems.append("%s is not executable: %s" % (name, path))
    netroot = os.path.join(workdir, "netroot")
    if not os.path.isdir(netroot):
        problems.append("hostfs root missing: %s" % netroot)
    return problems


# ---------------------------------------------------------------------------
# sample gate (offline self-test only)
# ---------------------------------------------------------------------------

SAMPLE_GATE = r'''#!/usr/bin/env python3
"""A stand-in for test-umnet-network.sh, for offline self-tests only.

It speaks the gate's protocol (`gate: <check> ok|MISSING`, `verdict=...`,
`log=...`) and can be told to fail, to flake, to leak a child process or to
hang, so the tooling around the real gate can be tested without a phone.
"""
import argparse
import os
import subprocess
import sys
import time

CHECKS = ["dhcp", "addr", "route", "dns-config", "icmp", "dns-lookup",
          "tcp", "concurrent", "clean-exit"]

GOOD_LOG = """NET_START
1: vec0: <BROADCAST,MULTICAST,UP> mtu 1500
NET_VEC_UP
NET_DHCP
udhcpc: lease of 10.0.2.15 obtained from 10.0.2.2, lease time 86400
    inet 10.0.2.15/24 scope global vec0
    default via 10.0.2.2 dev vec0
nameserver 1.1.1.1
NET_PING_GW
2 packets transmitted, 2 packets received, 0% packet loss
time=1.2 ms
NET_DNS
Name:      example.com
Address:   93.184.216.34
NET_TCP
<!doctype html><html><head><title>Example Domain</title>
NET_TCP_DONE
NET_CONCURRENT
NET_CONCURRENT_RC=0
NET_LATENCY
NET_DONE
umnet: passt exited
UM_NET_COMMAND_EXIT=0
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--log", default="net-gate.log")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--dns", default="1.1.1.1")
    parser.add_argument("--mode", default="pass")
    parser.add_argument("--marker", default="sample-gate-child")
    parser.add_argument("--counter", default="")
    args = parser.parse_args()

    def fake(token):
        """A stand-in process that keeps `token` in its command line.

        `sh -c 'sleep 300' token` does not work here: dash execs the single
        command, so the process becomes a bare `sleep 300` and the token --
        which is the whole point of the fixture -- disappears.
        """
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)", token],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    log_path = os.path.join(args.workdir, args.log)
    mode = args.mode
    stray = []
    if mode == "flake" and args.counter:
        # Deterministic flake: round 2 of a soak fails, everything else passes.
        try:
            with open(args.counter) as handle:
                seen = int(handle.read().strip() or "0")
        except (OSError, ValueError):
            seen = 0
        seen += 1
        with open(args.counter, "w") as handle:
            handle.write(str(seen))
        mode = "fail" if seen % 2 == 0 else "pass"
    if mode == "leak":
        stray.append(fake(args.marker))
    if mode in ("children", "children-leak", "kernel", "kernel-leak"):
        # A fake process tree shaped like a real run: umnet -> passt + UML.
        # The `-leak` variants keep the tree alive after the fault, which is
        # how the injector's "nothing survived" criterion gets a failing case
        # to prove it can fail.
        stray.append(fake(args.marker + "-umnet"))
        if mode.startswith("children"):
            stray.append(fake(args.marker + "-passt-bionic"))
        stray.append(fake(args.marker + "-linux-bionic"))
    if mode == "hang":
        stray.append(fake(args.marker + "-hang"))

    sys.stderr.write("gate: running on pty, timeout=%ss, announced dns=%s\n"
                     % (args.timeout, args.dns))
    if mode == "skip":
        sys.stderr.write("gate: not an executable file: %s/umnet\n" % args.workdir)
        sys.stderr.write("verdict=SKIP\n")
        return 77
    if mode == "hang":
        time.sleep(600)
        return 1
    if mode.startswith("children") or mode.startswith("kernel"):
        # Wait for the injected fault, then report the link going away (or the
        # guest dying).  The `-leak` variants leave the rest of the tree alive,
        # which is what the injector's survivor criterion must catch.
        target = stray[1] if mode.startswith("children") else stray[-1]
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            if target.poll() is not None:
                break
            time.sleep(0.2)
        if mode.startswith("children"):
            text = GOOD_LOG.replace("UM_NET_COMMAND_EXIT=0", "UM_NET_COMMAND_EXIT=1")
            text += "umnet: passt killed while linked\n"
            missing = ("dns-lookup", "tcp", "concurrent", "clean-exit")
            teardown = "umnet: passt killed while linked"
        else:
            text = GOOD_LOG.replace("UM_NET_COMMAND_EXIT=0", "")
            text += "Kernel panic - not syncing: injected\n"
            missing = ("tcp", "concurrent", "clean-exit")
            teardown = "umnet: passt exited"
        if not mode.endswith("-leak"):
            # A well-behaved bridge reaps what is left of the run.
            for proc in stray:
                if proc.poll() is None:
                    proc.kill()
        with open(log_path, "w") as handle:
            handle.write(text)
        for name in CHECKS:
            status = "MISSING" if name in missing else "ok"
            sys.stderr.write("gate: %-12s %s\n" % (name, status))
        sys.stderr.write("gate: teardown: %s\n" % teardown)
        sys.stderr.write("verdict=FAIL\n")
        sys.stderr.write("log=%s\n" % log_path)
        return 1

    text = GOOD_LOG
    if mode == "fail":
        text = GOOD_LOG.replace("Example Domain", "connection timed out")
        text = text.replace("UM_NET_COMMAND_EXIT=0", "UM_NET_COMMAND_EXIT=1")
    with open(log_path, "w") as handle:
        handle.write(text)
    for name in CHECKS:
        bad = mode == "fail" and name in ("tcp", "clean-exit")
        sys.stderr.write("gate: %-12s %s\n" % (name, "MISSING" if bad else "ok"))
    sys.stderr.write("gate: icmp rtt ms: min=1.2 avg=1.4 max=1.9 (n=5)\n")
    sys.stderr.write("gate: guest wget rates: 1.2 MB/s, 900 KB/s\n")
    sys.stderr.write("gate: teardown: umnet: passt exited\n")
    if mode != "noverdict":
        # `noverdict` models a gate that dies before judging anything: checks
        # are printed, no verdict line is, and the exit code is nonzero.  The
        # recorder must call that FAIL, never PASS and never SKIP.
        sys.stderr.write("verdict=%s\n" % ("FAIL" if mode == "fail" else "PASS"))
    sys.stderr.write("log=%s\n" % log_path)
    if mode == "noverdict":
        return 1
    return 1 if mode == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
'''


def write_sample_gate(path, mode=None):
    """Write the sample gate script; `mode` is only a convenience default."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(SAMPLE_GATE)
    os.chmod(path, 0o755)
    return path
