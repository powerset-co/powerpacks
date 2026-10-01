"""Relationship judgments preview spend and resume completed paid outputs."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from packs.ingestion.primitives.deep_context.db.models import ArtifactRow, FactRow, LinkRow, ParentRow, PersonRow, WriterSource
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.relationship import ReviewRelationships
from packs.ingestion.primitives.deep_context.shared.openai_responses import OpenAIResponse, OpenAIUsage


class RelationshipTest(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-key"})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Db(self.root / "context.sqlite")
        self.parent("jordan")
        self.response = OpenAIResponse({"reason": "Specific collaboration.",
            "useful_answerable_question": True, "human_question": "Is Jordan your robotics colleague?",
            "priority": 2}, OpenAIUsage(100, 50))

    def parent(self, parent):
        self.db.project_rows((
            ParentRow(parent, f"worth:{parent}", "Jordan Bravo"), PersonRow(f"candidate:{parent}", parent),
            ArtifactRow(f"facts:{parent}", "facts", parent, f"/facts/{parent}", "fixture", "projected"),
            FactRow(parent, parent, f"facts:{parent}", machine_worth="yes", facts_json=json.dumps({
                "canonical_name": "Jordan Bravo", "relationship_to_owner": "Robotics colleague",
                "network_worth": {"decision": "yes", "reason": "OLD_MACHINE_REASON"}})),
            LinkRow(f"{parent}:proposal", parent, f"{parent}-bravo", "research", candidate_origin=True,
                    linkedin_url=f"https://linkedin.com/in/{parent}-bravo", source=WriterSource.RECONCILE.value),
        ))

    def stage(self, **kwargs):
        return ReviewRelationships(db=self.db, out_dir=self.root / "relationships", **kwargs)

    def test_preview_does_not_call_or_write(self):
        before = self.db.db_path.read_bytes()
        with patch("packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call",
                   new_callable=AsyncMock) as call:
            result = self.stage().run()
        self.assertEqual((result["status"], result["calls"]), ("needs_approval", 1))
        call.assert_not_called()
        self.assertEqual(self.db.db_path.read_bytes(), before)

    def test_paid_decision_resumes_from_sqlite_and_omits_old_machine_labels(self):
        with patch("packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call",
                   new_callable=AsyncMock, return_value=self.response) as call:
            first = self.stage(approve_spend=True).run()
            before = self.db.query("SELECT * FROM links")
            second = self.stage().run()
        self.assertEqual(self.db.query("SELECT * FROM links"), before)
        self.assertEqual(call.call_count, 1)
        self.assertNotIn("OLD_MACHINE_REASON", call.call_args.kwargs["user_prompt"])
        self.assertEqual((first["status"], second["status"], second["reused"]), ("completed", "completed", 1))
        self.assertEqual(len((self.root / "relationships" / "decisions.jsonl").read_text().splitlines()), 1)

    def test_partial_failure_reuses_success_and_limit_does_not_finish_unjudged(self):
        self.parent("casey")
        async def partial(**kwargs):
            if kwargs["context"] == "casey":
                raise RuntimeError("fixture provider failure")
            return self.response
        target = "packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call"
        with patch(target, new_callable=AsyncMock, side_effect=partial):
            first = self.stage(approve_spend=True).run()
        self.assertEqual((first["status"], first["remaining"]), ("failed", 1))
        self.assertTrue(all(row["machine_approved"] is None for row in self.db.query("SELECT machine_approved FROM links")))
        with patch(target, new_callable=AsyncMock, return_value=self.response) as call:
            second = self.stage(approve_spend=True, limit=1).run()
        self.assertEqual((second["status"], second["reused"], call.call_count), ("completed", 1, 1))
        self.assertEqual(call.call_args.kwargs["context"], "casey")

    def test_limit_leaves_all_identity_decisions_pending(self):
        self.parent("casey")
        with patch("packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call",
                   new_callable=AsyncMock, return_value=self.response):
            result = self.stage(approve_spend=True, limit=1).run()
        self.assertEqual((result["status"], result["remaining"]), ("incomplete", 1))
        self.assertTrue(all(row["machine_approved"] is None for row in self.db.query("SELECT machine_approved FROM links")))
