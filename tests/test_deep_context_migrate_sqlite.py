from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context import migrate_sqlite
from packs.ingestion.primitives.deep_context.check_readiness import CheckReadiness
from packs.ingestion.primitives.deep_context.db.models import ParentRow, PersonRow
from packs.ingestion.primitives.deep_context.db.store import Db


class DeepContextMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / ".powerpacks"
        self.deep_context = self.state / "deep-context"
        self.facts = self.deep_context / "facts"
        self.facts.mkdir(parents=True)
        self.fact = self.facts / "person-a.jsonl"
        self.fact.write_text(json.dumps({
            "canonical_name": "Jordan Bravo",
            "network_worth": {"decision": "yes", "reason": "known collaborator"},
        }) + "\n", encoding="utf-8")
        self.people_csv = self.state / "network-import/merged/people.csv"
        self.people_csv.parent.mkdir(parents=True)
        self.people_csv.write_text("id,full_name\nperson-a,Jordan Bravo\n", encoding="utf-8")
        self.wacli = self.state / "messages/wacli.db"
        self.wacli.parent.mkdir(parents=True)
        self.wacli.touch()
        self.db_path = self.deep_context / "deep-context.sqlite"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def readiness(self, db: Db | None = None) -> dict:
        with (
            mock.patch(
                "packs.ingestion.primitives.deep_context.check_readiness.sources.probe_chat_db",
                return_value={"exists": False, "readable": False, "messages": 0, "error": None},
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

        self.assertFalse(result["ready"])
        self.assertEqual(
            result["checks"]["canonical_sqlite"]["status"], "migration_required"
        )
        self.assertEqual(result["next_command"], "bin/deep-context migrate-sqlite")
        self.assertFalse(self.db_path.exists())

    def test_empty_database_requires_migration_but_populated_database_does_not(self) -> None:
        database = Db(self.db_path)
        self.assertEqual(
            self.readiness(database)["checks"]["canonical_sqlite"]["status"],
            "migration_required",
        )
        database.project_rows((
            ParentRow("parent-one", "parent-one"),
            PersonRow("person-a", "parent-one"),
        ))

        self.assertEqual(
            self.readiness(database)["checks"]["canonical_sqlite"]["status"], "ok"
        )

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
            DEFAULT_PEOPLE_CSV=self.people_csv,
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
        self.assertEqual(database.query("SELECT COUNT(*) AS n FROM people")[0]["n"], 1)
        self.assertEqual(database.query("SELECT COUNT(*) AS n FROM facts")[0]["n"], 1)


if __name__ == "__main__":
    unittest.main()
