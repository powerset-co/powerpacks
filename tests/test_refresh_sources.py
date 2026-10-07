import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from packs.ingestion.primitives.refresh import refresh_sources
from packs.ingestion.primitives.setup.automations import accounts


def health(*, healthy=(), expired=(), missing=(), errors=()):
    return {"healthy_accounts": list(healthy), "expired_accounts": list(expired),
            "accounts_to_authorize": [*expired, *missing], "error_accounts": list(errors)}


class GmailRefreshAuthorizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        for patcher in (
            patch.object(refresh_sources, "MSGVAULT_HOME", self.home),
            patch.object(refresh_sources, "_msgvault_emails", return_value=["casey@example.com", "jordan@example.com"]),
            patch.object(refresh_sources, "resolve_discovery_inputs", return_value=SimpleNamespace(
                msgvault_db=self.home / "archive.db", sync_query="after:2025/01/01")),
            patch.object(refresh_sources.GmailDiscovery, "__init__", return_value=None),
            patch.object(refresh_sources.GmailDiscovery, "run", return_value=SimpleNamespace(status="completed")),
            patch.object(refresh_sources.GmailImport, "__init__", return_value=None),
            patch.object(refresh_sources.GmailImport, "run", return_value=SimpleNamespace(status="completed")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(refresh_sources, "sync_msgvault_account", return_value={"status": "completed"})
        self.sync = patcher.start()
        self.addCleanup(patcher.stop)

    def test_expired_stored_mailbox_gets_one_renewal_then_health_recheck(self):
        before = health(healthy=["jordan@example.com"], expired=["casey@example.com"])
        after = health(healthy=["casey@example.com", "jordan@example.com"])
        with patch.object(accounts, "check_accounts_payload", side_effect=[before, after]) as check, \
            patch.object(accounts, "add_account", return_value={"status": "ok"}) as authorize:
            result = refresh_sources._sync_gmail(None)
        authorize.assert_called_once_with(self.home, "casey@example.com", "", headless=False, force=True)
        self.assertEqual(check.call_count, 2)
        self.assertEqual([item.outcome for item in result], ["refreshed"])
        self.assertEqual(self.sync.call_count, 2)

    def test_failed_renewal_keeps_healthy_sync_and_rebuilds_all_archived_accounts(self):
        check_payload = health(healthy=["jordan@example.com"], expired=["casey@example.com"])
        with patch.object(accounts, "check_accounts_payload", return_value=check_payload) as check, \
            patch.object(accounts, "add_account", return_value={"status": "needs_user_action"}) as authorize, \
            patch.object(refresh_sources.GmailDiscovery, "__init__", return_value=None) as discovery:
            result = refresh_sources._sync_gmail(None)
        authorize.assert_called_once()
        self.assertEqual(check.call_count, 2)
        self.sync.assert_called_once_with("jordan@example.com", self.home / "archive.db", "after:2025/01/01")
        discovery.assert_called_once_with(account_emails=["casey@example.com", "jordan@example.com"], skip_msgvault_sync=True)
        self.assertEqual([item.outcome for item in result], ["refreshed", "needs_you"])
        self.assertEqual(result[1].accounts, ["casey@example.com"])

    def test_healthy_and_missing_tokens_do_not_start_authorization(self):
        for check_payload in (health(healthy=["casey@example.com", "jordan@example.com"]),
                              health(healthy=["jordan@example.com"], missing=["casey@example.com"])):
            with self.subTest(check_payload=check_payload), \
                patch.object(accounts, "check_accounts_payload", return_value=check_payload) as check, \
                patch.object(accounts, "add_account") as authorize:
                result = refresh_sources._sync_gmail(None)
            authorize.assert_not_called()
            check.assert_called_once()
            self.assertEqual(result[0].outcome, "refreshed")

    def test_transient_check_failure_stops_before_authorization_and_sync(self):
        check_payload = health(expired=["casey@example.com"], errors=["jordan@example.com"])
        with patch.object(accounts, "check_accounts_payload", return_value=check_payload), \
            patch.object(accounts, "add_account") as authorize:
            result = refresh_sources._sync_gmail(None)
        authorize.assert_not_called()
        self.sync.assert_not_called()
        self.assertEqual(result[0].outcome, "failed")

    def test_transient_failure_after_renewal_stops_before_sync(self):
        with patch.object(accounts, "check_accounts_payload", side_effect=[
            health(expired=["casey@example.com"]), health(errors=["casey@example.com"])]), \
            patch.object(accounts, "add_account", return_value={"status": "ok"}) as authorize:
            result = refresh_sources._sync_gmail(None)
        authorize.assert_called_once()
        self.sync.assert_not_called()
        self.assertEqual(result[0].outcome, "failed")

    def test_one_card_refresh_renews_only_that_selected_stored_mailbox(self):
        with patch.object(accounts, "check_accounts_payload", side_effect=[
            health(expired=["casey@example.com"]), health(healthy=["casey@example.com"])]) as check, \
            patch.object(accounts, "add_account", return_value={"status": "ok"}) as authorize:
            result = refresh_sources._sync_gmail("casey@example.com")
        self.assertEqual([call.args[1] for call in check.call_args_list], [["casey@example.com"], ["casey@example.com"]])
        authorize.assert_called_once_with(self.home, "casey@example.com", "", headless=False, force=True)
        self.assertEqual(result[0].accounts, ["casey@example.com"])
        self.sync.assert_called_once()

    def test_unknown_mailbox_is_never_added(self):
        with patch.object(accounts, "check_accounts_payload", return_value=health(expired=["new@example.com"])), \
            patch.object(accounts, "add_account") as authorize:
            result = refresh_sources._sync_gmail("new@example.com")
        authorize.assert_not_called()
        self.sync.assert_not_called()
        self.assertEqual(result[0].outcome, "needs_you")

    def test_empty_vault_does_not_probe_or_authorize(self):
        with patch.object(refresh_sources, "_msgvault_emails", return_value=[]), \
            patch.object(accounts, "check_accounts_payload") as check, \
            patch.object(accounts, "add_account") as authorize:
            result = refresh_sources._sync_gmail(None)
        check.assert_not_called()
        authorize.assert_not_called()
        self.assertEqual(result[0].outcome, "not_connected")

    def test_scheduled_cli_prints_success_after_renewal(self):
        disconnected = refresh_sources.SourceResult("imessage", "not_connected", "Synthetic fixture.")
        output = StringIO()
        with patch.object(accounts, "check_accounts_payload", side_effect=[
            health(expired=["casey@example.com"]), health(healthy=["casey@example.com"])]), \
            patch.object(accounts, "add_account", return_value={"status": "ok"}) as authorize, \
            patch.object(refresh_sources, "SYNC_LOCK", self.home / "sync.lock"), \
            patch.object(refresh_sources, "sync_imessage", return_value=disconnected), \
            patch.object(refresh_sources, "sync_whatsapp", return_value=disconnected), \
            patch.object(refresh_sources.sys, "argv", ["refresh_sources.py", "run"]), redirect_stdout(output):
            code = refresh_sources.main()
        self.assertEqual(code, 0)
        authorize.assert_called_once()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["sources"][0]["outcome"], "refreshed")


if __name__ == "__main__":
    unittest.main()
