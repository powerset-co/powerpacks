"""One-command onboarding against a local HTTP service, never real credentials."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import time
import sys
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from packs.powerset.primitives.auth import auth
from packs.powerset.primitives.install.onboard import Onboarding, main
from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys as keys


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.credentials = self.root / "credentials.json"
        self.calls = []
        self.networks = [dict(id="own", name="Personal Connections", person_count=120,
                              is_personal=True, role="owner"),
                         dict(id="shared", name="Powerset", person_count=40000,
                              is_personal=False, role="member")]
        self.fail_path = None
        self.fail_code = 503
        self.count = 120
        self.missing_keys = set()
        self.empty_contacts = False
        self.blank_keys = set()
        self.operator_id = ""
        self.reject_key_once = False
        self.reject_old_token = False
        self.owner_id = "auth0|operator"
        case = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                case.calls.append(("GET", self.path, None))
                if self.path == case.fail_path:
                    return self.reply({"detail": "mock failure"}, case.fail_code)
                if case.reject_key_once and self.path == "/v2/integrations/openai/key":
                    case.reject_key_once = False
                    return self.reply({"detail": "expired"}, 401)
                if case.reject_old_token and self.headers.get("Authorization") == "Bearer old-token":
                    return self.reply({"detail": "expired"}, 401)
                if self.path == "/v2/team/me":
                    account = {"user_id": "auth0|operator", "email": "jordan@example.com"}
                    return self.reply({**account, "operator_id": case.operator_id} if case.operator_id else account)
                if self.path == "/v2/sets":
                    return self.reply(case.networks)
                if self.path.startswith("/v2/sets/"):
                    return self.reply({"members": [{"user_id": case.owner_id, "role": "owner"}]})
                if self.path.startswith("/v2/set-contacts/"):
                    return self.reply({"leads": [] if case.empty_contacts else [{"id": "person-one"}]})
                fields = {field: "  " if key in case.blank_keys else "mock-value" for key, (path, field) in keys.KEY_SOURCES.items()
                          if path == self.path and key not in case.missing_keys}
                return self.reply(fields, 200 if fields else 404)

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                case.calls.append(("POST", self.path, payload))
                if self.path == case.fail_path:
                    return self.reply({"detail": "mock failure"}, case.fail_code)
                if self.path == "/oauth/token":
                    return self.reply({"access_token": "refreshed-token", "expires_in": 3600})
                if self.path == "/v2/search/count":
                    return self.reply({"count": case.count})
                return self.reply({}, 404)

            def reply(self, payload, status=200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        base = f"http://127.0.0.1:{self.server.server_port}"
        (self.root / ".env").write_text(
            f"POWERSET_API_URL={base}\nPOWERPACKS_MCP_URL={base}/mcp/\n"
            f"POWERPACKS_CREDENTIALS_PATH={self.credentials}\n"
            f"POWERPACKS_AUTH0_DOMAIN={base}\nPOWERPACKS_AUTH0_CLIENT_ID=mock-client\n"
            "POWERPACKS_AUTH0_AUDIENCE=mock-audience\n")
        self.save_credentials()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.mcp = patch("packs.powerset.primitives.install.onboard.mcp_install.codex_install",
                         return_value={"ok": True})
        self.mcp.start()

    def tearDown(self):
        self.mcp.stop()
        self.environment.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def save_credentials(self, *, expired=False):
        auth._save_credentials(self.credentials, {"access_token": "old-token", "refresh_token": "mock-refresh",
            "expires_at": time.time() + (-100 if expired else 3600), "email": "stale@example.com"})

    def run_onboarding(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = Onboarding(self.root, harnesses=["codex"], pid=os.getpid(),
                              retry_command="bin/bootstrap --powerset --no-tools").run()
        manifest = json.loads((self.root / ".powerpacks/install/manifest.json").read_text())
        self.assertNotIn("old-token", output.getvalue())
        self.assertNotIn("mock-value", output.getvalue())
        return code, manifest, output.getvalue()

    def test_warm_account_verifies_real_scoped_access_before_ready(self):
        code, state, output = self.run_onboarding()
        self.assertEqual(code, 0, output)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["account_email"], "jordan@example.com")
        self.assertEqual(state["steps"]["account"]["status"], "skipped")
        self.assertEqual(state["person_count"], 120)
        self.assertIn("POWERPACKS_DEFAULT_SET_ID=own", (self.root / ".env").read_text())
        self.assertIn(("GET", "/v2/set-contacts/own?page_size=1", None), self.calls)
        count_call = next(call for call in self.calls if call[1] == "/v2/search/count")
        self.assertEqual(count_call[2], {"set_id": "own", "is_current": True,
                                      "search_summary": False, "search_company_signal": False})

    def test_expired_account_refreshes_without_browser(self):
        self.save_credentials(expired=True)
        with patch.object(auth, "cmd_login") as login:
            code, _, _ = self.run_onboarding()
        self.assertEqual(code, 0)
        login.assert_not_called()
        self.assertTrue(any(call[1] == "/oauth/token" for call in self.calls))

    def test_missing_login_waits_then_continues_automatically(self):
        self.credentials.unlink()
        def login(args):
            state = json.loads((self.root / ".powerpacks/install/manifest.json").read_text())
            self.assertEqual((state["step"], state["status"]), ("account", "waiting"))
            self.save_credentials()
            return 0
        with patch.object(auth, "cmd_login", side_effect=login):
            code, state, _ = self.run_onboarding()
        self.assertEqual(code, 0)
        self.assertEqual(state["steps"]["account"]["status"], "completed")

    def test_rejected_cached_token_opens_login_once(self):
        self.reject_old_token = True
        def login(args):
            auth._save_credentials(self.credentials, {"access_token": "new-token"})
            return 0
        with patch.object(auth, "cmd_login", side_effect=login) as login_mock:
            code, _, _ = self.run_onboarding()
        self.assertEqual(code, 0)
        login_mock.assert_called_once()

    def test_expired_browser_link_renews_in_the_same_process(self):
        self.credentials.unlink()
        attempts = []
        def login(args):
            attempts.append(args)
            if len(attempts) == 1:
                print(json.dumps({"status": "failed", "error": "login timed out"}))
                return 1
            self.save_credentials()
            return 0
        with patch.object(auth, "cmd_login", side_effect=login):
            code, _, _ = self.run_onboarding()
        self.assertEqual(code, 0)
        self.assertEqual(len(attempts), 2)

    def test_login_timeout_is_failed_and_can_be_retried(self):
        self.credentials.unlink()
        with patch.object(auth, "cmd_login", return_value=1):
            code, state, output = self.run_onboarding()
        self.assertEqual(code, 1)
        self.assertEqual(state["status"], "failed")
        self.assertTrue(output.startswith("FAILED:"))
        self.assertEqual(self.calls, [])

    def test_zero_personal_does_not_switch_to_largest(self):
        self.networks[0]["person_count"] = 0
        code, state, _ = self.run_onboarding()
        self.assertEqual(code, 10)
        self.assertIn("jordan@example.com has 0 people", state["message"])
        self.assertEqual(state["action"]["details"]["alternative"]["person_count"], 40000)
        self.assertIn("POWERPACKS_DEFAULT_SET_ID=own", (self.root / ".env").read_text())
        self.assertFalse(any(call[0] == "POST" for call in self.calls))

    def test_all_empty_networks_explain_account_problem(self):
        for network in self.networks:
            network["person_count"] = 0
        code, state, _ = self.run_onboarding()
        self.assertEqual(code, 10)
        self.assertIn("Every network for jordan@example.com has 0 people", state["message"])

    def test_small_personal_network_is_ready_with_nonblocking_advice(self):
        self.networks[0]["person_count"] = 4
        code, state, _ = self.run_onboarding()
        self.assertEqual(code, 0)
        self.assertIn("small network", state["message"])

    def test_existing_accessible_default_is_preserved(self):
        with (self.root / ".env").open("a") as stream:
            stream.write("POWERPACKS_DEFAULT_SET_ID=shared\n")
        code, state, _ = self.run_onboarding()
        self.assertEqual(code, 0)
        self.assertEqual(state["network_name"], "Powerset")

    def test_stale_default_uses_verified_owned_personal_network(self):
        with (self.root / ".env").open("a") as stream:
            stream.write("POWERPACKS_DEFAULT_SET_ID=old-account-network\n")
        code, state, _ = self.run_onboarding()
        self.assertEqual(code, 0)
        self.assertEqual(state["network_name"], "Personal Network")
        self.assertIn("POWERPACKS_DEFAULT_SET_ID=own", (self.root / ".env").read_text())

    def run_install_command(self, *source_args):
        argv = ["onboard", "--root", str(self.root), "--harness", "codex", *source_args]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()), \
                patch("packs.powerset.primitives.install.onboard.SourceOnboarding") as sources, \
                patch("packs.powerset.primitives.install.onboard.start_server",
                      return_value={"url": "http://localhost:8899/install"}):
            sources.return_value.run.return_value = {"status": "waiting", "step": "deep_context",
                                                     "message": "Gmail: 1 contacts"}
            sources.return_value.retry_command = "bin/onboard"
            sources.return_value.gmail_emails = ("jordan@example.com",)
            with self.assertRaises(SystemExit) as result:
                main()
        return result.exception.code, sources

    def test_install_command_continues_into_imports_without_agent_handoff(self):
        code, sources = self.run_install_command()
        self.assertEqual(code, 10)
        sources.return_value.run.assert_called_once()

    def test_install_command_preserves_explicit_gmail_identity_and_history(self):
        code, sources = self.run_install_command("--gmail-email", "personal@gmail.com", "--sync-after", "2025-10-03")
        self.assertEqual(code, 10)
        self.assertEqual(sources.call_args.kwargs["gmail_emails"], ("personal@gmail.com",))
        self.assertEqual(sources.call_args.kwargs["sync_after"], "2025-10-03")

    def test_unindexed_hosted_network_does_not_block_local_imports(self):
        self.count = 0
        code, sources = self.run_install_command()
        self.assertEqual(code, 10)
        sources.return_value.run.assert_called_once()

    def test_empty_hosted_network_does_not_block_local_imports(self):
        self.networks[0]["person_count"] = 0
        code, sources = self.run_install_command()
        self.assertEqual(code, 10)
        sources.return_value.run.assert_called_once()

    def test_unprovisioned_hosted_search_does_not_block_local_imports(self):
        self.missing_keys.add("OPENAI_API_KEY")
        code, sources = self.run_install_command()
        self.assertEqual(code, 10)
        sources.return_value.run.assert_called_once()

    def test_failed_account_login_does_not_start_imports(self):
        self.credentials.unlink()
        with patch.object(auth, "cmd_login", return_value=1):
            code, sources = self.run_install_command()
        self.assertEqual(code, 1)
        sources.return_value.run.assert_not_called()

    def test_unknown_default_and_wrong_personal_owner_require_choice(self):
        self.owner_id = "auth0|someone-else"
        code, state, _ = self.run_onboarding()
        self.assertEqual(code, 10)
        self.assertIn("could not be confirmed", state["message"])
        with (self.root / ".env").open("a") as stream:
            stream.write("POWERPACKS_DEFAULT_SET_ID=not-accessible\n")
        code, state, _ = self.run_onboarding()
        self.assertEqual(code, 10)

    def test_catalog_count_does_not_claim_index_readiness(self):
        self.count = 0
        code, state, _ = self.run_onboarding()
        self.assertEqual(code, 10)
        self.assertIn("profiles are not ready", state["message"])
        self.assertEqual(state["person_count"], 120)

    def test_mcp_registration_failure_uses_verified_direct_connection(self):
        with patch("packs.powerset.primitives.install.onboard.mcp_install.codex_install",
                   return_value={"ok": False}):
            code, state, _ = self.run_onboarding()
        self.assertEqual(code, 0)
        self.assertEqual(state["steps"]["connection"]["status"], "skipped")

    def test_missing_required_access_waits_but_optional_providers_do_not(self):
        self.missing_keys = {"MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "PARALLEL_API_KEY"}
        code, _, _ = self.run_onboarding()
        self.assertEqual(code, 0)
        self.missing_keys.add("OPENAI_API_KEY")
        env = self.root / ".env"
        env.write_text("".join(line for line in env.read_text().splitlines(keepends=True)
                               if not line.startswith("OPENAI_API_KEY=")))
        code, state, _ = self.run_onboarding()
        self.assertEqual(code, 10)
        self.assertEqual(state["step"], "credentials")

    def test_required_credential_outage_fails_without_provisioning_advice(self):
        self.fail_path = "/v2/integrations/openai/key"
        code, state, output = self.run_onboarding()
        self.assertEqual(code, 1)
        self.assertEqual(state["status"], "failed")
        self.assertIn("503", output)
        self.assertNotIn("provision", state["message"])
        self.assertNotIn("finish enabling", output)

    def test_optional_credential_outage_does_not_block_search(self):
        self.fail_path = "/v2/integrations/parallel/key"
        code, state, _ = self.run_onboarding()
        self.assertEqual(code, 0)
        self.assertEqual(state["status"], "completed")

    def test_credential_401_reconnects_and_verifies_account_before_retry(self):
        self.reject_key_once = True
        def login(args):
            self.save_credentials()
            return 0
        with patch.object(auth, "cmd_login", side_effect=login) as login_mock:
            code, _, _ = self.run_onboarding()
        self.assertEqual(code, 0)
        login_mock.assert_called_once()
        self.assertEqual(sum(call[1] == "/v2/team/me" for call in self.calls), 2)
        self.assertEqual(sum(call[1] == "/v2/integrations/openai/key" for call in self.calls), 2)

    def test_blank_required_credential_is_not_usable(self):
        self.blank_keys.add("OPENAI_API_KEY")
        code, state, _ = self.run_onboarding()
        self.assertEqual(code, 10)
        self.assertEqual(state["event"], "credentials.not_provisioned")

    def test_keys_already_in_env_count_when_the_account_returns_none(self):
        self.blank_keys.update({"TURBOPUFFER_API_KEY", "DATABASE_URL"})
        env = self.root / ".env"
        with env.open("a") as stream:
            stream.write("\nTURBOPUFFER_API_KEY=synthetic-local-key\nDATABASE_URL=synthetic-local-db\n")
        code, state, _ = self.run_onboarding()
        self.assertEqual(code, 0)
        self.assertEqual(state["steps"]["credentials"]["status"], "completed")
        self.assertIn("TURBOPUFFER_API_KEY=synthetic-local-key", env.read_text())

    def test_the_accounts_operator_id_is_saved_for_modal_runs(self):
        self.operator_id = "33333333-3333-3333-3333-333333333333"
        code, _, _ = self.run_onboarding()
        self.assertEqual(code, 0)
        self.assertIn("POWERPACKS_OPERATOR_ID=33333333-3333-3333-3333-333333333333", (self.root / ".env").read_text())

    def test_unprovisioned_hosted_search_is_a_warning_and_setup_continues(self):
        self.blank_keys.update({"TURBOPUFFER_API_KEY", "DATABASE_URL"})
        code, state, _ = self.run_onboarding()
        self.assertEqual(code, 10)
        self.assertEqual(state["steps"]["credentials"]["status"], "skipped")
        self.assertIn("Hosted search isn't enabled", state["steps"]["credentials"]["message"])

    def test_service_failure_is_failed_and_not_mistaken_for_login(self):
        self.fail_path = "/v2/search/count"
        with patch.object(auth, "cmd_login") as login:
            code, state, output = self.run_onboarding()
        self.assertEqual(code, 1)
        self.assertEqual(state["status"], "failed")
        self.assertIn("503", output)
        login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
