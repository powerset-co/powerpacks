"""Synthetic fallback and enrichment terminal-ownership regression tests."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from parallel.types import TaskRunJsonOutput

from packs.ingestion.primitives.deep_context.manifests.receipt_counts import ReceiptCounts
from packs.ingestion.primitives.deep_context.enrich.parallel_research import driver, projection
from packs.ingestion.primitives.deep_context.enrich.parallel_research import models as research_models
from packs.ingestion.primitives.deep_context.enrich.parallel_research.queue import (
    ContactChannel,
    ResearchQueueRow,
)
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.selection import QUEUE_FIELDS
from packs.ingestion.primitives.deep_context.enrich.synthetic.assemble import (
    AssembleSyntheticProfile,
    build_synthetic_row,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult
from packs.ingestion.primitives.deep_context.db.identity_views import linkedin_queue
from packs.ingestion.primitives.deep_context.db.view_models import SyntheticFallbackRow
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    FactRow,
    IdentityMachineProjection,
    LinkRow,
    ParentRow,
    PersonRow,
    ProjectionStatus,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.profiles.prefetch import (
    PrefetchProfiles,
    review_queue_links,
)


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
            ArtifactRow(
                "facts:parent-1",
                ArtifactKind.FACTS.value,
                "parent-1",
                "/facts/parent-1.jsonl",
                "worth-parent-1",
                ProjectionStatus.PROJECTED.value,
                payload_json=json.dumps({"facts": {"network_worth": {
                    "decision": "yes", "reason": "fixture",
                }}}),
            ),
            FactRow(
                "parent-1",
                "parent-1",
                "facts:parent-1",
                machine_worth="yes",
                machine_worth_reason="fixture",
                facts_json=json.dumps({"network_worth": {
                    "decision": "yes", "reason": "fixture",
                }}),
            ),
        ))
        self.queue_row = ResearchQueueRow(
            parent_id="parent-1",
            candidate_exists=False,
            row_key="candidate:email:jordan@example.com",
            handle="jordan-bravo",
            source_parent_slug="jordan-bravo",
            source_person_ids=("person-a",),
            source_candidate_public_identifier="candidate:email:jordan@example.com",
            display_name="Jordan Bravo",
            bio="Synthetic fixture",
            known_info="Known collaborator",
            primary_email="jordan@example.com",
            source_channel=ContactChannel.EMAIL,
            retarget_hint="Find the correct profile",
        )
        with self.queue.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=QUEUE_FIELDS)
            writer.writeheader()
            writer.writerow(self.queue_row.csv_dict(QUEUE_FIELDS))
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
        self, linkedin_url: str = "",
    ) -> None:
        person_dir = self.research_dir / "jordan-bravo"
        person_dir.mkdir()
        output = TaskRunJsonOutput.model_validate({
            "type": "json",
            "content": {
                "real_name": "Jordan Bravo",
                "linkedin_url": linkedin_url or None,
                "work_experience": [{
                    "title": "Founder", "company_name": "Example Labs", "is_current": True,
                }],
                "education": [],
                "location_city": "Oakland",
                "location_country": "US",
                "summary": "Founder",
            },
            "basis": [{
                "field": "real_name", "reasoning": "Official biography", "confidence": "high", "citations": [],
            }],
        })
        result = ResearchResult.from_output(output)
        path = person_dir / "00_parallel_result.json"
        data = (json.dumps(result.to_payload(), sort_keys=True) + "\n").encode()
        path.write_bytes(data)
        params = research_models.ResearchRunParams(
            output_dir=self.research_dir,
            rows=(self.queue_row,),
            manifest=self.manifest,
            db=self.db,
        )
        driver.report_progress(
            params,
            "research_complete",
            ReceiptCounts(1, 1, 0, 0),
            projections=(projection.research_artifact_projection(
                params, self.queue_row, result, path, data
            ),),
        )

    def test_rejected_research_linkedin_still_yields_synthetic(self) -> None:
        self._write_no_linkedin_result(
            linkedin_url="https://www.linkedin.com/in/not-jordan-bravo",
        )
        self.queue.unlink()
        self.db.project_rows((IdentityMachineProjection(
            "candidate:email:jordan@example.com",
            machine_reject="yes",
            machine_reject_reason="wrong person",
            source=WriterSource.DEEP_RESEARCH.value,
        ),))

        result = AssembleSyntheticProfile(
            db=self.db, research_dir=self.research_dir, out=self.output,
            manifest=self.manifest,
        ).execute()

        self.assertEqual((result["built"], result["skipped_with_linkedin"]), (1, 0))
        self.assertEqual(query(self.db, "SELECT count(*) FROM synthetic_profiles")[0][0], 1)

    def test_no_linkedin_result_yields_one_synthetic_then_prefetch_completes(self) -> None:
        self._write_no_linkedin_result()
        self.queue.unlink()
        assembled = AssembleSyntheticProfile(
            db=self.db,
            research_dir=self.research_dir,
            out=self.output,
            manifest=self.manifest,
        ).execute()

        self.assertEqual((assembled["built"], assembled["pending_review"]), (1, 1))
        self.assertEqual(assembled["auto_approved"], 0)
        self.assertEqual(query(self.db, "SELECT count(*) FROM synthetic_profiles")[0][0], 1)
        receipt = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual((receipt["status"], receipt["phase"]),
                         ("research_complete", "profiles_pending"))

        with mock.patch(
            "packs.ingestion.primitives.deep_context.enrich.profiles.prefetch.provider_key_available",
            return_value=False,
        ):
            prefetched = PrefetchProfiles(
                db=self.db,
                profile_cache_dir=self.cache,
                enrichment_manifest=self.manifest,
                fetch=True,
            ).execute()

        self.assertEqual(prefetched["status"], "completed")
        receipt = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual((receipt["status"], receipt["phase"]),
                         ("completed", "profiles_complete"))
        self.assertNotIn("artifacts", receipt)
        self.assertFalse(query(self.db, "SELECT * FROM jobs"))

    def test_custom_output_without_manifest_still_projects_sqlite(self) -> None:
        self._write_no_linkedin_result()
        result = AssembleSyntheticProfile(
            db=self.db,
            research_dir=self.research_dir,
            out=self.output,
            manifest=None,
        ).execute()

        self.assertEqual(result["built"], 1)
        self.assertEqual(query(self.db, "SELECT count(*) FROM synthetic_profiles")[0][0], 1)
        self.assertTrue(next((self.research_dir / "synthetic").glob("*.json"), None))

    def test_provider_basis_never_auto_approves_synthetic_identity(self) -> None:
        source = SyntheticFallbackRow(
            handle="jordan",
            parent_id="parent-a",
            candidate_key="",
            result_json="",
            display_name="Jordan Bravo",
            display_slug="",
            effective_worth="yes",
            machine_reject="",
            person_ids=("person-a",),
            primary_email="",
            phone_e164="",
            existing_synthetics=(),
        )
        result = ResearchResult.from_output(TaskRunJsonOutput.model_validate({
            "type": "json",
            "content": {
                "real_name": "Jordan Bravo",
                "work_experience": [{"title": "Founder", "company_name": "Example", "is_current": True}],
                "education": [],
                "location_city": "Oakland",
                "location_country": "US",
                "linkedin_url": None,
                "summary": "Founder",
            },
            "basis": [{"field": "real_name", "reasoning": "fixture", "confidence": "high", "citations": []}],
        }))
        row = build_synthetic_row(result, source, ["person-a"])
        self.assertIsNone(row.approved)
        self.assertEqual(json.loads(row.to_payload()["synthetic_metadata"])["basis"][0]["confidence"], "high")

    def test_profile_prefetch_keeps_the_canonical_candidate_row_key(self) -> None:
        self.db.project_rows((LinkRow(
            row_key="Identity-Row-42",
            parent_id="parent-1",
            public_identifier="jordan-bravo",
            kind="pub",
            linkedin_url="https://www.linkedin.com/in/jordan-bravo",
            paid_profile=1,
            source=WriterSource.RECONCILE.value,
        ),))

        links = review_queue_links(linkedin_queue(self.db))

        self.assertEqual(links[0].candidate_key, "identity-row-42")


if __name__ == "__main__":
    unittest.main()
