from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.apply_retargets import ApplyRetargets
from packs.ingestion.primitives.deep_context.db.models import (
    CandidatePersonRow,
    LinkRow,
    ParentRow,
    PersonRow,
    ReviewSource,
    RowKind,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.reconcile_linkedin import (
    upsert_retargets,
    write_overrides,
)
from packs.ingestion.primitives.deep_context.synthesize_person_context import project_facts


class SqliteProducerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Db(self.root / "deep-context.sqlite")
        self.db.project_parent(ParentRow("parent-1", "parent-1"))
        self.db.project_person(PersonRow("person-1", "parent-1"))
        self.db.project_candidate(LinkRow(
            "alice", "parent-1", "alice", RowKind.PUB.value,
            linkedin_url="https://www.linkedin.com/in/alice",
        ))
        self.db.replace_candidate_people(
            "alice", (CandidatePersonRow("alice", "person-1", "parent-1"),)
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_reconcile_projects_machine_identity_without_touching_human(self) -> None:
        task = {
            "candidate_key": "alice", "person_ids": ["person-1"],
            "linkedin": {"linkedin_url": "https://www.linkedin.com/in/alice"},
            "verdict": {"verdict": "confirmed", "confidence": 0.99, "reason": "matches"},
            "action": "confirm", "no_link": False,
        }
        write_overrides(self.db, [task])
        row = self.db.query("SELECT * FROM links WHERE row_key='alice'")[0]
        self.assertEqual(row["machine_action"], "verify")
        self.assertEqual(row["machine_approved"], "auto")

        self.db.settle_identity("alice", "verify", source=ReviewSource.REVIEW.value)
        task["verdict"] = {
            "verdict": "wrong_person", "confidence": 1.0, "reason": "different"
        }
        task["action"] = "detach"
        self.assertEqual(write_overrides(self.db, [task])["preserved_user_rows"], 1)
        row = self.db.query("SELECT * FROM links WHERE row_key='alice'")[0]
        self.assertEqual(row["decision_action"], "verify")
        self.assertEqual(row["machine_action"], "verify")

    def test_retarget_and_downstream_baton_are_sqlite_derived(self) -> None:
        upsert_retargets(self.db, [{
            "old_public_identifier": "alice",
            "new_linkedin_url": "https://www.linkedin.com/in/alice-correct",
            "confidence": 0.9,
        }])
        row = self.db.query("SELECT * FROM links WHERE row_key='alice'")[0]
        self.assertEqual(row["machine_action"], "retarget")
        self.assertEqual(row["machine_proposed_public_identifier"], "alice-correct")

        baton = self.root / "review.csv"
        result = ApplyRetargets(
            db=self.db, overrides_csv=baton, people_csv=self.root / "people.csv",
            profile_cache_dir=self.root / "cache", out_csv=self.root / "retarget.csv",
        ).run()
        self.assertTrue(baton.exists())
        self.assertEqual(result.approved_retargets, 0)

    def test_synthesis_projects_fixed_facts_artifact(self) -> None:
        facts_dir = self.root / "facts"
        facts_dir.mkdir()
        record = {
            "final_confidence": 0.88,
            "facts": {"network_worth": {"decision": "maybe", "reason": "uncertain"}},
        }
        (facts_dir / "person-1.jsonl").write_text(
            json.dumps(record) + "\n", encoding="utf-8"
        )
        result = project_facts(self.db, facts_dir)
        self.assertEqual(result["synced_people"], 1)
        fact = self.db.query("SELECT * FROM facts WHERE subject_key='person-1'")[0]
        self.assertEqual(fact["machine_worth"], "maybe")
        self.assertEqual(fact["confidence"], 0.88)


if __name__ == "__main__":
    unittest.main()
