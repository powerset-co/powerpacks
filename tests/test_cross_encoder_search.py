"""CE beta runs alongside reranking, never owns filtering or final order."""
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import httpx

from packs.search.primitives.llm_rerank_candidates import llm_rerank_candidates as reranker
from packs.search.primitives.persist_search_results import results_io
from packs.search.primitives.search_network_pipeline import search_network_pipeline as pipeline
from packs.search.primitives.deep_search import search_harness
from tests.test_llm_rerank_candidates import _MockServer
from tests.test_cross_encoder_client import response_for
from tests.test_search_harness import _start, _payload


class CrossEncoderSearchTests(unittest.TestCase):
    def test_real_rerank_cli_uses_filtered_full_profiles_and_exports_both_scores(self):
        captured = []

        def respond(request):
            body = json.loads(request.content)
            captured.append(body)
            response = response_for(body)
            response["scores"][0]["score"] = -2.5
            return httpx.Response(200, json=response)

        client = httpx.Client(transport=httpx.MockTransport(respond))
        with tempfile.TemporaryDirectory() as tmp, _MockServer() as server:
            root = Path(tmp)
            profiles = [{"person_id": pid, "name": "Jordan Bravo", "inferred_age": 40,
                         "positions": [{"title": "Engineer", "description": "current work"},
                                       {"title": "Engineer", "description": "earlier distributed storage"}]}
                        for pid in ("keep", "filtered-out")]
            full, compact, state_path, jd = [root / name for name in ("full.jsonl", "compact.jsonl", "state.json", "jd.txt")]
            full.write_text("\n".join(map(json.dumps, profiles)))
            compact.write_text(json.dumps({"person_id": "keep", "positions": []}))
            jd.write_text("Own distributed storage and recovery protocols")
            state_path.write_text(json.dumps({"task_id": "synthetic-ce", "query": "software engineers", "steps": [
                {"id": "expand_search_request", "output": {"traits": [{"value": "Software engineer"}]}},
                {"id": "hydrate_people", "status": "completed", "output": {
                    "profiles_path": str(full), "llm_profiles_path": str(compact), "profile_ids": ["keep", "filtered-out"]}},
                {"id": "llm_filter_candidates", "status": "completed", "output": {
                    "passed_candidate_ids": ["keep"], "passed_count": 1}},
            ]}))
            output = io.StringIO()
            with mock.patch.object(sys, "argv", ["rerank", "--state", str(state_path), "--write-state",
                    "--api-base", server.url, "--api-key", "test-key", "--cross-encoder-beta",
                    "--cross-encoder-jd-file", str(jd)]), \
                    mock.patch.object(httpx, "Client", return_value=client), \
                    mock.patch.dict(os.environ, {"POWERSET_API_KEY": "test-powerset-key"}), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(reranker.main(), 0)
            saved = json.loads(state_path.read_text())
            self.assertEqual(len(saved["steps"]), 4)
            self.assertEqual(saved["steps"][-1]["output"]["cross_encoder"]["usage"]["pairs"], 1)
            rows = results_io.result_rows(saved)
            self.assertEqual([row["person_id"] for row in rows], ["keep"])
            self.assertEqual(float(rows[0]["final_score"]), .4)
            self.assertEqual(rows[0]["cross_encoder_score"], -2.5)
            self.assertEqual(json.loads(output.getvalue())["cross_encoder"]["status"], "ok")
        self.assertEqual([pair["id"] for pair in captured[0]["pairs"]], ["keep"])
        pair = captured[0]["pairs"][0]
        self.assertIn("Own distributed storage", pair["query"])
        self.assertIn("software engineers", pair["query"])
        self.assertEqual(len(json.loads(pair["passage"])["positions"]), 2)
        self.assertNotIn("inferred_age", pair["passage"])

    def test_deep_pond_passes_beta_flag_and_original_jd_to_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results_path = _start(root)
            payload = root / "payload.json"
            payload.write_text(json.dumps(_payload()))
            results = json.loads(results_path.read_text())
            results["status"] = "ready_to_run"
            results["pending_payload"] = {"pond_n": 1, "query": "software engineers",
                "payload_json": str(payload), "ledger": str(root / "pipeline.json"), "limit": 1000}
            results_path.write_text(json.dumps(results))
            with mock.patch.dict(os.environ, {"POWERPACKS_CROSS_ENCODER_BETA": "1"}), \
                    mock.patch.object(search_harness, "_run_command", side_effect=RuntimeError("captured")) as run, \
                    self.assertRaisesRegex(RuntimeError, "captured"):
                search_harness.run_pond(run_dir=root, env_file="/dev/null")
            command = run.call_args.args[0]
            self.assertIn("--cross-encoder-beta", command)
            self.assertEqual(command[command.index("--cross-encoder-jd-file") + 1], str(root / "jd.txt"))

    def test_reranker_and_ce_overlap_on_same_full_profiles(self):
        llm_started, ce_started = threading.Event(), threading.Event()
        profile = {"person_id": "candidate-1", "positions": [
            {"title": "Engineer", "description": "Current work"},
            {"title": "Engineer", "description": "Earlier distributed storage work"},
        ]}
        items = [reranker.RerankItem(position=0, payload=profile)]

        async def llm(received, **kwargs):
            self.assertEqual(received, items)
            llm_started.set()
            self.assertTrue(await asyncio.to_thread(ce_started.wait, 2))
            return ["llm-result"]

        def ce(**kwargs):
            ce_started.set()
            self.assertTrue(llm_started.wait(2))
            self.assertEqual(kwargs["profiles"], {"candidate-1": profile})
            self.assertIn("Job description", kwargs["query"])
            return {"status": "ok", "scores": [{"id": "candidate-1", "score": -2.5}]}

        with mock.patch.object(reranker, "rerank_all", side_effect=llm), \
                mock.patch.object(reranker.cross_encoder, "score_candidates", side_effect=ce):
            results, beta = asyncio.run(reranker._rerank_with_cross_encoder(
                items, cross_encoder_query="Job description: storage engineer",
                cross_encoder_output_dir=Path("unused"), query="engineers", traits=[]))
        self.assertEqual(results, ["llm-result"])
        self.assertEqual(beta["scores"][0]["score"], -2.5)

    def test_disabled_beta_makes_no_ce_calls(self):
        with mock.patch.object(reranker, "rerank_all", new_callable=mock.AsyncMock, return_value=[]), \
                mock.patch.object(reranker.cross_encoder, "score_candidates") as ce:
            results, beta = asyncio.run(reranker._rerank_with_cross_encoder(
                [], cross_encoder_query=None, cross_encoder_output_dir=None))
        self.assertEqual(results, [])
        self.assertIsNone(beta)
        ce.assert_not_called()

    def test_beta_failure_preserves_llm_results(self):
        with mock.patch.object(reranker, "rerank_all", new_callable=mock.AsyncMock, return_value=["kept"]), \
                mock.patch.object(reranker.cross_encoder, "score_candidates", side_effect=RuntimeError("CE HTTP 503")):
            results, beta = asyncio.run(reranker._rerank_with_cross_encoder(
                [], cross_encoder_query="engineers", cross_encoder_output_dir=Path("unused")))
        self.assertEqual(results, ["kept"])
        self.assertEqual(beta, {"status": "failed", "error": "CE HTTP 503"})

    def test_unreadable_beta_jd_does_not_stop_normal_rerank(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(reranker, "rerank_all", new_callable=mock.AsyncMock, return_value=["kept"]), \
                mock.patch.object(reranker.cross_encoder, "score_candidates") as ce:
            results, beta = asyncio.run(reranker._rerank_with_cross_encoder(
                [], cross_encoder_query="engineers", cross_encoder_output_dir=Path(tmp),
                cross_encoder_jd_file=str(Path(tmp) / "missing.txt")))
        self.assertEqual(results, ["kept"])
        self.assertEqual(beta["status"], "failed")
        ce.assert_not_called()

    def test_beta_flags_survive_prepare_and_approval_continuation(self):
        with tempfile.TemporaryDirectory() as tmp:
            jd = Path(tmp) / "jd.txt"
            jd.write_text("Senior storage engineer", encoding="utf-8")
            args = pipeline.build_parser().parse_args([
                "prepare", "--query", "engineers", "--cross-encoder-beta",
                "--cross-encoder-jd-file", str(jd)])
            suffix = pipeline.execution_contract_suffix(args)
            self.assertIn("--cross-encoder-beta", suffix)
            self.assertIn(str(jd), suffix)
            self.assertIn("--cross-encoder-jd-file", suffix)

    def test_beta_status_and_usage_reach_pipeline_summary(self):
        ce = {"status": "ok", "usage": {"pairs": 1, "input_tokens": 220, "output_tokens": 0},
              "requests": 1, "scores": [{"id": "synthetic", "score": 3.0}]}
        summary = pipeline.compact_summary({"ranked_count": 1, "cross_encoder": ce})
        saved = pipeline.pipeline_summary({"steps": {"llm_rerank_candidates": {"summary": summary}}})
        self.assertEqual(saved["cross_encoder"]["usage"], ce["usage"])
        self.assertNotIn("scores", saved["cross_encoder"])

    def test_ce_scores_export_without_changing_default_order_or_score(self):
        state = {"steps": [
            {"id": "hydrate_people", "output": {"profiles": [
                {"person_id": "first", "name": "Jordan Bravo"},
                {"person_id": "second", "name": "Casey Example"}]}},
            {"id": "llm_filter_candidates", "output": {"passed_candidate_ids": ["first", "second"]}},
            {"id": "llm_rerank_candidates", "output": {
                "ranked_candidate_ids": ["second", "first"],
                "cross_encoder": {"status": "ok", "model": "test-qwen", "scores": [
                    {"id": "first", "score": 8.5}, {"id": "second", "score": -1.0}]}}},
        ]}
        baseline = json.loads(json.dumps(state))
        del baseline["steps"][-1]["output"]["cross_encoder"]
        old_rows, new_rows = results_io.result_rows(baseline), results_io.result_rows(state)
        self.assertEqual([row["person_id"] for row in new_rows], ["second", "first"])
        self.assertEqual([row["cross_encoder_score"] for row in new_rows], [-1.0, 8.5])
        self.assertEqual(old_rows, [{k: v for k, v in row.items() if not k.startswith("cross_encoder_")}
                                    for row in new_rows])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.csv"
            results_io.write_csv(path, new_rows)
            self.assertIn("cross_encoder_score", path.read_text().splitlines()[0])
            results_io.write_csv(path, old_rows)
            self.assertNotIn("cross_encoder_score", path.read_text().splitlines()[0])


if __name__ == "__main__":
    unittest.main()
