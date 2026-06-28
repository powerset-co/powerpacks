"""Unit tests for the $logbook raw-archive pipeline (pure functions, no DB).

Covers CSV schema detection + founder-row parsing, slug/filename derivation,
message formatting, year sectioning, and the append-only EntryWriter (resume a
year, add a new year heading once, never rewrite frontmatter).
"""
from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.logbook import logbook_common as lc
from packs.ingestion.primitives.logbook import logbook_export as lx


class TestCsvParsing(unittest.TestCase):
    def _write(self, text: str) -> Path:
        d = Path(tempfile.mkdtemp())
        p = d / "in.csv"
        p.write_text(text, encoding="utf-8")
        return p

    def test_founder_schema_multi_phone_multi_email(self):
        csv = ("Founder,Cell,Emails,WhatsApp Groups\n"
               'Tom Hacohen,"+16076975207, +447775271260",tom@svix.com; tom@tdh.vc,Tom H - Powerset\n')
        people, groups = lc.load_people_from_csv(self._write(csv))
        self.assertEqual(len(people), 1)
        person = people[0]
        self.assertEqual(person.full_name, "Tom Hacohen")
        self.assertEqual(person.phones, ["+16076975207", "+447775271260"])
        self.assertEqual(person.emails, ["tom@svix.com", "tom@tdh.vc"])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].slug, "tom-h-powerset")
        self.assertEqual(groups[0].channel, "whatsapp")

    def test_person_id_and_slug_are_deterministic(self):
        csv = "Founder,Cell,Emails,WhatsApp Groups\nWes McKinney,,wesmckinn@gmail.com,\n"
        p1 = lc.load_people_from_csv(self._write(csv))[0][0]
        p2 = lc.load_people_from_csv(self._write(csv))[0][0]
        self.assertEqual(p1.person_id, p2.person_id)
        self.assertEqual(p1.slug, p2.slug)
        self.assertTrue(p1.slug.startswith("wes-mckinney-"))

    def test_blank_group_yields_no_target(self):
        csv = "Founder,Cell,Emails,WhatsApp Groups\nNico F,,nico@default.com,\n"
        _, groups = lc.load_people_from_csv(self._write(csv))
        self.assertEqual(groups, [])

    def test_slug_filter(self):
        csv = ("Founder,Cell,Emails,WhatsApp Groups\n"
               "Wes McKinney,,wesmckinn@gmail.com,\nDavid Li,,david@x.com,\n")
        path = self._write(csv)
        wes_slug = lc.load_people_from_csv(path)[0][0].slug
        people, _ = lc.load_people_from_csv(path, slug=wes_slug)
        self.assertEqual([p.full_name for p in people], ["Wes McKinney"])

    def test_group_slug_clean(self):
        self.assertEqual(lc.group_slug("George S - Powerset"), "george-s-powerset")
        self.assertEqual(lc.group_slug(""), "group")


class TestFilenamesAndFormatting(unittest.TestCase):
    def test_subject_slug_strips_reply_prefix(self):
        self.assertEqual(lx._subject_slug("RE: Wedding Block"), "wedding-block")
        self.assertEqual(lx._subject_slug("Fwd: Hi"), "hi")
        self.assertEqual(lx._subject_slug(""), "no-subject")

    def test_thread_filename_has_year_and_hash(self):
        row = {"channel": "gmail", "year": 2019, "subject": "Re: Hebbia beta", "container_id": "abc123"}
        name = lx._thread_filename("gmail", row)
        self.assertTrue(name.startswith("gmail/2019-hebbia-beta-"))
        self.assertTrue(name.endswith(".md"))

    def test_thread_filename_undated(self):
        row = {"channel": "gmail", "year": None, "subject": "Chat", "container_id": "z"}
        self.assertTrue(lx._thread_filename("gmail", row).startswith("gmail/undated-"))

    def test_container_filename_dm_and_group(self):
        self.assertEqual(lx._container_filename("imessage", "dm", {}), "imessage/dm.md")
        self.assertEqual(lx._container_filename("whatsapp", "group", {}), "whatsapp/group.md")

    def test_format_message_inline_vs_block(self):
        short = lx._format_message({"at": "2020-01-02T03:04:05", "sender": "me", "text": "hi"})
        self.assertEqual(short, "**2020-01-02 03:04 · me:** hi\n")
        long = lx._format_message({"at": "2020-01-02T03:04:05", "sender": "Jane", "text": "a\nb"})
        self.assertIn("**2020-01-02 03:04 · Jane:**\n\na\nb", long)


class TestEntryWriter(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self._orig = lx.LOGBOOK_ROOT
        lx.LOGBOOK_ROOT = self._tmp

    def tearDown(self):
        lx.LOGBOOK_ROOT = self._orig

    def _row(self, **kw):
        base = {"channel": "imessage", "kind": "dm", "container_id": "dm",
                "container_title": "X", "msg_id": "m", "watermark": 1,
                "at": "2020-03-01T10:00:00", "year": 2020, "sender": "me",
                "direction": "from_me", "subject": "", "text": "hello"}
        base.update(kw)
        return base

    def test_export_writes_frontmatter_year_and_messages(self):
        w = lx.EntryWriter("alice", append=False, prior={})
        w.write(self._row(watermark=1, at="2020-03-01T10:00:00", year=2020, text="one"))
        w.write(self._row(watermark=2, at="2021-04-01T10:00:00", year=2021, text="two"))
        containers = w.close()
        rel = next(iter(containers))
        body = (self._tmp / rel).read_text(encoding="utf-8")
        self.assertIn("entry: alice", body)
        self.assertIn("## 2020", body)
        self.assertIn("## 2021", body)
        self.assertEqual(containers[rel]["messages"], 2)
        self.assertEqual(containers[rel]["watermark"], 2)
        self.assertEqual(containers[rel]["last_year"], 2021)

    def test_sync_appends_without_rewriting_frontmatter_or_dup_year(self):
        w = lx.EntryWriter("bob", append=False, prior={})
        w.write(self._row(watermark=5, at="2020-03-01T10:00:00", year=2020, text="first"))
        containers = w.close()
        rel = next(iter(containers))
        prior = {(c["channel"], c["container_id"]): c for c in containers.values()}

        w2 = lx.EntryWriter("bob", append=True, prior=prior)
        w2.write(self._row(watermark=6, at="2020-09-01T10:00:00", year=2020, text="same-year"))
        w2.write(self._row(watermark=7, at="2021-01-01T10:00:00", year=2021, text="next-year"))
        containers2 = w2.close()

        body = (self._tmp / rel).read_text(encoding="utf-8")
        self.assertEqual(body.count("entry: bob"), 1)       # frontmatter written once, not on append
        self.assertEqual(body.count("## 2020"), 1)          # year not re-emitted on append
        self.assertEqual(body.count("## 2021"), 1)
        self.assertIn("first", body)
        self.assertIn("same-year", body)
        self.assertIn("next-year", body)
        self.assertEqual(containers2[rel]["messages"], 3)   # 1 prior + 2 appended
        self.assertEqual(containers2[rel]["watermark"], 7)


    def test_imessage_and_whatsapp_dm_do_not_collide(self):
        # Regression: both DMs use container_id "dm"; keying the open file by
        # container_id alone routed WhatsApp messages into the iMessage dm.md.
        w = lx.EntryWriter("carol", append=False, prior={})
        w.write(self._row(channel="imessage", container_id="dm", text="imsg-hi", watermark=1))
        w.write(self._row(channel="whatsapp", container_id="dm", text="wa-hi", watermark=2))
        containers = w.close()
        rels = sorted(containers)
        self.assertEqual(rels, ["carol/imessage/dm.md", "carol/whatsapp/dm.md"])
        imsg = (self._tmp / "carol/imessage/dm.md").read_text(encoding="utf-8")
        wa = (self._tmp / "carol/whatsapp/dm.md").read_text(encoding="utf-8")
        self.assertIn("imsg-hi", imsg)
        self.assertNotIn("wa-hi", imsg)       # WhatsApp must NOT leak into iMessage file
        self.assertIn("wa-hi", wa)
        self.assertNotIn("imsg-hi", wa)


class TestDeepenCommands(unittest.TestCase):
    def _write(self, text: str) -> Path:
        d = Path(tempfile.mkdtemp())
        p = d / "in.csv"
        p.write_text(text, encoding="utf-8")
        return p

    def test_whatsapp_deepen_uses_backfill_executor(self):
        csv = self._write("Founder,Cell,Emails,WhatsApp Groups\nJake Z,+16508048613,,Powerset Braintrust\n")
        args = Namespace(
            csv=str(csv),
            channels="whatsapp",
            msgvault_db="unused-msgvault.db",
            chat_db="unused-chat.db",
            wacli_db=str(csv.parent / "wacli" / "wacli.db"),
            limit=0,
            slug="",
            run=False,
            rounds=3,
        )

        with mock.patch.object(lx.src, "whatsapp_target_jids", return_value=[
            "120363419983815589@g.us",
            "16508048613@s.whatsapp.net",
        ]):
            result = lx.cmd_deepen(args)

        commands = "\n".join(result["recommended_commands"])
        self.assertIn("history backfill --chat 120363419983815589@g.us --requests 3", commands)
        self.assertIn("history backfill --chat 16508048613@s.whatsapp.net --requests 3", commands)
        self.assertNotIn("history fill", commands)


if __name__ == "__main__":
    unittest.main()
