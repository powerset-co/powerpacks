from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest import mock

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
    SynthesisRun,
    SyntheticProfileRow,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.pipeline.contract import PeopleRow
from packs.ingestion.primitives.deep_context.db.identity_views import (
    decision_parents,
    approved_identities,
    enrichment_queue,
    judge_candidates,
    linkedin_parents,
    linkedin_progress,
    linkedin_queue,
    research_candidate_urls,
    review_questions_pending,
    synthetic_fallback,
)
from packs.ingestion.primitives.deep_context.db.people_views import person_detail
from packs.ingestion.primitives.deep_context.review.feedback import build_feedback_request
from packs.ingestion.primitives.deep_context.db.workflow_views import synthesis_pending, workflow_state
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
            self.db.replace_imported_people((*queries.imported_people(self.db), PeopleRow(
                id=person_id, full_name=f"Jordan {parent_id.title()}",
            )))
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
                    facts_json=json.dumps({"canonical_name": f"Jordan {parent_id.title()}"}),
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

    def test_decision_tables_list_every_counted_worth_parent_without_links(self) -> None:
        self.add_parent("alpha", "yes")
        self.add_parent("bravo", "yes")
        self.add_parent("charlie", "maybe", human="no")
        counts = worth_counts(self.db)

        yes = decision_parents(self.db, "yes")
        no = decision_parents(self.db, "no")

        self.assertEqual([row.parent_id for row in yes], ["alpha", "bravo"])
        self.assertEqual([row.parent_id for row in no], ["charlie"])
        self.assertEqual((len(yes), len(no)), (counts.yes, counts.no))
        self.assertEqual([row.candidates for row in yes + no], [(), (), ()])
        self.assertEqual(
            [row.parent_id for row in decision_parents(self.db, "yes", offset=1, limit=1)],
            ["bravo"],
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
            linkedin_url="https://www.linkedin.com/in/visible-link",
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
            {"candidate:email:jordan@example.com"},
        )

    def test_linkedin_queue_encodes_standing_and_review_policies(self):
        review_people = self.add_parent("review", "yes")
        self.add_candidate(
            "review",
            "paid-reject",
            person_ids=review_people,
            paid_profile=1,
            linkedin_url="https://www.linkedin.com/in/paid-reject",
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
            linkedin_url="https://www.linkedin.com/in/jordan-member",
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
        self.assertEqual(set(queue), {"review"})
        self.assertEqual(
            [candidate.row_key for candidate in queue["review"].candidates],
            ["paid-reject"],
        )
        self.assertNotIn("synthetic", queue)
        self.assertEqual(
            asdict(linkedin_progress(self.db)),
            {"total": 3, "pending": 1, "done": 2},
        )

    def test_raw_sibling_does_not_hide_attached_link(self) -> None:
        people = self.add_parent("mixed", "yes")
        self.add_candidate("mixed", "jordan-mixed", person_ids=people,
                           linkedin_url="https://www.linkedin.com/in/jordan-mixed")
        self.add_candidate("mixed", "candidate:email:mixed@example.test", person_ids=people,
                           kind="candidate_email", candidate_origin=1, raw_import=1)
        self.assertEqual([row.row_key for row in judge_candidates(self.db)], ["jordan-mixed"])
        self.assertEqual([row.parent_id for row in linkedin_queue(self.db)], ["mixed"])

    def test_old_machine_verdict_is_selected_for_current_input_check(self) -> None:
        people = self.add_parent("judged", "yes")
        self.add_candidate("judged", "jordan-judged", person_ids=people,
                           linkedin_url="https://www.linkedin.com/in/jordan-judged",
                           judgment_fingerprint="old-input",
                           judgment_payload_json=json.dumps({"verdict": "needs_review", "confidence": 0.7}))
        self.assertEqual([row.row_key for row in judge_candidates(self.db)], ["jordan-judged"])
        self.add_candidate("judged", "jordan-empty", person_ids=people,
                           linkedin_url="https://www.linkedin.com/in/jordan-empty",
                           judgment_fingerprint="failed-input", judgment_payload_json="{}")
        self.assertEqual([row.row_key for row in judge_candidates(self.db)], ["jordan-empty", "jordan-judged"])

    def test_human_synthetic_keep_stays_local_without_linkedin_progress(self):
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
            {"total": 0, "pending": 0, "done": 0},
        )
        decision = self.db.query("SELECT decision_action,decision_approved FROM links WHERE row_key='synthetic:kept'")[0]
        self.assertEqual(tuple(decision), ("verify", "yes"))

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
        self.assertEqual((detail.emails, detail.phones), (("casey@example.com",), ("+15550100",)))
        self.assertEqual(detail.sources, ("gmail",))
        self.assertEqual(detail.source_channels, ("gmail_msgvault", "linkedin_csv"))

    def test_researched_linkedin_uses_fetched_profile_not_research(self) -> None:
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

        self.db.project_rows((ArtifactRow(
            'profile:research-profile-link', 'profile', 'research-profile', '/fixture/profile.json',
            'profile-hash', 'projected', candidate_key='research-profile-link',
            payload_json=json.dumps({'public_identifier': 'jordan-research',
                'linkedin_url': 'https://www.linkedin.com/in/jordan-research',
                'normalized_profile': {'success': True, 'full_name': 'Jordan Fetched',
                    'headline': 'Fetched engineer', 'experiences': [{'title': 'Engineer', 'company_name': 'Actual Labs'}]}}),
        ),))

        research = person_detail(self.db, "research-profile")
        synthetic = person_detail(self.db, "synthetic-profile")
        missing = person_detail(self.db, "missing-research-profile")
        assert research is not None and synthetic is not None and missing is not None
        research_candidate = research.candidates[0]
        synthetic_candidate = synthetic.candidates[0]
        self.assertEqual(research_candidate.full_name, "Jordan Fetched")
        self.assertEqual(research_candidate.headline, "Fetched engineer")
        self.assertEqual(
            research_candidate.experiences,
            ("Engineer @ Actual Labs",),
        )
        self.assertEqual(synthetic_candidate.full_name, "Jordan Synthetic")
        self.assertEqual(synthetic_candidate.headline, "Synthetic leader")
        self.assertEqual(synthetic_candidate.experiences, ("Designer @ ?",))
        self.assertEqual(synthetic_candidate.location, "Portland, Oregon")
        self.assertEqual(missing.candidates[0].full_name, "Wrong Attached Profile")
        self.assertTrue(missing.candidates[0].has_profile)

    def test_synthetic_only_parent_stays_local_without_identity_review(self) -> None:
        people = self.add_parent("synthetic-review", "yes")
        key = "synthetic:review"
        self.add_candidate("synthetic-review", key, person_ids=people, kind="synthetic")
        project_synthetic_profile(self.db, SyntheticProfileRow(key, key, "{}"))
        self.db.project_rows((ResearchRow("synthetic-review", "synthetic-review", "no_match", key),))
        before = [tuple(row) for row in self.db.query("SELECT * FROM links")]
        self.assertEqual(linkedin_queue(self.db), [])
        self.assertEqual(asdict(linkedin_progress(self.db)), {"total": 0, "pending": 0, "done": 0})
        self.assertEqual(review_questions_pending(self.db), 0)
        self.assertEqual(workflow_state(self.db).next_action, "enrich")
        self.assertEqual(approved_identities(self.db), [])
        self.assertEqual(len(person_detail(self.db, "synthetic-review").candidates), 1)
        self.assertEqual([tuple(row) for row in self.db.query("SELECT * FROM links")], before)
        self.assertEqual(self.db.query("SELECT profile_json FROM synthetic_profiles")[0][0], "{}")

    def test_mixed_parent_reviews_real_candidate_only(self) -> None:
        people = self.add_parent("mixed-synthetic", "yes")
        self.add_candidate("mixed-synthetic", "synthetic:local", person_ids=people, kind="synthetic")
        project_synthetic_profile(self.db, SyntheticProfileRow("synthetic:local", "synthetic:local", "{}"))
        self.add_candidate("mixed-synthetic", "jordan-real", person_ids=people,
                           linkedin_url="https://www.linkedin.com/in/jordan-real")
        queue = linkedin_queue(self.db)
        self.assertEqual([row.parent_id for row in queue], ["mixed-synthetic"])
        self.assertEqual([row.row_key for row in queue[0].candidates], ["jordan-real"])

    def test_historical_synthetic_retarget_remains_a_real_accepted_identity(self) -> None:
        people = self.add_parent("retargeted-synthetic", "yes")
        key = "synthetic:retargeted"
        self.add_candidate("retargeted-synthetic", key, person_ids=people, kind="synthetic")
        project_synthetic_profile(self.db, SyntheticProfileRow(key, key, "{}"))
        url = "https://www.linkedin.com/in/jordan-real"
        self.db.decide_identity(key, "retarget", replacement_url=url, replacement_public_identifier="jordan-real")
        before = [tuple(row) for row in self.db.query("SELECT * FROM links")]
        self.assertEqual(linkedin_queue(self.db), [])
        self.assertEqual(asdict(linkedin_progress(self.db)), {"total": 1, "pending": 0, "done": 1})
        self.assertEqual([row.linkedin_url for row in approved_identities(self.db)], [url])
        self.assertEqual([tuple(row) for row in self.db.query("SELECT * FROM links")], before)

    def test_unsynthesized_contact_is_not_hidden_by_a_siblings_facts(self) -> None:
        self.add_parent('shared', 'yes')
        project_person(self.db, PersonRow('shared-person-2', 'shared'))
        project_artifact(self.db, ArtifactRow('source-bundle:shared-person-2', 'source_bundle', 'shared',
                                             '/raw/shared-person-2.json', 'fixture', 'projected',
                                             person_id='shared-person-2'))
        self.assertEqual(synthesis_pending(self.db), ('shared-person-2',))
        self.assertEqual(workflow_state(self.db).progress.synthesize_pending, 1)

    def test_collected_parent_without_facts_queues_synthesize(self) -> None:
        self.add_factsless_parent("linkedin-only")
        self.assertEqual(workflow_state(self.db).next_action, "realize")

        self.add_factsless_parent("collected")
        project_artifact(
            self.db,
            ArtifactRow(
                "source_bundle:collected",
                "source_bundle",
                "collected",
                "/raw/collected.json",
                "sha-collected",
                "projected",
            ),
        )
        state = workflow_state(self.db)
        self.assertEqual(state.next_action, "synthesize")
        self.assertEqual(state.progress.synthesize_pending, 1)

        project_artifact(
            self.db,
            ArtifactRow("facts:collected", "facts", "collected", "/facts/collected.jsonl", "sha-facts", "projected"),
        )
        project_fact(
            self.db,
            FactRow("collected", "collected", "facts:collected", machine_worth="maybe"),
        )
        state = workflow_state(self.db)
        self.assertEqual(state.next_action, "realize")
        self.assertEqual(state.progress.synthesize_pending, 0)

    def test_a_parent_synthesis_could_not_finish_does_not_hold_the_flow(self) -> None:
        for parent in ("skipped", "later"):
            self.add_factsless_parent(parent)
        def bundle(parent):
            return ArtifactRow(
                f"source_bundle:{parent}", "source_bundle", parent, f"/raw/{parent}.json", f"sha-{parent}", "projected")
        project_artifact(self.db, bundle("skipped"))
        self.assertEqual(workflow_state(self.db).next_action, "synthesize")
        self.assertEqual(synthesis_pending(self.db), ("skipped-person-1",))

        # The run tried this parent and could not write its facts: it waits for the next run.
        self.db.record_synthesis_run(SynthesisRun(("skipped-person-1: model answer unusable",), ("skipped-person-1",)))
        state = workflow_state(self.db)
        self.assertEqual((state.next_action, state.progress.synthesize_pending), ("realize", 0))

        # A parent collected since then is new work.
        project_artifact(self.db, bundle("later"))
        state = workflow_state(self.db)
        self.assertEqual((state.next_action, state.progress.synthesize_pending), ("synthesize", 1))

        # A run that left nobody behind: both are pending again.
        self.db.record_synthesis_run(SynthesisRun())
        self.assertEqual(workflow_state(self.db).progress.synthesize_pending, 2)

    def test_maybe_worth_does_not_stop_pending_enrichment(self) -> None:
        self.add_parent("maybe", "maybe")
        self.add_parent("research", "yes")

        state = workflow_state(self.db)

        self.assertEqual(state.progress.worth_pending, 1)
        self.assertGreater(state.progress.enrichment_untried, 0)
        self.assertEqual(state.next_action, "enrich")

    def test_workflow_with_nothing_pending_realizes(self) -> None:
        self.assertEqual(workflow_state(self.db).next_action, "realize")

    def test_workflow_reuses_selection_for_worth_counts(self):
        self.add_parent("yes", "yes")
        self.add_parent("no", "no")
        self.add_parent("maybe", "maybe")
        people = self.add_parent("synthetic", "maybe")
        self.add_candidate("synthetic", "synthetic-link", person_ids=people, kind="synthetic")
        expected = worth_counts(self.db)
        with mock.patch.object(self.db, "query", wraps=self.db.query) as reads:
            state = workflow_state(self.db)
        self.assertEqual((state.progress.worth_total, state.progress.worth_pending,
                          state.progress.worth_yes, state.progress.worth_no),
                         (expected.total, expected.pending, expected.yes, expected.no))
        self.assertEqual((state.selection.maybe, state.progress.worth_pending), (2, 1))
        statements = [call.args[0] for call in reads.call_args_list]
        self.assertFalse(any("sum(w.effective_worth='maybe'" in sql for sql in statements))
        self.assertTrue(any("AS lookup_ready" in sql and "AS rejected" in sql for sql in statements))

    def test_workflow_counts_materialize_identity_scope_once(self):
        self.add_parent("fixture", "yes")
        with mock.patch.object(self.db, "query", wraps=self.db.query) as reads:
            workflow_state(self.db)
        sql = next(call.args[0] for call in reads.call_args_list if "AS candidate_keys" in call.args[0])
        plan = self.db.query("EXPLAIN QUERY PLAN " + sql)
        self.assertEqual(sum(row["detail"] == "MATERIALIZE identity_scope" for row in plan), 1)

    def test_workflow_counts_do_not_load_candidate_or_contact_snapshots(self):
        people = self.add_parent("fixture", "yes")
        self.add_candidate("fixture", "fixture-link", person_ids=people,
                           linkedin_url="https://linkedin.com/in/jordan-fixture", candidate_origin=1)
        with mock.patch.object(self.db, "query", wraps=self.db.query) as reads:
            self.assertGreater(workflow_state(self.db).progress.enrichment_untried, 0)
        statements = [call.args[0] for call in reads.call_args_list]
        self.assertFalse(any(sql.startswith("SELECT * FROM links") for sql in statements))
        self.assertFalse(any("AS emails_json" in sql for sql in statements))
        self.assertFalse(any("SELECT * FROM worth" in sql for sql in statements))
        self.assertEqual(sum("candidate_policy AS" in sql for sql in statements), 1)

    def test_research_candidate_urls_excludes_a_non_text_linkedin_url(self):
        for slug in ("casey-delta", "jordan-bravo"):
            people = self.add_parent(slug, "yes")
            self.add_candidate(slug, slug, person_ids=people)
        url = "https://www.linkedin.com/in/jordan-bravo"
        self.db.project_rows((
            ResearchRow("casey-delta", "casey-delta", "complete", "casey-delta",
                        result_json=json.dumps({"content": {"linkedin_url": 123}})),
            ResearchRow("jordan-bravo", "jordan-bravo", "complete", "jordan-bravo",
                        result_json=json.dumps({"content": {"linkedin_url": url}})),
        ))

        self.assertEqual(research_candidate_urls(self.db), {"jordan-bravo": url})
        self.assertEqual(len(self.db.query("SELECT * FROM research WHERE status='complete'")), 2)

    def test_terminal_research_is_selected_for_current_input_cache_check(self):
        for status in ("pending", "running", "complete", "no_match", "failed"):
            self.add_parent(status, "yes")
            self.db.project_rows((ResearchRow(f"research:{status}", status, status),))
        self.add_parent("no-research", "yes")
        self.assertEqual({row.parent_id for row in enrichment_queue(self.db)},
                         {"pending", "running", "failed", "no-research", "complete", "no_match"})

    def test_enrichment_queue_scopes_identifiers_and_scans_research_once(self):
        self.add_parent("fixture", "yes")
        with mock.patch.object(self.db, "query", wraps=self.db.query) as reads:
            enrichment_queue(self.db)
        sql = reads.call_args_list[0].args[0]
        plan = self.db.query("EXPLAIN QUERY PLAN " + sql)
        details = [row["detail"] for row in plan]
        self.assertFalse(any("identifiers_by_value (kind=?)" in detail for detail in details))
        research_scans = [row for row in plan if row["detail"] == "SCAN done"]
        self.assertEqual(research_scans, [])

    def test_decision_page_does_not_hydrate_profiles_or_read_dossiers(self):
        from packs.ingestion.primitives.deep_context.db import _view_rows
        people = self.add_parent("alpha", "yes")
        self.add_candidate("alpha", "alpha-profile", person_ids=people, paid_profile=1,
            linkedin_url="https://www.linkedin.com/in/alpha-profile")
        with (
            mock.patch.object(_view_rows, "_hydrate_parents", side_effect=AssertionError("collapsed page hydrated profiles")),
            mock.patch.object(self.db, "query", wraps=self.db.query) as reads,
        ):
            rows = decision_parents(self.db, "yes")
        self.assertEqual(len(reads.call_args_list), 1)
        self.assertNotIn("FROM artifacts", reads.call_args.args[0])
        self.assertEqual((rows[0].candidates, rows[0].dossier_body, rows[0].dossier_path), ((), "", ""))

    def test_decision_page_defaults_to_ten_and_preserves_labels_and_order(self):
        for number in range(12):
            self.add_parent(f"contact-{number:02}", "yes")
            with self.db.transaction() as conn:
                conn.execute("UPDATE facts SET facts_json=? WHERE parent_id=?",
                    (json.dumps({"labels": {"Product": number / 12}}), f"contact-{number:02}"))
        first = decision_parents(self.db, "yes")
        second = decision_parents(self.db, "yes", offset=10)
        self.assertEqual(len(first), 10)
        self.assertEqual([row.parent_id for row in first + second],
            [f"contact-{number:02}" for number in range(12)])
        self.assertEqual(first[3].labels, (("Product", 0.25),))
        self.assertEqual(worth_counts(self.db).yes, 12)


if __name__ == "__main__":
    unittest.main()
