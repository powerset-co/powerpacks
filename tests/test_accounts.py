"""Accounts page health and the scheduled refresh's run status."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    def test_a_finished_reconnect_rechecks_sign_ins_while_another_sync_still_runs(self):
        api = AccountsApi()
        api.jobs["whatsapp"] = Job("running", "Syncing…")
        api._signins = {"casey@example.com": "reauthorization_required"}
        api._sync("casey@example.com", lambda: [])
        self.assertIsNone(api._signins)


if __name__ == "__main__":
    unittest.main()
