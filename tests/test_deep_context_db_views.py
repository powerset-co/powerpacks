from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    CandidatePersonRow,
    FactRow,
    LinkRow,
    ParentRow,
    PersonIdentifierRow,
    PersonRow,
    PersonSourceRow,
    ResearchRow,
    ReviewSource,
    SyntheticProfileRow,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.identity_views import (
    enrichment_queue,
    linkedin_parents,
    linkedin_progress,
    linkedin_queue,
    synthetic_fallback,
)
from packs.ingestion.primitives.deep_context.db.people_views import person_detail
from packs.ingestion.primitives.deep_context.review.feedback import build_feedback_request
from packs.ingestion.primitives.deep_context.db.workflow_views import workflow_state
from packs.ingestion.primitives.deep_context.db.worth_views import worth_counts, worth_queue, worth_rows
from deep_context_sqlite_test_helpers import (
    project_artifact,
    project_candidate,
    project_fact,
    project_parent,
    project_person,
    project_synthetic_profile,
    query,
    replace_candidate_people,
    replace_person_identifiers,
    replace_person_sources,
)


class DeepContextDbViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Db(self.root / "deep-context.sqlite")

    def tearDown(self):
        self.temp.cleanup()

    def add_parent(
        self,
        parent_id: str,
        *worths: str | None,
        owner: bool = False,
        ghost: bool = False,
        human: str | None = None,
    ) -> list[str]:
        project_parent(self.db, ParentRow(parent_id, parent_id, f"Jordan {parent_id.title()}", f"jordan-{parent_id}"))
        person_ids = []
        for index, worth in enumerate(worths or ("maybe",), 1):
            person_id = f"{parent_id}-person-{index}"
            person_ids.append(person_id)
            project_person(
                self.db,
                PersonRow(
                    person_id,
                    parent_id,
                    display_name=f"Jordan {parent_id.title()}",
                    is_owner=int(owner),
                    is_ghost=int(ghost),
                ),
            )
            artifact_key = f"facts:{person_id}"
            project_artifact(
                self.db,
                ArtifactRow(
                    artifact_key,
                    "facts",
                    parent_id,
                    f"/facts/{person_id}.jsonl",
                    f"sha-{person_id}",
                    "projected",
                    person_id=person_id,
                ),
            )
            project_fact(
                self.db,
                FactRow(
                    person_id,
                    parent_id,
                    artifact_key,
                    person_id=person_id,
                    machine_worth=worth,
                    machine_worth_reason=f"{worth or 'default'} evidence",
                    is_owner=int(owner),
                    facts_json=json.dumps({"canonical_name": "Jordan Bravo"}),
                ),
            )
        if human:
            self.db.decide_worth(parent_id, human, decided_at="2026-08-05T00:00:00Z")
        return person_ids

    def add_candidate(
        self,
        parent_id: str,
        key: str,
        *,
        person_ids: list[str] | None = None,
        **values: object,
    ) -> None:
        values.setdefault("source", WriterSource.RECONCILE.value)
        project_candidate(
            self.db,
            LinkRow(
                key,
                parent_id,
                str(values.pop("public_identifier", key)),
                str(values.pop("kind", "pub")),
                **values,
            ),
        )
        members = tuple(CandidatePersonRow(key, person_id, parent_id) for person_id in (person_ids or []))
        replace_candidate_people(self.db, key, members)

    def add_factsless_parent(self, parent_id: str) -> list[str]:
        project_parent(self.db, ParentRow(parent_id, parent_id, f"Jordan {parent_id.title()}", f"jordan-{parent_id}"))
        person_id = f"{parent_id}-person-1"
        project_person(self.db, PersonRow(person_id, parent_id))
        return [person_id]

    def test_worth_is_facts_backed_grouped_and_policy_exact(self):
        self.add_parent("alpha", "no", "yes")
        self.add_parent("bravo", "maybe", human="no")
        self.add_parent("default", "no", None)
        self.add_parent("owner", "maybe", owner=True)
        self.add_parent("ghost", "maybe", ghost=True)
        synthetic_people = self.add_parent("synthetic", "maybe")
        self.add_candidate("synthetic", "synthetic:jordan", person_ids=synthetic_people, kind="synthetic")
        project_synthetic_profile(self.db, SyntheticProfileRow("synthetic:jordan", "synthetic:jordan", "{}"))

        rows = {row.parent_id: row for row in worth_rows(self.db)}
        self.assertEqual(set(rows), {"alpha", "bravo", "default", "synthetic"})
        self.assertEqual(rows["alpha"].machine.decision, "yes")
        self.assertEqual(rows["bravo"].effective, "no")
        self.assertEqual(rows["bravo"].source, "user")
        self.assertEqual([row.parent_id for row in worth_queue(self.db)], ["default"])
        self.assertEqual(
            asdict(worth_counts(self.db)),
            {"total": 4, "pending": 1, "yes": 1, "no": 1},
        )

    def test_owner_person_is_excluded_without_hiding_merged_family(self) -> None:
        visible_people = self.add_factsless_parent("visible")
        owner_people = self.add_factsless_parent("owner-member")
        project_person(
            self.db,
            PersonRow(owner_people[0], "owner-member", is_owner=1),
        )
        project_artifact(
            self.db,
            ArtifactRow(
                "facts:visible-parent",
                "facts",
                "visible",
                "/facts/visible.jsonl",
                "sha-visible-parent",
                "projected",
            ),
        )
        project_fact(
            self.db,
            FactRow(
                "visible-parent-fact",
                "visible",
                "facts:visible-parent",
                machine_worth="maybe",
                is_owner=1,
                facts_json="{}",
            ),
        )
        replace_person_identifiers(
            self.db,
            visible_people[0],
            (PersonIdentifierRow(visible_people[0], "email", "visible@example.test"),),
        )
        replace_person_identifiers(
            self.db,
            owner_people[0],
            (PersonIdentifierRow(owner_people[0], "email", "owner@example.test"),),
        )
        replace_person_sources(
            self.db,
            visible_people[0],
            (PersonSourceRow(visible_people[0], "imessage"),),
        )
        replace_person_sources(
            self.db,
            owner_people[0],
            (PersonSourceRow(owner_people[0], "gmail_msgvault"),),
        )
        self.db.merge_parents("visible", "owner-member")
        self.add_candidate(
            "visible",
            "visible-link",
            person_ids=[*visible_people, *owner_people],
            paid_profile=1,
        )
        self.add_candidate(
            "visible",
            "synthetic:owner-member",
            person_ids=owner_people,
            kind="synthetic",
        )
        project_synthetic_profile(
            self.db,
            SyntheticProfileRow(
                "synthetic:owner-member",
                "synthetic:owner-member",
                "{}",
            ),
        )

        hidden_people = self.add_parent("owner-only", "yes", owner=True)
        self.add_candidate(
            "owner-only",
            "owner-only-link",
            person_ids=hidden_people,
            paid_profile=1,
        )
        self.db.project_rows(
            (
                ResearchRow("visible", "visible", "no_match", "visible-link"),
                ResearchRow(
                    "owner-visible",
                    "visible",
                    "no_match",
                    "synthetic:owner-member",
                ),
                ResearchRow("owner-only", "owner-only", "no_match", "owner-only-link"),
            )
        )

        worth_result = worth_rows(self.db)
        self.assertEqual([row.parent_id for row in worth_result], ["visible"])
        self.assertEqual(worth_result[0].person_ids, tuple(visible_people))
        self.assertEqual(worth_result[0].machine.decision, "maybe")
        self.assertEqual(
            [row.parent_id for row in worth_queue(self.db)],
            ["visible"],
        )

        linkedin_result = linkedin_queue(self.db)
        self.assertEqual([row.parent_id for row in linkedin_result], ["visible"])
        self.assertEqual(linkedin_result[0].person_ids, tuple(visible_people))
        self.assertEqual(linkedin_result[0].source_channels, ("imessage",))
        self.assertEqual(
            linkedin_result[0].candidates[0].match_emails,
            ("visible@example.test",),
        )
        candidate = linkedin_result[0].candidates[0]
        self.assertIsNone(candidate.confidence)
        feedback = build_feedback_request(
            linkedin_result[0],
            candidate,
            action="skip",
            comment="No judge confidence exists for this rule outcome.",
            environ={},
        )
        self.assertNotIn("linkedin_confidence", feedback.metadata)
        judged_feedback = build_feedback_request(
            linkedin_result[0],
            replace(candidate, confidence=0.0),
            action="skip",
            comment="The judge supplied zero confidence.",
            environ={},
        )
        self.assertEqual(judged_feedback.metadata["linkedin_confidence"], "0.0")
        self.assertEqual(
            [row.parent_id for row in linkedin_parents(self.db)],
            ["visible"],
        )
        synthetic_targets = synthetic_fallback(self.db)
        self.assertEqual(synthetic_targets, [])

    def test_synthetic_fallback_is_only_for_effective_yes_parents(self) -> None:
        for worth in ("yes", "maybe", "no"):
            parent_id = f"synthetic-{worth}"
            people = self.add_parent(parent_id, worth)
            candidate_key = f"{parent_id}-link"
            self.add_candidate(parent_id, candidate_key, person_ids=people, paid_profile=1)
            self.db.project_rows((ResearchRow(
                parent_id,
                parent_id,
                "no_match",
                candidate_key,
                result_json=json.dumps({"person": {"full_name": f"Jordan {worth}"}}),
            ),))

        targets = synthetic_fallback(self.db)

        self.assertEqual([row.parent_id for row in targets], ["synthetic-yes"])

    def test_enrichment_queue_is_one_effective_yes_sql_policy(self):
        wrong_people = self.add_parent("wrong", "yes")
        self.add_candidate(
            "wrong",
            "wrong-link",
            person_ids=wrong_people,
            linkedin_url="https://www.linkedin.com/in/wrong-link",
            machine_judgment="wrong_person",
            machine_confidence=0.91,
            judgment_payload_json=json.dumps({"recommend_deep_research": True}),
        )
        candidate_people = self.add_parent("candidate", "yes")
        self.add_candidate(
            "candidate",
            "candidate:email:jordan@example.com",
            person_ids=candidate_people,
            candidate_origin=1,
            raw_import=1,
        )
        no_people = self.add_parent("rejected", "no")
        self.add_candidate(
            "rejected",
            "rejected-link",
            person_ids=no_people,
            machine_judgment="wrong_person",
            machine_confidence=0.99,
            judgment_payload_json=json.dumps({"recommend_deep_research": True}),
        )

        default = enrichment_queue(self.db)

        self.assertEqual(
            {row.row_key for row in default},
            {"wrong-link", "candidate:email:jordan@example.com"},
        )

    def test_linkedin_queue_encodes_standing_and_review_policies(self):
        review_people = self.add_parent("review", "yes")
        self.add_candidate(
            "review",
            "paid-reject",
            person_ids=review_people,
            paid_profile=1,
            machine_judgment="wrong_person",
            machine_confidence=0.91,
        )
        self.add_candidate(
            "review",
            "hard-detach",
            person_ids=review_people,
            machine_action="detach",
            authoritative_detach=1,
        )

        accepted_people = self.add_parent("accepted", "yes")
        self.add_candidate(
            "accepted",
            "accepted-retarget",
            person_ids=accepted_people,
            candidate_origin=1,
            machine_action="retarget",
            machine_approved="auto",
            machine_proposed_url="https://www.linkedin.com/in/jordan-accepted",
            machine_proposed_public_identifier="jordan-accepted",
        )

        synthetic_people = self.add_parent("synthetic", "yes")
        self.add_candidate(
            "synthetic",
            "synthetic:pending",
            person_ids=synthetic_people,
            kind="synthetic",
            machine_action="verify",
            machine_approved="auto",
        )
        project_synthetic_profile(self.db, SyntheticProfileRow("synthetic:pending", "synthetic:pending", "{}"))

        human_people = self.add_parent("human", "yes")
        self.add_candidate("human", "human-verified", person_ids=human_people, paid_profile=1)
        self.db.decide_identity("human-verified", "verify", approved="yes")

        ignored_people = self.add_parent("review-only", "yes")
        self.add_candidate("review-only", "export-only", person_ids=ignored_people)

        raw_people = self.add_parent("raw", "yes")
        self.add_candidate(
            "raw",
            "candidate:email:casey@example.com",
            person_ids=raw_people,
            kind="candidate_email",
            candidate_origin=1,
            raw_import=1,
        )
        no_people = self.add_parent("no", "no")
        self.add_candidate("no", "worth-no-profile", person_ids=no_people)

        project_parent(self.db, ParentRow("candidate-member", "candidate-member", "Jordan Member", "jordan-member"))
        member_id = "candidate:email:member@example.com"
        project_person(self.db, PersonRow(member_id, "candidate-member"))
        project_artifact(
            self.db,
            ArtifactRow(
                "facts:candidate-member",
                "facts",
                "candidate-member",
                "/facts/candidate-member.jsonl",
                "sha-candidate-member",
                "projected",
                person_id=member_id,
            ),
        )
        project_fact(
            self.db,
            FactRow(
                member_id,
                "candidate-member",
                "facts:candidate-member",
                person_id=member_id,
                machine_worth="yes",
                facts_json="{}",
            ),
        )
        self.add_candidate(
            "candidate-member",
            "jordan-member",
            person_ids=[member_id],
            paid_profile=1,
            machine_action="verify",
            machine_approved="auto",
        )

        factsless_synthetic = self.add_factsless_parent("factsless-synthetic")
        self.add_candidate(
            "factsless-synthetic",
            "synthetic:factsless",
            person_ids=factsless_synthetic,
            kind="synthetic",
            machine_action="verify",
            machine_approved="auto",
        )
        project_synthetic_profile(self.db, SyntheticProfileRow("synthetic:factsless", "synthetic:factsless", "{}"))
        rejected_synthetic = self.add_factsless_parent("rejected-synthetic")
        self.add_candidate(
            "rejected-synthetic",
            "synthetic:rejected",
            person_ids=rejected_synthetic,
            kind="synthetic",
        )
        project_synthetic_profile(self.db, SyntheticProfileRow("synthetic:rejected", "synthetic:rejected", "{}"))
        self.db.decide_identity("synthetic:rejected", "detach", approved="yes")
        factsless_candidate = self.add_factsless_parent("factsless-candidate")
        self.add_candidate(
            "factsless-candidate",
            "candidate:email:review@example.com",
            person_ids=factsless_candidate,
            kind="candidate_email",
            candidate_origin=1,
            paid_profile=1,
            machine_action="retarget",
            machine_proposed_url="https://www.linkedin.com/in/jordan-review",
            machine_judgment="needs_review",
        )

        queue = {parent.parent_id: parent for parent in linkedin_queue(self.db)}
        self.assertEqual(
            set(queue),
            {
                "review",
                "synthetic",
            },
        )
        self.assertEqual(
            [candidate.row_key for candidate in queue["review"].candidates],
            ["paid-reject"],
        )
        self.assertTrue(queue["synthetic"].candidates[0].pending)
        self.assertEqual(
            asdict(linkedin_progress(self.db)),
            {"total": 5, "pending": 2, "done": 3},
        )

        progress = workflow_state(self.db).progress
        self.assertEqual(progress.linkedin_pending, 2)
        self.assertEqual(progress.linkedin_done, 3)
        self.assertEqual(progress.lookup_ready, 2)
        self.assertEqual(progress.rejected, 2)

    def test_human_kept_identity_rescues_only_machine_worth_no(self):
        people = self.add_parent("keepish", "no")
        self.add_candidate(
            "keepish",
            "real-detached",
            person_ids=people,
            paid_profile=1,
            machine_action="detach",
            machine_approved="auto",
            machine_judgment="needs_review",
        )
        self.add_candidate(
            "keepish",
            "synthetic:kept",
            person_ids=people,
            kind="synthetic",
        )
        project_synthetic_profile(
            self.db,
            SyntheticProfileRow("synthetic:kept", "synthetic:kept", "{}"),
        )
        self.db.decide_identity("synthetic:kept", "verify", approved="yes")

        self.assertEqual(
            asdict(worth_counts(self.db)),
            {"total": 1, "pending": 0, "yes": 0, "no": 1},
        )
        self.assertEqual(
            asdict(linkedin_progress(self.db)),
            {"total": 1, "pending": 0, "done": 1},
        )

    def test_settle_derives_every_sibling_and_replaces_the_prior_winner(self):
        people = self.add_parent("family", "yes", "maybe")
        self.add_candidate("family", "human-kept", person_ids=[people[1]])
        self.db.decide_identity("human-kept", "verify", approved="yes")
        self.add_candidate("family", "clicked", person_ids=[people[0]])
        self.add_candidate("family", "ghost", person_ids=[people[1]], kind="ghost")
        self.add_candidate("family", "synthetic:sibling", person_ids=[people[0]], kind="synthetic")
        project_synthetic_profile(self.db, SyntheticProfileRow("synthetic:sibling", "synthetic:sibling", "{}"))

        settled = self.db.decide_identity(
            "clicked",
            "retarget",
            replacement_url="https://www.linkedin.com/in/jordan-replacement",
            replacement_public_identifier="jordan-replacement",
        )
        self.assertEqual(
            set(settled),
            {"clicked", "ghost", "human-kept", "synthetic:sibling"},
        )
        rows = {row["row_key"]: row for row in query(self.db, "SELECT * FROM links")}
        self.assertEqual(rows["clicked"]["replacement_public_identifier"], "jordan-replacement")
        self.assertEqual(rows["human-kept"]["decision_action"], "detach")
        self.assertEqual(
            rows["human-kept"]["decision_source"],
            ReviewSource.SIBLING_SETTLE.value,
        )
        self.assertEqual(rows["ghost"]["decision_action"], "detach")
        self.assertEqual(rows["synthetic:sibling"]["decision_action"], "detach")

    def test_directory_and_person_detail_hydrate_only_sql_projection(self):
        people = self.add_parent("detail", "yes")
        replace_person_identifiers(
            self.db,
            people[0],
            (
                PersonIdentifierRow(people[0], "email", "casey@example.com"),
                PersonIdentifierRow(people[0], "phone", "+15550100"),
            ),
        )
        replace_person_sources(
            self.db,
            people[0],
            (
                PersonSourceRow(people[0], "gmail_msgvault"),
                PersonSourceRow(people[0], "linkedin_csv"),
            ),
        )
        self.add_candidate(
            "detail",
            "jordan-detail",
            person_ids=people,
            linkedin_url="https://www.linkedin.com/in/jordan-detail",
            display_name="Jordan Detail",
            machine_action="verify",
            machine_approved="auto",
            judgment_payload_json=json.dumps({"linkedin": {"headline": "Product leader", "location": "Oakland"}}),
        )
        project_artifact(
            self.db,
            ArtifactRow(
                "dossier:detail",
                "dossier",
                "detail",
                "/dossiers/jordan-detail.md",
                "sha-dossier",
                "projected",
            ),
        )

        directory = worth_rows(self.db)
        self.assertEqual(
            [
                {
                    "slug": row.parent_slug,
                    "name": row.name,
                    "worth": row.effective,
                }
                for row in directory
            ],
            [{"slug": "jordan-detail", "name": "Jordan Detail", "worth": "yes"}],
        )
        detail = person_detail(self.db, "jordan-detail")
        assert detail is not None
        self.assertEqual(detail.dossier_path, "/dossiers/jordan-detail.md")
        self.assertEqual(detail.candidates[0].headline, "")
        self.assertEqual(detail.candidates[0].full_name, "Jordan Detail")
        self.assertEqual(detail.candidates[0].match_emails, ("casey@example.com",))
        self.assertEqual(detail.candidates[0].match_phones, ("+15550100",))
        self.assertEqual(detail.sources, ("gmail",))
        self.assertEqual(detail.source_channels, ("gmail_msgvault", "linkedin_csv"))

    def test_candidate_profiles_use_their_typed_origin_only(self) -> None:
        research_people = self.add_parent("research-profile", "yes")
        self.add_candidate(
            "research-profile",
            "research-profile-link",
            person_ids=research_people,
            kind="research",
            paid_profile=1,
            judgment_payload_json=json.dumps(
                {
                    "linkedin": {"full_name": "Wrong Verdict Name", "headline": "Wrong"},
                }
            ),
        )
        research_payload = {
            "type": "json",
            "content": {
                "real_name": "Jordan Research",
                "summary": "Research leader",
                "work_experience": [{"title": "Founder", "company_name": "Example Labs"}],
                "education": [{"degree": "BS", "school_name": "Example University"}],
                "location_city": "Oakland",
                "location_country": "United States",
                "linkedin_url": "https://www.linkedin.com/in/jordan-research",
            },
            "basis": [],
        }
        synthetic_people = self.add_parent("synthetic-profile", "yes")
        self.add_candidate(
            "synthetic-profile",
            "synthetic:profile",
            person_ids=synthetic_people,
            kind="synthetic",
        )
        missing_people = self.add_parent("missing-research-profile", "yes")
        self.add_candidate(
            "missing-research-profile",
            "missing-research-profile-link",
            person_ids=missing_people,
            linkedin_url="https://www.linkedin.com/in/wrong-attached-profile",
            display_name="Wrong Attached Profile",
            paid_profile=1,
        )
        synthetic_payload = {
            "type": "json",
            "content": {
                "real_name": "Jordan Synthetic",
                "summary": "Synthetic leader",
                "work_experience": [{"title": "Designer"}],
                "education": [{"school_name": "Design School"}],
                "location_city": "Portland",
                "location_country": "Oregon",
                "linkedin_url": "https://www.linkedin.com/in/jordan-synthetic",
            },
            "basis": [],
        }
        self.db.project_rows(
            (
                ResearchRow(
                    "research-profile",
                    "research-profile",
                    "complete",
                    "research-profile-link",
                    result_json=json.dumps(research_payload),
                ),
                ResearchRow(
                    "synthetic-profile",
                    "synthetic-profile",
                    "complete",
                    "synthetic:profile",
                    result_json=json.dumps(research_payload),
                ),
                ResearchRow(
                    "missing-research-profile",
                    "missing-research-profile",
                    "pending",
                    "missing-research-profile-link",
                ),
            )
        )
        project_synthetic_profile(
            self.db,
            SyntheticProfileRow(
                "synthetic:profile",
                "synthetic:profile",
                json.dumps(synthetic_payload),
            ),
        )

        research = person_detail(self.db, "research-profile")
        synthetic = person_detail(self.db, "synthetic-profile")
        missing = person_detail(self.db, "missing-research-profile")
        assert research is not None and synthetic is not None and missing is not None
        research_candidate = research.candidates[0]
        synthetic_candidate = synthetic.candidates[0]
        self.assertEqual(research_candidate.full_name, "Jordan Research")
        self.assertEqual(research_candidate.headline, "Research leader")
        self.assertEqual(
            research_candidate.experiences,
            ("Founder @ Example Labs",),
        )
        self.assertEqual(synthetic_candidate.full_name, "Jordan Synthetic")
        self.assertEqual(synthetic_candidate.headline, "Synthetic leader")
        self.assertEqual(synthetic_candidate.experiences, ("Designer @ ?",))
        self.assertEqual(synthetic_candidate.location, "Portland, Oregon")
        self.assertEqual(missing.candidates[0].full_name, "")
        self.assertFalse(missing.candidates[0].has_profile)

    def test_workflow_is_only_the_four_ordered_queue_predicates(self) -> None:
        people = self.add_parent("state", "maybe")
        self.add_candidate(
            "state",
            "jordan-state",
            person_ids=people,
            paid_profile=1,
            machine_judgment="wrong_person",
            machine_confidence=0.9,
            judgment_payload_json=json.dumps({"recommend_deep_research": True}),
        )
        self.assertEqual(workflow_state(self.db).next_action, "review_people")

        self.db.decide_worth("state", "yes", decided_at="2026-08-05T00:30:00Z")
        self.assertEqual(workflow_state(self.db).next_action, "enrich")
        self.db.project_rows((ResearchRow("state", "state", "no_match", "jordan-state"),))
        self.assertEqual(workflow_state(self.db).next_action, "enrich")

        self.add_candidate(
            "state",
            "synthetic:state",
            person_ids=people,
            kind="synthetic",
            machine_action="verify",
            machine_approved="auto",
        )
        project_synthetic_profile(self.db, SyntheticProfileRow("synthetic:state", "synthetic:state", "{}"))
        self.assertEqual(workflow_state(self.db).next_action, "review_linkedin")
        self.db.decide_identity("synthetic:state", "verify")
        self.assertEqual(workflow_state(self.db).next_action, "realize")

if __name__ == "__main__":
    unittest.main()
