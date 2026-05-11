import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

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
