import argparse
import contextlib
import io
import json
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
            if "openai" in path:
                return "ok", {"openai_api_key": "sk-test"}
            if "parallel" in path:
                return "ok", {"parallel_api_key": "parallel-test"}
            return "ok", {"rapidapi_linkedin_key": "rapidapi-test"}

        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            with mock.patch.dict(os.environ, {"POWERSET_API_URL": "https://api.example.test"}, clear=True), \
                 mock.patch.object(stage, "bearer_token", return_value="tok"), \
                 mock.patch.object(stage, "fetch_endpoint", side_effect=fake_fetch):
                code = stage.cmd_pull(self._args(env))
            text = env.read_text()
            self.assertEqual(code, 0)
            self.assertIn("MODAL_TOKEN_ID=ak-xyz", text)
            self.assertIn("MODAL_TOKEN_SECRET=as-xyz", text)
            self.assertIn("OPENAI_API_KEY=sk-test", text)
            self.assertIn("PARALLEL_API_KEY=parallel-test", text)
            self.assertIn("RAPIDAPI_LINKEDIN_KEY=rapidapi-test", text)

    def test_pull_adds_parallel_to_existing_env(self):
        def fake_fetch(base, path, token, timeout=30):
            if "parallel" in path:
                return "ok", {"parallel_api_key": "parallel-test"}
            return "not_provisioned", None

        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text(
                "MODAL_TOKEN_ID=ak-existing\n"
                "MODAL_TOKEN_SECRET=as-existing\n"
                "OPENAI_API_KEY=sk-existing\n"
            )
            with mock.patch.dict(os.environ, {"POWERSET_API_URL": "https://api.example.test"}, clear=True), \
                 mock.patch.object(stage, "bearer_token", return_value="tok"), \
                 mock.patch.object(stage, "fetch_endpoint", side_effect=fake_fetch):
                code = stage.cmd_pull(self._args(env))
            text = env.read_text()
            self.assertEqual(code, 0)
            self.assertIn("MODAL_TOKEN_ID=ak-existing", text)
            self.assertIn("MODAL_TOKEN_SECRET=as-existing", text)
            self.assertIn("OPENAI_API_KEY=sk-existing", text)
            self.assertIn("PARALLEL_API_KEY=parallel-test", text)

    def test_pull_adds_rapidapi_to_existing_env(self):
        def fake_fetch(base, path, token, timeout=30):
            if "rapidapi" in path:
                return "ok", {"rapidapi_linkedin_key": "rapidapi-test"}
            return "not_provisioned", None

        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text(
                "MODAL_TOKEN_ID=ak-existing\n"
                "MODAL_TOKEN_SECRET=as-existing\n"
                "OPENAI_API_KEY=sk-existing\n"
                "PARALLEL_API_KEY=parallel-existing\n"
            )
            with mock.patch.dict(os.environ, {"POWERSET_API_URL": "https://api.example.test"}, clear=True), \
                 mock.patch.object(stage, "bearer_token", return_value="tok"), \
                 mock.patch.object(stage, "fetch_endpoint", side_effect=fake_fetch):
                code = stage.cmd_pull(self._args(env))
            text = env.read_text()
            self.assertEqual(code, 0)
            self.assertIn("RAPIDAPI_LINKEDIN_KEY=rapidapi-test", text)

    def test_pull_handles_not_provisioned(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            with mock.patch.dict(os.environ, {"POWERSET_API_URL": "https://api.example.test"}, clear=True), \
                 mock.patch.object(stage, "bearer_token", return_value="tok"), \
                 mock.patch.object(stage, "fetch_endpoint", return_value=("not_provisioned", None)):
                code = stage.cmd_pull(self._args(env))
            self.assertEqual(code, 2)              # nothing written -> non-zero
            self.assertFalse(env.exists())          # no .env created when nothing pulled

    def test_pull_reports_endpoint_errors_as_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            output = io.StringIO()
            with mock.patch.dict(os.environ, {
                "POWERSET_API_URL": "https://search-api.example.test",
            }, clear=True), \
                 mock.patch.object(stage, "bearer_token", return_value="tok"), \
                 mock.patch.object(stage, "fetch_endpoint", return_value=("error", {"http_status": 502})), \
                 contextlib.redirect_stdout(output):
                code = stage.cmd_pull(self._args(env))
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["status"], "error")
            self.assertFalse(env.exists())

    def test_pull_partial_when_only_one_endpoint(self):
        def fake_fetch(base, path, token, timeout=30):
            if "modal" in path:
                return "ok", {"modal_token_id": "ak-1", "modal_token_secret": "as-1"}
            return "not_provisioned", None

        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            with mock.patch.dict(os.environ, {"POWERSET_API_URL": "https://api.example.test"}, clear=True), \
                 mock.patch.object(stage, "bearer_token", return_value="tok"), \
                 mock.patch.object(stage, "fetch_endpoint", side_effect=fake_fetch):
                code = stage.cmd_pull(self._args(env))
            text = env.read_text()
            self.assertEqual(code, 0)               # wrote modal keys
            self.assertIn("MODAL_TOKEN_ID=ak-1", text)
            self.assertNotIn("OPENAI_API_KEY", text)

    def test_operator_id_key_maps_to_me_endpoint_and_is_pending(self):
        # Contract for the not-yet-live server endpoint; self-activates on ship.
        self.assertEqual(stage.KEY_SOURCES["POWERPACKS_OPERATOR_ID"], ("/v2/me", "operator_id"))
        self.assertIn("POWERPACKS_OPERATOR_ID", stage.PENDING_KEYS)

    def test_pull_stays_ok_while_operator_endpoint_is_dark(self):
        def fake_fetch(base, path, token, timeout=30):
            if path == "/v2/me":
                return "not_provisioned", None
            if "modal" in path:
                return "ok", {"modal_token_id": "ak-1", "modal_token_secret": "as-1"}
            if "parallel" in path:
                return "ok", {"parallel_api_key": "pk-test"}
            if "rapidapi" in path:
                return "ok", {"rapidapi_linkedin_key": "rk-test"}
            return "ok", {"openai_api_key": "sk-test"}

        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"POWERSET_API_URL": "https://api.example.test"}, clear=True), \
                 mock.patch.object(stage, "bearer_token", return_value="tok"), \
                 mock.patch.object(stage, "fetch_endpoint", side_effect=fake_fetch), \
                 contextlib.redirect_stdout(output):
                code = stage.cmd_pull(self._args(env))
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")               # pending never degrades status
            self.assertEqual(payload["missing"], [])
            self.assertEqual(payload["pending"], ["POWERPACKS_OPERATOR_ID"])
            self.assertNotIn("POWERPACKS_OPERATOR_ID", env.read_text())

    def test_pull_writes_operator_id_once_endpoint_ships(self):
        operator_id = "11111111-1111-4111-8111-111111111111"

        def fake_fetch(base, path, token, timeout=30):
            if path == "/v2/me":
                return "ok", {"operator_id": operator_id}
            if "modal" in path:
                return "ok", {"modal_token_id": "ak-1", "modal_token_secret": "as-1"}
            if "parallel" in path:
                return "ok", {"parallel_api_key": "pk-test"}
            if "rapidapi" in path:
                return "ok", {"rapidapi_linkedin_key": "rk-test"}
            return "ok", {"openai_api_key": "sk-test"}

        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"POWERSET_API_URL": "https://api.example.test"}, clear=True), \
                 mock.patch.object(stage, "bearer_token", return_value="tok"), \
                 mock.patch.object(stage, "fetch_endpoint", side_effect=fake_fetch), \
                 contextlib.redirect_stdout(output):
                code = stage.cmd_pull(self._args(env))
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["pending"], [])
            self.assertIn(f"POWERPACKS_OPERATOR_ID={operator_id}", env.read_text())

    def test_check_reports_pending_operator_id_without_failing(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("MODAL_TOKEN_ID=ak-1\nMODAL_TOKEN_SECRET=as-1\nOPENAI_API_KEY=sk-test\n"
                           "PARALLEL_API_KEY=pk-test\nRAPIDAPI_LINKEDIN_KEY=rk-test\n")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = stage.cmd_check(argparse.Namespace(env_file=str(env), func=stage.cmd_check))
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["missing"], [])
            self.assertEqual(payload["pending"], ["POWERPACKS_OPERATOR_ID"])

    def test_api_base_defaults_to_hosted(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(stage.api_base(), stage.DEFAULT_API_BASE)

    def test_api_base_env_var_beats_default(self):
        with mock.patch.dict(os.environ, {
            "POWERSET_API_URL": "https://search-api.example.test/",
        }, clear=True):
            self.assertEqual(stage.api_base(), "https://search-api.example.test")

    def test_api_base_ignores_retired_aliases(self):
        with mock.patch.dict(os.environ, {
            "POWERPACKS_SEARCH_API_URL": "https://alias.example.test",
        }, clear=True):
            self.assertEqual(stage.api_base(), stage.DEFAULT_API_BASE)

    def test_api_base_reads_the_pull_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("POWERSET_API_URL=https://search-api.example.test/\n")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(stage.api_base(env), "https://search-api.example.test")

    def test_cmd_pull_uses_default_base_without_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            with mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch.object(stage, "bearer_token", return_value="tok"), \
                 mock.patch.object(stage, "fetch_endpoint",
                                   return_value=("not_provisioned", None)) as fetch:
                stage.cmd_pull(self._args(env))
            self.assertEqual(fetch.call_args[0][0], stage.DEFAULT_API_BASE)


if __name__ == "__main__":
    unittest.main()
