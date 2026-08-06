"""Synthetic fallback and enrichment terminal-ownership regression tests."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context import deep_research_contacts as research
from packs.ingestion.primitives.deep_context.parallel_research import driver
from packs.ingestion.primitives.deep_context.research_reconcile.selection import QUEUE_FIELDS
from packs.ingestion.primitives.deep_context.assemble_synthetic_profile import (
    AssembleSyntheticProfile,
    ResearchContact,
    build_synthetic_row,
)
from packs.ingestion.primitives.deep_context.db.models import (
    IdentityMachineProjection,
    ParentRow,
    PersonRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.prefetch_profiles import PrefetchProfiles


def query(db: Db, sql: str):
    return db.query(sql)


class SyntheticPrefetchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.research_dir = self.root / "deep-research"
        self.research_dir.mkdir()
        self.manifest = self.research_dir / "manifest.json"
        self.queue = self.research_dir / "research_queue.csv"
        self.people = self.root / "people.csv"
        self.output = self.root / "synthetic-people.csv"
        self.cache = self.root / "profile-cache"
        self.db = Db(self.root / "deep-context.sqlite")
        self.db.project_rows((
            ParentRow(
                "parent-1", "parent-worth:parent-1", "Jordan Bravo",
                display_slug="jordan-bravo",
            ),
            PersonRow("person-a", "parent-1", display_name="Jordan Bravo"),
        ))
        self.queue_row = {
            "handle": "jordan-bravo",
            "source_parent_slug": "jordan-bravo",
            "source_person_ids": json.dumps(["person-a"]),
            "source_candidate_public_identifier": "candidate:email:jordan@example.com",
            "display_name": "Jordan Bravo",
            "bio": "Synthetic fixture",
            "known_info": "Known collaborator",
            "primary_email": "jordan@example.com",
            "phone_e164": "",
            "area_code": "",
            "source_channel": "email",
            "retarget_hint": "Find the correct profile",
        }
        with self.queue.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=QUEUE_FIELDS)
            writer.writeheader()
            writer.writerow(self.queue_row)
        with self.people.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=["id", "primary_email", "primary_phone"]
            )
            writer.writeheader()
            writer.writerow({
                "id": "person-a",
                "primary_email": "jordan@example.com",
                "primary_phone": "",
            })

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_no_linkedin_result(
        self, completeness: float = 0.72, linkedin_url: str = "",
    ) -> None:
        person_dir = self.research_dir / "jordan-bravo"
        person_dir.mkdir()
        (person_dir / "01_research_parallel.json").write_text(json.dumps({
            "person": {"full_name": "Jordan Bravo", "confidence": 0.91},
            "social": {"linkedin_url": linkedin_url},
            "positions": [{
                "title": "Founder", "company_name": "Example Labs", "is_current": True,
            }],
            "metadata": {"estimated_completeness": completeness},
        }), encoding="utf-8")
        params = research.ResearchRunParams(
            input_csv=self.queue,
            output_dir=self.research_dir,
            manifest=str(self.manifest),
            db=self.db,
        )
        driver.report_progress(
            params,
            "research_complete",
            {"total": 1, "completed": 1, "pending": 0, "failed": 0},
            selection={"fingerprint": "selection-1"},
        )

    def test_rejected_research_linkedin_still_yields_synthetic(self) -> None:
        self._write_no_linkedin_result(
            linkedin_url="https://www.linkedin.com/in/not-jordan-bravo",
        )
        self.db.project_identity((IdentityMachineProjection(
            "candidate:email:jordan@example.com",
            machine_reject="yes",
            machine_reject_reason="wrong person",
        ),))

        result = AssembleSyntheticProfile(
            db=self.db, research_dir=self.research_dir, queue_csv=self.queue,
            out=self.output, manifest=self.manifest,
        ).execute()

        self.assertEqual((result.built, result.skipped_with_linkedin), (1, 0))
        self.assertEqual(query(self.db, "SELECT count(*) FROM synthetic_profiles")[0][0], 1)

    def test_no_linkedin_result_yields_one_synthetic_then_prefetch_completes(self) -> None:
        self._write_no_linkedin_result()
        assembled = AssembleSyntheticProfile(
            db=self.db,
            research_dir=self.research_dir,
            queue_csv=self.queue,
            people_csv=self.people,
            verdicts_jsonl=self.root / "verdicts.jsonl",
            out=self.output,
            manifest=self.manifest,
        ).execute()

        self.assertEqual((assembled.built, assembled.auto_approved), (1, 1))
        self.assertEqual(query(self.db, "SELECT count(*) FROM synthetic_profiles")[0][0], 1)
        receipt = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual((receipt["status"], receipt["phase"]),
                         ("research_complete", "profiles_pending"))

        with mock.patch(
            "packs.ingestion.primitives.deep_context.prefetch_profiles.rapidapi_key",
            return_value="",
        ):
            prefetched = PrefetchProfiles(
                db=self.db,
                profile_cache_dir=self.cache,
                enrichment_manifest=self.manifest,
                fetch=True,
            ).execute()

        self.assertEqual(prefetched.status, "completed")
        receipt = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual((receipt["status"], receipt["phase"]),
                         ("completed", "profiles_complete"))
        self.assertEqual(query(self.db, "SELECT status FROM jobs WHERE name='enrich'")[0][0],
                         "applied")

    def test_completeness_threshold_preserves_auto_and_human_review_gates(self) -> None:
        contact = ResearchContact(handle="jordan", display_name="Jordan Bravo")
        profile = {
            "person": {"full_name": "Jordan Bravo"},
            "positions": [{"title": "Founder"}],
            "metadata": {"estimated_completeness": 0.6},
        }
        self.assertEqual(build_synthetic_row(profile, contact, None, "person-a")["approved"],
                         "auto")
        profile["metadata"]["estimated_completeness"] = 0.59
        self.assertEqual(build_synthetic_row(profile, contact, None, "person-a")["approved"],
                         "")


if __name__ == "__main__":
    unittest.main()
