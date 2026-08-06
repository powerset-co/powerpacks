from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    CandidatePersonRow,
    FactRow,
    GuidanceRow,
    JobRow,
    LinkRow,
    ParentRow,
    PersonIdentifierRow,
    PersonRow,
    PersonSourceRow,
    SpendApprovalRow,
    StageStateRow,
    SyntheticProfileRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.views import (
    avatar_path,
    directory,
    dossier_path,
    linkedin_review,
    person_detail,
    retarget_snapshot,
    worth_review,
    workflow_state,
)
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

        rows = {row["parent_id"]: row for row in worth_review(self.db, "rows")}
        self.assertEqual(set(rows), {"alpha", "bravo", "default", "synthetic"})
        self.assertEqual(rows["alpha"]["machine"]["decision"], "yes")
        self.assertEqual(rows["bravo"]["effective"], "no")
        self.assertEqual(rows["bravo"]["source"], "user")
        self.assertEqual([row["parent_id"] for row in worth_review(self.db, "queue")], ["default"])
        self.assertEqual(
            worth_review(self.db, "counts"),
            {"total": 4, "pending": 1, "yes": 1, "no": 1},
        )

    def test_enrichment_queue_is_one_effective_yes_sql_policy(self):
        wrong_people = self.add_parent("wrong", "yes")
        self.add_candidate(
            "wrong", "wrong-link", person_ids=wrong_people,
            linkedin_url="https://www.linkedin.com/in/wrong-link",
            machine_judgment="wrong_person", machine_confidence=0.91,
            judgment_payload_json=json.dumps({"recommend_deep_research": True}),
        )
        candidate_people = self.add_parent("candidate", "yes")
        self.add_candidate(
            "candidate", "candidate:email:jordan@example.com",
            person_ids=candidate_people, candidate_origin=1, raw_import=1,
        )
        no_people = self.add_parent("rejected", "no")
        self.add_candidate(
            "rejected", "rejected-link", person_ids=no_people,
            machine_judgment="wrong_person", machine_confidence=0.99,
            judgment_payload_json=json.dumps({"recommend_deep_research": True}),
        )

        default = linkedin_review(self.db, "enrichment")
        expanded = linkedin_review(self.db, "enrichment", include_candidates=True)

        self.assertEqual([row["candidate_key"] for row in default], ["wrong-link"])
        self.assertEqual(
            {row["candidate_key"] for row in expanded},
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
            machine_reject="yes",
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
            machine_reject="yes",
        )

        queue = {parent["parent_id"]: parent for parent in linkedin_review(self.db, "queue")}
        self.assertEqual(
            set(queue),
            {
                "review",
                "synthetic",
                "factsless-synthetic",
                "factsless-candidate",
            },
        )
        self.assertEqual(
            [candidate["row_key"] for candidate in queue["review"]["candidates"]],
            ["paid-reject"],
        )
        self.assertTrue(queue["synthetic"]["candidates"][0]["pending"])
        self.assertEqual(linkedin_review(self.db, "progress"), {"total": 7, "pending": 4, "done": 3})

        progress = workflow_state(self.db)["progress"]
        self.assertEqual(progress["linkedin_pending"], 4)
        self.assertEqual(progress["linkedin_done"], 3)
        self.assertEqual(progress["lookup_ready"], 2)
        self.assertEqual(progress["rejected"], 2)

    def test_settle_derives_every_sibling_and_preserves_existing_human(self):
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
        self.assertEqual(set(settled), {"clicked", "ghost", "synthetic:sibling"})
        rows = {row["row_key"]: row for row in query(self.db, "SELECT * FROM links")}
        self.assertEqual(rows["clicked"]["replacement_public_identifier"], "jordan-replacement")
        self.assertEqual(rows["human-kept"]["decision_action"], "verify")
        self.assertEqual(rows["human-kept"]["decision_source"], "deep-context-review")
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

        self.assertEqual(directory(self.db), [{"slug": "jordan-detail", "name": "Jordan Detail", "worth": "yes"}])
        detail = person_detail(self.db, "jordan-detail")
        assert detail is not None
        self.assertEqual(detail["dossier_path"], "/dossiers/jordan-detail.md")
        self.assertEqual(detail["candidates"][0]["headline"], "Product leader")
        self.assertEqual(detail["candidates"][0]["match_emails"], ["casey@example.com"])
        self.assertEqual(detail["candidates"][0]["match_phones"], ["+15550100"])
        self.assertEqual(detail["sources"], ["gmail"])
        self.assertEqual(detail["source_channels"], ["gmail_msgvault", "linkedin_csv"])

    def test_web_snapshots_are_named_sql_reads(self):
        people = self.add_parent("state", "yes")
        self.add_candidate(
            "state",
            "jordan-state",
            person_ids=people,
            paid_profile=1,
            linkedin_url="https://www.linkedin.com/in/jordan-state",
        )
        revision = "2026-08-05T00:30:00Z"
        self.db.save_state(
            StageStateRow(
                "worth",
                "complete",
                completed_at=revision,
                updated_at=revision,
            )
        )
        selection = workflow_state(self.db)["selection"]
        expected_decisions = [
            {
                "person_id": "parent-worth:state",
                "decision": "yes",
            }
        ]
        self.assertEqual(
            selection,
            {
                "sha256": hashlib.sha256(json.dumps(expected_decisions, separators=(",", ":")).encode()).hexdigest(),
                "total": 1,
                "yes": 1,
                "maybe": 0,
                "no": 0,
                "review_revision": revision,
            },
        )
        self.db.save_state(
            StageStateRow(
                "enrich",
                "complete",
                selection["sha256"],
                "artifact-1",
                "2026-08-05T01:00:00Z",
                updated_at="2026-08-05T01:00:00Z",
            )
        )
        self.db.save_state(
            JobRow(
                "enrich",
                "enrichment",
                "applied",
                selection_fingerprint=selection["sha256"],
                completed_count=1,
                total_count=1,
                result_json=json.dumps(
                    {
                        "would_submit": 1,
                        "selection": {
                            "sha256": selection["sha256"],
                            "review_revision": revision,
                        },
                    }
                ),
            )
        )
        self.db.save_state(SpendApprovalRow("enrich", selection["sha256"], 1, 0.25, "2026-08-05T00:00:00Z"))
        self.db.save_state(
            GuidanceRow(
                "guide-state",
                "state",
                "use another profile",
                candidate_key="jordan-state",
                detail_json='{"attempt":1}',
            )
        )
        self.db.save_state(
            JobRow(
                "retarget:state",
                "guided_retarget",
                "queued",
                parent_id="state",
                candidate_key="jordan-state",
                total_count=1,
                result_json="{}",
            )
        )
        project_artifact(
            self.db,
            ArtifactRow(
                "avatar:state",
                "avatar",
                "state",
                "/avatars/jordan-state.image",
                "sha-avatar",
                "projected",
                candidate_key="jordan-state",
            ),
        )
        project_artifact(
            self.db,
            ArtifactRow(
                "dossier:state",
                "dossier",
                "state",
                "/dossiers/jordan-state.md",
                "sha-dossier",
                "projected",
            ),
        )

        manifest = workflow_state(self.db)["review_manifest"]
        self.assertEqual(
            manifest,
            {
                "stage": "enrich",
                "status": "completed",
                "counts": {"total": 0, "completed": 0, "pending": 0, "failed": 0},
                "completed_stages": ["worth", "enrich"],
                "people_revision": revision,
                "updated_at": "2026-08-05T01:00:00Z",
                "completed_at": "2026-08-05T01:00:00Z",
            },
        )
        state = workflow_state(self.db)["enrichment"]
        self.assertEqual(state["status"], "completed")
        self.assertTrue(state["current"])
        self.assertFalse(state["approval_current"])
        self.assertEqual(state["state"], "done")
        self.assertEqual(state["would_submit"], 1)
        workflow = workflow_state(self.db)
        self.assertEqual(
            set(workflow),
            {
                "primitive",
                "status",
                "next_action",
                "progress",
                "selection",
                "review_manifest",
                "enrichment",
                "state_token",
            },
        )
        self.assertEqual(workflow["next_action"], "review_linkedin")
        self.assertNotEqual(workflow["state_token"], workflow_state(self.db, job_running=True)["state_token"])
        self.assertEqual(len(retarget_snapshot(self.db)["guidance"]), 1)
        self.assertEqual(avatar_path(self.db, "jordan-state"), "/avatars/jordan-state.image")
        self.assertEqual(dossier_path(self.db, "jordan-state"), "/dossiers/jordan-state.md")
        self.assertEqual(linkedin_review(self.db, "parents")[0]["parent_id"], "state")

    def test_enrichment_freshness_and_approval_are_one_named_read(self) -> None:
        self.add_parent("approval", "yes")
        revision = "2026-08-05T02:00:00Z"
        self.db.save_state(
            StageStateRow(
                "worth",
                "complete",
                completed_at=revision,
                updated_at=revision,
            )
        )
        selection = workflow_state(self.db)["selection"]
        result = {
            "status": "needs_approval",
            "counts": {"total": 2, "completed": 0, "pending": 2, "failed": 0},
            "selection": {
                "sha256": selection["sha256"],
                "review_revision": revision,
            },
            "would_submit": 2,
            "estimated_usd": 0.5,
        }
        self.db.save_state(
            StageStateRow(
                "enrich",
                "needs_approval",
                selection["sha256"],
                updated_at=revision,
            )
        )
        self.db.save_state(
            JobRow(
                "enrich",
                "enrichment",
                "queued",
                selection_fingerprint=selection["sha256"],
                total_count=2,
                result_json=json.dumps(result),
            )
        )
        self.db.save_state(
            SpendApprovalRow(
                "enrich",
                selection["sha256"],
                2,
                0.5,
                revision,
            )
        )

        current = workflow_state(self.db)["enrichment"]
        self.assertEqual(current["status"], "needs_approval")
        self.assertEqual(current["selection"], result["selection"])
        self.assertTrue(current["current"])
        self.assertTrue(current["approval_current"])
        self.assertTrue(current["approvable"])
        self.assertEqual(current["state"], "needs_approval")
        self.assertEqual(
            current["approval"],
            {
                "status": "approved",
                "approved_at": revision,
                "approved_budget_usd": 0.5,
                "estimated_usd": 0.5,
                "would_submit": 2,
                "selection_sha256": selection["sha256"],
                "review_revision": revision,
            },
        )
        self.assertEqual(workflow_state(self.db)["next_action"], "run_approved_enrichment")

        self.db.decide_worth("approval", "no", decided_at="2026-08-05T02:01:00Z")
        stale = workflow_state(self.db)["enrichment"]
        self.assertEqual(stale["status"], "stale")
        self.assertFalse(stale["current"])
        self.assertFalse(stale["approval_current"])
        self.assertEqual(stale["state"], "free_pending")
        self.assertEqual(workflow_state(self.db)["next_action"], "preview_enrichment")


if __name__ == "__main__":
    unittest.main()
