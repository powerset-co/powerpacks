"""Local CSV/SQLite inputs and deterministic matching helpers; no contact data."""
from __future__ import annotations

import json
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout

import jinja2

from packs.ingestion.primitives.deep_context_v2 import assets, components, names, text_similarity
from packs.ingestion.primitives.deep_context_v2.db.owner import owner_from_payload
from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.ingestion.primitives.deep_context_v2.import_load import gmail_names, load
from packs.ingestion.primitives.imports.messages.importer import MessagesImport
from packs.ingestion.schemas.message_contacts import CSV_HEADERS
from packs.shared.csv_io import CsvIO


class InputFixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = open_store(self.root / "store.sqlite")
        self.addCleanup(self.conn.close)
        self.archive = self.root / "msgvault.sqlite"
        with sqlite3.connect(self.archive) as con:
            con.executescript("""
                CREATE TABLE participants(id INTEGER PRIMARY KEY, email_address TEXT);
                CREATE TABLE message_recipients(participant_id INTEGER, display_name TEXT);
                INSERT INTO participants VALUES(1, 'JORDAN@example.com');
                INSERT INTO participants VALUES(2, 'casey@example.com');
                INSERT INTO message_recipients VALUES(1, 'Jordan Bravo');
                INSERT INTO message_recipients VALUES(1, ' Jordan Bravo ');
                INSERT INTO message_recipients VALUES(1, 'J. Bravo');
                INSERT INTO message_recipients VALUES(2, 'casey@example.com');
                INSERT INTO message_recipients VALUES(2, '');
            """)
        self.owner = owner_from_payload({"emails": ["owner@example.com"], "phones": ["+15550100"]})

    def csv(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        CsvIO.write_dict_rows(path, list(rows[0]), rows)
        return path

    def row(self, **overrides):
        return dict(full_name=" Jordan Bravo ", primary_email="JORDAN@example.com",
                    primary_phone="+15550100", source_channels="gmail_msgvault", **overrides)

    def test_header_names_frequency_and_email_only_omission(self):
        self.assertEqual(gmail_names.header_names(self.archive), {"jordan@example.com": "Jordan Bravo"})

    def test_header_names_quotes_merge_and_tie_alphabetically(self):
        with sqlite3.connect(self.archive) as con:
            con.executemany("INSERT INTO message_recipients VALUES(2, ?)",
                            [('"Casey Delta"',), ('Casey  Delta',), ('Casey Bravo',), ('Casey Bravo',)])
        self.assertEqual(gmail_names.header_names(self.archive)["casey@example.com"], "Casey Bravo")

    def test_header_names_missing_and_bad_schema_fail(self):
        with self.assertRaises(sqlite3.OperationalError):
            gmail_names.header_names(self.root / "missing.sqlite")
        bad = self.root / "bad.sqlite"
        sqlite3.connect(bad).close()
        with self.assertRaises(sqlite3.OperationalError):
            gmail_names.header_names(bad)

    def test_gmail_normalizes_identifier_prefers_archive_name_drops_role(self):
        rows = [self.row(), {**self.row(), "primary_email": "billing@example.com"},
                {**self.row(), "primary_email": "owner@example.com", "full_name": "Owner Bravo"}]
        result = load.gmail_candidates(self.csv(self.root / "gmail.csv", rows), self.owner, self.archive)
        self.assertEqual(result.shared_mailboxes_dropped, 1)
        self.assertEqual([c.candidate_id for c in result.candidates],
                         ["candidate:email:jordan@example.com", "candidate:email:owner@example.com"])
        self.assertEqual(result.candidates[0].display_name, "Jordan Bravo")
        self.assertTrue(result.candidates[1].is_owner)
        self.assertEqual(json.loads(result.candidates[0].import_json), rows[0])

    def test_phone_channels_and_csv_name(self):
        row = {**self.row(), "source_channels": "imessage, whatsapp"}
        candidate, = load.phone_candidates(self.csv(self.root / "phones.csv", [row]), self.owner)
        self.assertEqual(candidate.candidate_id, "candidate:phone:+15550100")
        self.assertEqual(candidate.display_name, "Jordan Bravo")
        self.assertTrue(candidate.is_owner)
        self.assertEqual(candidate.sources, ("imessage", "whatsapp"))


    def test_invalid_and_blank_channels_rejected(self):
        for channel in ("", "unknown", "imessage,"):
            with self.subTest(channel=channel), self.assertRaises(ValueError):
                load.phone_candidates(self.csv(self.root / "phones.csv", [{**self.row(), "source_channels": channel}]), self.owner)


    def test_valid_identifier_formats_preserve_existing_normalization(self):
        for value, normalized in ((" +1 (555) 0100 ", "+15550100"), ("1555-0100", "15550100")):
            with self.subTest(value=value):
                candidate, = load.phone_candidates(self.csv(self.root / "phones.csv", [{**self.row(), "primary_phone": value}]), self.owner)
                self.assertEqual(candidate.normalized, normalized)
                self.assertEqual(candidate.display, value)
        row = dict(full_name="Jordan Bravo", primary_email="", current_title="Engineer", current_company="Example Labs")
        for value in ("https://linkedin.com/in/Jordan-Bravo", "https://www.linkedin.com/in/jordan-bravo/?trk=example", "linkedin.com/in/jordan-bravo"):
            with self.subTest(value=value):
                self.assertEqual(load.write_connections(self.conn, self.csv(self.root / "connections.csv", [{**row, "linkedin_url": value}]), "now"), 1)
                self.assertIsNotNone(self.conn.execute("SELECT 1 FROM connections WHERE linkedin_url=?", (value,)).fetchone())


    def test_import_requires_owner_but_not_channel_exports(self):
        node = load.ImportLoad(self.conn, self.root, msgvault_db=self.archive)
        self.assertEqual(node.run().status, "not_ready")
        owner_path = self.root / load.OWNER_JSON
        owner_path.parent.mkdir(parents=True, exist_ok=True)
        owner_path.write_text('{"name":"Owner Bravo"}')
        result = node.run()
        self.assertEqual(result.status, "completed")
        self.assertTrue(all(value == 0 for value in result.counts.values()))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM owner").fetchone()[0], 1)

    def test_import_run_persists_counts_and_idempotent_candidates(self):
        owner_path = self.root / load.OWNER_JSON
        owner_path.parent.mkdir(parents=True, exist_ok=True)
        owner_path.write_text(json.dumps({"emails": ["owner@example.com"], "phones": ["+15550100"]}))
        self.csv(self.root / load.GMAIL_CSV, [self.row(), {**self.row(), "primary_email": "support@example.com"}])
        self.csv(self.root / load.MESSAGES_CSV, [{**self.row(), "source_channels": "imessage,whatsapp"}])
        self.csv(self.root / load.CONNECTIONS_CSV, [dict(linkedin_url="https://linkedin.com/in/jordan-bravo",
                  full_name="Jordan Bravo", primary_email="", current_title="Engineer", current_company="Example Labs")])
        node = load.ImportLoad(self.conn, self.root, msgvault_db=self.archive)
        first = node.run()
        self.assertEqual(first.status, "completed")
        self.assertEqual(first.counts, dict(candidates=2, identifiers_email=1, identifiers_phone=1,
                         shared_mailboxes_dropped=1, connections=1, owner_flagged=1,
                         source_gmail_msgvault=1, source_imessage=1, source_whatsapp=1))
        self.assertEqual(node.run().counts, first.counts)
        for table, expected in (("candidates", 2), ("candidate_identifiers", 2), ("candidate_sources", 3), ("connections", 1)):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], expected)
        self.assertIsNone(self.conn.execute("SELECT email FROM connections").fetchone()[0])

    def test_import_bad_channel_rolls_back_owner(self):
        owner_path = self.root / load.OWNER_JSON
        owner_path.parent.mkdir(parents=True, exist_ok=True)
        owner_path.write_text('{}')
        self.csv(self.root / load.MESSAGES_CSV, [{**self.row(), "source_channels": "unknown"}])
        with self.assertRaises(ValueError):
            load.ImportLoad(self.conn, self.root, msgvault_db=self.archive).run()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM owner").fetchone()[0], 0)
        manifest = json.loads((self.root / "deep-context/v2-manifests/import_load.json").read_text())
        self.assertEqual(manifest["status"], "failed")

    def test_import_cli_missing_then_owner_only(self):
        args = ["--data-root", str(self.root), "--msgvault-db", str(self.archive)]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(load.main(args), 1)
        self.assertIn("not_ready", output.getvalue())
        owner_path = self.root / load.OWNER_JSON
        owner_path.write_text('{}')
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(load.main(args), 0)
        self.assertIn("completed", output.getvalue())

    def test_real_messages_csv_producer_feeds_v2_import(self):
        contact = {field: "" for field in CSV_HEADERS}
        contact.update(phone="+15550100000", name="Jordan Bravo", source="imessage,whatsapp",
                       message_count="3", imessage_message_count="2", whatsapp_message_count="1",
                       is_in_group_chats="false", last_message="2026-01-02")
        contacts_path = self.csv(self.root / "contacts.csv", [contact])
        producer = MessagesImport(contacts_csv=contacts_path, import_dir=self.root / load.IMPORT_DIR)
        self.assertEqual(producer.run().status, "completed")
        self.assertEqual(producer.people_csv, self.root / load.MESSAGES_CSV)
        owner_path = self.root / load.OWNER_JSON
        owner_path.parent.mkdir(parents=True, exist_ok=True)
        owner_path.write_text('{}')
        result = load.ImportLoad(self.conn, self.root, msgvault_db=self.root / "never-opened.sqlite").run()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.counts["candidates"], 1)
        self.assertEqual(result.counts["source_imessage"], 1)
        self.assertEqual(result.counts["source_whatsapp"], 1)
        row = self.conn.execute("SELECT candidate_id, display_name, import_json FROM candidates").fetchone()
        self.assertEqual(row["candidate_id"], "candidate:phone:+15550100000")
        self.assertEqual(row["display_name"], "Jordan Bravo")
        self.assertEqual(json.loads(json.loads(row["import_json"])["interaction_counts"]), {"imessage": 2, "whatsapp": 1})


class NameTests(unittest.TestCase):
    def test_precision_positive_variants(self):
        for a, b in (("Dr. Jordan Q. Bravo, PhD", "Bravo, Jordan Quinn"),
                     ("Ben Bravo", "Benjamin Bravo"), ("Wei (David) Bravo", "David Bravo"),
                     ("Jordan O’Bravo", "Jordan O'Bravo"), ("José Bravo", "Jose\u0301 Bravo"),
                     ("Jordan Bravo (she/her)", "Jordan Bravo")):
            with self.subTest(a=a, b=b):
                self.assertTrue(names.same_person_name(names.parse_name(a), names.parse_name(b)))

    def test_precision_negative_variants(self):
        for a, b in (("J Bravo", "Jordan Bravo"), ("Bob Bravo", "Robert Bravo"),
                     ("José Bravo", "Jose Bravo"), ("Jordan Bravo Jr", "Jordan Bravo"),
                     ("Jordan Quinn Bravo", "Jordan Casey Bravo"), ("Jordan", "Jordan Bravo"),
                     ("", ""), ("jordan@example.com", "Jordan Bravo"),
                     ("Jordan Q C Bravo", "Jordan Q Bravo")):
            with self.subTest(a=a, b=b):
                self.assertFalse(names.same_person_name(names.parse_name(a), names.parse_name(b)))

    def test_recall_gate_initials_spelling_and_reordered_words(self):
        for a, b in (("Jordan Bravo", "J Bravo"), ("Jordan Bravo", "Jordan B"),
                     ("Jon Bravo", "John Bravo"), ("Jordan Bravo", "Bravo Jordan"),
                     ("Jordan Tan", "Jordan Tang")):
            with self.subTest(a=a, b=b):
                self.assertTrue(names.written_names_can_match(a, b))

    def test_recall_gate_negative_names(self):
        for a, b in (("Jordan Li", "Jordan Litwak"), ("Jordan Ho", "Jordan Hoang"),
                     ("Jordan Bravo Jr", "Jordan Bravo"), ("Jordan", "Casey Bravo"),
                     ("", ""), ("casey@example.com", "Casey Bravo")):
            with self.subTest(a=a, b=b):
                self.assertFalse(names.written_names_can_match(a, b))


    def test_dossier_name_only_extends_same_written_given_name(self):
        for written, dossier, expected in (("Jordan", "Jordan Bravo", "Jordan Bravo"),
                 ("Jordan", "Casey Bravo", "Jordan"), ("", "Jordan Bravo", ""),
                 ("Jordan Delta", "Jordan Bravo", "Jordan Delta")):
            with self.subTest(written=written, dossier=dossier):
                self.assertEqual(names.names_for_matching(written, dossier), expected)

    def test_name_words_and_alternates(self):
        self.assertEqual(names.name_words("Bravo, Dr Jordan Jr"), ("jordan", "bravo", "jr"))
        self.assertEqual(names.name_words("Jordan O'Bravo"), ("jordan", "obravo"))
        self.assertEqual(names.parse_name("Wei (David) Bravo").given, ("wei", "david"))
        self.assertEqual(names.parse_name("Jordan Bravo (PhD)").given, ("jordan",))

    def test_jaro_known_scores_and_symmetry(self):
        self.assertAlmostEqual(names.jaro("MARTHA", "MARHTA"), 0.9444444444)
        self.assertAlmostEqual(names.jaro_winkler("MARTHA", "MARHTA"), 0.9611111111)
        for a, b in (("", "a"), ("a", "b"), ("same", "same"), ("dixon", "dicksonx")):
            with self.subTest(a=a, b=b):
                self.assertEqual(names.jaro(a, b), names.jaro(b, a))
                self.assertGreaterEqual(names.jaro_winkler(a, b), 0)
                self.assertLessEqual(names.jaro_winkler(a, b), 1)


class HelperTests(unittest.TestCase):
    def test_shingles_normalize_and_short_text(self):
        self.assertEqual(text_similarity.shingles("ONE, two three four!"), frozenset({"one two three", "two three four"}))
        self.assertEqual(text_similarity.shingles("one one"), frozenset({"one"}))
        self.assertEqual(text_similarity.shingles("!!!"), frozenset())
        self.assertEqual(text_similarity.shingles("a b c", 2), frozenset({"a b", "b c"}))

    def test_jaccard_overlap_and_empty(self):
        self.assertEqual(text_similarity.jaccard(frozenset("ab"), frozenset("bc")), 1 / 3)
        self.assertEqual(text_similarity.jaccard(frozenset(), frozenset()), 0)
        self.assertEqual(text_similarity.jaccard(frozenset("a"), frozenset("a")), 1)

    def test_components_transitive_cycles_self_and_order(self):
        self.assertEqual(components.connected_components([]), [])
        self.assertEqual(components.connected_components([("b", "c"), ("x", "x"), ("a", "b"), ("c", "a"), ("b", "c")]),
                         [["a", "b", "c"], ["x"]])

    def test_assets_text_json_and_strict_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "prompt.txt").write_text(" leading\n\n")
            self.assertEqual(assets.text(root, "prompt.txt"), " leading\n")
            (root / "schema.json").write_text('{"type":"object"}')
            self.assertEqual(assets.json_file(root, "schema.json"), {"type": "object"})
            (root / "template.j2").write_text("{% if show %}\n{{ value }}\n{% endif %}")
            template = assets.template(root, "template.j2")
            self.assertEqual(template.render(show=True, value="<bio>"), "<bio>\n")
            with self.assertRaises(jinja2.UndefinedError):
                template.render(show=True)
            with self.assertRaises(FileNotFoundError):
                assets.text(root, "missing")
            (root / "schema.json").write_text("not json")
            with self.assertRaises(json.JSONDecodeError):
                assets.json_file(root, "schema.json")


if __name__ == "__main__":
    unittest.main()
