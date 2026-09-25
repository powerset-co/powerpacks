"""Jev decisions keep their native scale through the shipped JD pipeline."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.search.primitives.deep_search import search_harness as harness
from packs.search.primitives.llm_rerank_candidates import llm_rerank_candidates as reranker
from packs.search.primitives.persist_search_results import results_io
from packs.search.primitives.search_network_pipeline import search_network_pipeline as pipeline


class JevPipelineTests(unittest.TestCase):
    def test_jev_requires_jd_before_any_pipeline_work(self):
        for command in ("prepare", "run"):
            args = pipeline.build_parser().parse_args([command, "--query", "engineers", "--capability-judge", "jev"])
            with self.assertRaisesRegex(pipeline.Failed, "requires --jd-file"):
                pipeline._validate_capability_input(args)

    def test_jd_runs_default_to_jev_and_trait_runs_have_no_judge(self):
        with tempfile.TemporaryDirectory() as directory:
            jd = Path(directory) / "jd.txt"
            jd.write_text("Synthetic JD")
            for command in ("prepare", "run"):
                with_jd = pipeline.build_parser().parse_args([command, "--query", "engineers", "--jd-file", str(jd)])
                pipeline._validate_capability_input(with_jd)
                self.assertEqual(with_jd.capability_judge, "jev")
                self.assertEqual(pipeline.cross_encoder_child_args(with_jd)[-2:], ["--capability-judge", "jev"])
                explicit = pipeline.build_parser().parse_args(
                    [command, "--query", "engineers", "--jd-file", str(jd), "--capability-judge", "terra"])
                pipeline._validate_capability_input(explicit)
                self.assertEqual(explicit.capability_judge, "terra")
                without_jd = pipeline.build_parser().parse_args([command, "--query", "engineers"])
                pipeline._validate_capability_input(without_jd)
                self.assertIsNone(without_jd.capability_judge)

    def test_switching_judge_invalidates_completed_rerank(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            jd = Path(directory) / "jd.txt"
            jd.write_text("Build storage systems.")
            request_hash = pipeline.capability_contract.request_sha256(
                jd=jd.read_text(), title="", company_name="", evaluation_query="", judge="terra")
            state.write_text(json.dumps({"steps": [{"id": "llm_rerank_candidates", "output": {
                "model": "gpt-5.6-terra", "capability_request_sha256": request_hash}}]}))
            args = pipeline.build_parser().parse_args(["run", "--jd-file", str(jd), "--capability-judge", "jev"])
            self.assertTrue(pipeline._capability_judge_changed(args, state))
            args.capability_judge = "terra"
            self.assertFalse(pipeline._capability_judge_changed(args, state))
            jd.write_text("Own product strategy.")
            self.assertTrue(pipeline._capability_judge_changed(args, state))
            args.capability_judge = "jev"
            args.search_only = True
            self.assertFalse(pipeline._capability_judge_changed(args, state))

    def test_native_scores_drive_export_and_downstream_judgment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles_path, state_path, jd_path = [root / name for name in ("profiles.jsonl", "state.json", "jd.txt")]
            profiles = [{"person_id": pid, "positions": [{"title": "Engineer", "description": "Built storage"}]}
                        for pid in ("yes", "no")]
            profiles_path.write_text("\n".join(map(json.dumps, profiles)))
            jd_path.write_text("Build storage systems.")
            state_path.write_text(json.dumps({"task_id": "synthetic-jev", "query": "engineers", "steps": [
                {"id": "hydrate_people", "status": "completed", "output": {
                    "profiles_path": str(profiles_path), "profile_ids": ["yes", "no"]}},
                {"id": "llm_filter_candidates", "status": "completed", "output": {
                    "passed_candidate_ids": ["yes", "no"], "passed_count": 2}},
            ]}))
            native = {"status": "ok", "model": "jev-1.13.0", "score_type": "qualification_score",
                      "threshold": 0.29855554570561965,
                      "scores": [{"id": "yes", "score": .31, "passed": True, "evidence": "Relevant work"},
                                 {"id": "no", "score": .28, "passed": False, "evidence": "Insufficient support"}]}
            with mock.patch.object(sys, "argv", ["rerank", "--state", str(state_path), "--write-state",
                    "--jd-file", str(jd_path), "--capability-judge", "jev"]), \
                    mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "synthetic-typesafe-key", "OPENAI_API_KEY": ""}), \
                    mock.patch.object(reranker.jd_cleaner, "clean_job_description",
                                      return_value="Responsibilities\n- Build storage systems.") as cleaner, \
                    mock.patch.object(reranker.jev, "score_candidates", new_callable=mock.AsyncMock,
                                      return_value=native) as score, \
                    mock.patch.object(reranker.terra, "score_candidates") as terra, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(reranker.main(), 0)
            terra.assert_not_called()
            cleaner.assert_called_once()
            self.assertEqual(cleaner.call_args.kwargs["jd"], jd_path.read_text())
            self.assertEqual(score.call_args.kwargs["jd"], "Responsibilities\n- Build storage systems.")
            self.assertEqual(score.call_args.kwargs["api_key"], "synthetic-typesafe-key")
            self.assertEqual(score.call_args.kwargs["concurrency"], 4)
            saved_state = json.loads(state_path.read_text())
            output = saved_state["steps"][-1]["output"]
            spec = json.loads(Path(output["artifacts"]["system_prompt"]).read_text())
            self.assertIn("jev", json.dumps(spec).lower())
            self.assertTrue(output["capability_request_sha256"])
            rows = results_io.result_rows(json.loads(state_path.read_text()))
            self.assertEqual([r["cross_encoder_score"] for r in rows], [.31, .28])
            self.assertEqual([r["cross_encoder_score_1_to_5"] for r in rows], [None, None])
            self.assertEqual([harness._candidate_judgment_eligible(r) for r in rows], [True, False])
            self.assertEqual([float(r["final_score"]) for r in rows], [.31, .28])

    def test_prepare_and_spend_contract_select_jev(self):
        with tempfile.TemporaryDirectory() as directory:
            jd = Path(directory) / "jd.txt"
            jd.write_text("Synthetic JD")
            for command in ("prepare", "run"):
                args = pipeline.build_parser().parse_args([command, "--query", "engineers",
                    "--jd-file", str(jd), "--capability-judge", "jev"])
                child = pipeline.cross_encoder_child_args(args)
                self.assertEqual(child[-2:], ["--capability-judge", "jev"])
                if command == "run":
                    approval = pipeline._llm_approval_payload(args, Path("unused"))
                    self.assertEqual(approval["model"], "jev-1.13.0")
                    self.assertEqual(approval["reasoning_effort"], "none")
                    self.assertEqual(approval["capability_judge"], "jev")

    def test_jev_failure_is_not_converted_to_rejection(self):
        import asyncio
        with mock.patch.object(reranker.jd_cleaner, "clean_job_description", return_value="Clean JD"), \
                mock.patch.object(reranker.jev, "score_candidates", side_effect=RuntimeError("unavailable")), \
                self.assertRaisesRegex(RuntimeError, "unavailable"):
            asyncio.run(reranker._rerank_with_jev(
                [reranker.RerankItem(position=0, payload={"person_id": "synthetic"})],
                jd="JD", title="Engineer", company_name="Example", evaluation_query="",
                as_of="2026-09-19", output_dir=Path("unused"), cleaner_output_dir=Path("unused"),
                cleaner_api_key="synthetic-openai", api_key="test", concurrency=1))

    def test_missing_binary_decision_never_falls_back_to_sigmoid(self):
        self.assertFalse(harness._candidate_judgment_eligible({
            "cross_encoder_status": "ok", "cross_encoder_score_type": "qualification_score",
            "cross_encoder_score": .9}))
