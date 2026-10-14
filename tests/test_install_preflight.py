"""Desktop prerequisite checks and installs run without Homebrew."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from packs.ingestion.primitives.setup.automations import oauth_browser
from packs.powerset.primitives.install import preflight


class PreflightTests(unittest.TestCase):
    @patch.object(preflight.subprocess, "run")
    @patch.object(preflight.shutil, "which", return_value=None)
    @patch.object(Path, "is_file", return_value=False)
    def test_checks_missing(self, _file, _which, run):
        run.return_value.stdout = "null"
        self.assertEqual(preflight.checks(), {"browser": {"ok": False, "name": None}, "gcloud": {"ok": False}})

    @patch.object(preflight.subprocess, "run")
    @patch.object(preflight.shutil, "which", return_value=None)
    @patch.object(Path, "is_file", return_value=True)
    def test_checks_browser_and_local_gcloud(self, _file, _which, run):
        run.return_value.stdout = json.dumps({"name": "Arc", "path": "/fixture/Arc"})
        self.assertEqual(preflight.checks(), {"browser": {"ok": True, "name": "Arc"}, "gcloud": {"ok": True}})
        self.assertEqual(run.call_args.args[0][-1], "--which")

    @patch.object(preflight.subprocess, "run", side_effect=FileNotFoundError("node"))
    @patch.object(preflight.shutil, "which", return_value="/fixture/gcloud")
    def test_checks_without_node(self, _which, _run):
        self.assertEqual(preflight.checks(), {"browser": {"ok": False, "name": None}, "gcloud": {"ok": True}})

    @patch.object(preflight.subprocess, "Popen")
    def test_chromium_streams_cli_output(self, popen):
        process = popen.return_value.__enter__.return_value
        process.stdout = iter(["Downloading Chromium\n", "Done\n"])
        process.wait.return_value = 0
        lines = []
        self.assertEqual(preflight.install("chromium", lines.append), {"status": "ok"})
        self.assertEqual(lines, ["Downloading Chromium", "Done"])
        self.assertEqual(popen.call_args.args[0], ["node", str(oauth_browser.VENDORED_NODE_MODULES / "playwright-core/cli.js"), "install", "chromium"])

    @patch.object(preflight.subprocess, "Popen")
    def test_chromium_failure(self, popen):
        process = popen.return_value.__enter__.return_value
        process.stdout = iter(["download failed\n"])
        process.wait.return_value = 1
        result = preflight.install("chromium", Mock())
        self.assertEqual(result["status"], "failed")
        self.assertIn("download failed", result["message"])

    @patch.object(preflight.subprocess, "Popen", side_effect=OSError("missing node"))
    def test_install_start_failure(self, _popen):
        self.assertEqual(preflight.install("chromium", Mock()), {"status": "failed", "message": "missing node"})

    @patch.object(preflight, "_stream")
    @patch.object(preflight.tarfile, "open")
    @patch.object(preflight.urllib.request, "urlretrieve")
    @patch.object(Path, "mkdir")
    @patch.object(preflight.platform, "machine")
    def test_gcloud_archives_and_no_installer(self, machine, _mkdir, download, archive, stream):
        for arch, expected in [("arm64", "arm"), ("x86_64", "x86_64")]:
            with self.subTest(arch=arch):
                machine.return_value = arch
                self.assertEqual(preflight.install("gcloud", Mock()), {"status": "ok"})
                self.assertTrue(download.call_args.args[0].endswith(f"google-cloud-cli-darwin-{expected}.tar.gz"))
                archive.return_value.__enter__.return_value.extractall.assert_called_with(
                    preflight.GCLOUD_SDK.parent, filter="data")
                self.assertEqual(stream.call_args.args[0], [str(preflight.GCLOUD_SDK / "bin/gcloud"), "--version"])

    @patch.object(preflight.urllib.request, "urlretrieve", side_effect=OSError("offline"))
    @patch.object(Path, "mkdir")
    def test_gcloud_download_failure(self, _mkdir, _download):
        self.assertEqual(preflight.install("gcloud", Mock()), {"status": "failed", "message": "offline"})


class VendoredPlaywrightTests(unittest.TestCase):
    @patch.object(oauth_browser, "run_command")
    @patch.object(oauth_browser.shutil, "which")
    @patch.object(Path, "is_dir", return_value=True)
    def test_vendor_needs_no_node_or_npm_check(self, _directory, which, run):
        self.assertEqual(oauth_browser.ensure_playwright_core(), {
            "status": "ok", "installed": False, "node_path": str(oauth_browser.VENDORED_NODE_MODULES)})
        which.assert_not_called()
        run.assert_not_called()

    @patch.object(oauth_browser, "run_command")
    @patch.object(oauth_browser.shutil, "which", return_value="/fixture/bin/tool")
    @patch.object(Path, "is_dir", return_value=False)
    @patch.object(Path, "exists", return_value=True)
    def test_cli_reuses_existing_npm_package(self, _exists, _directory, _which, run):
        prefix = Path("/fixture/browser-node")
        self.assertEqual(oauth_browser.ensure_playwright_core(prefix), {
            "status": "ok", "installed": False, "node_path": str(prefix / "node_modules")})
        run.assert_not_called()

    @patch.object(oauth_browser.shutil, "which", return_value=None)
    @patch.object(Path, "is_dir", return_value=False)
    def test_cli_install_keeps_node_requirement(self, _directory, _which):
        self.assertEqual(oauth_browser.ensure_playwright_core()["message"], "node is not installed")


if __name__ == "__main__":
    unittest.main()
