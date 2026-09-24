"""Synthetic, network-free checks for the packaged Sol + four-Jev scorer."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from packs.search.primitives.deep_search.shortlist_priority import ReviewCase, ShortlistPriority
from packs.search.primitives.llm_rerank_candidates.jev import client as jev
from packs.search.primitives.shared import openai_client


def _case(person="synthetic-1"):
    return ReviewCase(person_id=person, state={
        "job_description": "Build a reliable distributed database.", "reference_date": "2026-09-23",
        "profile": {"positions": [{"company": "Example Systems", "title": "Engineer",
                                  "start_date": "2020-01", "description": "Built database replication."}]},
        "pinned": True, "human_score": 5})


def _signals(request):
    return {"model": jev.MODEL, "usage": {"input_tokens": 100, "output_tokens": 100},
            "answers": {name: ({"type": "noul", "noul": .8} if q["type"] == "noul" else {
                "type": "choice", "probabilities": {k: float(k == "direct") for k in q["criteria"]},
                "choice": "direct", "confidence": 1.0}) for name, q in request["questions"].items()}}


_RESULT = {"decision": "introduce", "priority": 85, "reason": "Built relevant replication systems.",
           "inference": "none", "unresolved": [], "evidence": [{"path": "profile.positions[0]", "fact": "Built replication."}]}


class ShortlistPriorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)

    def runner(self, *, cases=None, approved=False, budget=1):
        return ShortlistPriority(cases=cases or [_case()], output_dir=self.output,
                                 max_cost_usd=budget, approve_spend=approved)

    def providers(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.chat.completions.create.return_value = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=100),
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(_RESULT)))],
            model_dump=lambda **kwargs: {"model": "gpt-6-sol", "fixture": True})
        async def evaluate(**kwargs):
            return _signals(kwargs["request"])
        return client, AsyncMock(side_effect=evaluate)

    def test_preview_does_not_write_or_call_providers(self):
        with patch.object(openai_client, "make_async_openai_client") as factory, \
                patch.object(jev, "evaluate_once") as evaluate:
            output = self.runner().run()
        self.assertEqual(output["status"], "needs_approval")
        self.assertEqual(output["pending_calls"], 2)
        self.assertGreater(output["estimated_cost_usd"], 0)
        self.assertFalse(list(self.output.iterdir()))
        factory.assert_not_called()
        evaluate.assert_not_called()

    def test_four_questions_and_no_human_labels_or_dense_evidence(self):
        runner, case = self.runner(), _case()
        request = runner._jev_request(case)
        self.assertEqual(set(request["questions"]), {
            "scope_match", "role_company_corroboration", "function_evidence", "mechanism_depth"})
        self.assertNotIn("pinned", request["state"])
        self.assertNotIn("human_score", request["state"])
        signals = _signals(request)
        sol = runner._sol_request(case, signals)
        self.assertEqual(sol["model"], "gpt-6-sol")
        self.assertEqual(sol["reasoning_effort"], "high")
        self.assertFalse(sol["store"])
        self.assertEqual(json.loads(sol["messages"][1]["content"])["job_description"], case.state["job_description"])
        self.assertEqual(json.loads(sol["messages"][-1]["content"])["auxiliary_assessment"]["answers"], signals["answers"])

    def test_approved_smoke_reuses_both_calls_without_keys_or_spend(self):
        client, evaluate = self.providers()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "synthetic", "TYPESAFE_API_KEY": "synthetic"}), \
                patch.object(openai_client, "make_async_openai_client", return_value=client), \
                patch.object(jev, "evaluate_once", evaluate):
            first = self.runner(approved=True).run()
        with patch.dict("os.environ", {}, clear=True), \
                patch.object(openai_client, "make_async_openai_client") as factory:
            second = self.runner().run()
        self.assertEqual(first["scores"], second["scores"])
        self.assertEqual(second["pending_calls"], 0)
        self.assertAlmostEqual(first["cost_usd_upper"], second["cost_usd_upper"])
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual(evaluate.call_count, 1)
        factory.assert_not_called()
        self.assertEqual(len(list((self.output / "responses").glob("*.json"))), 2)

    def test_budget_stops_before_calls(self):
        with patch.object(jev, "evaluate_once") as evaluate:
            with self.assertRaisesRegex(ValueError, "budget"):
                self.runner(approved=True, budget=.00001).run()
        evaluate.assert_not_called()

    def test_identical_profiles_share_one_pair_of_paid_calls(self):
        client, evaluate = self.providers()
        async def yielding_evaluate(**kwargs):
            await asyncio.sleep(0)
            return _signals(kwargs["request"])
        evaluate.side_effect = yielding_evaluate
        cases = [_case("first"), _case("second")]
        with patch.dict("os.environ", {"OPENAI_API_KEY": "synthetic", "TYPESAFE_API_KEY": "synthetic"}), \
                patch.object(openai_client, "make_async_openai_client", return_value=client), \
                patch.object(jev, "evaluate_once", evaluate):
            output = self.runner(cases=cases, approved=True).run()
        self.assertEqual(set(output["scores"]), {"first", "second"})
        self.assertEqual(evaluate.call_count, 1)
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_error_is_preserved_and_never_becomes_rejection_or_automatic_retry(self):
        client, evaluate = self.providers()
        client.chat.completions.create.side_effect = RuntimeError("synthetic provider failure")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "synthetic", "TYPESAFE_API_KEY": "synthetic"}), \
                patch.object(openai_client, "make_async_openai_client", return_value=client), \
                patch.object(jev, "evaluate_once", evaluate):
            with self.assertRaisesRegex(RuntimeError, "provider"):
                self.runner(approved=True).run()
            with self.assertRaisesRegex(RuntimeError, "Inspect unfinished"):
                self.runner(approved=True).run()
        self.assertEqual(client.chat.completions.create.call_count, 1)
        records = [json.loads(p.read_text()) for p in (self.output / "responses").glob("*.json")]
        self.assertEqual({r["status"] for r in records}, {"valid", "failed"})

    def test_changed_evidence_changes_request_and_cache_key(self):
        runner, old = self.runner(), _case()
        state = deepcopy(old.state)
        state["profile"]["positions"][0]["description"] = "Different work"
        new = ReviewCase(person_id=old.person_id, state=state)
        self.assertNotEqual(runner._path(runner._jev_request(old)), runner._path(runner._jev_request(new)))

    def test_jev_exact_request_transport_has_no_capability_compaction_or_retry(self):
        request = self.runner()._jev_request(_case())
        http = AsyncMock()
        response = http.post.return_value
        response.raise_for_status = lambda: None
        response.json = lambda: _signals(request)
        with patch.object(jev, "append_usage_row") as log:
            result = asyncio.run(jev.evaluate_once(client=http, request=request, api_key="synthetic"))
        self.assertEqual(result, _signals(request))
        self.assertEqual(http.post.call_count, 1)
        self.assertEqual(http.post.call_args.kwargs["json"], request)
        log.assert_called_once()


if __name__ == "__main__":
    unittest.main()
