"""Failed metadata extraction preserves the last successful exports."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.discover.messages import extract_imessage


class IMessageFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.chat_db = root / "chat.db"
        with sqlite3.connect(self.chat_db) as connection:
            connection.executescript("""
                CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
                CREATE TABLE message (
                    handle_id INTEGER, date INTEGER, associated_message_type INTEGER
                );
                CREATE TABLE chat (
                    ROWID INTEGER PRIMARY KEY, chat_identifier TEXT,
                    display_name TEXT, room_name TEXT
                );
                CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
                INSERT INTO handle VALUES (1, '+15550100');
                INSERT INTO message VALUES (1, 725846400000000000, NULL);
            """)
        self.extractor = extract_imessage.IMessageExtractor(
            chat_db=self.chat_db,
            addressbook_glob=str(root / "absent-addressbook"),
        )
        self.outputs = {
            "output_csv": root / "contacts.csv",
            "output_jsonl": root / "contacts.jsonl",
            "manifest": root / "manifest.json",
        }
        result = self.extractor.extract(**self.outputs)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["counts"]["contacts"], 1)
        self.saved = {
            key: self.outputs[key].read_bytes()
            for key in ("output_csv", "output_jsonl")
        }

    def _assert_preserved(self, result: dict) -> None:
        self.assertEqual(result["status"], "failed")
        self.assertEqual(json.loads(self.outputs["manifest"].read_text()), result)
        for key, content in self.saved.items():
            with self.subTest(artifact=key):
                self.assertEqual(self.outputs[key].read_bytes(), content)

    def test_unreadable_database_preserves_successful_exports(self) -> None:
        self.extractor.chat_db = self.chat_db.with_name("absent.db")
        self._assert_preserved(self.extractor.extract(**self.outputs))

    def test_extraction_error_preserves_successful_exports(self) -> None:
        with mock.patch.object(
            extract_imessage,
            "aggregate_message_stats",
            side_effect=sqlite3.OperationalError("synthetic extraction failure"),
        ):
            result = self.extractor.extract(**self.outputs)
        self.assertEqual(result["diagnostics"]["exception_type"], "OperationalError")
        self._assert_preserved(result)


if __name__ == "__main__":
    unittest.main()
