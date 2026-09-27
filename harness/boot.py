#!/usr/bin/env python3
"""Boot bionic ARM64 UML through a pty."""
import os, pty, select, sys, time
if len(sys.argv) < 4: raise SystemExit("usage: boot.py KERNEL STUB ROOTFS [init-command]")
kernel, stub, rootfs = sys.argv[1:4]
init_command = sys.argv[4] if len(sys.argv) > 4 else "echo UMARM_BOOT_OK; uname -a; poweroff -f"
cmd = [kernel, "mem=512M", "ubd0=" + rootfs, "root=/dev/ubda", "rw", "init=/bin/sh", "con=null", "con0=fd:0,fd:1", "stub_exe=" + stub, "panic=5"]
pid, fd = pty.fork()
if pid == 0: os.execv(cmd[0], cmd)
sent = False
start = time.monotonic()
while time.monotonic() - start < 120:
    ready, _, _ = select.select([fd], [], [], 0.5)
    if fd not in ready: continue
    try: data = os.read(fd, 65536)
    except OSError: break
    if not data: break
    sys.stdout.buffer.write(data); sys.stdout.buffer.flush()
    if not sent and b"mc-1" in data:
        time.sleep(0.2); os.write(fd, (init_command + "\n").encode()); sent = True
try: os.close(fd)
except OSError: pass
_, status = os.waitpid(pid, 0)
raise SystemExit(os.waitstatus_to_exitcode(status))
