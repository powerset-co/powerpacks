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
    def test_update_fills_missing_or_empty_typesafe_key_with_one_bearer(self):
        for original in (
            "KEEP=yes\n",
            "KEEP=yes\nTYPESAFE_API_KEY=\n",
            "KEEP=yes\nexport TYPESAFE_API_KEY=\n",
            "KEEP=yes\nTYPESAFE_API_KEY =   \n",
        ):
            with self.subTest(original=original), tempfile.TemporaryDirectory() as tmp:
                env = Path(tmp) / ".env"
                env.write_text(original)

                def fake_fetch(_base, path, _token, timeout=30):
                    if "typesafe" in path:
                        return "ok", {"typesafe_api_key": "typesafe-test"}
                    return "ok", {"powerset_api_key": "gateway-test"}

                with mock.patch.object(stage, "bearer_token", return_value="tok") as bearer, \
                     mock.patch.object(stage, "fetch_endpoint", side_effect=fake_fetch):
                    result = stage.refresh_update_keys(env)
                values = stage._read_env_file(env)
                self.assertEqual(values["TYPESAFE_API_KEY"], "typesafe-test")
                self.assertEqual(values["POWERSET_API_KEY"], "gateway-test")
                self.assertEqual(result["typesafe_api_key"], "installed")
                self.assertEqual(env.read_text().count("TYPESAFE_API_KEY"), 1)
                bearer.assert_called_once_with(env)

    def test_update_preserves_nonempty_typesafe_key_without_fetching_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("export TYPESAFE_API_KEY=personal\n")
            with mock.patch.object(stage, "bearer_token", return_value="tok"), \
                 mock.patch.object(stage, "fetch_endpoint", return_value=(
                     "ok", {"powerset_api_key": "gateway-test"})) as fetch:
                result = stage.refresh_update_keys(env)
            self.assertEqual(stage._read_env_file(env)["TYPESAFE_API_KEY"], "personal")
            self.assertEqual(result["typesafe_api_key"], "preserved")
            self.assertEqual([call.args[1] for call in fetch.call_args_list], [
                "/v2/integrations/powerset-api/key",
            ])

    def test_update_rejects_empty_or_non_string_typesafe_response(self):
        for key in ("", "   ", 123, None):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                env = Path(tmp) / ".env"

                def fake_fetch(_base, path, _token, timeout=30):
                    if "typesafe" in path:
                        return "ok", {"typesafe_api_key": key}
                    return "not_provisioned", None

                with mock.patch.object(stage, "bearer_token", return_value="tok"), \
                     mock.patch.object(stage, "fetch_endpoint", side_effect=fake_fetch):
                    result = stage.refresh_update_keys(env)
                self.assertFalse(env.exists())
                self.assertEqual(result["typesafe_api_key"], "error")

    def test_update_signed_out_or_unavailable_leaves_typesafe_setting_intact(self):
        for state in ("signed_out", "not_provisioned", "error"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                env = Path(tmp) / ".env"
                original = "KEEP=yes\nTYPESAFE_API_KEY=\n"
                env.write_text(original)
                bearer = mock.patch.object(stage, "bearer_token", return_value="tok")
                fetch = mock.patch.object(stage, "fetch_endpoint", return_value=(state, None))
                if state == "signed_out":
                    bearer = mock.patch.object(stage, "bearer_token", side_effect=SystemExit("signed out"))
                with bearer, fetch as fetch_endpoint:
                    result = stage.refresh_update_keys(env)
                self.assertEqual(env.read_text(), original)
                expected = "not_signed_in" if state == "signed_out" else state
                self.assertEqual(result["typesafe_api_key"], expected)
                if state == "signed_out":
                    fetch_endpoint.assert_not_called()

    def test_update_typesafe_pull_is_independent_of_gateway_provisioning(self):
        def fake_fetch(_base, path, _token, timeout=30):
            if "typesafe" in path:
                return "ok", {"typesafe_api_key": "typesafe-test"}
            return "not_provisioned", None

        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            with mock.patch.object(stage, "bearer_token", return_value="tok"), \
                 mock.patch.object(stage, "fetch_endpoint", side_effect=fake_fetch):
                result = stage.refresh_update_keys(env)
            self.assertEqual(stage._read_env_file(env)["TYPESAFE_API_KEY"], "typesafe-test")
            self.assertEqual(result["powerset_api_key_refresh"], "not_provisioned")
            self.assertEqual(result["typesafe_api_key"], "installed")

    def test_refresh_cross_encoder_updates_only_gateway_key_and_defaults_ce_on(self):
        for preference in (None, "0", "1"):
            with self.subTest(preference=preference), tempfile.TemporaryDirectory() as tmp:
                env = Path(tmp) / ".env"
                original = "# keep\nOPENAI_API_KEY=personal\nPOWERSET_API_KEY=old\n"
                if preference is not None:
                    original += f"POWERPACKS_CROSS_ENCODER_BETA={preference}\n"
                env.write_text(original)
                with mock.patch.object(stage, "bearer_token", return_value="tok") as bearer, \
                     mock.patch.object(stage, "fetch_endpoint", return_value=(
                         "ok", {"powerset_api_key": "refreshed-test-key"})) as fetch:
                    result = stage.refresh_cross_encoder(env)
                values = stage._read_env_file(env)
                self.assertEqual(values["POWERSET_API_KEY"], "refreshed-test-key")
                self.assertEqual(values["OPENAI_API_KEY"], "personal")
                self.assertEqual(values["POWERPACKS_CROSS_ENCODER_BETA"], preference or "1")
                self.assertIn("# keep", env.read_text())
                self.assertEqual(env.stat().st_mode & 0o777, 0o600)
                self.assertEqual(result, {
                    "powerset_api_key_refresh": "refreshed",
                    "cross_encoder": "disabled" if preference == "0" else "enabled",
                })
                bearer.assert_called_once_with(env)
                self.assertEqual(fetch.call_args.args[1:], (
                    "/v2/integrations/powerset-api/key", "tok"))

    def test_refresh_cross_encoder_leaves_settings_when_key_unavailable(self):
        for state, payload in (("not_provisioned", None), ("error", None), ("ok", {})):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                env = Path(tmp) / ".env"
                original = "POWERSET_API_KEY=existing\n"
                env.write_text(original)
                with mock.patch.object(stage, "bearer_token", return_value="tok"), \
                     mock.patch.object(stage, "fetch_endpoint", return_value=(state, payload)):
                    result = stage.refresh_cross_encoder(env)
                self.assertEqual(env.read_text(), original)
                self.assertEqual(result["powerset_api_key_refresh"],
                                 "error" if state == "ok" else state)
                self.assertEqual(result["cross_encoder"], "disabled")

    def test_refresh_cross_encoder_signed_out_does_not_create_env_or_call_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            with mock.patch.object(stage, "bearer_token", side_effect=SystemExit("signed out")), \
                 mock.patch.object(stage, "fetch_endpoint") as fetch:
                result = stage.refresh_cross_encoder(env)
            self.assertEqual(result["powerset_api_key_refresh"], "not_signed_in")
            self.assertFalse(env.exists())
            fetch.assert_not_called()

    def test_refresh_cross_encoder_handles_read_failure_without_changing_env(self):
        for failure in (TimeoutError(), ConnectionResetError(),
                        json.JSONDecodeError("invalid response", "", 0)):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as tmp:
                env = Path(tmp) / ".env"
                original = "POWERSET_API_KEY=existing\n"
                env.write_text(original)
                with mock.patch.object(stage, "bearer_token", return_value="tok"), \
                     mock.patch.object(stage.urllib.request, "urlopen") as open_url:
                    open_url.return_value.__enter__.return_value.read.side_effect = failure
                    result = stage.refresh_cross_encoder(env)
                self.assertEqual(result["powerset_api_key_refresh"], "error")
                self.assertEqual(env.read_text(), original)

    def test_bearer_token_loads_auth_config_from_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("POWERPACKS_AUTH0_DOMAIN=auth.example.test\n")
            with mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch.object(stage.subprocess, "run", return_value=mock.Mock(
                     returncode=0, stdout="token")) as run:
                self.assertEqual(stage.bearer_token(env), "token")
            self.assertEqual(run.call_args.kwargs["env"]["POWERPACKS_AUTH0_DOMAIN"],
                             "auth.example.test")

    def _args(self, env_path: Path) -> argparse.Namespace:
        return argparse.Namespace(env_file=str(env_path), func=stage.cmd_pull)

    def test_check_requires_nonempty_values_and_normalizes_assignments(self):
        cases = (
            ("TYPESAFE_API_KEY=\n", False),
            ("export TYPESAFE_API_KEY =   \n", False),
            ("export TYPESAFE_API_KEY = provisioned\n", True),
        )
        for content, expected_present in cases:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmp:
                env = Path(tmp) / ".env"
                env.write_text(content)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    stage.cmd_check(argparse.Namespace(env_file=str(env)))
                payload = json.loads(output.getvalue())
                self.assertEqual("TYPESAFE_API_KEY" in payload["have"], expected_present)
                self.assertEqual("TYPESAFE_API_KEY" in payload["missing"], not expected_present)

        for key in stage.KEY_SOURCES:
            with self.subTest(empty_key=key), tempfile.TemporaryDirectory() as tmp:
                env = Path(tmp) / ".env"
                env.write_text(f"{key}=\n")
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    stage.cmd_check(argparse.Namespace(env_file=str(env)))
                self.assertIn(key, json.loads(output.getvalue())["missing"])

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
            if "typesafe" in path:
                return "ok", {"typesafe_api_key": "typesafe-test"}
            return "ok", {"powerset_api_key": "powerset-test"}

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
            self.assertIn("POWERSET_API_KEY=powerset-test", text)
            self.assertIn("TYPESAFE_API_KEY=typesafe-test", text)

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

    def test_pull_adds_powerset_api_key_to_existing_env(self):
        def fake_fetch(base, path, token, timeout=30):
            if "powerset-api" in path:
                return "ok", {"powerset_api_key": "powerset-test"}
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
            self.assertIn("POWERSET_API_KEY=powerset-test", text)

    def test_powerset_api_key_uses_gateway_endpoint(self):
        self.assertEqual(
            stage.KEY_SOURCES["POWERSET_API_KEY"],
            ("/v2/integrations/powerset-api/key", "powerset_api_key"),
        )
        self.assertNotIn("RAPIDAPI_LINKEDIN_KEY", stage.KEY_SOURCES)

    def test_typesafe_api_key_uses_dedicated_endpoint(self):
        self.assertEqual(
            stage.KEY_SOURCES["TYPESAFE_API_KEY"],
            ("/v2/integrations/typesafe/key", "typesafe_api_key"),
        )

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
