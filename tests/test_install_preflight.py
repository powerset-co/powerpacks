"""Desktop prerequisite checks and installs run without Homebrew."""
from __future__ import annotations

import json
import os
import tempfile
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

    @patch.object(preflight, "ensure_chromium", return_value={"status": "ok", "installed": False, "node_path": "/n"})
    def test_chromium_streams_its_install(self, ensure):
        lines = Mock()
        self.assertEqual(preflight.install("chromium", lines), {"status": "ok"})
        ensure.assert_called_once_with(lines)

    @patch.object(preflight, "ensure_chromium", return_value={"status": "error", "message": "download failed"})
    def test_chromium_failure(self, _ensure):
        self.assertEqual(preflight.install("chromium", Mock()), {"status": "failed", "message": "download failed"})

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



@patch.object(oauth_browser, "ensure_playwright_core", return_value={"status": "ok", "installed": False, "node_path": "/n"})
@patch.object(oauth_browser.subprocess, "Popen")
class EnsureChromiumTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        environment = patch.dict(os.environ, {"POWERPACKS_BROWSERS_DIR": str(self.folder)})
        environment.start()
        self.addCleanup(environment.stop)

    def finish(self, popen, lines, returncode):
        process = popen.return_value.__enter__.return_value
        process.stdout = iter(lines)
        process.wait.return_value = returncode

    def test_installed_chromium_is_not_installed_again(self, popen, _deps):
        (self.folder / "chromium-1223").mkdir()
        (self.folder / "chromium-1223/INSTALLATION_COMPLETE").touch()
        self.assertEqual(oauth_browser.ensure_chromium(Mock())["node_path"], "/n")
        popen.assert_not_called()

    def test_unfinished_download_is_installed_again(self, popen, _deps):
        (self.folder / "chromium-1223/chrome-mac-arm64").mkdir(parents=True)
        self.finish(popen, [], 0)
        oauth_browser.ensure_chromium(Mock())
        popen.assert_called_once()

    def test_installs_into_the_powerpacks_folder_with_the_bundled_cli(self, popen, _deps):
        self.finish(popen, ["Chrome for Testing downloaded\n"], 0)
        lines = []
        self.assertEqual(oauth_browser.ensure_chromium(lines.append)["status"], "ok")
        self.assertEqual(popen.call_args.args[0], ["node", "/n/playwright-core/cli.js", "install", "--no-shell", "chromium"])
        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["PLAYWRIGHT_BROWSERS_PATH"], str(self.folder))
        self.assertEqual(env["FORCE_COLOR"], "0")
        self.assertEqual(lines, ["Downloading the browser Powerpacks uses (about 180 MB).", "Chrome for Testing downloaded"])

    def test_failed_download_is_an_error(self, popen, _deps):
        self.finish(popen, ["Error: getaddrinfo ENOTFOUND cdn.playwright.dev\n"], 1)
        self.assertEqual(oauth_browser.ensure_chromium(Mock()), {
            "status": "error", "message": "Powerpacks could not download its browser: Error: getaddrinfo ENOTFOUND cdn.playwright.dev"})

    def test_missing_node_is_an_error(self, popen, _deps):
        popen.side_effect = OSError("node not found")
        result = oauth_browser.ensure_chromium(Mock())
        self.assertEqual(result["status"], "error")
        self.assertIn("node not found", result["message"])

    def test_missing_runtime_is_returned_as_is(self, popen, deps):
        deps.return_value = {"status": "error", "message": "npm is not installed"}
        self.assertEqual(oauth_browser.ensure_chromium(Mock()), {"status": "error", "message": "npm is not installed"})
        popen.assert_not_called()

    def test_default_folder(self, _popen, _deps):
        with patch.dict(os.environ, {"POWERPACKS_BROWSERS_DIR": ""}):
            self.assertEqual(oauth_browser.browsers_dir(), Path.home() / ".powerpacks/browsers")


if __name__ == "__main__":
    unittest.main()
