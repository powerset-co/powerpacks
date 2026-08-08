"""Synthetic-identity rekey migration regression tests.

Before 2026-08-08, a synthetic candidate's `links.row_key`/`public_identifier`
(and its dependent `candidate_people`/`artifacts`/`synthetic_profiles` rows)
were a hash of whichever email/phone won that assembly run. That hash could
change between runs, so the row_key was NOT a stable key — and some installs
carry a human yes/no decision recorded against the old, hash-based row_key.

These tests prove `migrate_legacy_synthetic_keys` re-keys those rows onto
the stable parent id in place, without dropping the human decision, and that
`AssembleSyntheticProfile.execute()` (which calls the migration first) keeps
serving that decision under the new key on every subsequent run."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.enrich.assemble_synthetic_profile import (
    AssembleSyntheticProfile,
)
from packs.ingestion.primitives.deep_context.db.context_queries import (
    migrate_legacy_synthetic_keys,
)
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactProjection,
    ArtifactRow,
    CandidatePeopleProjection,
    CandidatePersonRow,
    FactRow,
    LinkRow,
    ParentRow,
    PersonRow,
    ProjectionStatus,
    ResearchRow,
    ResearchStatus,
    ReviewAction,
    ReviewSource,
    RowKind,
    SyntheticProfileRow,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db

OLD_KEY = "synth-email-1af7a7e7773a"
PARENT_ID = "parent-a1b2c3d4e5f6"


def _seed_legacy_synthetic(db: Db) -> None:
    """Project one synthetic candidate under the pre-2026-08-08 hash key."""
    profile_payload = {
        "id": OLD_KEY, "public_identifier": OLD_KEY, "full_name": "Jordan Bravo",
        "headline": "Product Manager at Example Co", "approved": "",
    }
    profile_json = json.dumps(profile_payload, sort_keys=True, separators=(",", ":"))
    artifact_key = f"synthetic:{OLD_KEY}"
    db.project_rows((ArtifactProjection(
        artifact=ArtifactRow(
            artifact_key=artifact_key,
            kind=ArtifactKind.SYNTHETIC.value,
            parent_id=PARENT_ID,
            path=f"/synthetic/{OLD_KEY}.json",
            content_fingerprint="fp-1",
            status=ProjectionStatus.PROJECTED.value,
            candidate_key=OLD_KEY,
        ),
        candidate=LinkRow(
            OLD_KEY, PARENT_ID, OLD_KEY, RowKind.SYNTHETIC.value,
            display_name="Jordan Bravo", source=WriterSource.DEEP_RESEARCH.value,
        ),
        candidate_people=CandidatePeopleProjection(
            OLD_KEY, (CandidatePersonRow(OLD_KEY, "person-a", PARENT_ID),),
        ),
        synthetic_profile=SyntheticProfileRow(
            OLD_KEY, OLD_KEY, profile_json, artifact_key, None, "Jordan Bravo",
        ),
    ),))


class MigrateLegacySyntheticKeysTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Db(Path(self.temp.name) / "deep-context.sqlite")
        self.db.project_rows((
            ParentRow(PARENT_ID, f"parent-worth:{PARENT_ID}", "Jordan Bravo"),
            PersonRow("person-a", PARENT_ID, display_name="Jordan Bravo"),
        ))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_rekeys_links_and_every_dependent_row(self) -> None:
        _seed_legacy_synthetic(self.db)

        migrated = migrate_legacy_synthetic_keys(self.db)

        self.assertEqual(migrated, 1)
        rows = self.db.query("SELECT row_key, public_identifier, parent_id, kind FROM links")
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0]["row_key"], rows[0]["public_identifier"], rows[0]["kind"]),
            (PARENT_ID, PARENT_ID, RowKind.SYNTHETIC.value),
        )
        self.assertFalse(self.db.query("SELECT 1 FROM links WHERE row_key=?", (OLD_KEY,)))

        cp = self.db.query("SELECT row_key FROM candidate_people")
        self.assertEqual([row["row_key"] for row in cp], [PARENT_ID])
        artifact = self.db.query("SELECT candidate_key FROM artifacts WHERE kind='synthetic'")
        self.assertEqual([row["candidate_key"] for row in artifact], [PARENT_ID])
        sp = self.db.query("SELECT public_identifier, candidate_key FROM synthetic_profiles")
        self.assertEqual(len(sp), 1)
        self.assertEqual((sp[0]["public_identifier"], sp[0]["candidate_key"]), (PARENT_ID, PARENT_ID))

    def test_preserves_a_human_decision_across_the_rekey(self) -> None:
        _seed_legacy_synthetic(self.db)
        self.db.decide_identity(
            OLD_KEY, ReviewAction.VERIFY.value, approved="yes",
            source=ReviewSource.REVIEW.value, note="confirmed by owner",
        )

        migrated = migrate_legacy_synthetic_keys(self.db)

        self.assertEqual(migrated, 1)
        row = self.db.query(
            "SELECT decision_action, decision_approved, decision_source, decision_note "
            "FROM links WHERE row_key=?",
            (PARENT_ID,),
        )
        self.assertEqual(len(row), 1)
        self.assertEqual(
            (row[0]["decision_action"], row[0]["decision_approved"],
             row[0]["decision_source"], row[0]["decision_note"]),
            (ReviewAction.VERIFY.value, "yes", ReviewSource.REVIEW.value, "confirmed by owner"),
        )

    def test_no_legacy_rows_is_a_no_op(self) -> None:
        self.assertEqual(migrate_legacy_synthetic_keys(self.db), 0)

    def test_rerunning_after_migration_is_idempotent(self) -> None:
        _seed_legacy_synthetic(self.db)
        self.assertEqual(migrate_legacy_synthetic_keys(self.db), 1)
        self.assertEqual(migrate_legacy_synthetic_keys(self.db), 0)


class AssembleSyntheticProfileRekeyEndToEndTest(unittest.TestCase):
    """The full `execute()` path: migrate-then-assemble, proving a synthetic
    row built under the OLD key rekeys itself and keeps a human decision on
    the very next pipeline run — no research re-run required."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.out = self.root / "synthetic-people.csv"
        self.db = Db(self.root / "deep-context.sqlite")
        self.db.project_rows((
            ParentRow(PARENT_ID, f"parent-worth:{PARENT_ID}", "Jordan Bravo", display_slug="jordan-bravo"),
            PersonRow("person-a", PARENT_ID, display_name="Jordan Bravo"),
            ArtifactRow(
                f"facts:{PARENT_ID}", ArtifactKind.FACTS.value, PARENT_ID,
                f"/facts/{PARENT_ID}.jsonl", "worth-fp", ProjectionStatus.PROJECTED.value,
                payload_json=json.dumps({"facts": {"network_worth": {"decision": "yes", "reason": "fixture"}}}),
            ),
            FactRow(
                PARENT_ID, PARENT_ID, f"facts:{PARENT_ID}", machine_worth="yes",
                machine_worth_reason="fixture",
                facts_json=json.dumps({"network_worth": {"decision": "yes", "reason": "fixture"}}),
            ),
            LinkRow(
                "candidate:email:jordan@example.com", PARENT_ID,
                "candidate:email:jordan@example.com", RowKind.CANDIDATE_EMAIL.value,
                source=WriterSource.DEEP_RESEARCH.value,
            ),
            ResearchRow(
                handle="jordan-bravo", parent_id=PARENT_ID, status=ResearchStatus.COMPLETE.value,
                candidate_key="candidate:email:jordan@example.com",
                result_json=json.dumps({
                    "person": {"full_name": "Jordan Bravo", "confidence": 0.9},
                    "social": {"linkedin_url": ""},
                    "positions": [{
                        "title": "Founder", "company_name": "Example Labs", "is_current": True,
                    }],
                    "metadata": {"estimated_completeness": 0.9},
                }),
            ),
        ))
        _seed_legacy_synthetic(self.db)
        self.db.decide_identity(
            OLD_KEY, ReviewAction.VERIFY.value, approved="yes", source=ReviewSource.REVIEW.value,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_execute_rekeys_and_preserves_the_decision(self) -> None:
        result = AssembleSyntheticProfile(db=self.db, out=self.out, manifest=None).execute()

        self.assertEqual(result["migrated_legacy_synthetic_keys"], 1)
        self.assertEqual(result["preserved_user_rows"], 1)
        self.assertEqual(result["built"], 0)

        links = self.db.query("SELECT row_key, decision_approved FROM links WHERE kind='synthetic'")
        self.assertEqual(len(links), 1)
        self.assertEqual((links[0]["row_key"], links[0]["decision_approved"]), (PARENT_ID, "yes"))

        rows = list(csv_rows(self.out))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["public_identifier"], PARENT_ID)
        self.assertEqual(rows[0]["approved"], "yes")

    def test_second_run_is_stable_under_the_new_key(self) -> None:
        AssembleSyntheticProfile(db=self.db, out=self.out, manifest=None).execute()

        result = AssembleSyntheticProfile(db=self.db, out=self.out, manifest=None).execute()

        self.assertEqual(result["migrated_legacy_synthetic_keys"], 0)
        self.assertEqual(result["preserved_user_rows"], 1)
        rows = list(csv_rows(self.out))
        self.assertEqual((rows[0]["public_identifier"], rows[0]["approved"]), (PARENT_ID, "yes"))


def csv_rows(path: Path):
    with path.open(newline="", encoding="utf-8") as handle:
        yield from csv.DictReader(handle)


if __name__ == "__main__":
    unittest.main()
