"""The Gmail window counts: paging ids with the sync's query, the measured pace, the route."""
from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
import urllib.parse
from datetime import date
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.discover.gmail import windows


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class GmailWindowsTests(unittest.TestCase):
    def test_count_pages_ids_with_the_query_and_the_window_date(self) -> None:
        calls = []

        def urlopen(request, timeout=0):
            calls.append(urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query))
            page = {"messages": [{"id": str(n)} for n in range(500)], "nextPageToken": "p2"} if len(calls) == 1 \
                else {"messages": [{"id": "a"}, {"id": "b"}]}
            return _Response(json.dumps(page).encode())

        with patch.object(windows.urllib.request, "urlopen", urlopen):
            total = windows.count("tok", "-category:promotions", date(2025, 10, 28))
        self.assertEqual(total, 502)
        self.assertEqual(calls[0]["q"], ["-category:promotions after:2025/10/28"])
        self.assertEqual(calls[1]["pageToken"], ["p2"])

    def test_pace_is_messages_a_second_over_completed_syncs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "msgvault.db"
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE sources (id INTEGER, identifier TEXT)")
                conn.execute("CREATE TABLE sync_runs (source_id INTEGER, status TEXT, messages_processed INTEGER, "
                             "started_at TEXT, completed_at TEXT)")
                conn.execute("INSERT INTO sources VALUES (1, 'casey@example.com')")
                conn.execute("INSERT INTO sync_runs VALUES (1, 'completed', 120, '2026-10-28 10:00:00', '2026-10-28 10:01:00')")
                conn.execute("INSERT INTO sync_runs VALUES (1, 'failed', 5, '2026-10-28 10:02:00', '2026-10-28 10:02:30')")
            self.assertEqual(windows.pace(db, "casey@example.com"), 2.0)
            self.assertEqual(windows.pace(db, "nobody@example.com"), windows.DEFAULT_PACE)

    def test_window_start_is_a_date_or_nothing_for_everything(self) -> None:
        self.assertEqual(windows.window_start(0), "")
        self.assertRegex(windows.window_start(1), r"^\d{4}-\d{2}-\d{2}$")


if __name__ == "__main__":
    unittest.main()
