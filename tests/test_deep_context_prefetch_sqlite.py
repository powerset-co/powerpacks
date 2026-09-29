from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.models import LinkRow, ParentRow, PersonRow
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import project_imported_people, read_imported_people
from packs.ingestion.primitives.deep_context.enrich.profiles.prefetch import PrefetchProfiles, classify_queue
from packs.ingestion.primitives.deep_context.enrich.profiles.models import ProfileResult, ProfileTarget
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS
from packs.shared.csv_io import CsvIO


class PrefetchSqliteTest(unittest.TestCase):
    def test_synthetic_retarget_queues_the_real_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            db.project_rows((
                ParentRow("parent-1", "parent-worth:parent-1"),
                PersonRow("candidate:email:jordan@example.test", "parent-1"),
                LinkRow("synthetic:jordan", "parent-1", "synthetic:jordan", "synthetic",
                        source="deep-context-reconcile"),
            ))
            db.decide_identity("synthetic:jordan", "retarget",
                               replacement_url="https://www.linkedin.com/in/jordan-bravo",
                               replacement_public_identifier="jordan-bravo")
            result = PrefetchProfiles(db=db, profile_cache_dir=root / "cache").run()
            self.assertEqual((result.status, result.distinct_profiles, result.cache_misses),
                             ("dry_run", 1, 1))

    def test_old_projected_profile_cannot_satisfy_a_retarget(self) -> None:
        target = ProfileTarget("new-slug", "https://www.linkedin.com/in/new-slug", "old-slug", "parent-1")
        profile = ProfileResult.from_payload("old-slug", "https://www.linkedin.com/in/old-slug", {
            "state": "content", "data": {"full_name": "Jordan Bravo"},
            "normalized_profile": {"success": True, "full_name": "Jordan Bravo",
                                   "public_identifier": "old-slug"},
        })
        result = classify_queue([target], {"old-slug": profile})
        self.assertEqual(result.fetch, (target,))
        self.assertEqual(result.cached, ())

    def test_approved_retarget_queues_new_slug_without_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            people_csv = root / "people.csv"
            CsvIO.write_dict_rows(people_csv, PEOPLE_SCHEMA_COLUMNS, [
                {"id": "person-1", "full_name": "Jordan Bravo", "primary_email": "jordan@example.test",
                 "public_identifier": "old-slug", "linkedin_url": "https://www.linkedin.com/in/old-slug"},
            ])
            project_imported_people(db, read_imported_people(people_csv))
            db.decide_identity("old-slug", "retarget", approved="yes",
                               replacement_url="https://www.linkedin.com/in/new-slug",
                               replacement_public_identifier="new-slug")
            result = PrefetchProfiles(db=db, profile_cache_dir=root / "cache").run()
            self.assertEqual((result.status, result.distinct_profiles, result.cache_misses),
                             ("dry_run", 1, 1))
