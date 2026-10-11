#!/usr/bin/env python3
"""End-to-end network gate for the UML vector fd + umnet + passt path.

Run from the Termux host, never inside a traced neoproot container.  The gate
boots the UML kernel through ``umnet`` with a passt-provided userspace stack
and checks what the guest can actually do with the network it was given:
DHCP, DNS, IPv4 TCP, concurrency, ICMP, and clean teardown.

Layout expected under ``--workdir`` (default ``$PREFIX/tmp/um-network``):

    passt-bionic        static bionic passt
    umnet               framing bridge (harness/umnet.c)
    netroot/            Alpine hostfs root (net-command.sh is rewritten here)

The kernel and stub are read from ``$PREFIX/tmp/um-arm64/new`` and are prepared
by the other harness scripts; this one only drives them and judges the result.

UML needs a controlling terminal: with ``con0=fd:0,fd:1`` on a plain pipe the
guest hangs before userspace.  The gate therefore runs the bridge on a pty,
exactly as ``boot.py`` does.
"""
import argparse
import errno
import os
import pty
import re
import select
import signal
import sys
import time

PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")

# The guest command script.  Written by this gate into the hostfs root so the
# measured behaviour lives in one place rather than in an ad-hoc shell snippet.
GUEST_COMMAND = """#!/bin/sh
set +e
bb=/bin/busybox
echo NET_START
$bb ip link show 2>&1
$bb ip link set vec0 up 2>&1
echo NET_VEC_UP

echo NET_DHCP
$bb udhcpc -n -q -t 3 -T 1 -i vec0 2>&1
$bb ip addr show vec0 2>&1
$bb ip route 2>&1
$bb cat /etc/resolv.conf 2>&1

echo NET_PING_GW
$bb ping -c 2 -W 3 10.0.2.2 2>&1
echo NET_PING_DONE

echo NET_DNS
$bb nslookup example.com 2>&1

echo NET_TCP
$bb wget -T 8 -O - http://example.com/ 2>&1 | $bb head -3
echo NET_TCP_DONE

echo NET_CONCURRENT
for i in 1 2 3 4 5 6; do
    $bb wget -T 8 -q -O /dev/null http://example.com/ &
done
wait
echo NET_CONCURRENT_RC=$?

echo NET_LATENCY
for i in 1 2 3 4 5; do
    $bb ping -c 1 -W 2 10.0.2.2 2>&1 | $bb grep -o 'time=[0-9.]*'
done

echo NET_DONE
exit 0
"""


def fail(message):
    print("gate: " + message, file=sys.stderr)
    raise SystemExit(1)


def write_guest_command(netroot):
    script = os.path.join(netroot, "net-command.sh")
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(GUEST_COMMAND)
    os.chmod(script, 0o755)
    return script


def run_on_pty(cmd, timeout):
    """Run cmd with a pty as its controlling terminal, bounded in time.

    Returns (completed, captured_bytes).  completed is False when the deadline
    expired and the process group had to be killed.
    """
    log = bytearray()
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execv(cmd[0], cmd)
        except OSError as exc:
            print(exc, file=sys.stderr, flush=True)
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
                if exc.errno == errno.EIO:
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
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        os.close(fd)
    return completed, bytes(log)


def run_gate(args):
    umnet = os.path.join(args.workdir, "umnet")
    passt = os.path.join(args.workdir, "passt-bionic")
    netroot = os.path.join(args.workdir, "netroot")
    kernel = os.path.join(PREFIX, "tmp/um-arm64/new/linux-bionic")
    stub = os.path.join(PREFIX, "tmp/um-arm64/new/stub_exe")

    for path in (umnet, passt, kernel, stub):
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            fail("not an executable file: " + path)
    if not os.path.isdir(netroot):
        fail("missing hostfs root: " + netroot)

    write_guest_command(netroot)
    log_path = os.path.join(args.workdir, args.log)
    cmd = [umnet, "--passt", passt, "--dns", args.dns, "--", kernel,
           "mem=512M", "rw", "init=/um-init", "con=null", "con0=fd:0,fd:1",
           "stub_exe=" + stub, "rootfstype=hostfs", "rootflags=" + netroot,
           "panic=1"]
    print("gate: running on pty, timeout=%ss, announced dns=%s"
          % (args.timeout, args.dns), file=sys.stderr)

    completed, raw = run_on_pty(cmd, args.timeout)
    text = raw.decode("utf-8", errors="replace").replace("\r", "")
    with open(log_path, "w", encoding="utf-8") as handle:
        handle.write(text)

    checks = [
        ("dhcp", r"lease of 10\.0\.2\.15 obtained from 10\.0\.2\.2"),
        ("addr", r"inet 10\.0\.2\.15/24"),
        ("route", r"default via 10\.0\.2\.2 dev vec0"),
        # Any announced resolver proves the handshake; asserting a specific
        # address would fail on a network that blocks that one and say nothing
        # about the build.
        ("dns-config", r"nameserver \d+\.\d+\.\d+\.\d+"),
        ("icmp", r"2 packets transmitted, 2 packets received"),
        ("dns-lookup", r"Name:\s*example\.com\s*\nAddress:\s*\d+\.\d+\.\d+\.\d+"),
        ("tcp", r"Example Domain"),
        ("concurrent", r"NET_CONCURRENT_RC=0"),
        ("clean-exit", r"UM_NET_COMMAND_EXIT=0"),
    ]
    results = [(name, re.search(pattern, text) is not None)
               for name, pattern in checks]
    for name, passed in results:
        print("gate: %-12s %s" % (name, "ok" if passed else "MISSING"),
              file=sys.stderr)

    # Informational: guest-observed latency and transfer rate.
    latencies = re.findall(r"time=([0-9.]+) ms", text)
    if latencies:
        values = [float(v) for v in latencies]
        print("gate: icmp rtt ms: min=%.1f avg=%.1f max=%.1f (n=%d)" % (
            min(values), sum(values) / len(values), max(values), len(values)),
            file=sys.stderr)
    rates = re.findall(r"\(([0-9.]+) [KMG]B/s\)", text)
    if rates:
        print("gate: guest wget rates: " + ", ".join(rates), file=sys.stderr)

    teardown = re.search(r"umnet: passt (exited|killed[^\n]*)", text)
    print("gate: teardown: " + (teardown.group(0) if teardown
                                else "no passt teardown line"), file=sys.stderr)

    passed = completed and all(ok for _, ok in results)
    if not completed:
        print("gate: deadline expired before clean shutdown", file=sys.stderr)
    print("verdict=" + ("PASS" if passed else "FAIL"), file=sys.stderr)
    print("log=" + log_path, file=sys.stderr)
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workdir",
                        default=os.path.join(PREFIX, "tmp/um-network"))
    parser.add_argument("--log", default="net-gate.log")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--check-host-only", action="store_true",
                        help="verify the host is untraced and assets exist")
    parser.add_argument("--dns", default="1.1.1.1",
                        help="nameserver passt announces to the guest (default: "
                             "1.1.1.1).  Some networks block plain DNS to "
                             "particular resolvers, which breaks the guest's "
                             "lookups without anything being wrong with the "
                             "build; pass a resolver this network reaches.")
    args = parser.parse_args()

    if args.timeout <= 0:
        parser.error("timeout must be positive")
    with open("/proc/self/status", encoding="ascii") as status_file:
        for line in status_file:
            if line.startswith("TracerPid:") and int(line.split()[1]):
                fail("run on the Termux host, not inside a traced container")

    if args.check_host_only:
        for name in ("umnet", "passt-bionic"):
            path = os.path.join(args.workdir, name)
            print("%-14s %s" % (name, "present" if os.path.isfile(path)
                                else "MISSING"))
        return 0

    return run_gate(args)


if __name__ == "__main__":
    sys.exit(main())
