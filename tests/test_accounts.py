"""Accounts page health and the scheduled refresh's run status."""

import json
import io
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock
from urllib.parse import urlparse

from packs.ingestion.primitives.accounts import accounts
from packs.ingestion.primitives.accounts.accounts import _GmailRow, _gmail_health
from packs.ingestion.primitives.accounts.api import AccountsApi, Job
from packs.ingestion.primitives.refresh.refresh_sources import SourceResult, _run_status
from packs.ingestion.primitives.refresh.tasks import PROMPT, _claude_task_start, _strip_codex_citations


def _row(*, synced_days_ago: float, last_run: str = "completed") -> _GmailRow:
    synced = (datetime.now(timezone.utc) - timedelta(days=synced_days_ago)).isoformat()
    return _GmailRow("casey@example.com", 10, synced, synced, last_run, None)


class GmailHealthTest(unittest.TestCase):
    def test_expired_sign_in_needs_action_even_after_a_recent_sync(self):
        self.assertEqual(_gmail_health(_row(synced_days_ago=0), "reauthorization_required")[0], "error")

    def test_healthy_sign_in_without_a_recent_sync_is_a_warning(self):
        self.assertEqual(_gmail_health(_row(synced_days_ago=5), "healthy")[0], "warning")

    def test_failed_last_sync_with_a_good_sign_in_offers_sync_not_reconnect(self):
        self.assertEqual(_gmail_health(_row(synced_days_ago=0, last_run="failed"), "healthy")[0], "warning")

    def test_recent_sync_with_healthy_sign_in_is_ok(self):
        self.assertEqual(_gmail_health(_row(synced_days_ago=0), "healthy")[0], "ok")


class RefreshStatusTest(unittest.TestCase):
    def test_a_not_connected_source_is_still_a_clean_run(self):
        results = [SourceResult("gmail", "refreshed", "Synced."), SourceResult("whatsapp", "not_connected", "")]
        self.assertEqual(_run_status(results), "ok")

    def test_a_source_that_needs_the_user_is_surfaced(self):
        results = [SourceResult("gmail", "refreshed", "Synced."), SourceResult("gmail", "needs_you", "Expired.")]
        self.assertEqual(_run_status(results), "needs_you")

    def test_a_failure_outranks_needs_you(self):
        results = [SourceResult("gmail", "needs_you", ""), SourceResult("imessage", "failed", "")]
        self.assertEqual(_run_status(results), "failed")


class TaskHistoryTest(unittest.TestCase):
    def test_claude_session_is_a_run_only_when_its_first_prompt_carries_the_task_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            ours, other = Path(directory, "ours.jsonl"), Path(directory, "other.jsonl")
            ours.write_text(json.dumps({"type": "user", "timestamp": "2026-09-28T13:00:00Z",
                                        "message": {"content": PROMPT}}) + "\n")
            other.write_text(json.dumps({"type": "user", "timestamp": "2026-09-28T13:00:00Z",
                                         "message": {"content": "find people at Example Co"}}) + "\n")
            self.assertEqual(_claude_task_start(ours), "2026-09-28T13:00:00Z")
            self.assertIsNone(_claude_task_start(other))

    def test_codex_report_drops_memory_citations(self):
        text = "Refresh completed.\n\n<oai-mem-citation>\nMEMORY.md:1-2\n</oai-mem-citation>"
        self.assertEqual(_strip_codex_citations(text), "Refresh completed.")


class AccountsJobsTest(unittest.TestCase):
    def test_accounts_reuse_signins_and_signed_in_refreshes_the_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "msgvault.db"
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE sources (source_type TEXT, identifier TEXT)")
                conn.execute("INSERT INTO sources (source_type, identifier) VALUES ('gmail', 'casey@example.com')")
            api = AccountsApi()
            with patch.object(accounts, "DEFAULT_MSGVAULT_DB", db), \
                 patch.object(accounts, "check_account", return_value=SimpleNamespace(status="reauthorization_required")) as probe, \
                 patch("packs.ingestion.primitives.accounts.api.read_accounts", return_value=[]), \
                 patch("packs.ingestion.primitives.accounts.api.installed_runners", return_value=[]), \
                 patch.object(accounts, "_linkedin_session", return_value=False), \
                 patch.object(accounts, "_messages_readable", return_value=False), \
                 patch.object(accounts, "_whatsapp_linked", return_value=False):
                def get(path):
                    handler = Mock(wfile=io.BytesIO())
                    self.assertTrue(api.get(handler, urlparse(path)))
                    return json.loads(handler.wfile.getvalue())

                get("/accounts/api/accounts")
                get("/accounts/api/accounts")
                self.assertEqual(probe.call_count, 1)
                self.assertEqual(api._signins, {"casey@example.com": "reauthorization_required"})
                probe.return_value = SimpleNamespace(status="healthy")
                self.assertEqual(get("/accounts/api/signed-in")["mailboxes"], ["casey@example.com"])
                self.assertEqual(probe.call_count, 2)
                self.assertEqual(api._signins, {"casey@example.com": "healthy"})
                get("/accounts/api/accounts")
                self.assertEqual(probe.call_count, 2)
                api.jobs["whatsapp"] = Job("running", "Syncing…")
                get("/accounts/api/accounts")
                self.assertEqual(probe.call_count, 2)
                with sqlite3.connect(db) as conn:
                    conn.execute("INSERT INTO sources (source_type, identifier) VALUES ('gmail', 'jordan@example.com')")
                get("/accounts/api/accounts")
                self.assertEqual(set(api._signins), {"casey@example.com", "jordan@example.com"})
                self.assertEqual(probe.call_count, 4)
                get("/accounts/api/accounts")
                self.assertEqual(probe.call_count, 4)
                with sqlite3.connect(db) as conn:
                    conn.execute("DELETE FROM sources WHERE identifier = 'casey@example.com'")
                self.assertEqual(get("/accounts/api/signed-in")["mailboxes"], ["jordan@example.com"])
                self.assertEqual(probe.call_count, 5)
                api._sync("whatsapp", lambda: [])
                get("/accounts/api/accounts")
                self.assertEqual(probe.call_count, 6)

    def test_a_finished_reconnect_rechecks_sign_ins_while_another_sync_still_runs(self):
        api = AccountsApi()
        api.jobs["whatsapp"] = Job("running", "Syncing…")
        api._signins = {"casey@example.com": "reauthorization_required"}
        api._sync("casey@example.com", lambda: [])
        self.assertIsNone(api._signins)


if __name__ == "__main__":
    unittest.main()


class SignedInTest(unittest.TestCase):
    """accounts.signed_in: each source's sign-in, read where it lives (setup and Connect share it)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = Path(self.tmp.name) / "linkedin"
        patch.object(accounts, "BROWSER_PROFILE", self.profile).start()
        patch.object(accounts, "gmail_signins", return_value={}).start()
        patch.object(accounts.msgvault_home, "load_setup_state",
                     return_value=accounts.msgvault_home.SetupState(email="owner@example.com")).start()
        patch.object(accounts, "_messages_readable", return_value=False).start()
        patch.object(accounts.wacli_auth, "auth_status", side_effect=OSError("wacli not installed")).start()
        self.addCleanup(patch.stopall)

    def cookie(self, *, expires_in_days: float) -> None:
        (self.profile / "Default").mkdir(parents=True)
        expires = int((time.time() + expires_in_days * 86_400 + accounts.CHROME_EPOCH_SECONDS) * 1_000_000)
        with closing(sqlite3.connect(self.profile / "Default" / "Cookies")) as conn:
            conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, expires_utc INTEGER)")
            conn.execute("INSERT INTO cookies VALUES ('.www.linkedin.com', 'li_at', ?)", (expires,))
            conn.commit()

    def test_nothing_signed_in_on_a_fresh_mac(self):
        self.assertEqual(accounts.signed_in(), accounts.SignedIn(linkedin=False, gmail=False, imessage=False,
                                                                 whatsapp=False, mailboxes=()))

    def test_reading_a_profile_chrome_has_not_written_cookies_to_leaves_it_as_is(self):
        (self.profile / "Default").mkdir(parents=True)
        self.assertFalse(accounts.signed_in().linkedin)
        self.assertFalse((self.profile / "Default" / "Cookies").exists())

    def test_linkedin_is_the_sign_in_browsers_unexpired_session(self):
        self.cookie(expires_in_days=300)
        self.assertTrue(accounts.signed_in().linkedin)

    def test_an_expired_linkedin_session_is_signed_out(self):
        self.cookie(expires_in_days=-1)
        self.assertFalse(accounts.signed_in().linkedin)

    def test_gmail_needs_every_mailbox_signed_in_now_and_puts_the_apps_owner_first(self):
        with patch.object(accounts, "gmail_signins",
                          return_value={"work@example.com": "healthy", "owner@example.com": "healthy"}):
            ready = accounts.signed_in()
        self.assertTrue(ready.gmail)
        self.assertEqual(ready.mailboxes, ("owner@example.com", "work@example.com"))
        with patch.object(accounts, "gmail_signins",
                          return_value={"owner@example.com": "healthy", "work@example.com": "reauthorization_required"}):
            self.assertFalse(accounts.signed_in().gmail)

    def test_messages_and_whatsapp_read_macos_and_wacli(self):
        with patch.object(accounts, "_messages_readable", return_value=True), \
             patch.object(accounts.wacli_auth, "auth_status", return_value=SimpleNamespace(authenticated=True)):
            ready = accounts.signed_in()
        self.assertEqual((ready.imessage, ready.whatsapp), (True, True))


class MessagesReadableTest(unittest.TestCase):
    def test_messages_is_read_in_a_fresh_process_each_time(self):
        # macOS answers a process once: a child process per check sees a grant made since.
        with tempfile.TemporaryDirectory() as tmp:
            chat = Path(tmp) / "chat.db"
            with patch.object(accounts, "DEFAULT_CHAT_DB", chat):
                self.assertFalse(accounts._messages_readable())
                with closing(sqlite3.connect(chat)) as conn:
                    conn.execute("CREATE TABLE message (date INTEGER)")
                self.assertTrue(accounts._messages_readable())
