"""Installer progress reflects completion, human waits, and interrupted work."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.powerset.primitives.install.status import InstallState, InstallStatus, InstallStep


class InstallStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.status = InstallStatus(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_install_waits_without_animating(self) -> None:
        self.assertEqual(self.status.read()["status"], "waiting")

    def test_invalid_manifest_has_actionable_failure(self) -> None:
        self.status.directory.mkdir(parents=True)
        self.status.manifest_path.write_text("broken json")
        self.assertEqual(self.status.read()["status"], "failed")
        self.assertIn("rerun", self.status.read()["message"])

    def test_running_install_is_read_from_atomic_manifest(self) -> None:
        self.status.write(step=InstallStep.DEPENDENCIES, status=InstallState.RUNNING,
                          message="Installing dependencies", pid=os.getpid())
        record = self.status.read()
        self.assertEqual(record["primitive"], "powerpacks_install")
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["step"], "dependencies")
        self.assertEqual(record["retry_command"], "bin/bootstrap")
        self.assertEqual(record["log_path"], str(self.root / ".powerpacks/install/install.log"))
        self.assertFalse((self.root / ".powerpacks/install/manifest.tmp").exists())

    def test_dead_installer_is_failed_with_rerun_guidance(self) -> None:
        self.status.write(step=InstallStep.DEPENDENCIES, status=InstallState.RUNNING,
                          message="Installing dependencies", pid=99999999)
        record = self.status.read()
        self.assertEqual(record["status"], "failed")
        self.assertIn("interrupted", record["message"])
        self.assertIn("bin/bootstrap", record["message"])

    def test_human_wait_survives_installer_exit(self) -> None:
        self.status.write(step=InstallStep.TOOLS, status=InstallState.WAITING,
                          message="Approve the free Gmail tools in chat", pid=99999999,
                          retry_command="bin/bootstrap --tools")
        record = self.status.read()
        self.assertEqual(record["status"], "waiting")
        self.assertEqual(record["retry_command"], "bin/bootstrap --tools")

    def test_completed_install_survives_installer_exit(self) -> None:
        self.status.write(step=InstallStep.READY, status=InstallState.COMPLETED,
                          message="Powerpacks is installed", pid=99999999)
        self.assertEqual(self.status.read()["status"], "completed")

    def test_failed_replacement_keeps_previous_readable_manifest(self) -> None:
        self.status.write(step=InstallStep.RUNTIME, status=InstallState.RUNNING,
                          message="Preparing Python", pid=os.getpid())
        with patch("pathlib.Path.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.status.write(step=InstallStep.READY, status=InstallState.COMPLETED,
                                  message="Powerpacks is installed", pid=os.getpid())
        self.assertEqual(self.status.read()["step"], "runtime")


if __name__ == "__main__":
    unittest.main()
