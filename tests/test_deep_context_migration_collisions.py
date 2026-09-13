import csv
import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.identity_invariants import IdentityInvariantAudit
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.migration.legacy import import_legacy


class MigrationCandidateCollisionTests(unittest.TestCase):
    def _migrate(self, root: Path, *, human: bool, verdict_parents: tuple[str, ...]) -> Db:
        index = {
            "parents": {
                name: {"parent_id": f"parent-{name}", "name": f"Casey {name.title()}", "children": [name]}
                for name in ("alpha", "bravo", "charlie")
            },
            "slugs": {
                name: {"person_id": f"candidate:email:{name}@example.com", "name": f"Casey {name.title()}"}
                for name in ("alpha", "bravo", "charlie")
            },
        }
        index_path = root / "index.json"
        index_path.write_text(json.dumps(index))
        review = root / "review.csv"
        with review.open("w") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "public_identifier", "person_id", "source", "action", "approved", "confidence", "linkedin_url",
            ])
            writer.writeheader()
            writer.writerow({
                "public_identifier": "casey-example",
                "person_id": "candidate:email:alpha@example.com",
                "source": "deep-context-review" if human else "deep-context-name-match",
                "action": "detach" if human else "review",
                "approved": "yes" if human else "",
                "confidence": "0.97",
                "linkedin_url": "https://www.linkedin.com/in/casey-example",
            })
        verdicts = root / "verdicts.jsonl"
        verdicts.write_text("".join(json.dumps({
            "candidate_key": "casey-example",
            "person_ids": [f"candidate:email:{name}@example.com"],
            "parent_slug": name,
            "linkedin": {"linkedin_url": "https://www.linkedin.com/in/casey-example"},
            "verdict": {"verdict": "confirmed", "confidence": 0.99},
        }) + "\n" for name in verdict_parents))
        db = Db(root / "deep-context.sqlite")
        import_legacy(db, review_csv=review, index_json=index_path, verdicts_jsonl=verdicts)
        self.assertTrue(IdentityInvariantAudit(db).run().ok)
        self.assertEqual(db.query("PRAGMA foreign_key_check"), [])
        self.assertEqual(
            {(row["person_id"], row["parent_id"]) for row in db.query("SELECT person_id, parent_id FROM people")},
            {(f"candidate:email:{name}@example.com", f"parent-{name}") for name in ("alpha", "bravo", "charlie")},
        )
        return db

    def test_same_linkedin_for_another_parent_does_not_inherit_human_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            db = self._migrate(Path(directory), human=True, verdict_parents=("bravo",))
            rows = {row["parent_id"]: row for row in db.query("SELECT * FROM links")}
            self.assertEqual(set(rows), {"parent-alpha", "parent-bravo"})
            self.assertEqual(rows["parent-alpha"]["decision_action"], "detach")
            self.assertEqual(rows["parent-alpha"]["decision_approved"], "yes")
            self.assertIsNone(rows["parent-bravo"]["decision_action"])
            self.assertFalse(rows["parent-bravo"]["authoritative_detach"])
            self.assertEqual(rows["parent-bravo"]["machine_judgment"], "confirmed")
            self.assertEqual(rows["parent-bravo"]["linkedin_url"], "https://www.linkedin.com/in/casey-example")
            self.assertEqual({row["public_identifier"] for row in rows.values()}, {"casey-example"})
            self.assertEqual(len(db.query("SELECT * FROM candidate_people")), 2)

    def test_repeated_linkedin_verdicts_keep_each_parent_membership(self):
        with tempfile.TemporaryDirectory() as directory:
            db = self._migrate(Path(directory), human=False, verdict_parents=("bravo", "charlie", "bravo"))
            rows = db.query("SELECT l.parent_id, l.public_identifier, cp.person_id FROM links l JOIN candidate_people cp USING(row_key)")
            self.assertEqual(len(rows), 3)
            self.assertEqual({row["public_identifier"] for row in rows}, {"casey-example"})
            self.assertTrue(all(row["person_id"] == f"candidate:email:{row['parent_id'].removeprefix('parent-')}@example.com" for row in rows))
            self.assertEqual(len(db.query("SELECT * FROM links WHERE machine_judgment='confirmed'")), 2)

    def test_same_parent_repeated_verdict_preserves_original_key_and_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            db = self._migrate(Path(directory), human=True, verdict_parents=("alpha", "alpha"))
            rows = db.query("SELECT row_key, decision_action FROM links")
            self.assertEqual([(row["row_key"], row["decision_action"]) for row in rows], [("casey-example", "detach")])
            self.assertEqual(len(db.query("SELECT * FROM candidate_people")), 1)


if __name__ == "__main__":
    unittest.main()
