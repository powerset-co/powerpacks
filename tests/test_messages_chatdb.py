import sqlite3
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.shared import build_owner
from packs.ingestion.primitives.deep_context.collection import context_sources
from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.shared.common import Person
from packs.ingestion.primitives.deep_context.synthesis import prompting
from packs.ingestion.primitives.discover.messages import chatdb
from packs.ingestion.primitives.discover.messages import extract_imessage
from packs.ingestion.primitives.discover.messages.wacli import message_db as wacli_messages
from packs.ingestion.primitives.discover.messages.wacli import store_db as wacli_store
from packs.ingestion.primitives.logbook import logbook_sources


ATTRIBUTED_HELLO = b"archiveNSString" + (b"\x00" * 5) + b"\x05hello"


def make_chat_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE message (
              ROWID INTEGER PRIMARY KEY,
              guid TEXT,
              handle_id INTEGER,
              date INTEGER,
              is_from_me INTEGER,
              associated_message_type INTEGER,
              text TEXT,
              attributedBody BLOB
            );
            CREATE TABLE chat (
              ROWID INTEGER PRIMARY KEY,
              guid TEXT,
              chat_identifier TEXT,
              display_name TEXT,
              room_name TEXT
            );
            CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
            CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);

            INSERT INTO handle (ROWID, id) VALUES
              (1, '+1 (415) 555-0101'),
              (2, 'CASEY@example.com'),
              (3, 'urn:+14155550101');
            INSERT INTO chat (ROWID, guid, chat_identifier, display_name, room_name) VALUES
              (1, 'dm-guid', 'iMessage;-;+14155550101', NULL, NULL),
              (2, 'group-guid', 'chat123', 'Synthetic Group', NULL);
            INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (1, 1), (2, 1);
            """
        )
        rows = [
            (1, "guid-1", 1, 725_846_400_000_000_000, 0, None, "plain dm", None, 1),
            (2, "guid-2", 1, 725_846_401_000_000_000, 0, 2001, "reaction", None, 1),
            (3, "guid-3", 1, 725_846_402_000_000_000, 1, 4000, None, ATTRIBUTED_HELLO, 1),
            (4, "guid-4", 1, 725_846_403_000_000_000, 0, None, "plain group", None, 2),
            (5, "guid-5", 1, 725_846_404_000_000_000, 0, 3006, "reaction", None, 2),
            (6, "guid-6", 1, 725_846_405_000_000_000, 1, 3007, None, ATTRIBUTED_HELLO, 2),
        ]
        conn.executemany(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row[:8] for row in rows),
        )
        conn.executemany(
            "INSERT INTO chat_message_join VALUES (?, ?)",
            ((row[8], row[0]) for row in rows),
        )
        conn.executescript(
            """
            ALTER TABLE chat ADD COLUMN account_login TEXT;
            ALTER TABLE message ADD COLUMN destination_caller_id TEXT;
            UPDATE chat SET account_login = 'P:+14155550101' WHERE ROWID = 1;
            UPDATE chat SET account_login = 'E:owner@example.com' WHERE ROWID = 2;
            UPDATE message SET destination_caller_id = '+15550009999' WHERE ROWID IN (1, 4);
            UPDATE message SET destination_caller_id = '+15550008888' WHERE ROWID = 3;
            """
        )


def make_wacli_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE chats (jid TEXT PRIMARY KEY, kind TEXT, name TEXT);
            CREATE TABLE groups (jid TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE messages (
              rowid INTEGER PRIMARY KEY,
              chat_jid TEXT,
              chat_name TEXT,
              msg_id TEXT,
              sender_jid TEXT,
              sender_name TEXT,
              ts INTEGER,
              from_me INTEGER,
              text TEXT,
              display_text TEXT,
              media_caption TEXT,
              media_type TEXT
            );
            INSERT INTO chats VALUES
              ('14155550101@s.whatsapp.net', 'dm', 'Jordan Bravo'),
              ('4155550101@s.whatsapp.net', 'dm', 'Jordan Bravo local'),
              ('987654321@g.us', 'group', 'Founders');
            INSERT INTO groups VALUES ('987654321@g.us', 'Founders');
            INSERT INTO messages VALUES
              (1, '14155550101@s.whatsapp.net', 'Jordan Bravo', 'wa-1',
               '14155550101@s.whatsapp.net', 'Jordan Bravo', 1735689600, 0,
               'plain dm', NULL, NULL, NULL),
              (2, '4155550101@s.whatsapp.net', 'Jordan Bravo local', 'wa-2',
               '4155550101@s.whatsapp.net', 'Jordan Bravo', 1735689700, 1,
               NULL, 'display dm', NULL, NULL),
              (3, '987654321@g.us', 'Founders', 'wa-3',
               '14155550101@s.whatsapp.net', 'Jordan Bravo', 1735689800, 0,
               NULL, NULL, 'group caption', 'image'),
              (4, '987654321@g.us', 'Founders', 'wa-4',
               '19995550199@s.whatsapp.net', 'Casey Delta', 1735689900, 1,
               NULL, NULL, NULL, 'image'),
              (5, '19995550199@s.whatsapp.net', 'Casey Delta', 'wa-5',
               '19995550199@s.whatsapp.net', 'Casey Delta', 1735690000, 0,
               'unrelated dm', NULL, NULL, NULL);
            """
        )


class ChatDbTests(unittest.TestCase):
    def test_probe_and_readonly_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.db"
            make_chat_db(path)

            probe = chatdb.probe_chat_db(path)
            self.assertTrue(probe["readable"])
            self.assertEqual(probe["missing_tables"], [])
            self.assertTrue(probe["has_group_tables"])

            with chatdb.open_sqlite_readonly(path) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM message").fetchone()[0], 6)
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("DELETE FROM message")

    def test_probe_reports_missing_tables_without_mutating_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.db"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY)")
            probe = chatdb.probe_chat_db(path)
            self.assertTrue(probe["readable"])
            self.assertEqual(probe["missing_tables"], ["handle"])

    def test_timestamp_and_phone_policy_match_extractor_contract(self) -> None:
        apple_seconds = 725_846_400
        apple_nanoseconds = apple_seconds * chatdb.NS_PER_SEC
        self.assertEqual(
            chatdb.apple_timestamp_to_iso(apple_seconds),
            chatdb.apple_timestamp_to_iso(apple_nanoseconds),
        )
        self.assertIsNone(chatdb.apple_timestamp_to_iso(0))
        self.assertEqual(chatdb.phone_lookup_key("+1 (415) 555-0101"), "4155550101")
        self.assertEqual(chatdb.phone_lookup_key("+44 20 7946 0958"), "442079460958")
        self.assertTrue(chatdb.is_phone_identifier("+1 (415) 555-0101"))
        self.assertFalse(chatdb.is_phone_identifier("casey@example.com"))

    def test_handle_resolution_accepts_phone_and_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.db"
            make_chat_db(path)
            with chatdb.open_sqlite_readonly(path) as conn:
                self.assertEqual(
                    chatdb.resolve_handle_ids(
                        conn,
                        (value for value in ("4155550101", "casey@EXAMPLE.com")),
                    ),
                    [1, 2],
                )

    def test_message_queries_filter_reactions_and_decode_attributed_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.db"
            make_chat_db(path)
            with chatdb.open_sqlite_readonly(path) as conn:
                direct = list(chatdb.query_direct_messages(conn, [1]))
                group = list(chatdb.query_group_messages(conn, 2))
                direct_count = chatdb.count_direct_messages(conn, [1])

            self.assertEqual([row["rid"] for row in direct], [1, 3])
            self.assertEqual([row["rid"] for row in group], [4, 6])
            self.assertEqual(direct_count, 2)
            self.assertEqual(chatdb.message_text(direct[0]), "plain dm")
            self.assertEqual(chatdb.message_text(direct[1]), "hello")
            self.assertEqual(chatdb.message_text(group[1]), "hello")
            self.assertTrue(chatdb.is_reaction_type(2000))
            self.assertTrue(chatdb.is_reaction_type(3006))
            self.assertFalse(chatdb.is_reaction_type(3007))

    def test_small_group_query_joins_sender_handle_like_dm_queries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.db"
            make_chat_db(path)
            with sqlite3.connect(path) as conn:
                # A synthetic second group member, distinct from the resolved
                # contact (handle 1) — a third participant sharing the group.
                conn.execute("INSERT INTO handle (ROWID, id) VALUES (4, '+19995550199')")
                conn.execute("INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (2, 4)")
                conn.execute(
                    "INSERT INTO message (ROWID, guid, handle_id, date, is_from_me, "
                    "associated_message_type, text, attributedBody) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (7, "guid-7", 4, 725_846_406_000_000_000, 0, None, "third party body", None),
                )
                conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (2, 7)")

            with chatdb.open_sqlite_readonly(path) as conn:
                rows = {
                    row["guid"]: row
                    for row in chatdb.query_small_group_messages(conn, [1], max_group_size=25, limit=10)
                }

            # Previously query_small_group_messages hardcoded NULL AS handle,
            # discarding the sender entirely. It must now carry the same
            # handle_id/handle shape the DM queries already carry.
            self.assertEqual(rows["guid-4"]["handle_id"], 1)
            self.assertEqual(rows["guid-4"]["handle"], "+1 (415) 555-0101")
            self.assertEqual(rows["guid-7"]["handle_id"], 4)
            self.assertEqual(rows["guid-7"]["handle"], "+19995550199")

    def test_equal_time_message_limits_use_content_not_physical_row_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chat_path = Path(tmp) / "chat.db"
            make_chat_db(chat_path)
            tied_date = 725_846_400_000_000_000
            with sqlite3.connect(chat_path) as conn:
                conn.execute(
                    "UPDATE message SET date=?, guid='z-guid', text='alpha', attributedBody=NULL WHERE ROWID=1",
                    (tied_date,),
                )
                conn.execute(
                    "UPDATE message SET date=?, guid='a-guid', text='zulu', attributedBody=NULL WHERE ROWID=3",
                    (tied_date,),
                )
                conn.execute(
                    "UPDATE message SET date=?, guid='z-group', text='alpha', attributedBody=NULL WHERE ROWID=4",
                    (tied_date,),
                )
                conn.execute(
                    "UPDATE message SET date=?, guid='a-group', text='zulu', attributedBody=NULL WHERE ROWID=6",
                    (tied_date,),
                )
                conn.executemany(
                    "INSERT INTO chat (ROWID, guid, chat_identifier, display_name) VALUES (?, ?, ?, ?)",
                    (
                        (3, "z-group", "chat-z", "Zulu Group"),
                        (4, "a-group", "chat-a", "Alpha Group"),
                    ),
                )
                conn.executemany(
                    "INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (?, 1)",
                    ((3,), (4,)),
                )
            with chatdb.open_sqlite_readonly(chat_path) as conn:
                direct = chatdb.query_direct_messages(
                    conn,
                    [1],
                    limit=1,
                    newest_first=True,
                )
                group = chatdb.query_small_group_messages(
                    conn,
                    [1],
                    max_group_size=25,
                    limit=1,
                )
                group_chats = chatdb.query_group_chats_for_handles(conn, [1])

            self.assertEqual(chatdb.message_text(direct[0]), "zulu")
            self.assertEqual(chatdb.message_text(group[0]), "zulu")
            self.assertEqual(
                [row["dn"] for row in group_chats],
                ["Alpha Group", "Synthetic Group", "Zulu Group"],
            )

            whatsapp_path = Path(tmp) / "wacli.db"
            make_wacli_db(whatsapp_path)
            with sqlite3.connect(whatsapp_path) as conn:
                conn.execute(
                    "UPDATE messages SET ts=?, msg_id='z-id', text='alpha' WHERE rowid=1",
                    (1735689600,),
                )
                conn.execute(
                    "UPDATE messages SET ts=?, msg_id='a-id', text='zulu' WHERE rowid=2",
                    (1735689600,),
                )
            with wacli_store.open_readonly_db(whatsapp_path) as conn:
                whatsapp = wacli_messages.query_whatsapp_messages(
                    conn,
                    phones=["+1 (415) 555-0101"],
                    limit=1,
                    newest_first=True,
                )

            self.assertEqual(
                wacli_messages.whatsapp_message_text(whatsapp[0], include_media=False),
                "zulu",
            )

    def test_extractor_stats_use_shared_reaction_and_timestamp_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.db"
            make_chat_db(path)
            stats = extract_imessage.aggregate_message_stats(path)

            self.assertEqual(stats["4155550101"]["message_count"], 4)
            self.assertEqual(
                stats["4155550101"]["last_message"],
                chatdb.apple_timestamp_to_iso(725_846_405_000_000_000),
            )

    def test_build_owner_phone_harvest_uses_shared_metadata_reader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.db"
            make_chat_db(path)

            self.assertEqual(
                chatdb.owner_phone_identifiers(path),
                ["+14155550101", "+15550009999"],
            )
            self.assertEqual(
                build_owner.harvest_owner_phones(path),
                ["+14155550101", "+15550009999"],
            )

    def test_owner_phone_harvest_keeps_account_result_on_older_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.db"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE chat (account_login TEXT)")
                conn.execute("CREATE TABLE message (is_from_me INTEGER)")
                conn.execute("INSERT INTO chat VALUES ('P:+14155550101')")

            self.assertEqual(build_owner.harvest_owner_phones(path), ["+14155550101"])

    def test_deep_context_apple_outputs_keep_existing_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.db"
            make_chat_db(path)
            person = Person("person-1", "Jordan Bravo", phones=["4155550101"])
            reader = context_sources.ContextSources(
                store=context_sources.gni.MsgvaultStore(Path(tmp) / "missing-msgvault.db"),
                chat_db=path,
                wacli_db=Path(tmp) / "missing-wacli.db",
                deep_cap=context_sources.CHAT_MESSAGE_CAP,
            )
            reader.readiness()

            collected, available = reader.collect_person(person)
            messages = [row.to_payload() for row in collected if row.channel == "imessage"]
            group_messages = [row.to_payload() for row in collected if row.channel == "imessage_group"]
            groups = reader.imessage_groups(person)

            self.assertEqual(available, 4)
            self.assertEqual(groups, ["Synthetic Group"])
            self.assertEqual(
                messages,
                [
                    {
                        "channel": "imessage",
                        "at": chatdb.apple_timestamp_to_iso(725_846_400_000_000_000),
                        "direction": "from_them",
                        "subject": "",
                        "text": "plain dm",
                    },
                    {
                        "channel": "imessage",
                        "at": chatdb.apple_timestamp_to_iso(725_846_402_000_000_000),
                        "direction": "from_me",
                        "subject": "",
                        "text": "hello",
                    },
                ],
            )
            self.assertEqual(
                group_messages,
                [
                    {
                        "channel": "imessage_group",
                        "at": chatdb.apple_timestamp_to_iso(725_846_403_000_000_000),
                        "direction": "from_them",
                        "subject": "Synthetic Group",
                        "text": "plain group",
                    },
                    {
                        "channel": "imessage_group",
                        "at": chatdb.apple_timestamp_to_iso(725_846_405_000_000_000),
                        "direction": "from_me",
                        "subject": "Synthetic Group",
                        "text": "hello",
                    },
                ],
            )
            self.assertEqual(chatdb.decode_attributed_body(ATTRIBUTED_HELLO), "hello")
            self.assertEqual(
                context_sources.probe_chat_db(chat_db=path).to_payload(),
                {
                    "exists": True,
                    "readable": True,
                    "messages": 6,
                    "handles": 3,
                    "error": None,
                },
            )

    def test_group_message_distinguishes_owner_contact_and_third_party(self) -> None:
        """Owner, this contact, and a third group participant must render distinguishably.

        Regression for the collapsed-speaker defect: group rows used to carry
        no sender identity, so every non-owner message read as if the
        dossier's own contact had said it — including words from someone
        else entirely sharing the same group chat.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.db"
            make_chat_db(path)
            with sqlite3.connect(path) as conn:
                # Synthetic third participant in "Synthetic Group", distinct from
                # both the owner (is_from_me=1) and the resolved contact (handle 1).
                conn.execute("INSERT INTO handle (ROWID, id) VALUES (4, '+19995550199')")
                conn.execute("INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (2, 4)")
                conn.execute(
                    "INSERT INTO message (ROWID, guid, handle_id, date, is_from_me, "
                    "associated_message_type, text, attributedBody) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        7,
                        "guid-7",
                        4,
                        725_846_406_000_000_000,
                        0,
                        None,
                        "a different group member's own words",
                        None,
                    ),
                )
                conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (2, 7)")

            person = Person("person-1", "Jordan Bravo", phones=["4155550101"])
            reader = context_sources.ContextSources(
                store=context_sources.gni.MsgvaultStore(Path(tmp) / "missing-msgvault.db"),
                chat_db=path,
                wacli_db=Path(tmp) / "missing-wacli.db",
                deep_cap=context_sources.CHAT_MESSAGE_CAP,
            )
            reader.readiness()

            collected, _ = reader.collect_person(person)
            group_directions = {
                row.text: row.direction for row in collected if row.channel == "imessage_group"
            }

            # Same chat, three distinct senders: the contact's own DM-equivalent
            # "them" case, the owner, and a third party who must NOT collapse
            # onto either.
            self.assertEqual(group_directions["plain group"], "from_them")
            self.assertEqual(group_directions["hello"], "from_me")
            self.assertEqual(group_directions["a different group member's own words"], "from_other")

            bundle = CollectionBundle.of(
                person,
                messages=list(collected),
                groups=["Synthetic Group"],
                thread_participants=(),
                available=len(collected),
            )
            rendered = prompting.render_chunk(bundle, bundle.messages)
            date = (chatdb.apple_timestamp_to_iso(725_846_403_000_000_000) or "")[:10]
            self.assertIn(
                f"[imessage_group {date} THEM] Synthetic Group: plain group",
                rendered,
            )
            self.assertIn(
                f"[imessage_group {date} ME] Synthetic Group: hello",
                rendered,
            )
            self.assertIn(
                f"[imessage_group {date} OTHER-IN-GROUP] Synthetic Group: "
                "a different group member's own words",
                rendered,
            )

    def test_logbook_apple_outputs_keep_existing_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.db"
            make_chat_db(path)
            person = Person(
                "person-1",
                "Jordan Bravo",
                emails=["casey@example.com"],
                phones=["4155550101"],
            )

            direct = list(logbook_sources.stream_imessage_dm(person, path))
            groups = logbook_sources.resolve_imessage_groups(person, path)
            group = list(
                logbook_sources.stream_imessage_group(
                    path,
                    2,
                    "Synthetic Group",
                    "group-guid",
                )
            )

            self.assertEqual(logbook_sources.count_imessage_dm(person, path), (2, 1))
            self.assertEqual([row["watermark"] for row in direct], [1, 3])
            self.assertEqual([row["msg_id"] for row in direct], ["guid-1", "guid-3"])
            self.assertEqual([row["text"] for row in direct], ["plain dm", "hello"])
            self.assertEqual(
                groups,
                [{"chat_rowid": 2, "guid": "group-guid", "title": "Synthetic Group"}],
            )
            self.assertEqual([row["watermark"] for row in group], [4, 6])
            self.assertEqual([row["text"] for row in group], ["plain group", "hello"])
            self.assertEqual(
                [
                    row["watermark"]
                    for row in logbook_sources.stream_imessage_dm(
                        person,
                        path,
                        since_rowid=1,
                    )
                ],
                [3],
            )
            self.assertEqual(
                [
                    row["watermark"]
                    for row in logbook_sources.stream_imessage_group(
                        path,
                        2,
                        "Synthetic Group",
                        "group-guid",
                        since_rowid=4,
                    )
                ],
                [6],
            )

    def test_deep_context_whatsapp_outputs_keep_existing_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wacli.db"
            make_wacli_db(path)
            person = Person("person-1", "Jordan Bravo", phones=["+1 (415) 555-0101"])
            reader = context_sources.ContextSources(
                store=context_sources.gni.MsgvaultStore(Path(tmp) / "missing-msgvault.db"),
                chat_db=Path(tmp) / "missing-chat.db",
                wacli_db=path,
                deep_cap=context_sources.CHAT_MESSAGE_CAP,
            )
            reader.readiness()

            entries, _ = reader.collect_person(person)
            messages = [entry.to_payload() for entry in entries]

            self.assertEqual(
                messages,
                [
                    {
                        "channel": "whatsapp",
                        "at": "2025-01-01T00:00:00Z",
                        "direction": "from_them",
                        "subject": "",
                        "text": "plain dm",
                    },
                ],
            )
            self.assertEqual(
                wacli_store.whatsapp_dm_jids(person.phones),
                (
                    "14155550101@s.whatsapp.net",
                    "4155550101@s.whatsapp.net",
                ),
            )

    def test_logbook_whatsapp_outputs_keep_existing_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wacli.db"
            make_wacli_db(path)
            person = Person("person-1", "Jordan Bravo", phones=["+1 (415) 555-0101"])

            direct = list(logbook_sources.stream_whatsapp_dm(person, path))
            groups = logbook_sources.resolve_whatsapp_groups(path, [" founders "], person)
            group = list(
                logbook_sources.stream_whatsapp_group(
                    path,
                    "987654321@g.us",
                    "Founders",
                )
            )

            self.assertEqual(logbook_sources.count_whatsapp_dm(person, path), (2, 1))
            self.assertEqual([row["watermark"] for row in direct], [1, 2])
            self.assertEqual([row["msg_id"] for row in direct], ["wa-1", "wa-2"])
            self.assertEqual([row["text"] for row in direct], ["plain dm", "display dm"])
            self.assertEqual(groups, [{"jid": "987654321@g.us", "title": "Founders"}])
            self.assertEqual([row["watermark"] for row in group], [3, 4])
            self.assertEqual([row["text"] for row in group], ["group caption", "[image]"])
            self.assertEqual(
                logbook_sources.whatsapp_target_jids(path, person, ["Founders"]),
                [
                    "987654321@g.us",
                    "14155550101@s.whatsapp.net",
                    "4155550101@s.whatsapp.net",
                ],
            )
            self.assertEqual(
                [
                    row["watermark"]
                    for row in logbook_sources.stream_whatsapp_dm(
                        person,
                        path,
                        since_rowid=1,
                    )
                ],
                [2],
            )
            self.assertEqual(
                [
                    row["watermark"]
                    for row in logbook_sources.stream_whatsapp_group(
                        path,
                        "987654321@g.us",
                        "Founders",
                        since_rowid=3,
                    )
                ],
                [4],
            )

    def test_whatsapp_sparse_display_text_schema_is_normalized_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wacli.db"
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "CREATE TABLE messages (chat_jid TEXT, timestamp INTEGER, is_from_me INTEGER, display_text TEXT)",
                )
                conn.execute(
                    "INSERT INTO messages VALUES (?, ?, ?, ?)",
                    ("14155550101@s.whatsapp.net", 1735689600, 1, "sparse body"),
                )
            person = Person("person-1", "Jordan Bravo", phones=["4155550101"])
            reader = context_sources.ContextSources(
                store=context_sources.gni.MsgvaultStore(Path(tmp) / "missing-msgvault.db"),
                chat_db=Path(tmp) / "missing-chat.db",
                wacli_db=path,
                deep_cap=context_sources.CHAT_MESSAGE_CAP,
            )
            reader.readiness()

            entries, _ = reader.collect_person(person)
            messages = [entry.to_payload() for entry in entries]
            self.assertEqual(
                messages,
                [
                    {
                        "channel": "whatsapp",
                        "at": "2025-01-01T00:00:00Z",
                        "direction": "from_me",
                        "subject": "",
                        "text": "sparse body",
                    }
                ],
            )
            streamed = list(logbook_sources.stream_whatsapp_dm(person, path))
            self.assertEqual(len(streamed), 1)
            self.assertEqual(streamed[0]["msg_id"], "rid-1")
            self.assertEqual(streamed[0]["text"], "sparse body")

    def test_deep_context_keeps_primary_text_universe_while_logbook_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wacli.db"
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "CREATE TABLE messages (chat_jid TEXT, ts INTEGER, from_me INTEGER, "
                    "text TEXT, display_text TEXT, media_caption TEXT, media_type TEXT)",
                )
                conn.execute(
                    "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "14155550101@s.whatsapp.net",
                        1735689600,
                        0,
                        None,
                        "display fallback",
                        None,
                        "image",
                    ),
                )
            person = Person("person-1", "Jordan Bravo", phones=["4155550101"])
            reader = context_sources.ContextSources(
                store=context_sources.gni.MsgvaultStore(Path(tmp) / "missing-msgvault.db"),
                chat_db=Path(tmp) / "missing-chat.db",
                wacli_db=path,
                deep_cap=context_sources.CHAT_MESSAGE_CAP,
            )
            reader.readiness()

            messages, _ = reader.collect_person(person)
            self.assertEqual(messages, [])
            streamed = list(logbook_sources.stream_whatsapp_dm(person, path))
            self.assertEqual(len(streamed), 1)
            self.assertEqual(streamed[0]["text"], "display fallback")


if __name__ == "__main__":
    unittest.main()
