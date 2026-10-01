"""Relationship judgments preview spend and resume completed paid outputs."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from packs.ingestion.primitives.deep_context.db.models import ArtifactRow, FactRow, LinkRow, ParentRow, PersonRow, SyntheticProfileRow, WriterSource
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.relationship import ReviewRelationships, _RelationshipTask
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
        self.response = OpenAIResponse({"candidates": [{"url": "https://www.linkedin.com/in/jordan-bravo",
            "verdict": "review", "reason": "Two plausible histories", "confidence": 0.5}]}, OpenAIUsage(100, 50))

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

    def test_failed_profile_defers_parent_without_recording_a_decision(self):
        self.db.project_rows((ArtifactRow('profile:jordan:proposal', 'profile', 'jordan',
            '/fixture/profile.json', 'fixture', 'projected', candidate_key='jordan:proposal',
            payload_json=json.dumps({'state': 'error', 'status_code': 503,
                'detail': 'fetch failed (503)', 'normalized_profile': {}})),))
        with patch('packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call',
                   new_callable=AsyncMock) as call:
            result = self.stage(approve_spend=True).run()
        self.assertEqual(result['status'], 'completed')
        call.assert_not_called()
        self.assertIsNone(self.db.query('SELECT machine_judgment FROM links')[0][0])

    def test_judge_receives_full_fetched_career_history(self):
        self.db.project_rows((ArtifactRow('profile:jordan:proposal', 'profile', 'jordan',
            '/fixture/profile.json', 'fixture-profile', 'projected', candidate_key='jordan:proposal',
            payload_json=json.dumps({'public_identifier': 'jordan-bravo',
                'linkedin_url': 'https://www.linkedin.com/in/jordan-bravo',
                'normalized_profile': {'success': True, 'full_name': 'Jordan Bravo',
                    'experiences': [{'title': 'Founder', 'company_name': 'Actual Labs',
                        'starts_at': {'year': 2014}, 'ends_at': {'year': 2018}, 'description': 'Built robotics systems.'}],
                    'education': [{'school_name': 'Example University', 'degree': 'BS', 'field': 'Robotics',
                        'starts_at': {'year': 2010}, 'ends_at': {'year': 2014}}]}})),))
        with patch('packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call',
                   new_callable=AsyncMock, return_value=self.response) as call:
            self.stage(approve_spend=True).run()
        candidate = json.loads(call.call_args.kwargs['user_prompt'])['candidates'][0]
        self.assertEqual(candidate['experiences'][0], {'title': 'Founder', 'company_name': 'Actual Labs',
            'starts_at': 2014, 'ends_at': 2018, 'description': 'Built robotics systems.'})
        self.assertEqual(candidate['education'][0]['field'], 'Robotics')
        self.assertEqual(candidate['education'][0]['starts_at'], 2010)

    def test_retarget_does_not_borrow_previous_profile_history(self):
        self.db.project_rows((ArtifactRow('profile:jordan:proposal', 'profile', 'jordan',
            '/fixture/profile.json', 'fixture-profile', 'projected', candidate_key='jordan:proposal',
            payload_json=json.dumps({'public_identifier': 'old-profile',
                'linkedin_url': 'https://www.linkedin.com/in/old-profile',
                'normalized_profile': {'success': True, 'full_name': 'Wrong Person',
                    'experiences': [{'title': 'Founder', 'company_name': 'Wrong Labs'}]}})),))
        with patch('packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call',
                   new_callable=AsyncMock, return_value=self.response) as call:
            self.stage(approve_spend=True).run()
        candidate = json.loads(call.call_args.kwargs['user_prompt'])['candidates'][0]
        self.assertEqual(candidate['name'], '')
        self.assertEqual(candidate['experiences'], [])
        self.assertEqual(candidate['url'], 'https://www.linkedin.com/in/jordan-bravo')

    def test_synthetic_research_does_not_override_real_profile_for_same_url(self):
        self.db.project_rows((
            LinkRow('synthetic:zz', 'jordan', 'synthetic:zz', 'synthetic', source=WriterSource.RECONCILE.value),
            SyntheticProfileRow('synthetic:zz', 'synthetic:zz', json.dumps({'type': 'json', 'content': {
                'real_name': 'Invented Research Person', 'summary': 'Invented career',
                'linkedin_url': 'https://www.linkedin.com/in/jordan-bravo'}})),
        ))
        with patch('packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call',
                   new_callable=AsyncMock, return_value=self.response) as call:
            self.stage(approve_spend=True).run()
        candidates = json.loads(call.call_args.kwargs['user_prompt'])['candidates']
        self.assertEqual(len(candidates), 1)
        self.assertNotIn('Invented', json.dumps(candidates))

    def test_preview_does_not_call_or_write(self):
        before = self.db.db_path.read_bytes()
        with patch("packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call",
                   new_callable=AsyncMock) as call:
            result = self.stage().run()
        self.assertEqual((result["status"], result["calls"]), ("needs_approval", 1))
        call.assert_not_called()
        self.assertEqual(self.db.db_path.read_bytes(), before)

    def test_paid_decision_resumes_from_sqlite_and_includes_worth_evidence(self):
        with patch("packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call",
                   new_callable=AsyncMock, return_value=self.response) as call:
            first = self.stage(approve_spend=True).run()
            before = self.db.query("SELECT * FROM links")
            second = self.stage().run()
        self.assertEqual(self.db.query("SELECT * FROM links"), before)
        self.assertEqual(call.call_count, 1)
        self.assertIn("OLD_MACHINE_REASON", call.call_args.kwargs["user_prompt"])
        self.assertEqual((first["status"], second["status"], second["reused"]), ("completed", "completed", 1))
        self.assertEqual(len((self.root / "relationships" / "decisions.jsonl").read_text().splitlines()), 1)

    def test_saved_question_skips_judging_after_prompt_or_model_changes(self):
        target = "packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call"
        with patch(target, new_callable=AsyncMock, return_value=self.response):
            self.stage(approve_spend=True).run()
        before = self.db.query("SELECT * FROM links")
        with patch(target, new_callable=AsyncMock) as call, patch(
            "packs.ingestion.primitives.deep_context.enrich.identity_reconcile.relationship.SYSTEM_PROMPT",
            "A revised prompt",
        ):
            result = self.stage(model="gpt-6-sol").run()
        call.assert_not_called()
        self.assertEqual((result["status"], result["reused"]), ("completed", 1))
        self.assertEqual(self.db.query("SELECT * FROM links"), before)

    def test_question_prompt_includes_the_candidate_identity_uncertainty(self):
        with self.db.transaction() as conn:
            conn.execute("UPDATE links SET machine_judgment='needs_review', "
                "machine_reason='Two different Jordan profiles.' WHERE parent_id='jordan'")
        with patch("packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call",
            new_callable=AsyncMock, return_value=self.response) as call:
            self.stage(approve_spend=True).run()
        candidates = json.loads(call.call_args.kwargs["user_prompt"])["candidates"]
        self.assertEqual(candidates[0]["url"], "https://www.linkedin.com/in/jordan-bravo")
        self.assertEqual(candidates[0]["identity_reason"], "Two different Jordan profiles.")

    def test_partial_failure_reuses_success_and_limit_does_not_finish_unjudged(self):
        self.parent("casey")
        async def partial(**kwargs):
            if kwargs["context"] == "casey" and not self.db.query("SELECT judgment_payload_json FROM links WHERE parent_id='jordan'")[0][0]:
                raise RuntimeError("fixture provider failure")
            return OpenAIResponse({"candidates": [{"url": f"https://www.linkedin.com/in/{kwargs["context"]}-bravo",
                "verdict": "review", "reason": "Two plausible histories", "confidence": .5}]}, OpenAIUsage(100, 50))
        target = "packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call"
        with patch(target, new_callable=AsyncMock, side_effect=partial):
            first = self.stage(approve_spend=True).run()
        self.assertEqual((first["status"], first["remaining"]), ("failed", 1))
        self.assertIsNone(self.db.query("SELECT machine_judgment FROM links WHERE parent_id='casey'")[0][0])
        self.assertEqual(self.db.query("SELECT machine_judgment FROM links WHERE parent_id='jordan'")[0][0], "needs_review")
        self.assertEqual(first["errors"], [{"parent_id": "casey", "error": "fixture provider failure"}])
        with patch(target, new_callable=AsyncMock, side_effect=partial) as call:
            second = self.stage(approve_spend=True, limit=1).run()
        self.assertEqual((second["status"], second["reused"], call.call_count), ("completed", 1, 1))
        self.assertEqual(call.call_args.kwargs["context"], "casey")

    def test_success_is_cached_before_other_request_finishes(self):
        self.parent("casey")
        stage = self.stage(approve_spend=True)
        stage.out_dir.mkdir()
        async def check():
            release = asyncio.Event()
            responded = asyncio.Event()
            async def call(**kwargs):
                if kwargs["context"] == "casey":
                    await release.wait()
                else:
                    responded.set()
                return OpenAIResponse({"candidates": [{"url": f"https://www.linkedin.com/in/{kwargs['context']}-bravo",
                    "verdict": "review", "reason": "Unresolved", "confidence": .5}]}, OpenAIUsage(100, 50))
            tasks = [_RelationshipTask(parent, json.dumps({"candidates": [
                {"url": f"https://www.linkedin.com/in/{parent}-bravo"}]}), "fixture") for parent in ("casey", "jordan")]
            with patch("packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call",
                       new_callable=AsyncMock, side_effect=call):
                judging = asyncio.create_task(stage._judge(tasks))
                await asyncio.wait_for(responded.wait(), timeout=1)
                cached = self.db.query("SELECT judgment_payload_json FROM links WHERE parent_id='jordan'")[0][0]
                release.set()
                completed, errors = await judging
            self.assertTrue(cached)
            self.assertEqual((len(completed), errors), (2, []))
        asyncio.run(check())

    def test_persistence_failure_is_not_a_parent_judgment_failure(self):
        target = "packs.ingestion.primitives.deep_context.enrich.identity_reconcile.relationship.cache_relationship_judgment"
        with patch("packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call",
                   new_callable=AsyncMock, return_value=self.response), patch(target, side_effect=RuntimeError("database write failed")):
            result = self.stage(approve_spend=True).run()
        self.assertEqual((result["status"], result["error"]), ("failed", "database write failed"))
        self.assertNotIn("errors", result)
        self.assertIsNone(self.db.query("SELECT machine_judgment FROM links")[0][0])

    def test_limit_settles_judged_identity_decisions(self):
        self.parent("casey")
        with patch("packs.ingestion.primitives.deep_context.shared.openai_responses.OpenAIResponsesCaller.call",
                   new_callable=AsyncMock, return_value=OpenAIResponse({"candidates": [{"url": "https://www.linkedin.com/in/casey-bravo", "verdict": "review", "reason": "Unresolved", "confidence": .5}]}, OpenAIUsage(100, 50))):
            result = self.stage(approve_spend=True, limit=1).run()
        self.assertEqual((result["status"], result["remaining"]), ("incomplete", 1))
        self.assertEqual(self.db.query("SELECT machine_judgment FROM links WHERE parent_id='casey'")[0][0], "needs_review")
        self.assertIsNone(self.db.query("SELECT machine_judgment FROM links WHERE parent_id='jordan'")[0][0])
