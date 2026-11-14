#!/usr/bin/env python3
"""Execute relocated um-init; mounts and shutdown are shell mocks, never host actions.

This checks exit/status ownership, not UML boot or hostfs mounting. No production
branch is extracted or reimplemented: only absolute guest paths are relocated.
"""
from pathlib import Path
import subprocess
import tempfile
import unittest


INIT = Path(__file__).resolve().parents[3] / "harness" / "um-init"


class RuntimeExitOwnership(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="um-init-runtime-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = self.root / "run" / "neoproot"
        self.session.mkdir(parents=True)
        self.status = self.session / "status"
        self.status.write_text("0\n")
        self.other = self.root / "other-session"
        self.other.mkdir()
        (self.other / "status").write_text("71\n")
        self.init = self.session / "init.sh"
        self.command = self.root / "um-command.sh"
        self.command.write_text("exit 0\n")
        self.set_init(0)

    def set_init(self, rc):
        self.init.write_text("printf 'INIT_RAN\\n'\nexit %d\n" % rc)
        self.init.chmod(0o755)

    def run_init(self, session=True, cwd="/", mount_failure=False):
        cmdline = "rw"
        if session:
            cmdline += " neoproot_protocol=session-v1 neoproot_session=/current-session"
            cmdline += " neoproot_cwd=" + cwd
        (self.root / "cmdline").write_text(cmdline)
        source = INIT.read_text()
        for old, new in (
            ("/proc/cmdline", str(self.root / "cmdline")),
            ("/run/", str(self.root / "run") + "/"),
            ("tmpfs /run ", "tmpfs " + str(self.root / "run") + " "),
            ("/sys/fs/cgroup", str(self.root / "sys/fs/cgroup")),
            ("/dev/pts", str(self.root / "dev/pts")),
            ("/um-command.sh", str(self.command)),
        ):
            source = source.replace(old, new)
        # Shell functions take precedence over PATH commands. poweroff exits the
        # test shell so production power_down cannot reach its infinite sleep.
        mock = """mount() {
    printf 'mount %s\\n' "$*" >> "$MOCK_LOG"
    case "$*" in *hostfs*) return "$HOSTFS_RC" ;; esac
    return 0
}
sync() { :; }
poweroff() { printf 'poweroff %s\\n' "$*" >> "$MOCK_LOG"; exit 0; }
"""
        script = self.root / "relocated-init.sh"
        script.write_text(mock + source)
        env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C",
               "MOCK_LOG": str(self.root / "calls"),
               "HOSTFS_RC": "1" if mount_failure else "0"}
        result = subprocess.run(["/bin/sh", str(script)], env=env,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.other / "status").read_text(), "71\n")
        calls = (self.root / "calls").read_text()
        self.assertIn("poweroff -f\n", calls)
        if session:
            self.assertIn("-t hostfs -o /current-session", calls)
        return result.stdout

    def test_session_failure_overwrites_stale_success(self):
        self.set_init(37)
        output = self.run_init()
        self.assertIn("INIT_RAN\n", output)
        self.assertEqual(self.status.read_text(), "37\n")
        self.assertIn("UM_COMMAND_EXIT=37\n", output)
        self.assertNotIn("UMARM_BOOT_OK", output)

    def test_session_success_overwrites_stale_failure(self):
        self.status.write_text("99\n")
        output = self.run_init()
        self.assertEqual(self.status.read_text(), "0\n")
        self.assertIn("UM_COMMAND_EXIT=0\nUMARM_BOOT_OK\n", output)

    def test_bad_cwd_owns_status_126_and_never_runs_command(self):
        output = self.run_init(cwd=str(self.root / "missing"))
        self.assertEqual(self.status.read_text(), "126\n")
        self.assertIn("UM_COMMAND_EXIT=126\n", output)
        self.assertNotIn("INIT_RAN", output)
        self.assertNotIn("UMARM_BOOT_OK", output)

    def test_missing_init_does_not_promote_stale_status_to_success(self):
        self.init.unlink()
        output = self.run_init()
        self.assertEqual(self.status.read_text(), "0\n")
        self.assertEqual(output, "UM_SESSION_ERROR=125\n")

    def test_failed_session_mount_does_not_promote_stale_status(self):
        output = self.run_init(mount_failure=True)
        self.assertEqual(self.status.read_text(), "0\n")
        self.assertEqual(output, "UM_SESSION_ERROR=125\n")

    def test_legacy_exit_markers_do_not_own_any_session_status(self):
        for rc in (0, 23):
            with self.subTest(rc=rc):
                self.status.write_text("88\n")
                self.command.write_text("exit %d\n" % rc)
                output = self.run_init(session=False)
                self.assertEqual(self.status.read_text(), "88\n")
                self.assertIn("UM_COMMAND_EXIT=%d\n" % rc, output)
                self.assertEqual("UMARM_BOOT_OK" in output, rc == 0)
                self.assertNotIn("INIT_RAN", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
