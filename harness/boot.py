#!/usr/bin/env python3
"""Run UML on an untraced host with a bounded lifetime and a strict gate."""
import argparse
import errno
import fcntl
import os
import pty
import re
import select
import signal
import sys
import time

def image_is_held(path: str) -> bool:
    """True when another process holds UML's exclusive lock on this image.

    UML takes an F_SETLK (write lock) on the backing image, so the lock itself is the
    condition a second boot runs into: if this process cannot take it, somebody else
    has it. The check has to happen BEFORE the boot, because a second guest on a held
    image does not fail loudly -- it boots with no disk (the console says NO_DISK) and
    silently discards every write, which is a data-loss bug that looks like a working
    guest.
    """
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        try:
            fcntl.lockf(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
    return False


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("kernel")
parser.add_argument("stub")
parser.add_argument("rootfs")
parser.add_argument("--hostfs", action="store_true")
parser.add_argument("--timeout", type=float, default=120)
args = parser.parse_args()
if args.timeout <= 0:
    parser.error("timeout must be positive")
with open("/proc/self/status", encoding="ascii") as status_file:
    for line in status_file:
        if line.startswith("TracerPid:") and int(line.split()[1]):
            parser.error("run on the Termux host, not inside a traced container")
kernel, stub, root = map(os.path.abspath, (args.kernel, args.stub, args.rootfs))
for path in (kernel, stub):
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        parser.error("not an executable file: " + path)
if not (os.path.isdir(root) if args.hostfs else os.path.isfile(root)):
    parser.error("invalid rootfs: " + root)
if not args.hostfs and image_is_held(root):
    parser.error(
        "another guest already holds " + root + ": UML takes an exclusive lock on the "
        "backing image, and a second boot would run diskless and lose every write. "
        "Copy the image, or stop the other guest first."
    )
cmd = [kernel, "mem=512M", "rw", "init=/um-init", "con=null",
       "con0=fd:0,fd:1", "stub_exe=" + stub, "panic=1"]
cmd += (["rootfstype=hostfs", "rootflags=" + root] if args.hostfs else
        ["ubd0=" + root, "root=/dev/ubda"])
pid, fd = pty.fork()
if pid == 0:
    try:
        os.execv(kernel, cmd)
    except OSError as exc:
        print(exc, file=sys.stderr, flush=True)
        os._exit(127)
log = bytearray()
status = None
deadline = time.monotonic() + args.timeout
try:
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], min(0.2, max(0, deadline - time.monotonic())))
        if ready:
            try:
                data = os.read(fd, 65536)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                break
            if not data:
                break
            log.extend(data)
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
    child, status = os.waitpid(pid, os.WNOHANG)
    if child == 0:
        status = None
finally:
    if status is None:
        # pty.fork creates a separate session: kill only this UML group.
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)
    os.close(fd)
if status is None:
    print("verdict=FAIL timeout or incomplete shutdown", file=sys.stderr)
    raise SystemExit(124)
text = log.decode("utf-8", errors="replace").replace("\r", "")
marker = re.search(r"^UMARM_BOOT_OK$", text, re.MULTILINE)
bugs = re.search(r"BUG:|WARNING:|Kernel panic", text)
passed = os.waitstatus_to_exitcode(status) == 0 and marker is not None and bugs is None
print("verdict=" + ("PASS" if passed else "FAIL"), file=sys.stderr)
raise SystemExit(0 if passed else 1)
