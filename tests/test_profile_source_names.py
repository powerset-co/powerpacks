"""Automatic profile associations cannot contradict original source names."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.deep_context.db import identity_queries, queries
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow, CandidatePeopleProjection, CandidatePersonRow, FactRow, LinkRow,
    ParentRow, PersonRow, ResearchRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.candidate_selection import (
    CandidateDecision, RelationshipDecision, finish_reviews,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import (
    IdentityJudgeResult, IdentityUsage, IdentityVerdict,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.results import RetargetProposal, upsert_retargets
from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult
from packs.ingestion.primitives.deep_context.enrich.profiles.models import ProfileResult, ProfileTarget
from packs.ingestion.primitives.deep_context.enrich.profiles.projection import project_profile_results
from packs.ingestion.primitives.deep_context.enrich.research_reconcile import judging
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import jev_judge
from packs.ingestion.primitives.deep_context.enrich.estimate import estimate_enrichment
from packs.ingestion.primitives.deep_context.enrich.settle import SettleEnrichment
from packs.ingestion.primitives.deep_context.enrich.synthetic.assemble import AssembleSyntheticProfile
from packs.ingestion.primitives.deep_context.realize.export_people import ExportPeople
from packs.ingestion.primitives.pipeline.contract import PeopleRow

URL = "https://www.linkedin.com/in/casey-south"
CONFIRMED = IdentityVerdict.from_payload({"verdict": "confirmed", "confidence": .99, "reason": "Fixture agrees"})


class ProfileSourceNamesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = Db(self.root / "context.sqlite")
        self.db.project_rows((
            ParentRow("parent", "parent", "Casey South", machine_worth="yes"),
            PersonRow("email", "parent", display_name="Casey South"),
            LinkRow("candidate", "parent", "casey-south", "pub", linkedin_url=URL, display_name="Casey South",
                    source="deep-context-reconcile"),
            CandidatePeopleProjection("candidate", (CandidatePersonRow("candidate", "email", "parent"),)),
            ArtifactRow("facts:parent", "facts", "parent", "/fixture", "fixture", "projected"),
            FactRow("parent", "parent", "facts:parent", machine_worth="yes",
                    facts_json='{"canonical_name":"Casey South"}'),
        ))
        self.source = PeopleRow(id="email", full_name="Jordan North", primary_email="jordan@example.test",
                                source_channels="gmail_msgvault")
        self.db.replace_imported_people((self.source,))

    def profile(self, name="Casey South"):
        result = ProfileResult.from_payload("casey-south", URL, {
            "state": "content", "normalized_profile": {"success": True, "full_name": name,
                "public_identifier": "casey-south", "linkedin_url": URL,
                "experiences": [{"title": "Engineer", "company_name": "Example"}]},
            "data": {"full_name": name, "public_identifier": "casey-south", "linkedin_url": URL,
                     "experiences": [{"title": "Engineer", "company_name": "Example"}]},
        })
        project_profile_results(self.db, ((ProfileTarget("casey-south", URL, "candidate", "parent"), result),), self.root)

    def research(self, name="Casey South", url=URL):
        result = ResearchResult.from_payload({"type": "json", "content": {
            "real_name": name, "linkedin_url": url, "work_experience": [{"title": "Engineer", "company_name": "Example"}],
            "education": [], "location_city": "Oakland",
        }, "basis": []})
        self.db.project_rows((
            ArtifactRow("research:parent", "research", "parent", "/fixture", "research", "projected", candidate_key="candidate"),
            ResearchRow("parent", "parent", "complete", "candidate", "research:parent", result.output.model_dump_json()),
        ))
        return result

    def assert_not_accepted(self, verdict="wrong_person"):
        row = identity_queries.links(self.db, row_key="candidate")[0]
        self.assertNotIn(row.machine_approved, {"yes", "auto"})
        self.assertEqual(row.machine_judgment, verdict)

    def test_mapped_imported_url_cannot_use_model_name_to_accept_wrong_cached_profile(self):
        self.profile()
        outcome = IdentityJudgeResult(CONFIRMED, IdentityUsage(), "", "jev-fixture")
        with patch.object(judging.jev_judge, "judge_batch", return_value=[outcome]):
            judging.judge_mapped_candidates(self.db)
        self.assert_not_accepted()

    def test_changed_source_name_invalidates_machine_approval_and_exact_input_reuses(self):
        self.db.replace_imported_people((self.source.model_copy(update={"full_name": "Casey South"}),))
        self.profile()
        def answer(tasks, **kwargs):
            return [IdentityJudgeResult(CONFIRMED, IdentityUsage(), "", jev_judge.judgment_fingerprint(
                task, urls, kwargs["reference_date"])) for task, urls in zip(tasks, kwargs["imported_urls"], strict=True)]
        with patch.object(judging.jev_judge, "judge_batch", side_effect=answer) as provider:
            self.assertGreater(estimate_enrichment(self.db).remaining_judgments, 0)
            self.assertEqual(judging.judge_mapped_candidates(self.db).judge_calls, 1)
            self.assertEqual(sum(item[-1] is None for item in judging.mapped_identity_tasks(self.db)), 0)
            self.assertEqual(judging.judge_mapped_candidates(self.db).judge_calls, 0)
            self.db.replace_imported_people((self.source,))
            self.assertGreater(estimate_enrichment(self.db).remaining_judgments, 0)
            self.assertEqual(judging.judge_mapped_candidates(self.db).judge_calls, 1)
            self.assertEqual(provider.call_count, 2)
        self.assert_not_accepted()

    def test_sol_schema_change_invalidates_proposal_request(self):
        from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import judge
        from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence
        from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import JudgeProfile
        evidence, profile = DossierEvidence(name="Jordan Bravo"), JudgeProfile(full_name="Jordan Bravo")
        before = judging.proposal_fingerprint(evidence, profile, model="fixture", effort="medium")
        with patch.object(judge, "RECONCILE_SCHEMA", {**judge.RECONCILE_SCHEMA, "description": "Changed schema"}):
            after = judging.proposal_fingerprint(evidence, profile, model="fixture", effort="medium")
        self.assertNotEqual(before, after)

    def test_changed_original_endpoint_invalidates_exact_request(self):
        self.profile()
        before = judging.mapped_identity_tasks(self.db)[0]
        self.db.replace_imported_people((self.source.model_copy(update={"primary_email": "changed@example.test"}),))
        after = judging.mapped_identity_tasks(self.db)[0]
        self.assertNotEqual(jev_judge.judgment_fingerprint(before[1], before[2], before[3]),
                            jev_judge.judgment_fingerprint(after[1], after[2], after[3]))

    def test_research_retarget_cannot_accept_wrong_name_without_profile_cache(self):
        self.research()
        upsert_retargets(self.db, [RetargetProposal("candidate", URL, judge_fingerprint="research-fixture",
                                                  approved="auto", judge_payload=CONFIRMED)])
        self.assert_not_accepted()

    def test_relationship_yes_cannot_override_source_name_veto(self):
        self.profile()
        finish_reviews(self.db, (RelationshipDecision("parent", "sol-fixture", (
            CandidateDecision(URL, "yes", "Fixture agrees", .99),)),))
        self.assert_not_accepted()

    def test_missing_profile_name_remains_uncertain(self):
        self.profile("")
        upsert_retargets(self.db, [RetargetProposal("candidate", URL, judge_fingerprint="missing-name",
                                                  approved="auto", judge_payload=CONFIRMED)])
        self.assert_not_accepted("needs_review")

    def test_missing_source_name_remains_uncertain(self):
        self.db.replace_imported_people((self.source.model_copy(update={"full_name": ""}),))
        self.profile()
        upsert_retargets(self.db, [RetargetProposal("candidate", URL, judge_fingerprint="missing-source",
                                                  approved="auto", judge_payload=CONFIRMED)])
        self.assert_not_accepted("needs_review")

    def test_compatible_first_child_cannot_hide_conflicting_source_member(self):
        self.db.project_rows((PersonRow("phone", "parent", display_name="Casey South"),))
        self.db.replace_imported_people((self.source.model_copy(update={"full_name": "Casey South"}),
                                        PeopleRow(id="phone", full_name="Jordan North", primary_phone="+15550100")))
        self.profile()
        upsert_retargets(self.db, [RetargetProposal("candidate", URL, judge_fingerprint="conflicting-sources",
                                                  approved="auto", judge_payload=CONFIRMED)])
        self.assert_not_accepted("needs_review")

    def test_compatible_source_still_needs_an_affirmative_judgment(self):
        self.db.replace_imported_people((self.source.model_copy(update={"full_name": "Casey S"}),))
        self.profile()
        upsert_retargets(self.db, [RetargetProposal("candidate", URL, judge_fingerprint="uncertain",
                                                  judge_payload=replace(CONFIRMED, value="needs_review"))])
        self.assert_not_accepted("needs_review")

    def test_compatible_abbreviation_with_affirmative_evidence_can_be_accepted(self):
        self.db.replace_imported_people((self.source.model_copy(update={"full_name": "Casey S"}),))
        self.profile()
        upsert_retargets(self.db, [RetargetProposal("candidate", URL, judge_fingerprint="supported",
                                                  approved="auto", judge_payload=CONFIRMED)])
        self.assertEqual(identity_queries.links(self.db)[0].machine_approved, "auto")

    def test_old_machine_approval_cannot_bypass_export(self):
        self.profile()
        with self.db.transaction() as conn:
            conn.execute("UPDATE links SET machine_action='verify',machine_approved='auto',machine_judgment='confirmed'")
        result = ExportPeople(db=self.db, out_dir=self.root / "export").run()
        self.assertEqual(result["accepted_identities"], 0)
        self.assertEqual(queries.imported_people(self.db)[0].public_identifier, "")

    def test_direct_linkedin_source_url_cannot_bypass_mismatching_fetched_name(self):
        self.profile()
        self.db.replace_imported_people((self.source.model_copy(update={
            "source_channels": "linkedin_csv", "public_identifier": "casey-south", "linkedin_url": URL,
        }),))
        ExportPeople(db=self.db, out_dir=self.root / "export").run()
        self.assertEqual(queries.imported_people(self.db)[0].public_identifier, "")

    def test_linkedin_only_source_contact_survives_missing_profile_evidence(self):
        self.db.replace_imported_people((self.source.model_copy(update={
            "primary_email": "", "source_channels": "linkedin_csv", "public_identifier": "casey-south", "linkedin_url": URL,
            "first_name": "Jordan", "last_name": "North", "headline": "Source headline",
        }),))
        result = ExportPeople(db=self.db, out_dir=self.root / "export").run()
        self.assertEqual((result["rows"], result["dropped_unkeyable"]), (1, 0))
        self.assertEqual([(row.id, row.public_identifier) for row in queries.imported_people(self.db)], [("email", "casey-south")])
        kept = queries.imported_people(self.db)[0]
        self.assertEqual((kept.first_name, kept.last_name, kept.headline), ("Jordan", "North", "Source headline"))
        self.assertEqual(result["accepted_identities"], 0)

    def test_accepted_profile_does_not_hydrate_unaccepted_other_parent_with_same_url(self):
        self.db.replace_imported_people((self.source.model_copy(update={"full_name": "Casey South"}),))
        self.profile()
        upsert_retargets(self.db, [RetargetProposal("candidate", URL, judge_fingerprint="fixture",
                                                  approved="auto", judge_payload=CONFIRMED)])
        other = PeopleRow(id="other", full_name="Jordan Bravo", source_channels="linkedin_csv",
                          public_identifier="casey-south", linkedin_url=URL)
        self.db.project_rows((ParentRow("other-parent", "other-parent"), PersonRow("other", "other-parent")))
        self.db.replace_imported_people((*queries.imported_people(self.db), other))
        result = ExportPeople(db=self.db, out_dir=self.root / "export").run()
        from packs.shared.csv_io import CsvIO
        rows = {row["id"]: row for row in CsvIO.read_dict_rows(self.root / "export" / "people.csv")}
        self.assertEqual(result["rows"], 2)
        self.assertEqual(rows["other"]["work_experiences"], "")

    def test_settle_rechecks_old_machine_approval_even_with_populated_profile(self):
        self.profile()
        with self.db.transaction() as conn:
            conn.execute("UPDATE links SET machine_action='verify',machine_approved='auto',machine_judgment='confirmed'")
        SettleEnrichment(db=self.db).run()
        self.assert_not_accepted()

    def test_synthetic_wrong_research_name_is_not_projected(self):
        self.research(url="")
        result = AssembleSyntheticProfile(db=self.db).run()
        self.assertEqual(result.counts.built, 0)
        self.assertEqual(identity_queries.synthetic_profiles(self.db), ())

    def test_provider_guidance_does_not_become_a_human_override(self):
        from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guided import GuidedResearch
        from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guidance import GuidanceRequest
        from packs.ingestion.primitives.deep_context.db.people_views import person_detail
        self.db.replace_imported_people((self.source.model_copy(update={"full_name": "Casey South"}),))
        self.profile()
        research = self.research()
        service = GuidedResearch(self.db, profile_cache_dir=self.root)
        request = GuidanceRequest("parent", "candidate", "Casey South", "Find their engineering profile")
        with patch.object(judging.projection, "hydrate_profiles"), patch.object(judging.judge, "judge_batch",
            return_value=[IdentityJudgeResult(CONFIRMED, IdentityUsage(), "", "guided-fixture")]):
            outcome = service.apply_provider_result("parent", person_detail(self.db, "parent"), request, research)
        self.assertEqual(outcome.state, "applied")
        self.assertIsNone(identity_queries.links(self.db, row_key="candidate")[0].decision_action)
        self.db.replace_imported_people((self.source,))
        self.assertEqual(len(judging.mapped_identity_tasks(self.db)), 1)
        result = ExportPeople(db=self.db, out_dir=self.root / "export").run()
        self.assertEqual(result["accepted_identities"], 0)

    def test_provider_result_cannot_claim_a_different_prior_human_url_was_applied(self):
        from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guided import GuidedResearch
        from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guidance import GuidanceRequest
        from packs.ingestion.primitives.deep_context.db.people_views import person_detail
        self.db.replace_imported_people((self.source.model_copy(update={"full_name": "Casey South"}),))
        self.profile()
        research = self.research()
        self.db.decide_identity("candidate", "retarget", replacement_url="https://www.linkedin.com/in/casey-human",
                                replacement_public_identifier="casey-human")
        before = identity_queries.links(self.db, row_key="candidate")[0]
        service = GuidedResearch(self.db, profile_cache_dir=self.root)
        request = GuidanceRequest("parent", "candidate", "Casey South", "Find their engineering profile")
        with patch.object(judging.projection, "hydrate_profiles"), patch.object(judging.judge, "judge_batch",
            return_value=[IdentityJudgeResult(CONFIRMED, IdentityUsage(), "", "guided-fixture")]):
            outcome = service.apply_provider_result("parent", person_detail(self.db, "parent"), request, research)
        self.assertEqual(outcome.state, "no_match")
        self.assertEqual(identity_queries.links(self.db, row_key="candidate")[0], before)

    def test_guidance_mentioning_a_rejected_url_is_not_a_human_attachment(self):
        from packs.ingestion.primitives.deep_context.review.guided_retarget import GuidedRetargetWorker
        from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guidance import GuidanceRequest
        worker = GuidedRetargetWorker(self.db, runner=lambda _: self.fail("No research in test"))
        with patch.object(worker, "_enqueue") as queued, patch(
            "packs.ingestion.primitives.deep_context.review.guided_retarget.parent_has_contact_identifier", return_value=True):
            outcome = worker.submit(GuidanceRequest("parent", "candidate", "Jordan North",
                                                     "Find someone different from " + URL))
        self.assertEqual(outcome.state, "queued")
        queued.assert_called_once()
        self.assertIsNone(identity_queries.links(self.db, row_key="candidate")[0].decision_action)

    def test_explicit_guidance_url_remains_a_human_override_without_provider(self):
        from packs.ingestion.primitives.deep_context.review.guided_retarget import GuidedRetargetWorker
        from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guidance import GuidanceRequest
        with patch.object(judging.projection, "hydrate_profiles", side_effect=AssertionError("No provider")):
            worker = GuidedRetargetWorker(self.db, runner=lambda _: self.fail("No research"))
            outcome = worker.submit(GuidanceRequest("parent", "candidate", "Jordan North", URL))
        self.assertEqual(outcome.state, "applied")
        self.assertEqual(identity_queries.links(self.db, row_key="candidate")[0].decision_action, "retarget")
        self.assertIn("candidate", outcome.resolved_pubs)

    def test_human_profile_choice_wins_over_machine_for_overlapping_source_child(self):
        self.db.replace_imported_people((self.source.model_copy(update={"full_name": "Casey South"}),))
        self.profile()
        self.db.decide_identity("candidate", "verify", approved="yes")
        alternative = "https://www.linkedin.com/in/casey-south-other"
        self.db.project_rows((
            LinkRow("z-machine", "parent", "casey-south-other", "pub", linkedin_url=alternative,
                    machine_action="verify", machine_approved="auto", source="deep-context-reconcile"),
            CandidatePeopleProjection("z-machine", (CandidatePersonRow("z-machine", "email", "parent"),)),
        ))
        result = ProfileResult.from_payload("casey-south-other", alternative, {"state": "content",
            "normalized_profile": {"success": True, "full_name": "Casey South", "linkedin_url": alternative,
                                   "public_identifier": "casey-south-other"}})
        project_profile_results(self.db, ((ProfileTarget("casey-south-other", alternative, "z-machine", "parent"), result),), self.root)
        ExportPeople(db=self.db, out_dir=self.root / "export").run()
        self.assertEqual(queries.imported_people(self.db)[0].public_identifier, "casey-south")

    def test_explicit_human_profile_choice_keeps_exact_source_membership(self):
        self.profile()
        self.db.project_rows((PersonRow("other-email", "parent", display_name="Casey South"),))
        self.db.replace_imported_people((self.source, PeopleRow(
            id="other-email", full_name="Casey South", primary_email="casey@example.test", source_channels="gmail_msgvault")))
        self.db.decide_identity("candidate", "verify", approved="yes")
        before = identity_queries.links(self.db)[0]
        upsert_retargets(self.db, [RetargetProposal("candidate", URL, judge_fingerprint="machine",
                                                  approved="auto", judge_payload=CONFIRMED)])
        self.assertEqual(identity_queries.links(self.db)[0], before)
        ExportPeople(db=self.db, out_dir=self.root / "export").run()
        roster = {row.id: row for row in queries.imported_people(self.db)}
        self.assertEqual(roster["email"].public_identifier, "casey-south")
        self.assertEqual(roster["other-email"].public_identifier, "")


if __name__ == "__main__":
    unittest.main()
