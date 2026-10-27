"""The Gmail sync's count on the page: msgvault's total from its startup line, the messages done
from the run row in its own database."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.discover.gmail.msgvault.sync import _SyncWatcher
from packs.powerset.primitives.install.workflow import GmailCount


class SyncWatcherTest(unittest.TestCase):
    def test_total_from_the_log_line_and_done_from_the_run_row(self) -> None:
        seen: list[tuple[int, int]] = []
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "msgvault.db"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY, identifier TEXT NOT NULL)")
                con.execute("CREATE TABLE sync_runs (id INTEGER PRIMARY KEY, source_id INTEGER, status TEXT, "
                            "messages_processed INTEGER)")
                con.execute("INSERT INTO sources VALUES (1, 'a@example.com'), (2, 'b@example.com')")
                con.execute("INSERT INTO sync_runs VALUES (1, 1, 'completed', 1120), (2, 2, 'running', 500)")
            watcher = _SyncWatcher(str(db), "b@example.com", lambda read, total: seen.append((read, total)))
            watcher.on_line('time=2026-10-27T15:30:27 level=INFO msg="syncing account" email=b@example.com messages=2369\n')
            self.assertEqual(watcher.total, 2369)
            self.assertEqual(watcher._processed(), 500)
            # A line that is not the startup line leaves the total alone.
            watcher.on_line('level=INFO msg="sync completed" messages_added=12\n')
            self.assertEqual(watcher.total, 2369)
        self.assertEqual(seen, [])


class GmailCountTest(unittest.TestCase):
    def test_mailboxes_add_up_and_a_mailbox_never_starts_the_count_over(self) -> None:
        count = GmailCount()
        self.assertEqual(count.add("a@example.com", 500, 2369), {"read": 500, "total": 2369})
        self.assertEqual(count.add("a@example.com", 2369, 2369), {"read": 2369, "total": 2369})
        self.assertEqual(count.add("b@example.com", 10, 800), {"read": 2379, "total": 3169})


if __name__ == "__main__":
    unittest.main()
