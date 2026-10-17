#!/usr/bin/env python3
"""End-to-end network gate for the UML vector fd + umnet + passt path.

Run from the Termux host, never inside a traced neoproot container.  The gate
boots the UML kernel through ``umnet`` with a passt-provided userspace stack
and checks what the guest can actually do with the network it was given:
DHCP, DNS, IPv4 TCP, concurrency, ICMP, and clean teardown.  With --ipv6
the guest also gets an IPv6 address on vec0, served by a second, IPv6-only
passt; the same run then adds address, route, ping6, AAAA and IPv6 TCP checks
(see the IPv6 section in tools/um-arm64/doc/90-networking.md).

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
import shlex
import signal
import socket
import sys
import time

PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")

# Exit code convention shared with harness/boot.py and verify-release.py: 77
# means "could not be attempted", never "good enough".
EXIT_SKIP = 77

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
# The resolver announced over DHCP is reached over the phone's own UDP path,
# which is lossy: measured on this network, ~10% of plain queries time out.
# A lost answer says nothing about the bridge, so ask up to eight times.  Only
# an IPv4 answer ends the loop: the AAAA one may arrive first and is not what
# the TCP check below uses.
dns_attempt=0
while [ "$dns_attempt" -lt 8 ]; do
    dns_attempt=$((dns_attempt + 1))
    dns_answer=$($bb nslookup example.com 2>&1)
    printf '%s\n' "$dns_answer"
    printf '%s\n' "$dns_answer" | $bb grep -qE 'Address: [0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+' && break
    $bb sleep 1
done

echo NET_TCP
# -T 30, not -T 8: this check is about frames flowing, not about how fast the
# public network happens to be. 8 s was tight on a slow mobile link and failed
# rounds for a reason that said nothing about the bridge.  The whole fetch is
# retried because wget resolves the name itself: one lost DNS answer otherwise
# fails the round.
tcp_attempt=0
while [ "$tcp_attempt" -lt 3 ]; do
    tcp_attempt=$((tcp_attempt + 1))
    tcp_body=$($bb wget -T 30 -O - http://example.com/ 2>&1 | $bb head -3)
    printf '%s\n' "$tcp_body"
    printf '%s\n' "$tcp_body" | $bb grep -q 'Example Domain' && break
    $bb sleep 1
done
echo NET_TCP_DONE

echo NET_CONCURRENT
for i in 1 2 3 4 5 6; do
    $bb wget -T 30 -q -O /dev/null http://example.com/ &
done
wait
echo NET_CONCURRENT_RC=$?

echo NET_LATENCY
for i in 1 2 3 4 5; do
    $bb ping -c 1 -W 2 10.0.2.2 2>&1 | $bb grep -o 'time=[0-9.]*'
done
__IPV6_SECTION__
echo NET_DONE
exit 0
"""

# The IPv6 half of the guest command, written only with --ipv6.  It is a
# separate section because the gate's IPv6 mode asks for a second passt and a
# guest that has both stacks; the IPv4 checks above are unchanged.
#
# The address is configured statically instead of left to SLAAC: the gate has
# to look for a known address, and passt also serves DHCPv6/RA for guests that
# prefer them (the handler is passt's, not this script's).
GUEST_IPV6_SECTION = """
echo NET_IPV6
if [ -e /proc/net/if_inet6 ]; then
    echo NET_IPV6_KERNEL=yes
else
    echo NET_IPV6_KERNEL=no
fi

if [ -e /proc/net/if_inet6 ]; then
    # Keep the kernel from configuring vec0 itself: the gate wants the
    # address it names, not one it would have to discover.
    $bb sysctl -w net.ipv6.conf.vec0.accept_ra=0 >/dev/null 2>&1
    $bb sysctl -w net.ipv6.conf.vec0.accept_dad=0 >/dev/null 2>&1
    $bb ip -6 addr add __ADDR__ dev vec0 2>&1
    $bb ip -6 route add default via __GW__ dev vec0 2>&1
    $bb sleep 3
    $bb ip -6 addr show vec0 2>&1
    $bb ip -6 route 2>&1

    echo NET_PING6_GW
    $bb ping6 -c 2 -W 3 -I vec0 __GW__ >/tmp/ping6 2>&1
    ping6_rc=$?
    # 127 is busybox saying the applet does not exist; anything else is the
    # ping itself and must not be retried through a different spelling.
    if [ "$ping6_rc" -eq 127 ]; then
        $bb ping -6 -c 2 -W 3 -I vec0 __GW__ >/tmp/ping6 2>&1
        ping6_rc=$?
    fi
    $bb cat /tmp/ping6
    echo NET_PING6_RC=$ping6_rc

    echo NET_IPV6_DNS
    dns6_attempt=0
    while [ "$dns6_attempt" -lt 8 ]; do
        dns6_attempt=$((dns6_attempt + 1))
        dns6_answer=$($bb nslookup -type=AAAA example.com __DNS6__ 2>&1)
        printf '%s\n' "$dns6_answer"
        printf '%s\n' "$dns6_answer" | $bb grep -q '^Name:' && break
        $bb sleep 1
    done
    aaaa=$(printf '%s\n' "$dns6_answer" | $bb sed -n '/^Name:/{n;s/^Address:[[:space:]]*//p;}' | $bb head -1)
    printf 'NET_IPV6_AAAA=%s\n' "$aaaa"

    echo NET_IPV6_TCP
    tcp6_attempt=0
    while [ "$tcp6_attempt" -lt 3 ]; do
        tcp6_attempt=$((tcp6_attempt + 1))
        if [ -n "$aaaa" ]; then
            tcp6_body=$($bb wget -T 30 -O - "http://[$aaaa]/" --header 'Host: example.com' 2>&1 | $bb head -3)
        else
            tcp6_body=""
        fi
        printf '%s\n' "$tcp6_body"
        printf '%s\n' "$tcp6_body" | $bb grep -q 'Example Domain' && { echo NET_IPV6_TCP_OK=1; break; }
        $bb sleep 1
    done
fi
"""


def fail(message):
    print("gate: " + message, file=sys.stderr)
    raise SystemExit(1)


def skip(message, log_path=None):
    """The gate cannot be attempted here, so it judges nothing.

    Missing assets or a traced container are facts about the environment, not
    about the build.  Reporting them as FAIL would hide the difference between
    "this is broken" and "this was never tested", which is exactly the
    difference a release record has to keep.  Exit 77 is the harness-wide
    convention for it (see docs/RELEASE.md).
    """
    print("gate: " + message, file=sys.stderr)
    if log_path:
        print("log=" + log_path, file=sys.stderr)
    print("verdict=SKIP", file=sys.stderr)
    raise SystemExit(EXIT_SKIP)


def write_guest_command(netroot, ipv6_section=""):
    script = os.path.join(netroot, "net-command.sh")
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(GUEST_COMMAND.replace("__IPV6_SECTION__", ipv6_section))
    os.chmod(script, 0o755)
    return script


def guest_ipv6_section(args):
    """The IPv6 half of the guest script, with this run's values filled in."""
    section = GUEST_IPV6_SECTION
    section = section.replace("__ADDR__", shlex.quote(args.ipv6_addr))
    section = section.replace("__GW__", shlex.quote(args.ipv6_gw))
    section = section.replace("__DNS6__", shlex.quote(args.ipv6_dns))
    return section


def host_ipv6_egress(dns6):
    """Whether this host can reach the IPv6 internet at all.

    The gate runs beside passt, so a DNS query (the path the guest will use)
    or a TCP connection from here is the same test passt's sockets will make.
    A host with no global IPv6 address, no route, or a network that filters
    both is an environment fact, not a verdict on the bridge: the caller is
    told to use --link-only instead.
    """
    query = (b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
             b"\x07example\x03com\x00\x00\x01\x00\x01")
    servers = []
    if dns6 and dns6 != "none":
        servers.append(dns6)
    servers += ["2001:4860:4860::8888", "2606:4700:4700::1111"]
    seen = set()
    for server in servers:
        if server in seen:
            continue
        seen.add(server)
        try:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            sock.settimeout(3)
            try:
                sock.sendto(query, (server, 53))
                if sock.recvfrom(4096)[0]:
                    return True
            finally:
                sock.close()
        except OSError:
            pass
    for target in ("2606:4700:4700::1111",):
        try:
            socket.create_connection((target, 443), timeout=4).close()
            return True
        except OSError:
            pass
    return False


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


def uml_runtime_dirs():
    """Names of the umid directories under $HOME/.uml, as a set."""
    try:
        return set(os.listdir(os.path.join(os.path.expanduser("~"), ".uml")))
    except OSError:
        return set()


def remove_new_uml_dirs(before):
    """Remove umid directories this run created and then abandoned.

    The kernel removes its own umid directory, but only on the shutdown path --
    an exitcall that runs when the guest powers off.  This gate kills the
    process group in its cleanup, so any run that did not power off under its
    own steam leaves the directory behind: a timeout, or a fault-injection
    scenario that killed something on purpose.  They accumulate quietly; fifty
    of them had built up on the phone this was written for.

    Only directories that appeared during this run are candidates, and of those
    only the ones whose pid file names a process that is already gone, so a
    second UML running at the same time is never disturbed.
    """
    uml = os.path.join(os.path.expanduser("~"), ".uml")
    for name in uml_runtime_dirs() - before:
        directory = os.path.join(uml, name)
        alive = True
        try:
            with open(os.path.join(directory, "pid"), encoding="ascii") as handle:
                pid = int(handle.read().strip())
            os.kill(pid, 0)
        except (OSError, ValueError):
            # No pid file, an unreadable one, or a pid that is gone: either way
            # nothing owns this directory any more.
            alive = False
        if alive:
            continue
        for entry in ("mconsole", "pid"):
            try:
                os.unlink(os.path.join(directory, entry))
            except OSError:
                pass
        try:
            os.rmdir(directory)
        except OSError:
            pass


def run_gate(args):
    umnet = os.path.join(args.workdir, "umnet")
    passt = os.path.join(args.workdir, "passt-bionic")
    netroot = args.netroot or os.path.join(args.workdir, "netroot")
    kernel = os.path.join(PREFIX, "tmp/um-arm64/new/linux-bionic")
    stub = os.path.join(PREFIX, "tmp/um-arm64/new/stub_exe")

    for path in (umnet, passt, kernel, stub):
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            skip("not an executable file: " + path)
    if not os.path.isdir(netroot):
        skip("missing hostfs root: " + netroot)

    ipv6_section = guest_ipv6_section(args) if args.ipv6 else ""
    if not args.keep_guest_command:
        write_guest_command(netroot, ipv6_section)
    log_path = os.path.join(args.workdir, args.log)
    cmd = [umnet, "--passt", passt]
    if args.passt_pid_file:
        # Where the bridge writes the helper's pid.  Needed because the helper
        # cannot be found by looking: it is a live same-uid process that /proc
        # does not list on this platform, while kill(pid, 0) works.
        cmd += ["--passt-pid-file", args.passt_pid_file]
    if args.ipv6:
        cmd += ["--ipv6", "--ipv6-addr", args.ipv6_addr,
                "--ipv6-gw", args.ipv6_gw]
        if args.ipv6_dns != "none":
            cmd += ["--ipv6-dns", args.ipv6_dns]
    cmd += ["--dns", args.dns, "--", kernel,
           "mem=512M", "rw", "init=/um-init", "con=null", "con0=fd:0,fd:1",
           "stub_exe=" + stub, "rootfstype=hostfs", "rootflags=" + netroot,
           "panic=1"]
    print("gate: running on pty, timeout=%ss, announced dns=%s%s"
          % (args.timeout, args.dns,
             ", ipv6=%s/%s" % (args.ipv6_addr, args.ipv6_gw)
             if args.ipv6 else ""), file=sys.stderr)

    uml_dirs_before = uml_runtime_dirs()
    completed, raw = run_on_pty(cmd, args.timeout)
    remove_new_uml_dirs(uml_dirs_before)
    text = raw.decode("utf-8", errors="replace").replace("\r", "")
    with open(log_path, "w", encoding="utf-8") as handle:
        handle.write(text)

    if args.ipv6:
        # Two environment facts make an IPv6 verdict impossible to judge, and
        # the gate says so instead of blaming the bridge.  Both are visible in
        # the guest log: the first is the kernel's own capability, the second
        # is umnet reporting that the helper it forked never reached passt's
        # event loop.
        if "NET_IPV6_KERNEL=no" in text:
            skip("guest kernel has no IPv6 stack (/proc/net/if_inet6 is "
                 "absent); this linux-bionic predates CONFIG_IPV6=y in "
                 "arch/um/configs/arm64_defconfig and has to be rebuilt",
                 log_path)
        if "umnet: ipv6 helper exited" in text:
            skip("the IPv6 helper (passt -6) exited at start-up; this host "
                 "cannot serve IPv6 (NETLINK_ROUTE denied?)", log_path)

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
    if args.ipv6:
        # Link-level checks are the bridge's: an address, a route and an echo
        # to passt's own gateway.  They must pass wherever the guest kernel
        # has IPv6, even when the phone has no IPv6 uplink.
        checks += [
            ("ipv6-addr", r"inet6\s+" + re.escape(args.ipv6_addr)),
            ("ipv6-route", r"default via " + re.escape(args.ipv6_gw) +
             r" dev vec0"),
            ("ipv6-icmp", r"NET_PING6_RC=0"),
        ]
        # Egress checks need a host that can reach IPv6.  --link-only (or an
        # explicitly disabled resolver) leaves them out rather than turning a
        # network fact into a FAIL.
        if not args.link_only and args.ipv6_dns != "none":
            checks += [
                ("ipv6-dns", r"Name:\s*example\.com\s*\nAddress:\s*"
                             r"[0-9a-fA-F:]+"),
                ("ipv6-tcp", r"NET_IPV6_TCP_OK=1"),
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
    if args.ipv6 and (args.link_only or args.ipv6_dns == "none"):
        print("gate: ipv6-egress skipped (--link-only or --ipv6-dns none)",
              file=sys.stderr)

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
    parser.add_argument("--netroot", default=None,
                        help="hostfs root to boot instead of <workdir>/netroot, "
                             "for a caller that prepared its own guest command")
    parser.add_argument("--keep-guest-command", action="store_true",
                        help="do not rewrite net-command.sh in the hostfs root")
    parser.add_argument("--passt-pid-file", default=None,
                        help="ask the bridge to write the helper's pid here, "
                             "so a caller can signal it")
    parser.add_argument("--dns", default="1.1.1.1",
                        help="nameserver passt announces to the guest (default: "
                             "1.1.1.1).  Some networks block plain DNS to "
                             "particular resolvers, which breaks the guest's "
                             "lookups without anything being wrong with the "
                             "build; pass a resolver this network reaches.")
    parser.add_argument("--ipv6", action="store_true",
                        help="also give the guest IPv6, served by a second, "
                             "IPv6-only passt (see doc/90-networking.md)")
    parser.add_argument("--ipv6-addr", default=None,
                        help="IPv6 address for the guest (default: fd00::2/64)")
    parser.add_argument("--ipv6-gw", default=None,
                        help="IPv6 gateway passt emulates (default: fe80::1)")
    parser.add_argument("--ipv6-dns", default=None,
                        help="IPv6 nameserver passt announces (default: "
                             "2001:4860:4860::8888; 'none' disables it and the "
                             "DNS/TCP egress checks)")
    parser.add_argument("--link-only", action="store_true",
                        help="with --ipv6, judge only the link (address, route, "
                             "ping6) and not IPv6 egress; for a host with no "
                             "IPv6 uplink")
    args = parser.parse_args()

    # The two options only make sense with IPv6, so let them turn it on the way
    # umnet does; then fill in the defaults.
    if args.link_only or args.ipv6_addr or args.ipv6_gw or args.ipv6_dns:
        args.ipv6 = True
    if args.ipv6_addr is None:
        args.ipv6_addr = "fd00::2/64"
    if args.ipv6_gw is None:
        args.ipv6_gw = "fe80::1"
    if args.ipv6_dns is None:
        args.ipv6_dns = "2001:4860:4860::8888"

    if args.timeout <= 0:
        parser.error("timeout must be positive")
    with open("/proc/self/status", encoding="ascii") as status_file:
        for line in status_file:
            if line.startswith("TracerPid:") and int(line.split()[1]):
                skip("run on the Termux host, not inside a traced container")

    if args.check_host_only:
        missing = []
        for name in ("umnet", "passt-bionic"):
            path = os.path.join(args.workdir, name)
            present = os.path.isfile(path)
            print("%-14s %s" % (name, "present" if present else "MISSING"))
            if not present:
                missing.append(name)
        # An asset check is a readiness question, so answer it in the same
        # vocabulary the gate uses for the same situation.
        print("verdict=" + ("SKIP" if missing else "PASS"), file=sys.stderr)
        return EXIT_SKIP if missing else 0

    # A host without IPv6 egress can still prove the link half of the path, so
    # say which mode is required rather than booting into a predictable FAIL.
    if args.ipv6 and not args.link_only and args.ipv6_dns != "none":
        if not host_ipv6_egress(args.ipv6_dns):
            skip("this host cannot reach IPv6 (no global address, no route, "
                 "or both the DNS query and the TCP probe failed); use "
                 "--link-only to judge only the local link")

    return run_gate(args)


if __name__ == "__main__":
    sys.exit(main())
