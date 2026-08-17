from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.migration import legacy, migrate_sqlite
from packs.ingestion.primitives.deep_context.shared.check_readiness import CheckReadiness
from packs.ingestion.primitives.deep_context.collection.models import ChatDbProbe
from packs.ingestion.primitives.deep_context.shared.readiness_models import ReadinessReport
from packs.ingestion.primitives.deep_context.db.models import ParentRow, PersonRow
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.ensure_parents import EnsureParents


class DeepContextMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / ".powerpacks"
        self.deep_context = self.state / "deep-context"
        self.facts = self.deep_context / "facts"
        self.facts.mkdir(parents=True)
        self.fact = self.facts / "person-a.jsonl"
        self.fact.write_text(
            json.dumps(
                {
                    "canonical_name": "Jordan Bravo",
                    "network_worth": {"decision": "yes", "reason": "known collaborator"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.people_csv = self.state / "network-import/merged/people.csv"
        self.people_csv.parent.mkdir(parents=True)
        self.people_csv.write_text("id,full_name\nperson-a,Jordan Bravo\n", encoding="utf-8")
        self.wacli = self.state / "messages/wacli.db"
        self.wacli.parent.mkdir(parents=True)
        self.wacli.touch()
        self.db_path = self.deep_context / "deep-context.sqlite"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def readiness(self, db: Db | None = None) -> ReadinessReport:
        with (
            mock.patch(
                "packs.ingestion.primitives.deep_context.shared.check_readiness.context_sources.probe_chat_db",
                return_value=ChatDbProbe(False, False, 0, 0, None),
            ),
            mock.patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-key"}),
        ):
            return CheckReadiness(
                db=db,
                db_path=self.db_path,
                people_csv=self.people_csv,
                msgvault_db=self.state / "missing-msgvault.db",
                chat_db=self.state / "missing-chat.db",
                wacli_db=self.wacli,
            ).run()

    def test_readiness_requires_migration_without_creating_database(self) -> None:
        result = self.readiness()

        self.assertFalse(result.ready)
        self.assertEqual(result.checks.canonical_sqlite.status, "migration_required")
        self.assertEqual(result.next_command, "bin/deep-context migrate-sqlite")
        self.assertFalse(self.db_path.exists())

    def test_empty_database_requires_migration_but_populated_database_does_not(self) -> None:
        database = Db(self.db_path)
        self.assertEqual(
            self.readiness(database).checks.canonical_sqlite.status,
            "migration_required",
        )
        database.project_rows(
            (
                ParentRow("parent-one", "parent-one"),
                PersonRow("person-a", "parent-one"),
            )
        )

        self.assertEqual(self.readiness(database).checks.canonical_sqlite.status, "ok")

    def test_migration_cli_imports_once_and_refuses_a_second_import(self) -> None:
        missing = self.state / "missing"
        review = self.state / "network-import/overrides/review.csv"
        with mock.patch.multiple(
            migrate_sqlite,
            LINKEDIN_OVERRIDES_CSV=review,
            SYNTHETIC_PEOPLE_CSV=review.parent / "synthetic-people.csv",
            LEGACY_INDEX_JSON=self.deep_context / "index.json",
            FACTS_DIR=self.facts,
            VERDICTS_JSONL=self.deep_context / "reconcile/verdicts.jsonl",
            DEEP_RESEARCH_DIR=self.deep_context / "reconcile/deep-research",
            OWNER_JSON=self.deep_context / "owner.json",
            PROFILE_CACHE_DIR=self.state / "network-import/profile_cache_v2",
            REVIEW_DIR=self.deep_context / "review",
            LEGACY_MERGE_VERDICTS_CSV=self.deep_context / "merge-verdicts.csv",
            MERGE_CSV=self.deep_context / "merge-candidates.csv",
            RAW_DIR=missing / "raw",
        ):
            first = migrate_sqlite.main(["--db", str(self.db_path)])
            second = migrate_sqlite.main(["--db", str(self.db_path)])

        self.assertEqual((first, second), (0, 1))
        database = Db(self.db_path)
        migrated_counts = (
            database.query("SELECT COUNT(*) AS n FROM parents")[0]["n"],
            database.query("SELECT COUNT(*) AS n FROM people")[0]["n"],
        )
        EnsureParents(db=database, people_csv=self.people_csv).run()
        projected_counts = (
            database.query("SELECT COUNT(*) AS n FROM parents")[0]["n"],
            database.query("SELECT COUNT(*) AS n FROM people")[0]["n"],
        )
        self.assertEqual(migrated_counts, projected_counts)
        self.assertEqual(projected_counts, (1, 1))
        self.assertEqual(database.query("SELECT COUNT(*) AS n FROM facts")[0]["n"], 1)


class LegacyPassContributionTests(unittest.TestCase):
    """Legacy rules survive, while numeric confidence requires a judge payload.

    Several passes contribute to one links row from different legacy files.
    A review score may derive a rule outcome such as authoritative detach, but
    it is not imported as judge confidence. A verdict may supply its own
    confidence and fingerprint; silence never fabricates either one.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.review = root / "review.csv"
        self.verdicts = root / "verdicts.jsonl"
        self.db_path = root / "deep-context.sqlite"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _migrate(self, verdict_line: dict[str, object]) -> dict[str, object]:
        """Import one review row plus one verdict for the same candidate."""
        self.review.write_text(
            "public_identifier,person_id,action,approved,confidence,reason,"
            "llm_judge_fingerprint,linkedin_url\n"
            "jordan-bravo,person-a,detach,auto,0.93,attached profile is a different person,"
            "fingerprint-from-review,https://www.linkedin.com/in/jordan-bravo\n",
            encoding="utf-8",
        )
        self.verdicts.write_text(json.dumps(verdict_line) + "\n", encoding="utf-8")
        database = Db(self.db_path)
        legacy.import_legacy(database, review_csv=self.review, verdicts_jsonl=self.verdicts)
        return dict(database.query("SELECT * FROM links WHERE row_key='jordan-bravo'")[0])

    def test_a_silent_verdict_does_not_promote_review_score_to_judge_confidence(self) -> None:
        # The real shape: every one of the owner's 544 verdict lines looks like
        # this — no "confidence" key anywhere in the payload.
        row = self._migrate({"candidate_key": "jordan-bravo", "person_ids": ["person-a"],
                             "verdict": {"verdict": "wrong_person"}})

        self.assertIsNone(row["machine_confidence"])

    def test_a_legacy_judge_fingerprint_is_not_imported_at_all(self) -> None:
        """A pre-SQLite fingerprint is not a cache key, and keeping it costs money.

        It came from `proposal_fingerprint` over a different payload, so it can
        never equal what `judgment_fingerprint` computes now. Left absent, the
        retarget path reuses the proposal for free when the URL still matches;
        present-but-stale, it falls through to a paid re-judge.
        """
        row = self._migrate({"candidate_key": "jordan-bravo", "person_ids": ["person-a"],
                             "verdict": {"verdict": "wrong_person"}})

        self.assertIsNone(row["judgment_fingerprint"])

    def test_a_verdict_that_carries_its_own_fingerprint_still_stores_it(self) -> None:
        """Only the review.csv column is dropped — a verdict-borne fingerprint
        was written by the current algorithm and remains a usable cache key."""
        row = self._migrate({"candidate_key": "jordan-bravo", "person_ids": ["person-a"],
                             "fingerprint": "fingerprint-from-verdict",
                             "verdict": {"verdict": "wrong_person"}})

        self.assertEqual(row["judgment_fingerprint"], "fingerprint-from-verdict")

    def test_a_speaking_verdict_still_wins(self) -> None:
        """Not "never overwrite" — a source that HAS a value is authoritative."""
        row = self._migrate({
            "candidate_key": "jordan-bravo", "person_ids": ["person-a"],
            "fingerprint": "fingerprint-from-verdict",
            "verdict": {"verdict": "wrong_person", "confidence": 0.41, "reason": "different employer"},
        })

        self.assertEqual(row["machine_confidence"], 0.41)
        self.assertEqual(row["judgment_fingerprint"], "fingerprint-from-verdict")
        self.assertEqual(row["machine_reason"], "different employer")

    def test_a_silent_verdict_cannot_revoke_an_authoritative_detach(self) -> None:
        """review.csv earned the flag at 0.93; a verdict with no confidence
        cannot compute it, and must not therefore withdraw it."""
        row = self._migrate({"candidate_key": "jordan-bravo", "person_ids": ["person-a"],
                             "verdict": {"verdict": "wrong_person"}})

        self.assertEqual(row["authoritative_detach"], 1)


if __name__ == "__main__":
    unittest.main()
