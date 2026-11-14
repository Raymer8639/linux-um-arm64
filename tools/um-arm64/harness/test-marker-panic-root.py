#!/usr/bin/env python3
"""Exercise real panic-root ownership checks with disposable filesystem trees.

Only prepare_panic_root is called. No scenario, guest, mount or signal is run.
The generated panic script is inspected as text and is never executed.
"""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
SPEC = importlib.util.spec_from_file_location("inject_faults_marker_test", HERE / "inject-faults.py")
faults = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(faults)


class PanicRootOwnership(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="panic-root-marker-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "gate"
        self.source = self.work / "netroot"
        self.source.mkdir(parents=True)
        (self.source / "um-init").write_text("original init\n")
        (self.source / "net-command.sh").write_text("original command\n")
        (self.source / "keep").write_text("source sentinel\n")
        self.target = self.root / "panic-root"

    def prepare(self, target=None):
        args = SimpleNamespace(workdir=str(self.work),
                               panic_root=str(target if target is not None else self.target))
        # Setup is allowed to copy/write, but must never start any process.
        with patch.object(faults.subprocess, "Popen", side_effect=AssertionError("unexpected process")):
            return faults.prepare_panic_root(args)

    def assert_source_intact(self):
        self.assertEqual((self.source / "net-command.sh").read_text(), "original command\n")
        self.assertEqual((self.source / "keep").read_text(), "source sentinel\n")
        self.assertFalse((self.source / faults.PANIC_ROOT_MARKER).exists())

    def assert_prepared(self):
        self.assertTrue((self.target / faults.PANIC_ROOT_MARKER).is_file())
        self.assertEqual((self.target / "net-command.sh").read_text(), faults.PANIC_SCRIPT)
        self.assertEqual((self.target / "net-command.sh").stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.target / "keep").read_text(), "source sentinel\n")
        self.assert_source_intact()

    def test_new_root_is_marked_and_only_copy_gets_panic_script(self):
        (self.source / "keep-link").symlink_to("keep")
        self.assertEqual(self.prepare(), str(self.target))
        self.assert_prepared()
        self.assertTrue((self.target / "keep-link").is_symlink())
        self.assertEqual(os.readlink(self.target / "keep-link"), "keep")

    def test_copied_command_symlink_does_not_overwrite_external_file(self):
        external = self.root / "external-command"
        external.write_text("external command sentinel\n")
        command = self.source / "net-command.sh"
        command.unlink()
        command.symlink_to(external)
        self.prepare()
        self.assertEqual(external.read_text(), "external command sentinel\n")
        self.assertTrue(command.is_symlink())
        self.assertFalse((self.target / "net-command.sh").is_symlink())
        self.assertEqual((self.target / "net-command.sh").read_text(), faults.PANIC_SCRIPT)
        self.assertEqual((self.target / "net-command.sh").stat().st_mode & 0o777, 0o755)

    def test_copied_marker_symlink_does_not_overwrite_external_file(self):
        external = self.root / "external-marker"
        external.write_text("external marker sentinel\n")
        marker = self.source / faults.PANIC_ROOT_MARKER
        marker.symlink_to(external)
        self.prepare()
        self.assertEqual(external.read_text(), "external marker sentinel\n")
        self.assertTrue(marker.is_symlink())
        self.assertFalse((self.target / faults.PANIC_ROOT_MARKER).is_symlink())
        self.assertIn("created by inject-faults.sh",
                      (self.target / faults.PANIC_ROOT_MARKER).read_text())

    def test_unmarked_directory_is_refused_without_deletion(self):
        self.target.mkdir()
        (self.target / "sentinel").write_text("not yours\n")
        with self.assertRaisesRegex(faults.SetupRefused, "no .* marker"):
            self.prepare()
        self.assertEqual(list(self.target.iterdir()), [self.target / "sentinel"])
        self.assertEqual((self.target / "sentinel").read_text(), "not yours\n")
        self.assert_source_intact()

    def test_owned_directory_is_rebuilt_rather_than_merged(self):
        self.prepare()
        (self.target / "old-only").write_text("obsolete\n")
        (self.target / "keep").write_text("stale copy\n")
        self.prepare()
        self.assertFalse((self.target / "old-only").exists())
        self.assert_prepared()

    def test_marker_directory_does_not_grant_ownership(self):
        self.target.mkdir()
        (self.target / faults.PANIC_ROOT_MARKER).mkdir()
        with self.assertRaises(faults.SetupRefused):
            self.prepare()
        self.assertTrue((self.target / faults.PANIC_ROOT_MARKER).is_dir())
        self.assert_source_intact()

    def test_regular_file_target_is_refused(self):
        self.target.write_text("not a directory\n")
        with self.assertRaises(faults.SetupRefused):
            self.prepare()
        self.assertEqual(self.target.read_text(), "not a directory\n")
        self.assert_source_intact()

    def test_unmarked_symlink_target_is_refused_without_following_for_delete(self):
        external = self.root / "external"
        external.mkdir()
        (external / "sentinel").write_text("external data\n")
        self.target.symlink_to(external, target_is_directory=True)
        with self.assertRaises(faults.SetupRefused):
            self.prepare()
        self.assertTrue(self.target.is_symlink())
        self.assertEqual((external / "sentinel").read_text(), "external data\n")
        self.assert_source_intact()

    def test_marked_symlink_is_rejected_by_rmtree_without_deleting_referent(self):
        # Current contract: isfile follows the marker, then shutil.rmtree refuses
        # the symlink itself with OSError (not SetupRefused).
        external = self.root / "external"
        external.mkdir()
        (external / faults.PANIC_ROOT_MARKER).write_text("owned marker\n")
        (external / "sentinel").write_text("external data\n")
        self.target.symlink_to(external, target_is_directory=True)
        with self.assertRaises(OSError):
            self.prepare()
        self.assertTrue(self.target.is_symlink())
        self.assertEqual((external / "sentinel").read_text(), "external data\n")
        self.assertEqual((external / faults.PANIC_ROOT_MARKER).read_text(), "owned marker\n")
        self.assert_source_intact()

    def test_dangling_symlink_cannot_be_replaced_by_copy(self):
        missing = self.root / "missing"
        self.target.symlink_to(missing)
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.assertTrue(self.target.is_symlink())
        self.assertFalse(missing.exists())
        self.assert_source_intact()

    def test_overlap_refused_even_when_target_has_marker(self):
        for target in (self.source, self.work, self.source / "nested"):
            with self.subTest(target=target):
                target.mkdir(exist_ok=True)
                marker = target / faults.PANIC_ROOT_MARKER
                marker.write_text("marker cannot authorize overlap\n")
                with self.assertRaisesRegex(faults.SetupRefused, "overlaps"):
                    self.prepare(target)
                self.assertEqual(marker.read_text(), "marker cannot authorize overlap\n")
                self.assertEqual((self.source / "keep").read_text(), "source sentinel\n")
                marker.unlink()
        self.assert_source_intact()


if __name__ == "__main__":
    unittest.main(verbosity=2)
