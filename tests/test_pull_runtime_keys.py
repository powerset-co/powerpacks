import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys as stage


class PullRuntimeKeysTests(unittest.TestCase):
    def _args(self, env_path: Path) -> argparse.Namespace:
        return argparse.Namespace(env_file=str(env_path), func=stage.cmd_pull)

    def test_write_env_upserts_and_preserves(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("# header\nEXISTING=keep\nOPENAI_API_KEY=old\n")
            written = stage.write_env(env, {"OPENAI_API_KEY": "new", "MODAL_TOKEN_ID": "ak-1"})
            text = env.read_text()
            self.assertIn("EXISTING=keep", text)          # unrelated line preserved
            self.assertIn("# header", text)               # comment preserved
            self.assertIn("OPENAI_API_KEY=new", text)     # existing key rewritten
            self.assertNotIn("OPENAI_API_KEY=old", text)
            self.assertIn("MODAL_TOKEN_ID=ak-1", text)    # new key appended
            self.assertEqual(set(written), {"OPENAI_API_KEY", "MODAL_TOKEN_ID"})
            self.assertEqual(oct(env.stat().st_mode)[-3:], "600")

    def test_pull_writes_all_keys_on_ok(self):
        def fake_fetch(base, path, token, timeout=30):
            if "modal" in path:
                return "ok", {"modal_token_id": "ak-xyz", "modal_token_secret": "as-xyz"}
            return "ok", {"openai_api_key": "sk-test"}

        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            with mock.patch.dict(os.environ, {"POWERPACKS_API_URL": "https://api.example.test"}), \
                 mock.patch.object(stage, "bearer_token", return_value="tok"), \
                 mock.patch.object(stage, "fetch_endpoint", side_effect=fake_fetch):
                code = stage.cmd_pull(self._args(env))
            text = env.read_text()
            self.assertEqual(code, 0)
            self.assertIn("MODAL_TOKEN_ID=ak-xyz", text)
            self.assertIn("MODAL_TOKEN_SECRET=as-xyz", text)
            self.assertIn("OPENAI_API_KEY=sk-test", text)

    def test_pull_handles_not_provisioned(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            with mock.patch.dict(os.environ, {"POWERPACKS_API_URL": "https://api.example.test"}), \
                 mock.patch.object(stage, "bearer_token", return_value="tok"), \
                 mock.patch.object(stage, "fetch_endpoint", return_value=("not_provisioned", None)):
                code = stage.cmd_pull(self._args(env))
            self.assertEqual(code, 2)              # nothing written -> non-zero
            self.assertFalse(env.exists())          # no .env created when nothing pulled

    def test_pull_partial_when_only_one_endpoint(self):
        def fake_fetch(base, path, token, timeout=30):
            if "modal" in path:
                return "ok", {"modal_token_id": "ak-1", "modal_token_secret": "as-1"}
            return "not_provisioned", None

        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            with mock.patch.dict(os.environ, {"POWERPACKS_API_URL": "https://api.example.test"}), \
                 mock.patch.object(stage, "bearer_token", return_value="tok"), \
                 mock.patch.object(stage, "fetch_endpoint", side_effect=fake_fetch):
                code = stage.cmd_pull(self._args(env))
            text = env.read_text()
            self.assertEqual(code, 0)               # wrote modal keys
            self.assertIn("MODAL_TOKEN_ID=ak-1", text)
            self.assertNotIn("OPENAI_API_KEY", text)

    def test_api_base_requires_explicit_env(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as cm:
                stage.api_base()
        self.assertIn("POWERPACKS_API_URL", str(cm.exception))
        self.assertIn("env.powerset.example", str(cm.exception))

    def test_cmd_pull_reports_missing_api_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            with mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch.object(stage, "bearer_token", return_value="tok") as bearer:
                code = stage.cmd_pull(self._args(env))
            self.assertEqual(code, 2)
            bearer.assert_not_called()
            self.assertFalse(env.exists())


if __name__ == "__main__":
    unittest.main()
