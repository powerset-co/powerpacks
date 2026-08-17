"""Synthetic fallback and enrichment terminal-ownership regression tests."""

from __future__ import annotations

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
    ResearchQueueRow,
)
from packs.ingestion.primitives.deep_context.enrich.synthetic.assemble import (
    AssembleSyntheticProfile,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult
from packs.ingestion.primitives.deep_context.db.identity_views import linkedin_queue
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    FactRow,
    IdentityMachineProjection,
    LinkRow,
    ParentRow,
    PersonRow,
    ProjectionStatus,
    ReviewSource,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.profiles.prefetch import (
    PrefetchProfiles,
    review_queue_links,
)
from packs.ingestion.primitives.deep_context.enrich.profiles import projection as profile_projection
from packs.ingestion.primitives.enrich import rapidapi_client


def query(db: Db, sql: str):
    return db.query(sql)


class SyntheticPrefetchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.research_dir = self.root / "deep-research"
        self.research_dir.mkdir()
        self.manifest = self.research_dir / "manifest.json"
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
            source_person_ids=("person-a",),
            source_candidate_public_identifier="candidate:email:jordan@example.com",
            display_name="Jordan Bravo",
            bio="Synthetic fixture",
            known_info="Known collaborator",
            primary_email="jordan@example.com",
            retarget_hint="Find the correct profile",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_no_linkedin_result(
        self, linkedin_url: str = "",
    ) -> None:
        person_dir = self.research_dir / "jordan-bravo"
        person_dir.mkdir(exist_ok=True)
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
        self.db.project_rows((projection.research_artifact_projection(
            params, self.queue_row, result, path, data
        ),))
        driver.report_progress(
            params, "research_complete", ReceiptCounts(1, 1, 0, 0)
        )

    def test_rejected_research_linkedin_still_yields_synthetic(self) -> None:
        self._write_no_linkedin_result(
            linkedin_url="https://www.linkedin.com/in/not-jordan-bravo",
        )
        self.db.project_rows((IdentityMachineProjection(
            "candidate:email:jordan@example.com",
            machine_action="retarget",
            machine_judgment="wrong_person",
            machine_reason="wrong person",
            source=WriterSource.DEEP_RESEARCH.value,
        ),))

        result = AssembleSyntheticProfile(
            db=self.db, manifest=self.manifest,
        ).execute()

        self.assertEqual((result["built"], result["skipped_with_linkedin"]), (1, 0))
        self.assertEqual(query(self.db, "SELECT count(*) FROM synthetic_profiles")[0][0], 1)

    def test_no_linkedin_result_yields_one_synthetic_then_prefetch_completes(self) -> None:
        self._write_no_linkedin_result()
        assembled = AssembleSyntheticProfile(
            db=self.db,
            manifest=self.manifest,
        ).execute()

        self.assertEqual((assembled["built"], assembled["pending_review"]), (1, 1))
        self.assertEqual(query(self.db, "SELECT count(*) FROM synthetic_profiles")[0][0], 1)
        receipt = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual((receipt["status"], receipt["phase"]),
                         ("running", "profiles_pending"))

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

    def test_without_manifest_projects_sqlite_without_duplicate_files(self) -> None:
        self._write_no_linkedin_result()
        result = AssembleSyntheticProfile(
            db=self.db, manifest=None,
        ).execute()

        self.assertEqual(result["built"], 1)
        self.assertEqual(query(self.db, "SELECT count(*) FROM synthetic_profiles")[0][0], 1)
        self.assertFalse((self.research_dir / "synthetic").exists())

    def test_provider_basis_never_auto_approves_synthetic_identity(self) -> None:
        self._write_no_linkedin_result()

        AssembleSyntheticProfile(db=self.db, manifest=None).execute()

        profile = json.loads(query(self.db, "SELECT profile_json FROM synthetic_profiles")[0][0])
        link = query(
            self.db,
            "SELECT machine_approved, decision_approved FROM links WHERE kind='synthetic'",
        )[0]
        candidate = next(
            item
            for parent in linkedin_queue(self.db)
            for item in parent.candidates
            if item.synthetic
        )
        self.assertEqual(profile["basis"][0]["confidence"], "high")
        self.assertEqual(tuple(link), (None, None))
        self.assertTrue(candidate.pending)
        self.assertEqual((candidate.action, candidate.approved), ("", ""))

    def test_stale_undecided_synthetic_is_pruned_from_sqlite(self) -> None:
        self._write_no_linkedin_result()
        AssembleSyntheticProfile(db=self.db, manifest=None).execute()
        self._write_no_linkedin_result("https://www.linkedin.com/in/jordan-bravo")

        result = AssembleSyntheticProfile(db=self.db, manifest=None).execute()

        self.assertEqual(result["pruned_stale_machine_rows"], 1)
        self.assertFalse(query(self.db, "SELECT 1 FROM links WHERE kind='synthetic'"))
        self.assertFalse(query(self.db, "SELECT 1 FROM synthetic_profiles"))

    def test_stale_synthetic_with_human_decision_is_preserved(self) -> None:
        self._write_no_linkedin_result()
        AssembleSyntheticProfile(db=self.db, manifest=None).execute()
        self.db.decide_identity(
            "parent-1", "detach", approved="yes", source=ReviewSource.REVIEW.value,
        )
        self._write_no_linkedin_result("https://www.linkedin.com/in/jordan-bravo")

        result = AssembleSyntheticProfile(db=self.db, manifest=None).execute()

        self.assertEqual(result["pruned_stale_machine_rows"], 0)
        decision = query(
            self.db,
            "SELECT decision_action, decision_approved FROM links WHERE row_key='parent-1'",
        )[0]
        self.assertEqual(tuple(decision), ("detach", "yes"))

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

    def test_one_shared_profile_fetch_projects_every_candidate(self) -> None:
        self.db.project_rows((
            ParentRow("parent-2", "parent-worth:parent-2", "Casey Delta"),
            PersonRow("person-b", "parent-2", display_name="Casey Delta"),
            ArtifactRow(
                "facts:parent-2", ArtifactKind.FACTS.value, "parent-2",
                "/facts/parent-2.jsonl", "worth-parent-2", ProjectionStatus.PROJECTED.value,
                payload_json=json.dumps({"facts": {"network_worth": {"decision": "yes"}}}),
            ),
            FactRow(
                "parent-2", "parent-2", "facts:parent-2", machine_worth="yes",
                facts_json=json.dumps({"network_worth": {"decision": "yes"}}),
            ),
            LinkRow(
                "shared-one", "parent-1", "shared-pub", "pub",
                "https://www.linkedin.com/in/shared-pub", paid_profile=1,
                source=WriterSource.RECONCILE.value,
            ),
            LinkRow(
                "shared-two", "parent-2", "shared-pub", "pub",
                "https://www.linkedin.com/in/shared-pub", paid_profile=1,
                source=WriterSource.RECONCILE.value,
            ),
        ))
        links = [
            link for link in review_queue_links(linkedin_queue(self.db))
            if link.public_identifier == "shared-pub"
        ]
        response = {
            "state": rapidapi_client.PROFILE_CONTENT,
            "normalized_profile": {
                "success": True,
                "public_identifier": "shared-pub",
                "experiences": [{"title": "Founder", "company_name": "Example"}],
                "education": [],
            },
            "from_cache": False,
            "fetched": True,
        }
        with (
            mock.patch.object(rapidapi_client.RapidApiClient, "resolve_key", return_value="key"),
            mock.patch.object(
                rapidapi_client.RapidApiClient, "get_profile", return_value=response,
            ) as fetch,
        ):
            profile_projection.hydrate_profiles(links, self.cache, db=self.db)

        self.assertEqual(
            sorted(link.candidate_key for link in links), ["shared-one", "shared-two"]
        )
        self.assertEqual(fetch.call_count, 1)
        projected = query(
            self.db,
            "SELECT candidate_key FROM artifacts WHERE kind='profile' ORDER BY candidate_key",
        )
        self.assertEqual([row[0] for row in projected], ["shared-one", "shared-two"])


if __name__ == "__main__":
    unittest.main()
