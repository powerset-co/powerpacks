import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.setup.automations import accounts, msgvault_home, oauth_browser, shell


class MsgvaultConsentTests(unittest.TestCase):
    def test_existing_authorization_needs_no_browser_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            secret = home / "client_secret.json"
            secret.write_text(json.dumps({"installed": {"client_id": "synthetic", "client_secret": "synthetic"}}))
            msgvault_home.write_msgvault_config(home / "config.toml", secret)
            (home / "tokens").mkdir()
            (home / "tokens/casey@example.com.json").write_text("SYNTHETIC_EXISTING_TOKEN")
            binary = root / "msgvault"
            binary.write_text("#!/bin/sh\necho 'Account is already authorized.'\n")
            binary.chmod(0o700)
            with mock.patch.dict(os.environ, {"PATH": str(root) + os.pathsep + os.environ["PATH"]}), \
                mock.patch.object(oauth_browser, "ensure_playwright_core") as runtime:
                result = oauth_browser.authorize_account(home, "casey@example.com", "", force=False)
            self.assertEqual(result["status"], "ok")
            runtime.assert_not_called()

    def test_real_child_protocol_cleans_up_and_restores_custom_data_dir(self):
        for outcome in ("ok", "needs_user_action"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                home = root / "home"
                home.mkdir()
                secret = home / "client_secret.json"
                secret.write_text(json.dumps({"installed": {"client_id": "synthetic", "client_secret": "PRIVATE_FIXTURE_SECRET"}}))
                msgvault_home.write_msgvault_config(home / "config.toml", secret)
                with (home / "config.toml").open("a") as config:
                    config.write('[data]\ndata_dir = "archive"\n')
                token = home / "archive/tokens/casey@example.com.json"
                token.parent.mkdir(parents=True)
                token.write_text("PRIVATE_FIXTURE_TOKEN")
                binary = root / "msgvault"
                binary.write_text("#!/usr/bin/env python3\nimport os,sys,time\nfrom pathlib import Path\n"
                    "home=Path(sys.argv[sys.argv.index('--home')+1])\n"
                    "(home/'child.pid').write_text(str(os.getpid()))\n"
                    "print('https://accounts.google.com/o/oauth2/auth?state=PRIVATE_FIXTURE_STATE',flush=True)\n"
                    "while not (home/'archive/tokens/casey@example.com.json').exists(): time.sleep(.01)\n")
                binary.chmod(0o700)
                browser = root / "browser.js"
                browser.write_text('const fs=require("fs");\nconst request=JSON.parse(fs.readFileSync(0,"utf8"));\n'
                    + (f'fs.writeFileSync({json.dumps(str(token))}, "NEW_FIXTURE_TOKEN");\n' if outcome == "ok" else "")
                    + 'process.stderr.write(\'powerpacks-progress {"stage":"sign_in"}\\n\');\n'
                    + f'console.log(JSON.stringify({{status:{json.dumps(outcome)}}}));\n')
                output = StringIO()
                stages = []
                with mock.patch.dict(os.environ, {"PATH": str(root) + os.pathsep + os.environ["PATH"]}), \
                    mock.patch.object(oauth_browser, "BROWSER_SCRIPT", browser), \
                    mock.patch.object(oauth_browser, "ensure_playwright_core", return_value={"status": "ok", "node_path": "synthetic"}), \
                    redirect_stderr(output):
                    result = oauth_browser.authorize_account(home, "casey@example.com", "", force=True,
                                                             timeout_seconds=1, on_progress=stages.append)
                self.assertEqual(result["status"], outcome)
                self.assertEqual(stages, [{"stage": "sign_in"}])
                self.assertNotIn("PRIVATE_FIXTURE", output.getvalue() + json.dumps(result))
                self.assertEqual(token.read_text(), "NEW_FIXTURE_TOKEN" if outcome == "ok" else "PRIVATE_FIXTURE_TOKEN")
                with self.assertRaises(ProcessLookupError):
                    os.kill(int((home / "child.pid").read_text()), 0)

    def test_add_account_uses_saved_session_for_new_and_expired_accounts(self):
        with mock.patch.object(oauth_browser, "authorize_account", return_value={"status": "ok"}) as authorize:
            for force in (False, True):
                result = accounts.add_account(Path("/synthetic"), "casey@example.com", "", headless=False, force=force)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(authorize.call_args.kwargs, {"force": force, "on_progress": None})

    def test_headless_instructions_are_not_reported_as_authorized(self):
        with mock.patch.object(accounts, "run_visible_command", return_value=shell.CommandResult(ok=True)):
            result = accounts.add_account(Path("/synthetic"), "casey@example.com", "", headless=True, force=False)
        self.assertEqual(result["status"], "needs_user_action")

    def test_callback_outcome_and_failures_preserve_existing_token(self):
        for browser_status in ("ok", "error", "needs_user_action", "invalid-json", "timeout", "nonzero"):
            with self.subTest(browser_status=browser_status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                home = root / "vault"
                home.mkdir()
                secret = home / "client_secret.json"
                secret.write_text(json.dumps({"installed": {"client_id": "synthetic.apps.googleusercontent.com", "client_secret": "SYNTHETIC_SECRET"}}))
                msgvault_home.write_msgvault_config(home / "config.toml", secret)
                token = home / "tokens" / "casey@example.com.json"
                token.parent.mkdir()
                token.write_text("SYNTHETIC_OLD_TOKEN")
                binary = root / "msgvault"
                binary.write_text("#!/usr/bin/env python3\nimport os,sys,time\nfrom pathlib import Path\n"
                    "home=Path(sys.argv[sys.argv.index('--home')+1])\n"
                    "os.system('open https://accounts.google.com/SYNTHETIC_AUTH_URL')\n"
                    "print('https://accounts.google.com/o/oauth2/auth?state=SYNTHETIC_STATE',flush=True)\n"
                    "while not (home/'tokens'/'casey@example.com.json').exists(): time.sleep(.01)\n")
                binary.chmod(0o700)

                def browser(cmd, **kwargs):
                    request = json.loads(kwargs["input_text"])
                    self.assertEqual(request["clientId"], "synthetic.apps.googleusercontent.com")
                    self.assertNotIn("SYNTHETIC_STATE", " ".join(cmd))
                    if browser_status == "timeout":
                        raise subprocess.TimeoutExpired(cmd, 1)
                    if browser_status in ("ok", "nonzero"):
                        token.write_text("SYNTHETIC_NEW_TOKEN")
                    status = "ok" if browser_status == "nonzero" else browser_status
                    output = "invalid" if browser_status == "invalid-json" else json.dumps({"status": status})
                    return shell.CommandResult(ok=browser_status != "nonzero", returncode=1 if browser_status == "nonzero" else 0, stdout=output)

                with mock.patch.dict(os.environ, {"PATH": str(root) + os.pathsep + os.environ["PATH"]}), \
                    mock.patch.object(oauth_browser, "ensure_playwright_core", return_value={"status": "ok", "node_path": "synthetic"}), \
                    mock.patch.object(oauth_browser, "run_streaming_command", side_effect=browser):
                    result = oauth_browser.authorize_account(home, "casey@example.com", "", force=True, timeout_seconds=1)
                self.assertNotIn("SYNTHETIC_STATE", json.dumps(result))
                self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))
                if browser_status == "ok":
                    self.assertEqual(result["status"], "ok")
                    self.assertEqual(token.read_text(), "SYNTHETIC_NEW_TOKEN")
                    self.assertEqual(token.with_suffix(".json.bkup").read_text(), "SYNTHETIC_OLD_TOKEN")
                else:
                    self.assertNotEqual(result["status"], "ok")
                    self.assertEqual(token.read_text(), "SYNTHETIC_OLD_TOKEN")


if __name__ == "__main__":
    unittest.main()
