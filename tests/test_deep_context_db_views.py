from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.schema import (
    ArtifactRow,
    CandidatePersonRow,
    FactRow,
    GuidanceRow,
    JobRow,
    LinkRow,
    ParentRow,
    PersonIdentifierRow,
    PersonRow,
    SpendApprovalRow,
    StageStateRow,
    SyntheticProfileRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.views import (
    all_parents,
    avatar_path,
    directory,
    dossier_path,
    enrichment_state,
    linkedin_progress,
    linkedin_queue,
    person_detail,
    retarget_snapshot,
    settle_identity,
    siblings_of,
    stage_progress,
    stage_states,
    worth_counts,
    worth_queue,
    worth_rows,
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
        self.db.project_parent(ParentRow(
            parent_id, parent_id, f"Jordan {parent_id.title()}", f"jordan-{parent_id}"
        ))
        person_ids = []
        for index, worth in enumerate(worths or ("maybe",), 1):
            person_id = f"{parent_id}-person-{index}"
            person_ids.append(person_id)
            self.db.project_person(PersonRow(
                person_id, parent_id, display_name=f"Jordan {parent_id.title()}",
                is_owner=int(owner), is_ghost=int(ghost),
            ))
            artifact_key = f"facts:{person_id}"
            self.db.project_artifact(ArtifactRow(
                artifact_key, "facts", parent_id, f"/facts/{person_id}.jsonl",
                f"sha-{person_id}", "projected", person_id=person_id,
            ))
            self.db.project_fact(FactRow(
                person_id, parent_id, artifact_key, person_id=person_id,
                machine_worth=worth, machine_worth_reason=f"{worth or 'default'} evidence",
                is_owner=int(owner), facts_json=json.dumps({"canonical_name": "Jordan Bravo"}),
            ))
        if human:
            self.db.set_worth(parent_id, human, decided_at="2026-08-05T00:00:00Z")
        return person_ids

    def add_candidate(
        self, parent_id: str, key: str, *, person_ids: list[str] | None = None,
        **values: object,
    ) -> None:
        self.db.project_candidate(LinkRow(
            key, parent_id, str(values.pop("public_identifier", key)),
            str(values.pop("kind", "pub")), **values,
        ))
        members = tuple(
            CandidatePersonRow(key, person_id, parent_id)
            for person_id in (person_ids or [])
        )
        self.db.replace_candidate_people(key, members)

    def add_factsless_parent(self, parent_id: str) -> list[str]:
        self.db.project_parent(ParentRow(
            parent_id, parent_id, f"Jordan {parent_id.title()}", f"jordan-{parent_id}"
        ))
        person_id = f"{parent_id}-person-1"
        self.db.project_person(PersonRow(person_id, parent_id))
        return [person_id]

    def test_worth_is_facts_backed_grouped_and_policy_exact(self):
        self.add_parent("alpha", "no", "yes")
        self.add_parent("bravo", "maybe", human="no")
        self.add_parent("default", "no", None)
        self.add_parent("owner", "maybe", owner=True)
        self.add_parent("ghost", "maybe", ghost=True)
        synthetic_people = self.add_parent("synthetic", "maybe")
        self.add_candidate(
            "synthetic", "synthetic:jordan", person_ids=synthetic_people, kind="synthetic"
        )
        self.db.project_synthetic_profile(SyntheticProfileRow(
            "synthetic:jordan", "synthetic:jordan", "{}"
        ))

        rows = {row["parent_id"]: row for row in worth_rows(self.db)}
        self.assertEqual(set(rows), {"alpha", "bravo", "default", "synthetic"})
        self.assertEqual(rows["alpha"]["machine"]["decision"], "yes")
        self.assertEqual(rows["bravo"]["effective"], "no")
        self.assertEqual(rows["bravo"]["source"], "user")
        self.assertEqual([row["parent_id"] for row in worth_queue(self.db)], ["default"])
        self.assertEqual(
            worth_counts(self.db), {"total": 4, "pending": 1, "yes": 1, "no": 1}
        )

    def test_linkedin_queue_encodes_standing_and_review_policies(self):
        review_people = self.add_parent("review", "yes")
        self.add_candidate(
            "review", "paid-reject", person_ids=review_people,
            paid_profile=1, machine_judgment="wrong_person", machine_reject="yes",
            machine_confidence=0.91,
        )
        self.add_candidate(
            "review", "hard-detach", person_ids=review_people,
            machine_action="detach", authoritative_detach=1,
        )

        accepted_people = self.add_parent("accepted", "yes")
        self.add_candidate(
            "accepted", "accepted-retarget", person_ids=accepted_people,
            candidate_origin=1, machine_action="retarget",
            machine_proposed_url="https://www.linkedin.com/in/jordan-accepted",
            machine_proposed_public_identifier="jordan-accepted",
        )

        synthetic_people = self.add_parent("synthetic", "yes")
        self.add_candidate(
            "synthetic", "synthetic:pending", person_ids=synthetic_people,
            kind="synthetic", machine_action="verify", machine_approved="auto",
        )
        self.db.project_synthetic_profile(SyntheticProfileRow(
            "synthetic:pending", "synthetic:pending", "{}"
        ))

        human_people = self.add_parent("human", "yes")
        self.add_candidate(
            "human", "human-verified", person_ids=human_people, paid_profile=1
        )
        self.db.settle_identity("human-verified", "verify", approved="yes")

        ignored_people = self.add_parent("review-only", "yes")
        self.add_candidate("review-only", "export-only", person_ids=ignored_people)

        raw_people = self.add_parent("raw", "yes")
        self.add_candidate(
            "raw", "candidate:email:casey@example.com", person_ids=raw_people,
            kind="candidate_email", candidate_origin=1, raw_import=1,
        )
        no_people = self.add_parent("no", "no")
        self.add_candidate("no", "worth-no-profile", person_ids=no_people)

        self.db.project_parent(ParentRow(
            "candidate-member", "candidate-member", "Jordan Member", "jordan-member"
        ))
        member_id = "candidate:email:member@example.com"
        self.db.project_person(PersonRow(member_id, "candidate-member"))
        self.db.project_artifact(ArtifactRow(
            "facts:candidate-member", "facts", "candidate-member",
            "/facts/candidate-member.jsonl", "sha-candidate-member", "projected",
            person_id=member_id,
        ))
        self.db.project_fact(FactRow(
            member_id, "candidate-member", "facts:candidate-member", person_id=member_id,
            machine_worth="yes", facts_json="{}",
        ))
        self.add_candidate(
            "candidate-member", "jordan-member", person_ids=[member_id], paid_profile=1,
            machine_action="verify", machine_approved="auto",
        )

        factsless_synthetic = self.add_factsless_parent("factsless-synthetic")
        self.add_candidate(
            "factsless-synthetic", "synthetic:factsless", person_ids=factsless_synthetic,
            kind="synthetic", machine_action="verify", machine_approved="auto",
        )
        self.db.project_synthetic_profile(SyntheticProfileRow(
            "synthetic:factsless", "synthetic:factsless", "{}"
        ))
        rejected_synthetic = self.add_factsless_parent("rejected-synthetic")
        self.add_candidate(
            "rejected-synthetic", "synthetic:rejected", person_ids=rejected_synthetic,
            kind="synthetic",
        )
        self.db.project_synthetic_profile(SyntheticProfileRow(
            "synthetic:rejected", "synthetic:rejected", "{}"
        ))
        self.db.settle_identity("synthetic:rejected", "detach", approved="yes")
        factsless_candidate = self.add_factsless_parent("factsless-candidate")
        self.add_candidate(
            "factsless-candidate", "candidate:email:review@example.com",
            person_ids=factsless_candidate, kind="candidate_email", candidate_origin=1,
            paid_profile=1, machine_action="retarget",
            machine_proposed_url="https://www.linkedin.com/in/jordan-review",
            machine_judgment="needs_review", machine_reject="yes",
        )

        queue = {parent["parent_id"]: parent for parent in linkedin_queue(self.db)}
        self.assertEqual(set(queue), {
            "review", "synthetic", "factsless-synthetic", "factsless-candidate",
        })
        self.assertEqual(
            [candidate["row_key"] for candidate in queue["review"]["candidates"]],
            ["paid-reject"],
        )
        self.assertTrue(queue["synthetic"]["candidates"][0]["pending"])
        self.assertEqual(linkedin_progress(self.db), {"total": 7, "pending": 4, "done": 3})

        progress = stage_progress(self.db)
        self.assertEqual(progress["linkedin_pending"], 4)
        self.assertEqual(progress["linkedin_done"], 3)
        self.assertEqual(progress["lookup_ready"], 1)

    def test_settle_derives_every_sibling_and_preserves_existing_human(self):
        people = self.add_parent("family", "yes", "maybe")
        self.add_candidate("family", "human-kept", person_ids=[people[1]])
        self.db.settle_identity("human-kept", "verify", approved="yes")
        self.add_candidate("family", "clicked", person_ids=[people[0]])
        self.add_candidate("family", "ghost", person_ids=[people[1]], kind="ghost")
        self.add_candidate(
            "family", "synthetic:sibling", person_ids=[people[0]], kind="synthetic"
        )
        self.db.project_synthetic_profile(SyntheticProfileRow(
            "synthetic:sibling", "synthetic:sibling", "{}"
        ))

        self.assertEqual(
            siblings_of(self.db, "clicked"),
            ["clicked", "ghost", "human-kept", "synthetic:sibling"],
        )
        settled = settle_identity(
            self.db, "clicked", "retarget",
            replacement_url="https://www.linkedin.com/in/jordan-replacement",
            replacement_public_identifier="jordan-replacement",
        )
        self.assertEqual(set(settled), {"clicked", "ghost", "synthetic:sibling"})
        rows = {row["row_key"]: row for row in self.db.query("SELECT * FROM links")}
        self.assertEqual(rows["clicked"]["replacement_public_identifier"], "jordan-replacement")
        self.assertEqual(rows["human-kept"]["decision_action"], "verify")
        self.assertEqual(rows["human-kept"]["decision_source"], "deep-context-review")
        self.assertEqual(rows["ghost"]["decision_action"], "detach")
        self.assertEqual(rows["synthetic:sibling"]["decision_action"], "detach")

    def test_directory_and_person_detail_hydrate_only_sql_projection(self):
        people = self.add_parent("detail", "yes")
        self.db.replace_person_identifiers(people[0], (
            PersonIdentifierRow(people[0], "email", "casey@example.com"),
            PersonIdentifierRow(people[0], "phone", "+15550100"),
        ))
        self.add_candidate(
            "detail", "jordan-detail", person_ids=people,
            linkedin_url="https://www.linkedin.com/in/jordan-detail",
            display_name="Jordan Detail", machine_action="verify", machine_approved="auto",
            judgment_payload_json=json.dumps({
                "linkedin": {"headline": "Product leader", "location": "Oakland"}
            }),
        )
        self.db.project_artifact(ArtifactRow(
            "dossier:detail", "dossier", "detail", "/dossiers/jordan-detail.md",
            "sha-dossier", "projected",
        ))

        self.assertEqual(directory(self.db), [
            {"slug": "jordan-detail", "name": "Jordan Detail", "worth": "yes"}
        ])
        detail = person_detail(self.db, "jordan-detail")
        assert detail is not None
        self.assertEqual(detail["dossier_path"], "/dossiers/jordan-detail.md")
        self.assertEqual(detail["candidates"][0]["headline"], "Product leader")
        self.assertEqual(detail["candidates"][0]["match_emails"], ["casey@example.com"])
        self.assertEqual(detail["candidates"][0]["match_phones"], ["+15550100"])

    def test_web_snapshots_are_named_sql_reads(self):
        people = self.add_parent("state", "yes")
        self.add_candidate(
            "state", "jordan-state", person_ids=people, paid_profile=1,
            linkedin_url="https://www.linkedin.com/in/jordan-state",
        )
        self.db.save_stage(StageStateRow(
            "enrich", "complete", "selection-1", "artifact-1",
            "2026-08-05T01:00:00Z", updated_at="2026-08-05T01:00:00Z",
        ))
        self.db.save_job(JobRow(
            "enrich", "enrichment", "applied", selection_fingerprint="selection-1",
            completed_count=1, total_count=1, result_json='{"would_submit":1}',
        ))
        self.db.approve_spend(SpendApprovalRow(
            "enrich", "selection-1", 1, 0.25, "2026-08-05T00:00:00Z"
        ))
        self.db.save_guidance(GuidanceRow(
            "guide-state", "state", "use another profile", candidate_key="jordan-state",
            detail_json='{"attempt":1}',
        ))
        self.db.save_job(JobRow(
            "retarget:state", "guided_retarget", "queued", parent_id="state",
            candidate_key="jordan-state", total_count=1, result_json="{}",
        ))
        self.db.project_artifact(ArtifactRow(
            "avatar:state", "profile", "state", "/avatars/jordan-state.jpg",
            "sha-avatar", "projected", candidate_key="jordan-state",
        ))
        self.db.project_artifact(ArtifactRow(
            "dossier:state", "dossier", "state", "/dossiers/jordan-state.md",
            "sha-dossier", "projected",
        ))

        self.assertEqual(stage_states(self.db)["enrich"]["status"], "complete")
        state = enrichment_state(self.db)
        self.assertTrue(state["approval_current"])
        self.assertEqual(state["result"]["would_submit"], 1)
        self.assertEqual(len(retarget_snapshot(self.db)["guidance"]), 1)
        self.assertEqual(avatar_path(self.db, "jordan-state"), "/avatars/jordan-state.jpg")
        self.assertEqual(dossier_path(self.db, "jordan-state"), "/dossiers/jordan-state.md")
        self.assertEqual(all_parents(self.db)[0]["parent_id"], "state")


if __name__ == "__main__":
    unittest.main()
