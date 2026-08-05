from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.legacy import LegacyImportError, import_legacy
from packs.ingestion.primitives.deep_context.db.schema import (
    FactRow,
    GuidanceRow,
    LinkRow,
    MachineWorth,
    ParentRow,
    PersonRow,
    ReviewSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db, SchemaVersionError
from packs.ingestion.primitives.deep_context.db.views import identity_decision, settle_parent


class DeepContextDbCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Db(self.root / "deep-context.sqlite")

    def tearDown(self):
        self.temp.cleanup()

    def test_open_never_drops_incompatible_database(self):
        self.db.upsert_parent(ParentRow("parent-1", "parent-1"))
        with sqlite3.connect(self.db.db_path) as conn:
            conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
        with self.assertRaisesRegex(SchemaVersionError, "expected 4"):
            Db(self.db.db_path)
        with sqlite3.connect(self.db.db_path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM parents").fetchone()[0], 1)

    def test_domain_upserts_keep_null_real_and_json_types(self):
        self.db.upsert_person(PersonRow("person-1", "parent-1"))
        self.db.upsert_parent(ParentRow("parent-1", "person-1", '["person-1"]'))
        self.db.upsert_fact(FactRow(
            "person-1", "person-1", "parent-1", "/paid/facts/person-1.jsonl", 7,
            MachineWorth.MAYBE.value, "uncertain", 0.42, '{"canonical_name":"Jordan Bravo"}'))
        self.db.upsert_link(LinkRow(
            "jordan-bravo", "jordan-bravo", "pub", person_id="person-1",
            parent_id="parent-1", confidence=0.75, match_emails='["casey@example.com"]'))
        self.db.upsert_guidance(GuidanceRow(
            "jordan-bravo-parent-1", "person-1", "use the supplied profile", detail_json="{}"))
        fact = self.db.query("SELECT * FROM facts")[0]
        self.assertEqual((fact["llm_worth"], fact["confidence"]), ("maybe", 0.42))
        self.assertIsNone(fact["updated_at"])
        self.assertEqual(json.loads(fact["facts_json"])["canonical_name"], "Jordan Bravo")
        self.assertEqual(self.db.query("SELECT state FROM guidance")[0]["state"], "pending")

    def test_settle_parent_is_one_durable_domain_action(self):
        self.db.upsert_person(PersonRow("person-1", "parent-1"))
        self.db.upsert_person(PersonRow("person-2", "parent-1"))
        self.db.upsert_link(LinkRow("first", "first", "pub", person_id="person-1"))
        self.db.upsert_link(LinkRow("second", "second", "pub", person_id="person-2"))
        settled = settle_parent(
            self.db, clicked_row_key="first", person_id="person-1",
            decision=identity_decision(target="first", decision="keep"),
            decided_at="2026-08-05T00:00:00Z")
        self.assertEqual(set(settled), {"first", "second"})
        rows = self.db.query("SELECT target, value FROM decisions ORDER BY target")
        self.assertEqual([(r["target"], r["value"]) for r in rows],
                         [("first", "verify"), ("second", "detach")])

    def test_explicit_legacy_import_hydrates_facts_and_exports_only_on_request(self):
        review = self.root / "review.csv"
        with review.open("w", newline="", encoding="utf-8") as fh:
            fields = [
                "public_identifier", "worth_person_ids", "action", "approved",
                "person_id", "linkedin_url", "confidence", "match_emails",
                "llm_worth", "llm_worth_reason", "network_worth", "source", "updated_at",
            ]
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerow({"public_identifier": "jordan-bravo", "action": "review",
                             "person_id": "person-1", "linkedin_url": "https://linkedin.com/in/jordan-bravo",
                             "confidence": "0.8", "match_emails": "casey@example.com",
                             "llm_worth": "maybe", "source": ReviewSource.RECONCILE.value})
        index = self.root / "index.json"
        index.write_text(json.dumps({
            "slugs": {"jordan": {"person_id": "person-1"}},
            "parents": {"jordan-parent": {"parent_id": "parent-1", "children": ["jordan"]}},
        }), encoding="utf-8")
        facts = self.root / "facts"
        facts.mkdir()
        fact_path = facts / "person-1.jsonl"
        fact_path.write_text(json.dumps({"final_confidence": 0.7, "facts": {
            "canonical_name": "Jordan Bravo",
            "network_worth": {"decision": "maybe", "reason": "not enough context"},
        }}) + "\n", encoding="utf-8")

        counts = import_legacy(self.db, review_csv=review, index_json=index, facts_dir=facts)
        self.assertEqual((counts["people"], counts["facts"], counts["links"]), (1, 1, 1))
        row = self.db.query("SELECT parent_id, llm_worth, facts_json FROM facts")[0]
        self.assertEqual((row["parent_id"], row["llm_worth"]), ("parent-1", "maybe"))
        self.assertEqual(json.loads(row["facts_json"])["canonical_name"], "Jordan Bravo")
        self.assertFalse(hasattr(self.db, "needs_import"))
        with self.assertRaisesRegex(LegacyImportError, "not empty"):
            import_legacy(self.db, review_csv=review)

        exported = self.root / "export" / "review.csv"
        self.assertFalse(exported.exists())
        self.db.export_batons(exported)
        self.assertTrue(exported.exists())


if __name__ == "__main__":
    unittest.main()
