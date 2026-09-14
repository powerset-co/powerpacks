from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow, FactRow, LinkRow, ParentRow, PersonRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.db.identity_queries import links
from packs.ingestion.primitives.deep_context.db.identity_views import enrichment_queue, pending_parent_ids
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.review_cap import (
    RelationshipDecision, cache_relationship_judgment, finish_reviews,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.queue import build_tasks
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.results import (
    RetargetProposal, load_tasks_from_store, upsert_retargets,
    write_overrides,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import IdentityVerdict
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.selection import select_research
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.healing import select_candidates
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.settlement import (
    MachineIdentitySettlement, settle_machine_identities,
)


class ReviewCapTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Db(Path(self.temp.name) / "deep-context.sqlite")

    def tearDown(self):
        self.temp.cleanup()

    def parent(self, number, *, children=1):
        parent_id = f"parent-{number:03}"
        person_id = f"candidate:{parent_id}"
        self.db.project_rows((
            ParentRow(parent_id, f"parent-worth:{parent_id}", "Jordan Bravo"),
            PersonRow(person_id, parent_id, display_name="Jordan Bravo"),
            ArtifactRow(f"facts:{parent_id}", "facts", parent_id,
                        f"/facts/{parent_id}.jsonl", parent_id, "projected"),
            FactRow(parent_id, parent_id, f"facts:{parent_id}", machine_worth="yes",
                    facts_json='{"canonical_name":"Jordan Bravo"}'),
            *(LinkRow(f"{parent_id}:{child}", parent_id, f"jordan-{number}-{child}", "pub",
                      linkedin_url=f"https://www.linkedin.com/in/jordan-{number}-{child}/",
                      candidate_origin=True, source="deep-context-reconcile")
              for child in range(children)),
        ))
        return parent_id

    def decision(self, parent_id, *, decision="keep", question=True, priority=1):
        return RelationshipDecision(parent_id, decision, "An actual recurring contact.",
            question, "Did you meet Jordan through astronomy club?" if question else "",
            priority, f"relationship:{parent_id}")

    def test_cap_counts_all_parents_and_all_children_finish_together(self):
        parents = [self.parent(i, children=2) for i in range(103)]
        result = finish_reviews(self.db, [self.decision(p) for p in reversed(parents)])
        self.assertEqual(pending_parent_ids(self.db), set(parents[:100]))
        self.assertEqual(result["review_parents"], 100)
        rows = self.db.query("SELECT * FROM links WHERE parent_id=?", (parents[-1],))
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual((row["machine_action"], row["machine_approved"],
                              row["machine_judgment"], row["authoritative_detach"]),
                             ("detach", "auto", "needs_review", 0))
            self.assertFalse(json.loads(row["judgment_payload_json"])["recommend_deep_research"])
            self.assertEqual(IdentityVerdict.from_payload(
                json.loads(row["judgment_payload_json"])).value, "needs_review")

    def test_incomplete_decisions_fail_before_any_write(self):
        parents = [self.parent(i) for i in range(2)]
        before = self.db.query("SELECT * FROM links")
        with self.assertRaises(StoreError):
            finish_reviews(self.db, [self.decision(parents[0])])
        self.assertEqual(self.db.query("SELECT * FROM links"), before)

    def test_only_useful_questions_remain_and_human_worth_yes_prevents_exclude(self):
        keep, exclude, human = [self.parent(i) for i in range(3)]
        self.db.decide_worth(human, "yes")
        finish_reviews(self.db, [self.decision(keep, question=False),
            self.decision(exclude, decision="exclude"),
            self.decision(human, decision="exclude", question=False)])
        self.assertEqual(pending_parent_ids(self.db), set())
        actions = {row["parent_id"]: row["machine_action"] for row in self.db.query("SELECT * FROM links")}
        self.assertEqual(actions, {keep: "detach", exclude: "exclude", human: "detach"})

    def test_reply_and_rerun_never_replace_the_question(self):
        first, second = [self.parent(i) for i in range(2)]
        decisions = [self.decision(first, priority=3), self.decision(second)]
        finish_reviews(self.db, decisions, limit=1)
        self.db.decide_identity(f"{first}:0", "detach")
        finish_reviews(self.db, decisions, limit=1)
        self.assertEqual(pending_parent_ids(self.db), set())
        self.assertEqual(self.db.query("SELECT decision_action FROM links WHERE parent_id=?", (first,))[0]["decision_action"], "detach")

    def test_question_priority_then_interaction_are_global(self):
        parents = [self.parent(i) for i in range(4)]
        decisions = [replace(self.decision(parent), message_count=5)
                     for parent in parents]
        decisions[2] = replace(decisions[2], message_count=6)
        decisions[3] = replace(decisions[3], priority=3)
        finish_reviews(self.db, decisions, limit=2)
        self.assertEqual(pending_parent_ids(self.db), set(parents[2:]))
        row = links(self.db, parent_id=parents[3])[0]
        self.assertIn(decisions[3].human_question, row.machine_reason)
        self.assertEqual(json.loads(row.judgment_payload_json)["relationship_decision"]["priority"], 3)

    def test_saved_question_is_visible_once_on_single_and_multiple_profile_cards(self):
        from packs.ingestion.primitives.deep_context.db.people_views import person_detail
        from packs.ingestion.primitives.deep_context.review.rendering import render_linkedin_card
        for number, children in enumerate((1, 2)):
            parent_id = self.parent(number, children=children)
            question = "Did you meet Jordan at <Example Robotics>?"
            decisions = [replace(self.decision(p), human_question=question)
                         for p in pending_parent_ids(self.db)]
            finish_reviews(self.db, decisions)
            parent = person_detail(self.db, parent_id)
            html = render_linkedin_card(parent, parent.candidates)
            self.assertEqual(html.count("Did you meet Jordan at &lt;Example Robotics&gt;?"), 1)
            self.assertNotIn("<Example Robotics>", html)

    def test_previously_settled_parent_is_untouched_even_if_supplied(self):
        parent = self.parent(1)
        settle_machine_identities(self.db, [MachineIdentitySettlement(
            f"{parent}:0", "verified", None, "verify", "auto", .99,
            "Verified from an owned address", "confirmed", source="deep-context-reconcile")])
        before = self.db.query("SELECT * FROM links")
        finish_reviews(self.db, [self.decision(parent, decision="exclude")])
        self.assertEqual(self.db.query("SELECT * FROM links"), before)

    def test_unresolved_conflict_replaces_old_verdict_without_claiming_wrong_person(self):
        parent = self.parent(1)
        settle_machine_identities(self.db, [MachineIdentitySettlement(
            f"{parent}:0", "old-judge", json.dumps({"verdict": "confirmed", "confidence": .8,
                "supporting_evidence": ["An old company overlap"]}),
            "review", None, .8, "Competing targets", "confirmed", source="deep-context-reconcile")])
        finish_reviews(self.db, [self.decision(parent, question=False)])
        row = links(self.db, parent_id=parent)[0]
        payload = json.loads(row.judgment_payload_json)
        self.assertEqual(IdentityVerdict.from_payload(payload).value, "needs_review")
        self.assertNotIn("value", payload)
        self.assertIsNone(row.machine_confidence)
        self.assertFalse(row.authoritative_detach)

    def test_selected_question_skips_ordinary_judging_but_explicit_force_can_rejudge(self):
        parent = self.parent(1)
        finish_reviews(self.db, [self.decision(parent)])
        before = self.db.query("SELECT * FROM links")
        self.assertEqual(build_tasks(self.db), [])
        self.assertEqual(load_tasks_from_store(self.db), [])
        self.assertEqual(enrichment_queue(self.db, include_plausibly_absent=True), [])
        self.assertEqual(select_candidates(self.db, None, lambda _: None).candidates, ())
        self.assertEqual(len(build_tasks(self.db, force=True)), 1)
        self.assertEqual(self.db.query("SELECT * FROM links"), before)

    def test_terminal_rows_do_not_reenter_reconcile_reapply_research_or_healing(self):
        parent = self.parent(1)
        finish_reviews(self.db, [self.decision(parent, question=False)])
        self.assertEqual(build_tasks(self.db), [])
        self.assertEqual(load_tasks_from_store(self.db), [])
        self.assertEqual(enrichment_queue(self.db, include_plausibly_absent=True), [])
        self.assertEqual(select_candidates(self.db, None, lambda _: None).candidates, ())

    def test_parent_completion_also_covers_nonreview_siblings_and_preserves_verified_link(self):
        parent = self.parent(1)
        self.db.project_rows((
            LinkRow("other-child", parent, "casey-bravo", "pub",
                    linkedin_url="https://www.linkedin.com/in/casey-bravo/",
                    machine_action="review", source="deep-context-reconcile"),
            LinkRow("verified-child", parent, "jordan-bravo", "pub",
                    linkedin_url="https://www.linkedin.com/in/jordan-bravo/", paid_profile=True,
                    machine_action="verify", machine_approved="auto", machine_judgment="confirmed",
                    machine_confidence=.99, source="deep-context-reconcile"),
        ))
        verified = links(self.db, row_key="verified-child")
        finish_reviews(self.db, [self.decision(parent, question=False)])
        self.assertEqual(links(self.db, row_key="verified-child"), verified)
        self.assertEqual(links(self.db, row_key="other-child")[0].machine_action, "detach")
        self.assertEqual(build_tasks(self.db), [])

    def test_question_suppresses_nonreview_siblings_too(self):
        parent = self.parent(1)
        self.db.project_rows((LinkRow("other-child", parent, "casey-bravo", "pub",
            linkedin_url="https://www.linkedin.com/in/casey-bravo/", source="deep-context-reconcile",
            machine_action="verify", machine_approved="auto", machine_judgment="confirmed"),))
        finish_reviews(self.db, [self.decision(parent)])
        self.assertEqual(build_tasks(self.db), [])

    def test_terminal_raw_import_cannot_schedule_paid_research(self):
        parent = self.parent(1)
        finish_reviews(self.db, [self.decision(parent, question=False)])
        row = links(self.db, parent_id=parent)[0]
        self.db.project_rows((replace(LinkRow(**{field.name: getattr(row, field.name)
            for field in fields(LinkRow)}), raw_import=True),))
        self.assertEqual(enrichment_queue(self.db, include_plausibly_absent=True), [])
        with patch("packs.ingestion.primitives.deep_context.enrich.research_reconcile.selection.build_queue_row") as build:
            selected = select_research(self.db, processor="core2x", confirm_threshold=.8,
                                       include_plausibly_absent=True)
        self.assertEqual(selected.pending, ())
        build.assert_not_called()

    def test_stale_machine_retarget_cannot_reopen_terminal_contact(self):
        parent = self.parent(1)
        key = f"{parent}:0"
        task = build_tasks(self.db)[0]
        finish_reviews(self.db, [self.decision(parent, question=False)])
        before = self.db.query("SELECT * FROM links")
        settle_machine_identities(self.db, [MachineIdentitySettlement(
            key, "stale", None, "review", None, None, "Stale verdict", "needs_review")])
        upsert_retargets(self.db, [RetargetProposal(key,
            "https://www.linkedin.com/in/casey-bravo/", judge_fingerprint="stale-research")])
        write_overrides(self.db, [replace(task, action="verify", judgment_fingerprint="stale-judge",
            verdict=IdentityVerdict.from_payload({"verdict": "confirmed", "confidence": .99}))])
        self.assertEqual(self.db.query("SELECT * FROM links"), before)

    def test_relationship_checkpoint_preserves_unjudged_identity_and_does_not_suppress_queue(self):
        parent = self.parent(1)
        before = links(self.db, parent_id=parent)[0]
        decision = self.decision(parent)
        cache_relationship_judgment(self.db, decision)
        after = links(self.db, parent_id=parent)[0]
        for field in fields(before):
            if field.name not in {"judgment_payload_json", "updated_at"}:
                self.assertEqual(getattr(after, field.name), getattr(before, field.name), field.name)
        payload = json.loads(after.judgment_payload_json)
        self.assertEqual(payload["relationship_judgment"]["fingerprint"], decision.fingerprint)
        self.assertNotIn("relationship_decision", payload)
        self.assertNotIn("verdict", payload)
        self.assertEqual(len(build_tasks(self.db)), 1)
        self.assertEqual(load_tasks_from_store(self.db), [])

    def test_identity_verdict_keeps_checkpoint_and_global_finish_replaces_it(self):
        parent = self.parent(1)
        decision = self.decision(parent)
        task = build_tasks(self.db)[0]
        cache_relationship_judgment(self.db, decision)
        write_overrides(self.db, [replace(task, action="review", judgment_fingerprint="identity-judge",
            verdict=IdentityVerdict.from_payload({"verdict": "needs_review", "confidence": .5}))])
        row = links(self.db, parent_id=parent)[0]
        self.assertEqual(json.loads(row.judgment_payload_json)["relationship_judgment"]["fingerprint"], decision.fingerprint)
        self.assertEqual(len(load_tasks_from_store(self.db)), 1)
        finish_reviews(self.db, [decision])
        payload = json.loads(links(self.db, parent_id=parent)[0].judgment_payload_json)
        self.assertNotIn("relationship_judgment", payload)
        self.assertIn("relationship_decision", payload)
        self.assertEqual(build_tasks(self.db), [])

    def test_checkpoint_update_preserves_selected_and_terminal_conclusions(self):
        parent = self.parent(1)
        decision = self.decision(parent)
        finish_reviews(self.db, [decision])
        before = links(self.db, parent_id=parent)[0]
        cache_relationship_judgment(self.db, replace(decision, fingerprint="changed-context"))
        after = links(self.db, parent_id=parent)[0]
        self.assertEqual(after.machine_reason, before.machine_reason)
        self.assertEqual(after.judgment_fingerprint, before.judgment_fingerprint)
        self.assertEqual(pending_parent_ids(self.db), {parent})
        finish_reviews(self.db, [replace(decision, useful_answerable_question=False, human_question="")])
        before = links(self.db, parent_id=parent)[0]
        cache_relationship_judgment(self.db, decision)
        self.assertEqual(links(self.db, parent_id=parent)[0], before)
        self.assertEqual(pending_parent_ids(self.db), set())


if __name__ == "__main__":
    unittest.main()
