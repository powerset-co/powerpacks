import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, rel):
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


msgvault_setup = load_module(
    "msgvault_setup",
    "packs/ingestion/primitives/msgvault_setup/msgvault_setup.py",
)


class MsgvaultSetupTests(unittest.TestCase):
    def invoke(self, argv):
        buf = StringIO()
        with redirect_stdout(buf):
            code = msgvault_setup.main(argv)
        payload = json.loads(buf.getvalue()) if buf.getvalue().strip() else {}
        return code, payload

    def write_secret(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "installed": {
                        "client_id": "abc.apps.googleusercontent.com",
                        "client_secret": "secret",
                        "redirect_uris": ["http://localhost"],
                    }
                }
            ),
            encoding="utf-8",
        )

    def test_validate_client_secret_accepts_installed_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / "client_secret.json"
            self.write_secret(secret)
            result = msgvault_setup.validate_client_secret(secret)
            self.assertTrue(result["ok"])
            self.assertEqual(result["client_id"], "abc.apps.googleusercontent.com")

    def test_validate_client_secret_rejects_web_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / "client_secret.json"
            secret.write_text(json.dumps({"web": {"client_id": "abc"}}), encoding="utf-8")
            result = msgvault_setup.validate_client_secret(secret)
            self.assertFalse(result["ok"])
            self.assertIn("installed", result["message"])

    def test_write_msgvault_config_default_and_named_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            msgvault_setup.write_msgvault_config(config, Path(tmp) / "default.json")
            msgvault_setup.write_msgvault_config(config, Path(tmp) / "acme.json", "acme")
            text = config.read_text(encoding="utf-8")
            self.assertIn("[oauth]", text)
            self.assertIn(f'client_secrets = "{Path(tmp) / "default.json"}"', text)
            self.assertIn("[oauth.apps.acme]", text)
            self.assertIn(f'client_secrets = "{Path(tmp) / "acme.json"}"', text)
            self.assertIn("[sync]", text)

    def test_parse_json_fragment_skips_msgvault_log_lines(self):
        text = 'time=2026 level=INFO msg="startup"\n[{"email":"me@example.com"}]\ntime=2026 level=INFO msg="exit"\n'
        self.assertEqual(msgvault_setup.parse_json_fragment(text), [{"email": "me@example.com"}])

    def test_create_oauth_app_returns_action_and_continue_command(self):
        with mock.patch.object(msgvault_setup, "gcloud_context", return_value={"project": "demo", "account": "me@example.com", "installed": True}), \
            mock.patch.object(msgvault_setup, "enable_gmail_api", return_value={"status": "ok"}):
            code, payload = self.invoke([
                "create-oauth-app",
                "--email",
                "me@example.com",
                "--no-open-console",
            ])
        self.assertEqual(code, 20)
        self.assertEqual(payload["status"], "needs_user_action")
        self.assertEqual(payload["action"]["expected_client_type"], "Desktop app")
        self.assertEqual(payload["action"]["oauth_client_name"], "local-msg-vault")
        self.assertIn("--client-secret /path/to/client_secret.json", payload["action"]["continue_command"])
        self.assertIn("gmail.googleapis.com", payload["action"]["urls"]["gmail_api"])

    def test_default_project_id_is_valid_and_local_msg_vault_prefixed(self):
        project_id = msgvault_setup.default_project_id()
        self.assertTrue(project_id.startswith("local-msg-vault-"))
        self.assertEqual(msgvault_setup.validate_project_id(project_id), project_id)
        self.assertEqual(msgvault_setup.default_project_id("me@example.com"), msgvault_setup.default_project_id("me@example.com"))

    def test_choose_project_reuses_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            msgvault_setup.save_setup_state(home, {"project_id": "local-msg-vault-state1"})
            project_id, choice = msgvault_setup.choose_project_id(home, "", "me@example.com", "me@example.com")
            self.assertEqual(project_id, "local-msg-vault-state1")
            self.assertEqual(choice["source"], "state")

    def test_choose_project_reuses_existing_local_project(self):
        with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(msgvault_setup, "gcloud_value", return_value="powerset-search"), \
            mock.patch.object(msgvault_setup, "local_msg_vault_projects", return_value=[
                {"projectId": "local-msg-vault-newest", "createTime": "2026-01-02T00:00:00Z"},
                {"projectId": "local-msg-vault-oldest", "createTime": "2026-01-01T00:00:00Z"},
            ]):
            project_id, choice = msgvault_setup.choose_project_id(Path(tmp), "", "me@example.com", "me@example.com")
            self.assertEqual(project_id, "local-msg-vault-newest")
            self.assertEqual(choice["source"], "existing_local_msg_vault_project")

    def test_choose_project_saves_current_local_project(self):
        with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(msgvault_setup, "gcloud_value", return_value="local-msg-vault-current1"):
            home = Path(tmp)
            project_id, choice = msgvault_setup.choose_project_id(home, "", "me@example.com", "me@example.com")
            self.assertEqual(project_id, "local-msg-vault-current1")
            self.assertEqual(choice["source"], "gcloud_current_project")
            self.assertEqual(msgvault_setup.load_setup_state(home)["project_id"], "local-msg-vault-current1")

    def test_gcloud_reauth_error_detection(self):
        self.assertTrue(msgvault_setup.is_gcloud_reauth_error("There was a problem refreshing your current auth tokens"))
        self.assertTrue(msgvault_setup.is_gcloud_reauth_error("Reauthentication failed. cannot prompt during non-interactive execution"))
        self.assertFalse(msgvault_setup.is_gcloud_reauth_error("permission denied"))

    def test_ensure_gcloud_auth_reauths_stale_selected_account(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:3] == ["gcloud", "auth", "print-access-token"] and len(calls) == 1:
                return {"ok": False, "stdout": "", "stderr": "Reauthentication failed. cannot prompt during non-interactive execution"}
            return {"ok": True, "stdout": "token", "stderr": ""}

        with mock.patch.object(msgvault_setup.shutil, "which", return_value="/bin/gcloud"), \
            mock.patch.object(msgvault_setup, "gcloud_value", return_value="me@example.com"), \
            mock.patch.object(msgvault_setup, "run_command", side_effect=fake_run), \
            mock.patch.object(msgvault_setup, "run_visible_command", return_value={"ok": True, "returncode": 0, "message": ""}):
            result = msgvault_setup.ensure_gcloud_auth(open_browser=True)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["login_ran"])

    def test_setup_without_secret_prompts_for_oauth_json(self):
        with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(msgvault_setup, "ensure_msgvault", return_value={"installed": True, "path": "/bin/msgvault"}), \
            mock.patch.object(msgvault_setup, "init_db", return_value={"status": "ok"}), \
            mock.patch.object(msgvault_setup, "install_mcp", return_value={"status": "ok"}), \
            mock.patch.object(msgvault_setup, "gcloud_context", return_value={"project": "demo", "account": "", "installed": True}), \
            mock.patch.object(msgvault_setup, "enable_gmail_api", return_value={"status": "ok"}):
            code, payload = self.invoke([
                "setup",
                "--home",
                str(Path(tmp) / "msgvault"),
                "--email",
                "me@example.com",
                "--no-open-console",
            ])
        self.assertEqual(code, 20)
        self.assertEqual(payload["status"], "needs_user_action")
        self.assertIn("client_secret.json", payload["action"]["continue_command"])

    def test_setup_with_secret_configures_and_authorizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            secret = tmp_path / "client_secret.json"
            home = tmp_path / "msgvault"
            self.write_secret(secret)
            with mock.patch.object(msgvault_setup, "ensure_msgvault", return_value={"installed": True, "path": "/bin/msgvault"}), \
                mock.patch.object(msgvault_setup, "init_db", return_value={"status": "ok"}), \
                mock.patch.object(msgvault_setup, "install_mcp", return_value={"status": "ok"}), \
                mock.patch.object(msgvault_setup, "gcloud_context", return_value={"project": "demo", "account": "", "installed": True}), \
                mock.patch.object(msgvault_setup, "enable_gmail_api", return_value={"status": "ok"}), \
                mock.patch.object(msgvault_setup, "add_account", return_value={"status": "ok", "email": "me@example.com"}), \
                mock.patch.object(msgvault_setup, "status_payload", return_value={"status": "ok"}):
                code, payload = self.invoke([
                    "setup",
                    "--home",
                    str(home),
                    "--client-secret",
                    str(secret),
                    "--email",
                    "me@example.com",
                ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertTrue((home / "config.toml").exists())
            self.assertTrue((home / "client_secret.json").exists())
            self.assertIn("client_id", payload["configured"])

    def test_browser_setup_configures_downloaded_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            secret = tmp_path / "client_secret.json"
            home = tmp_path / "msgvault"
            self.write_secret(secret)
            with mock.patch.object(msgvault_setup, "ensure_msgvault", return_value={"installed": True, "path": "/bin/msgvault"}), \
                mock.patch.object(msgvault_setup, "ensure_gcloud_auth", return_value={"status": "ok", "account": "me@example.com"}), \
                mock.patch.object(msgvault_setup, "create_gcloud_project", return_value={"status": "ok", "project": "local-msg-vault-test", "created": True}), \
                mock.patch.object(msgvault_setup, "set_gcloud_project", return_value={"status": "ok"}), \
                mock.patch.object(msgvault_setup, "enable_gmail_api", return_value={"status": "ok"}), \
                mock.patch.object(msgvault_setup, "init_db", return_value={"status": "ok"}), \
                mock.patch.object(msgvault_setup, "install_mcp", return_value={"status": "ok"}), \
                mock.patch.object(msgvault_setup, "run_browser_automation", return_value={"status": "ok", "client_secret_path": str(secret)}), \
                mock.patch.object(msgvault_setup, "add_account", return_value={"status": "ok", "email": "me@example.com"}), \
                mock.patch.object(msgvault_setup, "status_payload", return_value={"status": "ok"}):
                code, payload = self.invoke([
                    "browser-setup",
                    "--home",
                    str(home),
                    "--project",
                    "local-msg-vault-test",
                    "--email",
                    "me@example.com",
                ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["oauth_client_name"], "local-msg-vault")
            self.assertTrue((home / "client_secret.json").exists())

    def test_browser_setup_skips_when_client_secret_is_already_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "msgvault"
            secret = home / "client_secret.json"
            home.mkdir()
            self.write_secret(secret)
            msgvault_setup.write_msgvault_config(home / "config.toml", secret)
            with mock.patch.object(msgvault_setup, "ensure_msgvault", return_value={"installed": True, "path": "/bin/msgvault"}), \
                mock.patch.object(msgvault_setup, "ensure_gcloud_auth") as auth, \
                mock.patch.object(msgvault_setup, "init_db", return_value={"status": "ok"}), \
                mock.patch.object(msgvault_setup, "install_mcp", return_value={"status": "ok"}), \
                mock.patch.object(msgvault_setup, "run_browser_automation") as browser, \
                mock.patch.object(msgvault_setup, "status_payload", return_value={"status": "ok"}):
                code, payload = self.invoke([
                    "browser-setup",
                    "--home",
                    str(home),
                    "--email",
                    "me@example.com",
                ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["browser"]["status"], "skipped")
            self.assertEqual(payload["configured"]["client_id"], "abc.apps.googleusercontent.com")
            auth.assert_not_called()
            browser.assert_not_called()

    def test_browser_setup_add_account_flag_authorizes_after_existing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "msgvault"
            secret = home / "client_secret.json"
            home.mkdir()
            self.write_secret(secret)
            msgvault_setup.write_msgvault_config(home / "config.toml", secret)
            with mock.patch.object(msgvault_setup, "ensure_msgvault", return_value={"installed": True, "path": "/bin/msgvault"}), \
                mock.patch.object(msgvault_setup, "init_db", return_value={"status": "ok"}), \
                mock.patch.object(msgvault_setup, "install_mcp", return_value={"status": "ok"}), \
                mock.patch.object(msgvault_setup, "add_account", return_value={"status": "ok", "email": "me@example.com"}), \
                mock.patch.object(msgvault_setup, "status_payload", return_value={"status": "ok"}) as _:
                code, payload = self.invoke([
                    "browser-setup",
                    "--home",
                    str(home),
                    "--email",
                    "me@example.com",
                    "--add-account",
                ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["account"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
