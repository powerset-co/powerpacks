import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from packs.ingestion.primitives.deep_context.collection import context_sources as sources_module
from packs.ingestion.primitives.deep_context.collection.context_sources import ContextSources
from packs.ingestion.primitives.deep_context.collection.email_context import EmailContext
from packs.ingestion.primitives.deep_context.collection.models import MessageChannel, MessageEntry
from packs.ingestion.primitives.deep_context.shared.common import Person


def _message(text, at="2026-01-01", channel=MessageChannel.GMAIL):
    return MessageEntry.of(
        channel, at, from_me=False, text=text, subject="Update" if channel == MessageChannel.GMAIL else ""
    )


def _email(text, at="2026-01-01"):
    return dict(
        sender_email="casey@example.com", at=at, subject="Update", snippet=text, body_text=text, conversation_id=None
    )


class AppendSourcesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp_path = Path(temporary.name)

    def test_imessage_filters_before_source_cap(self):
        self._check_chat_filters_before_source_cap("imessage")

    def test_imessage_groups_filter_before_source_cap(self):
        self._check_chat_filters_before_source_cap("imessage_group_messages")

    def test_whatsapp_filters_before_source_cap(self):
        self._check_chat_filters_before_source_cap("whatsapp")

    def test_gmail_filters_processed_before_ranking_and_near_duplicate_selection(self):
        reader = EmailContext(Mock())
        old = "I am an engineer building data systems at Example."
        new = old + " Now in Boston."
        kept, _ = reader.select_emails_from_rows(
            [_email(old, "2026-02-01"), _email(new)],
            "casey@example.com",
            1,
            set(),
            processed=frozenset({_message(old, "2026-02-01").fingerprint()}),
        )
        assert [entry.snippet for entry in kept] == [new]


    def test_gmail_reads_backfill_past_candidate_cap(self):
        store = Mock()
        rows = [_email("already processed", "2026-02-01")] * 4 + [_email("older backfill")]
        store.fetch_recent_rows.side_effect = lambda email, limit: rows[:limit]
        reader = EmailContext(store)
        kept, _ = reader.recent_emails_for(
            "casey@example.com",
            1,
            set(),
            processed=frozenset({_message("already processed", "2026-02-01").fingerprint()}),
        )
        assert [entry.snippet for entry in kept] == ["older backfill"]
        store.fetch_recent_rows.assert_called_once_with("casey@example.com", None)


    def _check_chat_filters_before_source_cap(self, source):
        tmp_path = self.tmp_path
        reader = ContextSources(store=Mock(), chat_db=tmp_path / "chat.db", wacli_db=tmp_path / "wa.db", deep_cap=1)
        reader.wacli_db.touch()
        rows = [
            dict(text=text, date=0, is_from_me=0, dn="Update", rn="", handle_id=1, ts=0, from_me=0)
            for text in ["old", "same time new", "overflow"]
        ]
        reader._chat_query = Mock(side_effect=lambda person, query, empty: query(Mock(), [1]))
        channel = {
            "imessage": MessageChannel.IMESSAGE,
            "imessage_group_messages": MessageChannel.IMESSAGE_GROUP,
            "whatsapp": MessageChannel.WHATSAPP,
        }[source]
        old = MessageEntry.of(
            channel, "", from_me=False, text="old", subject="Update" if source == "imessage_group_messages" else ""
        )
        with (
            patch.object(sources_module.chatdb, "query_direct_messages", return_value=rows) as direct_query,
            patch.object(sources_module.chatdb, "query_small_group_messages", return_value=rows) as group_query,
            patch.object(sources_module, "apple_epoch_iso", return_value=""),
            patch.object(sources_module.chatdb, "message_text", side_effect=lambda row: row["text"]),
            patch.object(sources_module.wacli_store, "open_readonly_db"),
            patch.object(sources_module.wacli_store, "whatsapp_epoch_to_iso", return_value=""),
            patch.object(sources_module.wacli_messages, "query_whatsapp_messages", return_value=rows) as whatsapp_query,
            patch.object(sources_module.wacli_messages, "whatsapp_message_text", side_effect=lambda row, **kw: row["text"]),
        ):
            read = getattr(reader, "_read_" + source)
            processed = frozenset({old.fingerprint()})
            selected = read(Person("casey", "Casey", phones=["+15550100"]), processed=processed)
            assert [entry.text for entry in selected] == ["same time new"]
            query = {"imessage": direct_query, "imessage_group_messages": group_query, "whatsapp": whatsapp_query}[source]
            assert query.call_args.kwargs["limit"] is None
            selected_next = read(
                Person("casey", "Casey", phones=["+15550100"]), processed=processed | {selected[0].fingerprint()}
            )
            assert [entry.text for entry in selected_next] == ["overflow"]


    def test_multiple_gmail_addresses_do_not_spend_cap_on_shared_messages(self):
        store = Mock(db_path=Path("synthetic.db"))
        reader = ContextSources(store=store, chat_db=Path("chat.db"), wacli_db=Path("wa.db"), deep_cap=2)
        # The owner's message appears under both recipient addresses.
        shared = _email("I work on orchards", "2026-03-01")
        shared["sender_email"] = "owner@example.com"
        second = _email("Quantum microscopy conference", "2026-02-01")
        second["sender_email"] = "casey+work@example.com"
        store.fetch_recent_rows.side_effect = lambda email, limit: (
            [shared] if email == "casey@example.com" else [shared, second]
        )
        reader._accounts = {"owner@example.com"}
        kept = reader._read_gmail(
            Person("casey", "Casey", emails=["casey@example.com", "casey+work@example.com"]),
            processed=frozenset({_message("old").fingerprint()}),
        )
        assert [entry.text for entry in kept] == [shared["body_text"], second["body_text"]]


    def test_character_overflow_remains_available_next_collection(self):
        tmp_path = self.tmp_path
        reader = ContextSources(store=Mock(), chat_db=tmp_path / "chat.db", wacli_db=tmp_path / "wa.db", deep_cap=5)
        reader._require_readiness = Mock(return_value=Mock(gmail_available=True))
        reader._count_gmail = Mock(return_value=3)
        reader._accounts = set()
        reader._store.fetch_recent_rows.return_value = [_email("old"), _email("apple"), _email("banana")]
        processed = frozenset({_message("old").fingerprint()})
        person = Person("casey", "Casey", emails=["casey@example.com"])
        with patch.object(sources_module, "SAFETY_CHAR_CAP", 6):
            first, _ = reader.collect_person(person, processed=processed)
            assert len(first) == 1
            second, _ = reader.collect_person(person, processed=processed | {first[0].fingerprint()})
            assert len(second) == 1
            assert {first[0].text, second[0].text} == {"apple", "banana"}


    def test_msgvault_none_limit_reads_entire_local_history(self):
        from packs.ingestion.primitives.discover.gmail.msgvault.context_db import fetch_recent_rows

        with sqlite3.connect(":memory:") as connection:
            connection.row_factory = sqlite3.Row
            connection.executescript("""
                CREATE TABLE participants (id INTEGER, email_address TEXT);
                CREATE TABLE messages (
                    id INTEGER, sender_id INTEGER, sent_at TEXT, received_at TEXT,
                    internal_date TEXT, conversation_id TEXT, subject TEXT, snippet TEXT,
                    message_type TEXT, deleted_at TEXT, deleted_from_source_at TEXT
                );
                CREATE TABLE message_bodies (message_id INTEGER, body_text TEXT);
                CREATE TABLE message_recipients (message_id INTEGER, participant_id INTEGER);
                INSERT INTO participants VALUES (1, 'casey@example.com');
            """)
            connection.executemany(
                "INSERT INTO messages VALUES (?, 1, ?, NULL, NULL, NULL, 'Update', ?, 'email', NULL, NULL)",
                [(1, "2026-02-01", "recent"), (2, "2026-01-01", "backfill")],
            )
            assert len(fetch_recent_rows(connection, "casey@example.com", 1)) == 1
            assert [row["snippet"] for row in fetch_recent_rows(connection, "casey@example.com", None)] == [
                "recent",
                "backfill",
            ]
