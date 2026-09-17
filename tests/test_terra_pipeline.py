"""JD ranking replaces both Luna reranking and Gemma, keeping native integer ratings."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.search.primitives.llm_rerank_candidates import llm_rerank_candidates as reranker
from packs.search.primitives.persist_search_results import results_io
from packs.search.primitives.search_network_pipeline import search_network_pipeline as pipeline
from packs.search.primitives.deep_search import search_harness as harness


class TerraPipelineTests(unittest.TestCase):
    def test_cli_full_profiles_integer_export_and_downstream_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles_path, state_path, jd_path = [root / name for name in ("profiles.jsonl", "state.json", "jd.txt")]
            profiles = [{"person_id": pid, "positions": [
                {"title": "Engineer", "description": "Current original evidence"},
                {"title": "Engineer", "description": "Earlier original evidence"}]} for pid in ("yes", "no", "filtered")]
            profiles_path.write_text("\n".join(map(json.dumps, profiles)))
            jd_path.write_text("Own distributed storage and recovery protocols.")
            state_path.write_text(json.dumps({"task_id": "synthetic-terra", "query": "software engineers", "steps": [
                {"id": "expand_search_request", "output": {"traits": [{"value": "Software engineer"}]}},
                {"id": "hydrate_people", "status": "completed", "output": {
                    "profiles_path": str(profiles_path), "profile_ids": ["yes", "no", "filtered"]}},
                {"id": "llm_filter_candidates", "status": "completed", "output": {
                    "passed_candidate_ids": ["yes", "no"], "passed_count": 2}},
            ]}))
            native = {"status": "ok", "model": reranker.terra.MODEL, "score_type": reranker.terra.SCORE_TYPE,
                "scores": [{"id": "yes", "score": 4, "evidence": "Relevant work", "basis": "direct"},
                           {"id": "no", "score": 2, "evidence": "Different work", "basis": "mismatch"}]}
            with mock.patch.object(sys, "argv", ["rerank", "--state", str(state_path), "--write-state",
                    "--jd-file", str(jd_path), "--job-title", "Storage Engineer", "--job-company", "Example Systems",
                    "--api-key", "synthetic-key", "--cross-encoder-beta",
                    "--evaluation-query", "Engineers; exclude frontend-only experience"]), \
                    mock.patch.object(reranker.terra, "score_candidates", new_callable=mock.AsyncMock, return_value=native) as terra, \
                    mock.patch.object(reranker, "rerank_all") as luna, \
                    mock.patch.object(reranker.cross_encoder, "score_candidates") as gemma, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(reranker.main(), 0)
            luna.assert_not_called()
            gemma.assert_not_called()
            self.assertEqual(terra.call_args.kwargs["profiles"], {p["person_id"]: p for p in profiles[:2]})
            self.assertEqual(terra.call_args.kwargs["jd"], "Job: Storage Engineer at Example Systems\n\n"
                + jd_path.read_text() + "\n\nUser-reviewed criteria:\nEngineers; exclude frontend-only experience")
            rows = results_io.result_rows(json.loads(state_path.read_text()))
            self.assertEqual([r["person_id"] for r in rows], ["yes", "no"])
            self.assertEqual([r["cross_encoder_score_1_to_5"] for r in rows], [4, 2])
            self.assertEqual([harness._candidate_judgment_eligible(r) for r in rows], [True, False])
            self.assertEqual([float(r["final_score"]) for r in rows], [.8, .4])
            self.assertTrue(all(r["cross_encoder_model"] == reranker.terra.MODEL for r in rows))

    def test_prepare_carries_jd_and_omits_gemma_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            jd = Path(directory) / "jd.txt"
            jd.write_text("Synthetic JD")
            for command in ("prepare", "run"):
                args = pipeline.build_parser().parse_args([command, "--query", "Engineers",
                    "--jd-file", str(jd), "--job-title", "Engineer", "--job-company", "Example",
                    "--cross-encoder-beta"])
                self.assertEqual(pipeline.cross_encoder_child_args(args), [
                    "--jd-file", str(jd.resolve()), "--job-title", "Engineer", "--job-company", "Example"])
                if command == "run":
                    approval = pipeline._llm_approval_payload(args, Path("unused"))
                    self.assertEqual(approval["model"], "gpt-5.6-terra")
                    self.assertEqual(approval["reasoning_effort"], "high")
                    self.assertNotIn("cross_encoder_beta", approval)

    def test_api_failure_does_not_write_negative_results(self):
        import asyncio
        with mock.patch.object(reranker.terra, "score_candidates", side_effect=RuntimeError("unavailable")), \
                self.assertRaisesRegex(RuntimeError, "unavailable"):
            asyncio.run(reranker._rerank_with_terra(
                [reranker.RerankItem(position=0, payload={"person_id": "synthetic"})],
                jd="JD", as_of="2026-09-16", output_dir=Path("unused"), api_key="test", concurrency=1))
