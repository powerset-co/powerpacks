from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.collection import context_sources
from packs.ingestion.primitives.deep_context.collection.collect_person_context import CollectPersonContext
from packs.ingestion.primitives.deep_context.collection.models import ChatDbProbe
from packs.ingestion.primitives.deep_context.db.models import (
    ParentRow,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
    PersonRow,
    PersonSourceRow,
    PersonSourcesProjection,
)
from packs.ingestion.primitives.deep_context.db.projectors import project_parent_source_bundle
from packs.ingestion.primitives.deep_context.db.queries import artifacts
from packs.ingestion.primitives.deep_context.db.store import Db
from deep_context_sqlite_test_helpers import message_payload


class CollectionFailuresTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Db(self.root / "context.sqlite")
        self.db.project_rows((
            ParentRow("parent-1", "parent-worth:parent-1", "Jordan Bravo"),
            PersonRow("person-1", "parent-1", display_name="Jordan Bravo"),
            PersonIdentifiersProjection("person-1", (
                PersonIdentifierRow("person-1", "phone", "+15550100"),
                PersonIdentifierRow("person-1", "email", "jordan@example.com"),
            )),
            PersonSourcesProjection("person-1", (PersonSourceRow("person-1", "imessage"),)),
        ))
        self.raw = self.root / "raw"
        self.raw.mkdir()
        self.bundle = self.raw / "parent-1.json"
        self.bundle.write_text(json.dumps({
            "person_id": "parent-1",
            "messages": [message_payload("Previously collected evidence")],
        }))
        project_parent_source_bundle(self.db, self.bundle, "parent-1")
        self.before = self.bundle.read_bytes()
        self.projected_before = artifacts(self.db, kind="source_bundle")[0]
        self.node = CollectPersonContext(
            db=self.db,
            out_dir=self.raw,
            chat_db=self.root / "chat.db",
            msgvault_db=self.root / "msgvault.db",
            wacli_db=self.root / "wacli.db",
        )

    def _assert_preserved(self, source: Path) -> None:
        self.assertEqual(self.bundle.read_bytes(), self.before)
        self.assertEqual(artifacts(self.db, kind="source_bundle")[0], self.projected_before)
        receipt = json.loads((self.raw / "manifest.json").read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["stage"], "deep_collect")
        self.assertIn(str(source), receipt["error"])

    def test_corrupt_chat_preserves_bundle(self) -> None:
        self.node.chat_db.write_bytes(b"not a SQLite database")

        with self.assertRaisesRegex(RuntimeError, "file is not a database"):
            self.node.run()

        self._assert_preserved(self.node.chat_db)

    def test_corrupt_gmail_preserves_bundle(self) -> None:
        self.node.msgvault_db.write_bytes(b"not a SQLite database")

        with self.assertRaisesRegex(RuntimeError, "file is not a database"):
            self.node.run()

        self._assert_preserved(self.node.msgvault_db)

    def test_corrupt_whatsapp_preserves_bundle(self) -> None:
        self.node.wacli_db.write_bytes(b"not a SQLite database")

        with self.assertRaisesRegex(RuntimeError, "file is not a database"):
            self.node.run()

        self._assert_preserved(self.node.wacli_db)

    @staticmethod
    def _locked() -> sqlite3.OperationalError:
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        return error

    def test_chat_recovers_on_third_retry(self) -> None:
        self.node.chat_db.touch()
        connection = mock.Mock()
        probe = ChatDbProbe(True, True, 0, 0, None)
        with (
            mock.patch.object(context_sources, "probe_chat_db", return_value=probe),
            mock.patch.object(context_sources.chatdb, "open_sqlite_readonly", return_value=connection),
            mock.patch.object(context_sources.chatdb, "resolve_handle_ids", return_value=[1]),
            mock.patch.object(context_sources.chatdb, "query_direct_messages", side_effect=[
                self._locked(), self._locked(), self._locked(), [{
                    "text": "Recovered evidence", "attributed_body": None,
                    "date": 0, "is_from_me": False,
                }],
            ]) as query,
            mock.patch.object(context_sources.chatdb, "count_direct_messages", return_value=1),
            mock.patch.object(context_sources.chatdb, "query_small_group_messages", return_value=[]),
            mock.patch.object(context_sources.chatdb, "query_group_chats_for_handles", return_value=[]),
        ):
            result = self.node.run()

        self.assertEqual(query.call_count, 4)
        self.assertEqual(result.status, "completed")
        self.assertEqual(json.loads(self.bundle.read_text())["messages"][0]["text"], "Recovered evidence")

    def test_exhausted_reads_log_failure(self) -> None:
        self.node.chat_db.touch()
        with (
            mock.patch.object(context_sources, "probe_chat_db", return_value=ChatDbProbe(True, True, 0, 0, None)),
            mock.patch.object(context_sources.chatdb, "open_sqlite_readonly", side_effect=self._locked()) as read,
            self.assertRaisesRegex(RuntimeError, "4 attempts.*database is locked"),
        ):
            self.node.run()

        self.assertEqual(read.call_count, 4)
        self._assert_preserved(self.node.chat_db)

    def test_permissions_fail_without_retry(self) -> None:
        self.node.chat_db.touch()
        with (
            mock.patch.object(context_sources, "probe_chat_db", return_value=ChatDbProbe(True, True, 0, 0, None)),
            mock.patch.object(context_sources.chatdb, "open_sqlite_readonly", side_effect=PermissionError("denied")) as read,
            self.assertRaisesRegex(RuntimeError, "denied"),
        ):
            self.node.run()

        self.assertEqual(read.call_count, 1)
        self._assert_preserved(self.node.chat_db)

    def test_missing_optional_stores_skip(self) -> None:
        self.assertEqual(self.node.run().status, "completed")

    def test_unused_gmail_store_skips(self) -> None:
        self.db.project_rows((PersonIdentifiersProjection("person-1", (
            PersonIdentifierRow("person-1", "phone", "+15550100"),
        )),))
        self.node.msgvault_db.write_bytes(b"not a SQLite database")

        self.assertEqual(self.node.run().status, "completed")


if __name__ == "__main__":
    unittest.main()
