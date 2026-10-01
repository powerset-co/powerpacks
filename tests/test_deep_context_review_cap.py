"""Relationship finishing limits questions without guessing identities."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow, FactRow, LinkRow, ParentRow, PersonRow, WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.db.workflow_views import workflow_state
from packs.ingestion.primitives.deep_context.db.identity_queries import links
from packs.ingestion.primitives.deep_context.db.identity_views import pending_parent_ids, review_questions_pending, judge_candidates
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.results import RetargetProposal, upsert_retargets
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import IdentityVerdict
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.review_cap import (
    RelationshipDecision, cache_relationship_judgment, finish_reviews,
)


class ReviewCapTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Db(Path(self.temp.name) / "context.sqlite")

    def parent(self, number, *, children=1):
        parent = f"parent-{number:03}"
        self.db.project_rows((
            ParentRow(parent, f"worth:{parent}", "Jordan Bravo"),
            PersonRow(f"candidate:{parent}", parent, display_name="Jordan Bravo"),
            ArtifactRow(f"facts:{parent}", "facts", parent, f"/facts/{parent}", "fixture", "projected"),
            FactRow(parent, parent, f"facts:{parent}", machine_worth="yes", facts_json='{"canonical_name":"Jordan Bravo"}'),
            *(LinkRow(f"{parent}:{i}", parent, f"jordan-{number}-{i}", "pub",
                      linkedin_url=f"https://linkedin.com/in/jordan-{number}-{i}",
                      candidate_origin=True, source=WriterSource.RECONCILE.value) for i in range(children)),
        ))
        return parent

    def decision(self, parent, *, question=True, priority=1):
        return RelationshipDecision(parent, "A meaningful colleague.", question,
            "Is Jordan your robotics colleague?" if question else "", priority if question else 0,
            f"relationship:{parent}")

    def test_cap_counts_parents_and_finishes_all_children_without_verifying(self):
        parents = [self.parent(i, children=2) for i in range(102)]
        result = finish_reviews(self.db, [self.decision(parent) for parent in parents])
        self.assertEqual(result["review_parents"], 100)
        self.assertEqual(len(pending_parent_ids(self.db)), 100)
        for row in links(self.db, parent_ids=parents[-2:]):
            self.assertEqual((row.machine_action, row.machine_approved, row.machine_judgment),
                             ("detach", "auto", None))
            self.assertIsNone(row.machine_confidence)
            self.assertFalse(row.authoritative_detach)

    def test_priority_then_message_count_selects_global_questions(self):
        parents = [self.parent(i) for i in range(3)]
        decisions = [self.decision(parent) for parent in parents]
        decisions[1] = replace(decisions[1], message_count=20)
        decisions[2] = replace(decisions[2], priority=3)
        finish_reviews(self.db, decisions, limit=2)
        self.assertEqual(pending_parent_ids(self.db), set(parents[1:]))

    def test_finishing_preserves_human_worth_and_fact_worth(self):
        parent = self.parent(1)
        self.db.decide_worth(parent, "yes")
        before = self.db.query("SELECT human_worth FROM parents"), self.db.query("SELECT * FROM facts")
        finish_reviews(self.db, [self.decision(parent, question=False)])
        self.assertEqual((self.db.query("SELECT human_worth FROM parents"), self.db.query("SELECT * FROM facts")), before)
        self.assertEqual(links(self.db, parent_id=parent)[0].machine_action, "detach")

    def test_human_decisions_and_machine_accepted_rows_survive(self):
        parent = self.parent(1, children=3)
        self.db.decide_identity(f"{parent}:0", "detach")
        self.db.project_rows((LinkRow(f"{parent}:1", parent, "jordan-1-1", "pub",
            linkedin_url="https://linkedin.com/in/jordan-1-1", candidate_origin=True,
            machine_action="verify", machine_approved="auto", machine_judgment="confirmed",
            source=WriterSource.RECONCILE.value),))
        before = {row.row_key: row for row in links(self.db) if row.row_key.endswith((":0", ":1"))}
        finish_reviews(self.db, [self.decision(parent, question=False)])
        self.assertEqual({key: links(self.db, row_key=key)[0] for key in before}, before)

    def test_missing_decision_fails_before_writing(self):
        first, _ = [self.parent(i) for i in range(2)]
        before = links(self.db)
        with self.assertRaises(StoreError):
            finish_reviews(self.db, [self.decision(first)])
        self.assertEqual(links(self.db), before)

    def test_checkpoint_preserves_identity_and_answered_questions_do_not_refill(self):
        first, second = [self.parent(i) for i in range(2)]
        before = links(self.db, parent_id=first)[0]
        cache_relationship_judgment(self.db, self.decision(first))
        after = links(self.db, parent_id=first)[0]
        self.assertEqual(after.machine_action, before.machine_action)
        self.assertEqual(after.judgment_fingerprint, self.decision(first).fingerprint)
        finish_reviews(self.db, [self.decision(first), self.decision(second)], limit=1)
        self.db.decide_identity(f"{first}:0", "detach")
        finish_reviews(self.db, [self.decision(first), self.decision(second)], limit=1)
        self.assertEqual(pending_parent_ids(self.db), set())

    def test_detach_preserves_paid_identity_judgment(self):
        parent = self.parent(1)
        payload = {"verdict": "wrong_person", "confidence": 0.93, "reason": "Different school."}
        self.db.project_rows((LinkRow(f"{parent}:0", parent, "jordan-1-0", "pub",
            linkedin_url="https://linkedin.com/in/jordan-1-0", candidate_origin=True,
            machine_action="retarget", machine_judgment="wrong_person",
            machine_confidence=0.93, machine_reason="Different school.",
            machine_proposed_url="https://linkedin.com/in/jordan-other",
            machine_proposed_public_identifier="jordan-other", paid_profile=True,
            judgment_fingerprint="paid-identity", judgment_payload_json=json.dumps(payload),
            source=WriterSource.RECONCILE.value),))
        before = links(self.db)[0]
        finish_reviews(self.db, [self.decision(parent, question=False)])
        after = links(self.db)[0]
        self.assertEqual((after.machine_action, after.machine_approved), ("detach", "auto"))
        for field in ("machine_judgment", "machine_confidence", "machine_reason",
                      "paid_profile", "judgment_fingerprint"):
            self.assertEqual(getattr(after, field), getattr(before, field), field)
        self.assertIsNone(after.machine_proposed_url)
        self.assertIsNone(after.machine_proposed_public_identifier)
        saved = json.loads(after.judgment_payload_json)
        self.assertEqual({key: saved[key] for key in payload}, payload)

    def test_non_object_identity_payload_can_checkpoint_and_finish(self):
        for number, checkpoint in ((1, True), (2, False)):
            with self.subTest(checkpoint=checkpoint):
                parent = self.parent(number)
                with self.db.transaction() as conn:
                    conn.execute("UPDATE links SET judgment_payload_json=? WHERE parent_id=?",
                         (json.dumps("relationship_judgment"), parent))
                decision = self.decision(parent, question=False)
                if checkpoint:
                    cache_relationship_judgment(self.db, decision)
                finish_reviews(self.db, [decision])
                self.assertEqual(links(self.db, parent_id=parent)[0].machine_action, "detach")

    def test_finished_verdictless_identity_does_not_queue_paid_judging(self):
        parent = self.parent(1)
        self.assertEqual(len(judge_candidates(self.db)), 1)
        finish_reviews(self.db, [self.decision(parent, question=False)])
        self.assertEqual(judge_candidates(self.db), [])
        self.assertEqual(workflow_state(self.db).next_action, "realize")

    def test_pending_retarget_preserves_selected_question_and_updates_identity(self):
        parent = self.parent(1)
        with self.db.transaction() as conn:
            conn.execute("UPDATE links SET judgment_payload_json=? WHERE parent_id=?",
                (json.dumps({"verdict": "wrong_person", "reason": "Old evidence."}), parent))
        finish_reviews(self.db, [self.decision(parent)])
        before = json.loads(links(self.db)[0].judgment_payload_json)["relationship_decision"]
        verdict = IdentityVerdict.from_payload({
            "verdict": "needs_review", "confidence": 0.6, "reason": "New research evidence.",
        })
        self.assertEqual(upsert_retargets(self.db, [RetargetProposal(
            candidate_key=f"{parent}:0", new_linkedin_url="https://linkedin.com/in/jordan-other",
            judge_fingerprint="new-identity", judge_payload=verdict,
        )]), 1)
        row = links(self.db)[0]
        payload = json.loads(row.judgment_payload_json)
        self.assertEqual(payload["relationship_decision"], before)
        self.assertEqual(payload["verdict"], "needs_review")
        self.assertEqual(payload["reason"], "New research evidence.")
        self.assertEqual(row.machine_judgment, "needs_review")
        self.assertEqual(row.judgment_fingerprint, "new-identity")
        self.assertEqual(review_questions_pending(self.db), 0)
        self.assertEqual(pending_parent_ids(self.db), {parent})

    def test_new_identity_verdict_replaces_non_object_prior_payload(self):
        from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.settlement import (
            MachineIdentitySettlement, settle_machine_identities,
        )
        parent = self.parent(1)
        with self.db.transaction() as conn:
            conn.execute("UPDATE links SET judgment_payload_json=? WHERE parent_id=?",
                         (json.dumps("relationship_judgment"), parent))
        settlement = replace(MachineIdentitySettlement.from_link(links(self.db)[0]),
            judgment_fingerprint="new-identity", judgment_payload_json='{"verdict":"confirmed"}',
            machine_action="verify", machine_approved="auto", machine_judgment="confirmed")
        settle_machine_identities(self.db, [settlement])
        self.assertEqual(json.loads(links(self.db)[0].judgment_payload_json), {"verdict": "confirmed"})
