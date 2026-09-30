"""The typed Logbook build over synthetic local stores (no real message data).

A scoped build must leave every entry it did not build alone, set a rebuilt
archive aside instead of deleting it, say which channels it could not read, and
read everything the stores hold: all dates, `@lid` WhatsApp chats, new handles.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.shared.common import Person
from packs.ingestion.primitives.logbook import logbook_common as lc
from packs.ingestion.primitives.logbook import logbook_export as lx

# 2015-03-01 in seconds; ten years before any three-year depth window.
OLD_TS = 1_425_168_000
RECENT_TS = 1_756_684_800
# 2020-01-01 as an Apple-epoch nanosecond date.
APPLE_2020 = 599_616_000_000_000_000


def make_wacli(store: Path) -> Path:
    store.mkdir(parents=True, exist_ok=True)
    db = store / "wacli.db"
    with sqlite3.connect(db) as con:
        con.executescript(
            """
            CREATE TABLE chats (jid TEXT PRIMARY KEY, kind TEXT, name TEXT, last_message_ts INTEGER);
            CREATE TABLE contacts (jid TEXT PRIMARY KEY, phone TEXT, push_name TEXT, full_name TEXT,
                                   first_name TEXT, business_name TEXT, system_name TEXT);
            CREATE TABLE groups (jid TEXT PRIMARY KEY, name TEXT, left_at INTEGER);
            CREATE TABLE group_participants (group_jid TEXT, user_jid TEXT);
            CREATE TABLE messages (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT, chat_jid TEXT NOT NULL, chat_name TEXT,
                msg_id TEXT NOT NULL, sender_jid TEXT, sender_name TEXT, ts INTEGER NOT NULL,
                from_me INTEGER NOT NULL, text TEXT, display_text TEXT, media_caption TEXT, media_type TEXT);
            """
        )
    return db


def add_whatsapp(db: Path, chat_jid: str, msg_id: str, ts: int, text: str, *, sender_jid: str = "",
                 kind: str = "dm", name: str = "") -> None:
    with sqlite3.connect(db) as con:
        con.execute("INSERT OR IGNORE INTO chats (jid, kind, name) VALUES (?, ?, ?)", (chat_jid, kind, name))
        if kind == "group":
            con.execute("INSERT OR IGNORE INTO groups (jid, name) VALUES (?, ?)", (chat_jid, name))
        con.execute(
            "INSERT INTO messages (chat_jid, chat_name, msg_id, sender_jid, ts, from_me, text) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (chat_jid, name or None, msg_id, sender_jid or None, ts, text),
        )


def make_chat_db(path: Path) -> None:
    with sqlite3.connect(path) as con:
        con.executescript(
            """
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, handle_id INTEGER, date INTEGER,
                                  is_from_me INTEGER, associated_message_type INTEGER, text TEXT,
                                  attributedBody BLOB);
            CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, chat_identifier TEXT,
                               display_name TEXT, room_name TEXT);
            CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
            CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
            """
        )


def add_imessage(path: Path, handle_rowid: int, handle: str, rowid: int, text: str) -> None:
    with sqlite3.connect(path) as con:
        con.execute("INSERT OR IGNORE INTO handle (ROWID, id) VALUES (?, ?)", (handle_rowid, handle))
        con.execute("INSERT OR IGNORE INTO chat (ROWID, guid, chat_identifier) VALUES (?, ?, ?)",
                    (handle_rowid, f"dm-{handle_rowid}", f"iMessage;-;{handle}"))
        con.execute("INSERT OR IGNORE INTO chat_handle_join VALUES (?, ?)", (handle_rowid, handle_rowid))
        con.execute("INSERT INTO message (ROWID, guid, handle_id, date, is_from_me, text) VALUES (?, ?, ?, ?, 0, ?)",
                    (rowid, f"guid-{rowid}", handle_rowid, APPLE_2020 + rowid, text))
        con.execute("INSERT INTO chat_message_join VALUES (?, ?)", (handle_rowid, rowid))


def map_lid(wacli: Path, lid: str, phone_jid: str) -> None:
    """The session's lid map, as whatsmeow writes it next to wacli.db."""
    with sqlite3.connect(wacli.parent / "session.db") as session:
        session.execute("CREATE TABLE IF NOT EXISTS whatsmeow_lid_map (lid TEXT, pn TEXT)")
        session.execute("INSERT INTO whatsmeow_lid_map VALUES (?, ?)", (lid, phone_jid))


JORDAN = Person("parent-jordan01", "Jordan Bravo", emails=["jordan@example.com"], phones=["+15550100"])


class BuildFixture(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.root = self.tmp / "logbook"
        self.wacli = make_wacli(self.tmp / "wacli")
        self.chat_db = self.tmp / "chat.db"
        make_chat_db(self.chat_db)
        self.paths = {"gmail": self.tmp / "absent-msgvault.db", "imessage": self.chat_db, "whatsapp": self.wacli}

    def build(self, people=(JORDAN,), **kwargs) -> lx.LogbookBuild:
        return lx.build_logbook(list(people), paths=self.paths, root=self.root, **kwargs)

    def manifest(self) -> dict:
        return json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))

    def coverage(self, build: lx.LogbookBuild) -> dict[str, lx.ChannelCoverage]:
        return {row.channel: row for row in build.channels}


class ScopedBuildTests(BuildFixture):
    def test_building_one_person_keeps_every_other_entry_and_its_files(self):
        casey = Person("parent-casey001", "Casey Delta", phones=["+15550101"])
        add_whatsapp(self.wacli, "15550100@s.whatsapp.net", "j1", RECENT_TS, "to jordan")
        add_whatsapp(self.wacli, "15550101@s.whatsapp.net", "c1", RECENT_TS, "to casey")
        self.build([casey])
        casey_file = self.root / casey.slug / "whatsapp" / "dm.md"
        self.assertIn("to casey", casey_file.read_text(encoding="utf-8"))

        self.build([JORDAN])

        self.assertIn("to casey", casey_file.read_text(encoding="utf-8"))
        self.assertEqual(set(self.manifest()["entries"]), {casey.slug, JORDAN.slug})
        index = (self.root / "index.md").read_text(encoding="utf-8")
        self.assertIn(casey.slug, index)
        self.assertIn(JORDAN.slug, index)

    def test_rebuilding_a_person_sets_the_prior_archive_aside(self):
        add_whatsapp(self.wacli, "15550100@s.whatsapp.net", "j1", RECENT_TS, "first build")
        self.build()
        with sqlite3.connect(self.wacli) as con:
            # The store lost the message (deleted on the phone); only the archive held it.
            con.execute("DELETE FROM messages")
        add_whatsapp(self.wacli, "15550100@s.whatsapp.net", "j2", RECENT_TS + 60, "second build")

        self.build()

        current = (self.root / JORDAN.slug / "whatsapp" / "dm.md").read_text(encoding="utf-8")
        self.assertIn("second build", current)
        backups = list(self.root.glob(f"{JORDAN.slug}.bkup-*/whatsapp/dm.md"))
        self.assertEqual(len(backups), 1)
        self.assertIn("first build", backups[0].read_text(encoding="utf-8"))

    def test_repeated_build_writes_the_same_archive(self):
        add_whatsapp(self.wacli, "15550100@s.whatsapp.net", "j1", RECENT_TS, "hello")
        first = self.build()
        second = self.build()
        self.assertEqual((first.messages, first.files), (1, 1))
        self.assertEqual((second.messages, second.files), (1, 1))
        self.assertEqual(self.manifest()["entries"][JORDAN.slug]["messages"], 1)

    def test_a_missing_channel_is_reported_and_its_archive_left_in_place(self):
        add_whatsapp(self.wacli, "15550100@s.whatsapp.net", "j1", RECENT_TS, "hello")
        add_imessage(self.chat_db, 1, "+15550100", 1, "imessage hello")
        self.build()
        imessage_file = self.root / JORDAN.slug / "imessage" / "dm.md"
        self.assertTrue(imessage_file.exists())

        self.paths["imessage"] = self.tmp / "no-chat.db"
        build = self.build()

        coverage = self.coverage(build)
        self.assertEqual(coverage["imessage"].status, lx.ChannelStatus.MISSING)
        self.assertEqual(coverage["gmail"].status, lx.ChannelStatus.MISSING)
        self.assertEqual(coverage["whatsapp"].status, lx.ChannelStatus.OK)
        self.assertIn("imessage hello", imessage_file.read_text(encoding="utf-8"))
        rel_paths = set(self.manifest()["entries"][JORDAN.slug]["containers"])
        self.assertIn(f"{JORDAN.slug}/imessage/dm.md", rel_paths)

    def test_an_unreadable_chat_db_is_reported_unreadable_on_every_build_and_its_archive_kept(self):
        add_imessage(self.chat_db, 1, "+15550100", 1, "imessage hello")
        self.build()
        self.chat_db.write_bytes(b"not a database")
        for _ in range(2):
            build = self.build()
            self.assertEqual(self.coverage(build)["imessage"].status, lx.ChannelStatus.UNREADABLE)
        self.assertIn("imessage hello",
                      (self.root / JORDAN.slug / "imessage" / "dm.md").read_text(encoding="utf-8"))
        self.assertEqual(lx._store_depth("imessage", self.chat_db)["status"], "unreadable")

    def test_a_reader_failure_keeps_the_prior_archive_and_catalog(self):
        add_whatsapp(self.wacli, "15550100@s.whatsapp.net", "j1", RECENT_TS, "kept")
        self.build()
        rel_paths = set(self.manifest()["entries"][JORDAN.slug]["containers"])

        def failing(*_args, **_kwargs):
            yield {"channel": "whatsapp", "kind": "dm", "container_id": "dm", "container_title": "",
                   "msg_id": "x", "watermark": 9, "at": "2026-01-01T00:00:00Z", "year": 2026,
                   "sender": "them", "direction": "from_them", "subject": "", "text": "partial"}
            raise sqlite3.OperationalError("disk I/O error")

        close = lx.EntryWriter.close
        with mock.patch.object(lx.src, "stream_whatsapp_dm", failing), \
                mock.patch.object(lx.EntryWriter, "close", autospec=True, side_effect=close) as closed, \
                self.assertRaises(sqlite3.OperationalError):
            self.build()

        closed.assert_called_once()
        self.assertEqual(set(self.manifest()["entries"][JORDAN.slug]["containers"]), rel_paths)
        for rel_path in rel_paths:
            text = (self.root / rel_path).read_text(encoding="utf-8")
            self.assertIn("kept", text)
            self.assertNotIn("partial", text)

    def test_a_group_named_like_a_person_in_the_batch_keeps_off_their_folder(self):
        add_whatsapp(self.wacli, "15550100@s.whatsapp.net", "j1", RECENT_TS, "jordan dm")
        add_whatsapp(self.wacli, "333@g.us", "g1", RECENT_TS, "same name group", kind="group",
                     name="Jordan Bravo jordan01", sender_jid="15550100@s.whatsapp.net")
        self.assertEqual(lc.group_slug("Jordan Bravo jordan01"), JORDAN.slug)

        build = self.build()

        self.assertIn("jordan dm", (self.root / JORDAN.slug / "whatsapp" / "dm.md").read_text(encoding="utf-8"))
        group_slug = next(slug for slug in build.entries if slug != JORDAN.slug)
        self.assertNotEqual(group_slug, JORDAN.slug)
        self.assertIn("same name group",
                      (self.root / group_slug / "whatsapp" / "group.md").read_text(encoding="utf-8"))

    def test_group_slug_stays_with_its_chat_across_builds(self):
        casey = Person("parent-casey001", "Casey Delta", phones=["+15550101"])
        add_whatsapp(self.wacli, "111@g.us", "g1", RECENT_TS, "jordan family", kind="group", name="Family",
                     sender_jid="15550100@s.whatsapp.net")
        add_whatsapp(self.wacli, "222@g.us", "g2", RECENT_TS, "casey family", kind="group", name="Family",
                     sender_jid="15550101@s.whatsapp.net")
        self.build([JORDAN])
        self.build([casey])
        self.build([JORDAN])

        entries = self.manifest()["entries"]
        groups = {slug: entry for slug, entry in entries.items() if entry["kind"] == "group"}
        self.assertEqual(len(groups), 2)
        texts = {slug: (self.root / slug / "whatsapp" / "group.md").read_text(encoding="utf-8") for slug in groups}
        self.assertIn("jordan family", texts["family"])
        other = next(slug for slug in groups if slug != "family")
        self.assertIn("casey family", texts[other])


class ReaderCoverageTests(BuildFixture):
    def test_whatsapp_messages_older_than_three_years_are_archived(self):
        add_whatsapp(self.wacli, "15550100@s.whatsapp.net", "old", OLD_TS, "from 2015")
        add_whatsapp(self.wacli, "15550100@s.whatsapp.net", "new", RECENT_TS, "from 2025")
        build = self.build()
        text = (self.root / JORDAN.slug / "whatsapp" / "dm.md").read_text(encoding="utf-8")
        self.assertIn("## 2015", text)
        self.assertLess(text.index("from 2015"), text.index("from 2025"))
        self.assertEqual(self.coverage(build)["whatsapp"].earliest, "2015-03-01T00:00:00Z")

    def test_whatsapp_lid_chat_discovery_credited_to_the_phone_is_archived(self):
        add_whatsapp(self.wacli, "90001@lid", "lid1", RECENT_TS, "via lid")
        add_whatsapp(self.wacli, "15550100@s.whatsapp.net", "pn1", RECENT_TS + 60, "via phone")
        map_lid(self.wacli, "90001", "15550100@s.whatsapp.net")

        self.build()

        text = (self.root / JORDAN.slug / "whatsapp" / "dm.md").read_text(encoding="utf-8")
        self.assertIn("via lid", text)
        self.assertIn("via phone", text)
        self.assertLess(text.index("via lid"), text.index("via phone"))

    def test_a_group_where_the_person_speaks_as_a_lid_is_archived(self):
        map_lid(self.wacli, "90001", "15550100@s.whatsapp.net")
        add_whatsapp(self.wacli, "444@g.us", "g1", RECENT_TS, "lid sender", kind="group", name="Climbing",
                     sender_jid="90001@lid")
        build = self.build()
        self.assertIn("climbing", build.entries)

    def test_a_group_with_the_person_as_a_silent_lid_participant_is_archived(self):
        map_lid(self.wacli, "90001", "15550100@s.whatsapp.net")
        add_whatsapp(self.wacli, "555@g.us", "g1", RECENT_TS, "someone else talks", kind="group",
                     name="Book Club", sender_jid="15550999@s.whatsapp.net")
        with sqlite3.connect(self.wacli) as con:
            con.execute("INSERT INTO group_participants VALUES ('555@g.us', '90001@lid')")

        self.build()

        text = (self.root / "book-club" / "whatsapp" / "group.md").read_text(encoding="utf-8")
        self.assertIn("someone else talks", text)

    def test_count_and_deepen_targets_use_the_same_lid_chats(self):
        map_lid(self.wacli, "90001", "15550100@s.whatsapp.net")
        add_whatsapp(self.wacli, "90001@lid", "l1", RECENT_TS, "lid dm")
        add_whatsapp(self.wacli, "15550100@s.whatsapp.net", "p1", RECENT_TS, "phone dm")
        add_whatsapp(self.wacli, "444@g.us", "g1", RECENT_TS, "lid sender", kind="group", name="Climbing",
                     sender_jid="90001@lid")
        self.assertEqual(lx.src.count_whatsapp_dm(JORDAN, self.wacli), (2, 1))
        self.assertEqual(sorted(lx.src.whatsapp_target_jids(self.wacli, JORDAN, [])),
                         ["15550100@s.whatsapp.net", "444@g.us", "90001@lid"])

    def test_a_handle_added_after_the_first_build_is_read_by_the_next(self):
        add_imessage(self.chat_db, 1, "jordan@example.com", 1, "by email")
        self.build()
        add_imessage(self.chat_db, 2, "+15550100", 2, "by phone")

        self.build()

        text = (self.root / JORDAN.slug / "imessage" / "dm.md").read_text(encoding="utf-8")
        self.assertIn("by email", text)
        self.assertIn("by phone", text)


if __name__ == "__main__":
    unittest.main()
