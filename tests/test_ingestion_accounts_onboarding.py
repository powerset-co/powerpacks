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


accounts = load_module("accounts_helper", "packs/ingestion/accounts.py")
account_registry = load_module("account_registry", "packs/ingestion/primitives/account_registry/account_registry.py")
onboarding = load_module("onboarding", "packs/ingestion/primitives/onboarding/onboarding.py")
linkedin_mcp_import = load_module("linkedin_mcp_import", "packs/ingestion/primitives/linkedin_mcp_import/linkedin_mcp_import.py")


class IngestionAccountsOnboardingTests(unittest.TestCase):
    def invoke(self, module, argv):
        buf = StringIO()
        with redirect_stdout(buf):
            code = module.main(argv)
        payload = json.loads(buf.getvalue()) if buf.getvalue().strip() else {}
        return code, payload

    def test_account_registry_init_and_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.json"
            code, payload = self.invoke(account_registry, ["init", "--path", str(path)])
            self.assertEqual(code, 0)
            self.assertTrue(path.exists())
            self.assertEqual(payload["status"], "initialized")
            code, payload = self.invoke(account_registry, [
                "mark", "--path", str(path), "--channel", "twitter", "--username", "alice", "--success",
            ])
            self.assertEqual(code, 0)
            registry = accounts.load_registry(path)
            self.assertTrue(registry["accounts"]["twitter"]["linked"])
            self.assertEqual(registry["accounts"]["twitter"]["usernames"], ["alice"])

    def test_onboarding_plan_uses_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.json"
            accounts.save_registry(accounts.default_registry(), path)
            accounts.update_channel("gmail", path=path, username="me@example.com", success=True)
            code, payload = self.invoke(onboarding, ["plan", "--accounts", str(path)])
            self.assertEqual(code, 0)
            self.assertIn("todo", payload)
            self.assertNotIn("gmail", [item["channel"] for item in payload["todo"]])

    def test_conversational_run_and_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.json"
            ledger = Path(tmp) / "onboarding-run.json"
            code, payload = self.invoke(onboarding, ["run", "--accounts", str(accounts_path), "--ledger", str(ledger), "--force"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "needs_user_input")
            self.assertEqual(payload["step"], "messages")
            code, payload = self.invoke(onboarding, ["continue", "--accounts", str(accounts_path), "--ledger", str(ledger), "--input", "skip"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["step"], "gmail")
            code, payload = self.invoke(onboarding, ["continue", "--accounts", str(accounts_path), "--ledger", str(ledger), "--input", "yes"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "needs_user_action")
            self.assertEqual(payload["step"], "gmail")
            self.assertEqual(payload["url"], "https://search.powerset.dev/gmail/connect")
            self.assertIn("--timeout-seconds 600", payload["command"])
            self.assertNotIn("--no-wait", payload["command"])

    def test_messages_yes_advances_when_contacts_already_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.json"
            ledger = Path(tmp) / "onboarding-run.json"
            code, payload = self.invoke(onboarding, ["run", "--accounts", str(accounts_path), "--ledger", str(ledger), "--force"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["step"], "messages")
            with mock.patch.object(onboarding, "artifact_exists", return_value=True):
                code, payload = self.invoke(onboarding, [
                    "continue", "--accounts", str(accounts_path), "--ledger", str(ledger), "--input", "yes",
                ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["step"], "gmail")
            self.assertEqual(payload["completed_action"]["message"], "Messages contacts already imported.")
            registry = accounts.load_registry(accounts_path)
            self.assertTrue(registry["accounts"]["messages"]["linked"])

    def test_messages_yes_returns_agent_action_when_import_needed(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.json"
            ledger = Path(tmp) / "onboarding-run.json"
            code, payload = self.invoke(onboarding, ["run", "--accounts", str(accounts_path), "--ledger", str(ledger), "--force"])
            self.assertEqual(code, 0)
            with mock.patch.object(onboarding, "artifact_exists", return_value=False):
                code, payload = self.invoke(onboarding, [
                    "continue", "--accounts", str(accounts_path), "--ledger", str(ledger), "--input", "yes",
                ])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "needs_agent_action")
            self.assertEqual(payload["step"], "messages")
            self.assertIn("import_contacts_pipeline.py run", payload["command"])

    def test_enrich_step_uses_canonical_pathless_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts_path = Path(tmp) / "accounts.json"
            ledger = Path(tmp) / "onboarding-run.json"
            merged = Path(tmp) / "people.csv"
            merged.write_text("id,full_name\n1,Jane Example\n", encoding="utf-8")
            onboarding.write_json(ledger, {
                "version": 1,
                "created_at": onboarding.now_iso(),
                "updated_at": onboarding.now_iso(),
                "index": onboarding.ONBOARDING_FLOW.index("enrich"),
                "phase": "awaiting_enrich_input",
                "answers": {},
                "skipped": [],
                "context": {},
            })
            with mock.patch.object(onboarding, "MERGED_PEOPLE_CSV", merged):
                code, payload = self.invoke(onboarding, [
                    "continue", "--accounts", str(accounts_path), "--ledger", str(ledger), "--input", "yes",
                ])
            self.assertEqual(code, 0)
            command = payload["completed_action"]["command"]
            self.assertEqual(command, "uv run --project . python packs/ingestion/primitives/enrich_people/enrich_people.py run")
            self.assertNotIn("--input", command)

    def test_linkedin_mcp_instructions_and_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.json"
            code, payload = self.invoke(linkedin_mcp_import, ["instructions", "--accounts", str(path)])
            self.assertEqual(code, 0)
            self.assertEqual(payload["mcp_config"]["mcpServers"]["linkedin"]["command"], "uvx")
            code, _ = self.invoke(linkedin_mcp_import, ["mark-linked", "--accounts", str(path), "--username", "https://www.linkedin.com/in/me"])
            self.assertEqual(code, 0)
            registry = accounts.load_registry(path)
            self.assertTrue(registry["accounts"]["linkedin_mcp"]["linked"])


if __name__ == "__main__":
    unittest.main()
