"""People-page Logbook reader: the saved archive read back from disk.

Synthetic stores and archives only. The reader must list what a build saved after a
restart without building, key every file read by the manifest (no path from the
request reaches the disk), and parse the markdown ``EntryWriter`` writes back into
the messages it was given.
"""
from __future__ import annotations

import json
import sqlite3
import urllib.parse
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.logbook import logbook_export
from packs.ingestion.primitives.logbook.logbook_export import EntryWriter
from packs.ingestion.primitives.share.web import logbook as web_logbook
from packs.ingestion.primitives.share.web import logbook_archive
from packs.ingestion.primitives.share.web.logbook_archive import parse_conversation
from packs.ingestion.primitives.share.web.server import share_routes
from test_logbook_build import RECENT_TS, add_whatsapp
from test_logbook_people import Handler, LogbookPeopleFixture, ts_interface

LONG = "A long line. " * 20


def _row(at: str, sender: str, text: str, *, cid: str = "dm", kind: str = "dm", channel: str = "whatsapp") -> dict:
    return {"channel": channel, "kind": kind, "container_id": cid, "container_title": "", "at": at,
            "year": int(at[:4]) if at else None, "sender": sender, "text": text, "watermark": 1}


class ParseConversationTests(LogbookPeopleFixture):
    def test_the_writer_output_parses_back_to_the_messages_it_was_given(self):
        rows = [
            _row("2019-12-31T23:59:00Z", "Casey Delta", "short one"),
            _row("2020-01-01T08:05:00Z", "me", LONG.strip()),
            _row("2020-01-01T08:06:00Z", "Casey Delta", "first line\n\n## 2021\n**bold** <b>x</b>\nlast line"),
            _row("2020-02-01T10:00:00Z", "me", ""),
            _row("", "Casey Delta", "<script>alert(1)</script>"),
            _row("2021-03-04T05:06:00Z", "Casey Delta", "a: **not a header**"),
        ]
        writer = EntryWriter(self.root, "casey-delta-bbbb", append=False, prior={})
        for row in rows:
            writer.write(row)
        (meta,) = writer.close().values()

        messages = parse_conversation((self.root / meta["rel_path"]).read_text(encoding="utf-8"))

        self.assertEqual(
            [(m.at, m.sender, m.text) for m in messages],
            [(r["at"][:16].replace("T", " "), r["sender"], r["text"]) for r in rows],
        )

    def test_a_body_line_shaped_like_a_year_marker_is_kept(self):
        rows = [
            _row("2020-01-01T08:00:00Z", "Casey Delta", "pasted notes\n## 2021"),
            _row("2020-01-01T08:01:00Z", "me", "notes\n\n## 2020"),
            _row("2020-01-01T08:02:00Z", "Casey Delta", "same year after"),
            _row("2021-01-01T08:00:00Z", "me", "last\n\n## 2021"),
            _row("2022-01-01T08:00:00Z", "Casey Delta", "new year after"),
        ]
        writer = EntryWriter(self.root, "casey-delta-bbbb", append=False, prior={})
        for row in rows:
            writer.write(row)
        (meta,) = writer.close().values()

        messages = parse_conversation((self.root / meta["rel_path"]).read_text(encoding="utf-8"))

        self.assertEqual([m.text for m in messages], [r["text"] for r in rows])

    def test_a_file_without_messages_parses_empty(self):
        self.assertEqual(parse_conversation("---\nentry: x\n---\n\n# Direct messages\n"), ())


class LogbookArchiveRoutesTests(LogbookPeopleFixture):
    def _routes(self):
        return share_routes(self.db, self.root, upload_db=self.root / "local-search.duckdb",
                            upload_dir=self.root / "upload-powerset", logbook_root=self.logbook_root,
                            logbook_stores=self.stores)

    def _get(self, routes, route, **query):
        handler = Handler()
        url = f"/api/people/logbook/{route}?{urllib.parse.urlencode(query)}"
        self.assertTrue(routes.get(handler, urllib.parse.urlparse(url)))
        return handler.response_status, json.loads(handler.wfile.getvalue())

    def _build_casey(self) -> str:
        add_whatsapp(self.wacli, "14155550101@s.whatsapp.net", "c1", RECENT_TS, "hi casey\n<b>not bold</b>")
        add_whatsapp(self.wacli, "123@g.us", "g1", RECENT_TS, "group hello", kind="group", name="Family",
                     sender_jid="14155550101@s.whatsapp.net")
        routes = self._routes()
        routes.logbook.start(["parent-bbbb"])
        routes.logbook._thread.join(timeout=10)
        return routes.logbook.status()["result"]["entries"]

    def test_saved_entries_are_read_after_a_restart_without_building(self):
        built = self._build_casey()
        with mock.patch.object(web_logbook, "build_logbook", side_effect=AssertionError("no build")), \
                mock.patch.object(logbook_export, "build_logbook", side_effect=AssertionError("no build")):
            restarted = self._routes()
            self.assertEqual(restarted.logbook.status()["status"], "idle")
            code, payload = self._get(restarted, "entries")

        self.assertEqual(code, 200)
        entries = {entry["slug"]: entry for entry in payload["entries"]}
        self.assertEqual(sorted(entries), sorted(built))
        person = next(entry for entry in entries.values() if entry["kind"] == "person")
        group = next(entry for entry in entries.values() if entry["kind"] == "group")
        self.assertEqual((person["parent_id"], person["messages"], person["channels"]),
                         ("parent-bbbb", 1, ["whatsapp"]))
        self.assertEqual((group["name"], group["parent_id"]), ("Family", None))
        self.assertEqual(person["first_at"][:10], person["last_at"][:10])

    def test_an_entry_lists_its_conversations_and_one_reads_back_verbatim(self):
        self._build_casey()
        routes = self._routes()
        slug = next(entry["slug"] for entry in self._get(routes, "entries")[1]["entries"] if entry["parent_id"])

        code, entry = self._get(routes, "entry", slug=slug)
        self.assertEqual(code, 200)
        (conversation,) = entry["conversations"]
        self.assertEqual((conversation["channel"], conversation["kind"], conversation["messages"]),
                         ("whatsapp", "dm", 1))

        code, read = self._get(routes, "conversation", slug=slug, path=conversation["path"])
        self.assertEqual(code, 200)
        (message,) = read["messages"]
        self.assertEqual(message["text"], "hi casey\n<b>not bold</b>")

    def test_no_saved_logbook_lists_nothing_and_reads_nothing(self):
        routes = self._routes()
        self.assertEqual(self._get(routes, "entries"), (200, {"entries": []}))
        self.assertEqual(self._get(routes, "entry", slug="casey-delta-bbbb"),
                         (404, {"error": logbook_archive.NO_ENTRY}))
        self.assertEqual(self._get(routes, "conversation", slug="casey-delta-bbbb", path="x.md")[0], 404)

    def test_only_paths_the_manifest_lists_for_that_entry_are_read(self):
        self._build_casey()
        routes = self._routes()
        entries = self._get(routes, "entries")[1]["entries"]
        person = next(entry["slug"] for entry in entries if entry["kind"] == "person")
        group = next(entry["slug"] for entry in entries if entry["kind"] == "group")
        group_path = self._get(routes, "entry", slug=group)[1]["conversations"][0]["path"]
        secret = self.root / "deep-context.sqlite"

        for path in ("../deep-context.sqlite", str(secret), f"{person}/../../deep-context.sqlite",
                     group_path, f"{person}/whatsapp/../../manifest.json", ""):
            with self.subTest(path=path):
                self.assertEqual(self._get(routes, "conversation", slug=person, path=path),
                                 (404, {"error": logbook_archive.NO_CONVERSATION}))
        self.assertEqual(self._get(routes, "conversation", slug="../..", path="deep-context.sqlite")[0], 404)

    def test_a_manifest_path_outside_its_entry_or_a_missing_file_is_not_read(self):
        (slug, *_) = self._build_casey()
        manifest_path = self.logbook_root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        containers = manifest["entries"][slug]["containers"]
        (meta,) = containers.values()
        escaped = {**meta, "rel_path": f"{slug}/../../deep-context.sqlite"}
        containers[escaped["rel_path"]] = escaped
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (self.logbook_root / meta["rel_path"]).rename(self.root / "moved.md")
        routes = self._routes()

        for path in (escaped["rel_path"], meta["rel_path"]):
            with self.subTest(path=path):
                self.assertEqual(self._get(routes, "conversation", slug=slug, path=path),
                                 (404, {"error": logbook_archive.MISSING_FILE}))

    def test_payload_keys_match_the_page_types(self):
        self._build_casey()
        routes = self._routes()
        summary = self._get(routes, "entries")[1]["entries"][0]
        entry = self._get(routes, "entry", slug=summary["slug"])[1]
        conversation = entry["conversations"][0]
        message = self._get(routes, "conversation", slug=summary["slug"], path=conversation["path"])[1]
        self.assertEqual(ts_interface("LogbookEntry"), tuple(summary))
        self.assertEqual(ts_interface("LogbookEntry") + ("conversations",), tuple(entry))
        self.assertEqual(ts_interface("LogbookConversation"), tuple(conversation))
        self.assertEqual(ts_interface("LogbookMessage"), tuple(message["messages"][0]))


class EmailParticipantsTests(LogbookPeopleFixture):
    def test_thread_participants_include_senders_and_recipients_without_rebuilding(self):
        store = self.root / "mail.sqlite"
        with sqlite3.connect(store) as con:
            con.executescript("""
                CREATE TABLE conversations(id INTEGER, source_conversation_id TEXT);
                CREATE TABLE messages(id INTEGER, conversation_id INTEGER, sender_id INTEGER);
                CREATE TABLE participants(id INTEGER, display_name TEXT, email_address TEXT);
                CREATE TABLE message_recipients(message_id INTEGER, participant_id INTEGER, recipient_type TEXT, display_name TEXT);
                INSERT INTO conversations VALUES(7, 'thread-7'),(8, 'other');
                INSERT INTO participants VALUES(1,'Jordan Bravo','jordan@example.com'),
                  (2,'Casey Delta','casey@example.com'),(3,'Riley Echo','riley@example.com'),
                  (4,'Morgan Fox','morgan@example.com'),(5,'Unrelated','unrelated@example.com');
                INSERT INTO messages VALUES(10,7,1),(11,7,2),(12,8,5);
                INSERT INTO message_recipients VALUES(10,2,'to','Casey Delta'),
                  (10,3,'cc','Riley Echo'),(10,4,'bcc','Morgan Fox'),(11,1,'to','Jordan Bravo');
            """)
        from packs.ingestion.primitives.share.web.logbook_participants import email_participants
        participants = email_participants(store, "thread-7")
        self.assertEqual({p.email: p.roles for p in participants}, {
            "jordan@example.com": ("from", "to"), "casey@example.com": ("from", "to"),
            "riley@example.com": ("cc",), "morgan@example.com": ("bcc",),
        })
        self.assertIsNone(email_participants(self.root / "missing.sqlite", "thread-7"))
        self.assertEqual(email_participants(store, "other")[0].email, "unrelated@example.com")
        self.assertEqual(email_participants(store, "conv-7"), participants)
        self.stores["gmail"] = store
        slug = "casey-delta-bbbb"
        writer = EntryWriter(self.logbook_root, slug, append=False, prior={})
        writer.write(_row("2026-09-30T12:00:00Z", "Jordan", "hello", cid="thread-7", kind="thread", channel="gmail"))
        containers = writer.close()
        entries = {}
        logbook_export._record_entry(entries, slug, "Casey Delta", "person", containers, replaced=[])
        (self.logbook_root / "manifest.json").write_text(json.dumps({"entries": entries}))
        routes = share_routes(self.db, self.root, logbook_root=self.logbook_root, logbook_stores=self.stores)
        handler = Handler()
        path = next(iter(containers))
        query = urllib.parse.urlencode({"slug": slug, "path": path})
        routes.get(handler, urllib.parse.urlparse("/api/people/logbook/conversation?" + query))
        payload = json.loads(handler.wfile.getvalue())
        self.assertEqual(len(payload["participants"]), 4)
        self.assertEqual(payload["messages"][0]["text"], "hello")
        with sqlite3.connect(store) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM messages").fetchone()[0], 3)
