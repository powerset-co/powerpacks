from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.search.primitives.deep_search import simple_deep_search as search_v2


def _plan() -> dict:
    return {
        "job_id": "jd-1", "job_title": "Search Engineer",
        "normalized_archetype": "software engineer",
        "source_url": "https://example.test/job", "set_scope": {"set_id": "set-1"},
        "search_scope": {"location": "San Francisco Bay Area", "filters": {}},
        "filters": [], "retrieval_filters": {},
        "traits": {"must_have": [{"trait": "search systems", "tier": "core"}]},
    }


def _payload() -> dict:
    return {
        "intent_type": "role", "source_type": "query", "normalized_query": "software engineer",
        "vertical": "people", "role_search_filters": {
            "semantic_query": "Software engineers who have built production search systems at scale.",
            "role_ids": [1], "bm25_queries": ["software engineer"],
            "seniority_bands": ["senior"],
        },
        "traits": [
            {"value": "Software engineer", "temporal": "current", "meaning": "role"},
            {"value": "search systems", "temporal": "all", "meaning": "experience"},
        ],
        "has_domain_intent": True,
    }


def _start(directory: Path) -> Path:
    jd = directory / "source-jd.txt"
    jd.write_text("Synthetic complete job description", encoding="utf-8")
    plan = directory / "epoch0" / "plan.json"
    plan.parent.mkdir()
    plan.write_text(json.dumps(_plan()), encoding="utf-8")
    queries = directory / "queries.json"
    queries.write_text(json.dumps([
        {"key": "literal_search", "query": "Software engineer with search systems experience in San Francisco Bay Area"},
        {"key": "adjacent_search", "query": "Infrastructure engineer with retrieval systems experience in San Francisco Bay Area"},
    ]), encoding="utf-8")
    (directory / "decision.json").write_text(json.dumps({
        "surface": "people", "backend": "powerset", "depth": "deep",
    }), encoding="utf-8")
    return search_v2.initialize_run(run_dir=directory, jd_path=jd,
                                    plan_path=plan, queries_path=queries)


class SearchV2Tests(unittest.TestCase):
    def test_approved_deep_loop_initializes_without_searching(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            jd = run_dir / "jd.txt"
            jd.write_text("Synthetic complete job description", encoding="utf-8")
            plan = run_dir / "epoch0" / "plan.json"
            plan.parent.mkdir()
            plan.write_text(json.dumps(_plan()), encoding="utf-8")
            queries = run_dir / "queries.json"
            queries.write_text(json.dumps([{
                "key": "literal_search", "query": "Software engineer in San Francisco Bay Area",
            }]), encoding="utf-8")
            args = SimpleNamespace(
                approved_plan=None, queries_file=None, plan_approved=True,
                jd_file=str(jd), jd_url=None, backend="powerset", set_id="set-1",
                db="unused.duckdb",
            )

            result = search_v2.run_simple_mode(
                args, run_dir, run_dir / "decision.json",
                validate_plan=lambda path, **_kwargs: _plan(),
                resolve_identity=lambda *_args: ({"backend": "powerset", "set_id": "set-1"},
                                                 "set-1", "unused.duckdb"),
                bind_plan=lambda _run, path, _identity, _jd, **_kwargs: (path, "digest"),
            )

        self.assertEqual(result["status"], "ready_to_compile")
        self.assertTrue(Path(result["results"]).name == "results.json")

    def test_fixed_artifacts_match_lab_v3_schema(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            results_path = _start(run_dir)
            results = json.loads(results_path.read_text())
            manifest = json.loads((run_dir / "manifest.json").read_text())

        self.assertEqual(results["schema_version"], "lab.search-v2.v3")
        self.assertEqual(results["status"], "ready_to_compile")
        self.assertEqual(results["pending_query"], results["frozen_initial_queries"][0])
        self.assertEqual(manifest, {
            "cost_usd": 0.0, "gt_recall": None, "jd_id": "jd-1", "ponds_run": 0,
            "results": str(run_dir / "results.json"),
            "schema_version": "lab.search-v2.manifest.v3", "status": "ready_to_compile",
        })

    def test_query_review_accepts_one_or_two_clean_population_queries(self) -> None:
        one = [{"key": "literal_search", "query": " Software engineer in Europe "}]
        self.assertEqual(search_v2.validate_query_arms(one)[0]["query"], "Software engineer in Europe")
        with self.assertRaisesRegex(ValueError, "1 or 2"):
            search_v2.validate_query_arms([])
        with self.assertRaisesRegex(ValueError, "only key and query"):
            search_v2.validate_query_arms([{"key": "q", "query": "x", "filters": {}}])

    def test_pattern_defaults_are_logged_and_reviewable(self) -> None:
        payload = _payload()
        payload["role_search_filters"].update({
            "fields_of_study": ["Computer Science"],
            "seniority_bands": ["junior", "manager"],
        })
        edited, changes = search_v2._pattern_defaults(payload, _plan())

        self.assertNotIn("fields_of_study", edited["role_search_filters"])
        self.assertEqual(edited["role_search_filters"]["seniority_bands"],
                         ["mid", "senior", "staff", "principal"])
        self.assertEqual({row["pattern"] for row in changes},
                         {"drop_duplicate_hard_filter", "retune_seniority"})

    def test_payload_review_supports_current_past_and_rerank_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            _start(run_dir)
            payload_path = run_dir / "ponds" / "pond-01" / "payload.json"
            payload = _payload()
            payload["role_search_filters"]["is_current_role"] = False
            payload_path.parent.mkdir(parents=True)
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            results = json.loads((run_dir / "results.json").read_text())
            results["status"] = "awaiting_payload_review"
            results["pending_payload"] = {
                "pond_n": 1, "query": "query", "payload_json": str(payload_path),
                "ledger": "ledger", "payload": payload, "rerank_exclusions": [],
                "rerank_only": False, "pattern_default_edits": [],
            }
            (run_dir / "results.json").write_text(json.dumps(results), encoding="utf-8")
            search_v2.review_payload(run_dir=run_dir,
                                     rerank_exclusions=["chip design", "mechanical design"])
            reviewed = json.loads((run_dir / "results.json").read_text())

        self.assertEqual(reviewed["status"], "ready_to_run")
        self.assertFalse(reviewed["pending_payload"]["payload"]["role_search_filters"]["is_current_role"])
        self.assertEqual(reviewed["pending_payload"]["rerank_exclusions"],
                         ["chip design", "mechanical design"])

    def test_run_records_edit_and_result_deltas_without_quality_labels(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            _start(run_dir)
            payload_path = run_dir / "ponds" / "pond-01" / "payload.json"
            payload_path.parent.mkdir(parents=True)
            payload_path.write_text(json.dumps(_payload()), encoding="utf-8")
            rows_path = run_dir / "rows.jsonl"
            rows_path.write_text("".join(json.dumps(row) + "\n" for row in [
                {"person_id": "p1", "name": "Jordan Bravo", "final_score": .91,
                 "current_titles": "Senior Software Engineer", "current_companies": "Alpha"},
                {"person_id": "p2", "name": "Casey Delta", "final_score": .74,
                 "current_titles": "Software Engineer", "current_companies": "Beta"},
            ]), encoding="utf-8")
            results = json.loads((run_dir / "results.json").read_text())
            results["status"] = "ready_to_run"
            results["pending_payload"] = {
                "pond_n": 1, "query": results["pending_query"]["query"],
                "payload_json": str(payload_path), "ledger": "ledger", "payload": _payload(),
                "rerank_exclusions": [], "rerank_only": False,
                "pattern_default_edits": [{"pattern": "retune_seniority"}],
            }
            (run_dir / "results.json").write_text(json.dumps(results), encoding="utf-8")
            with mock.patch.object(search_v2, "_run_command", return_value={
                "artifacts": {"jsonl": str(rows_path)},
            }):
                search_v2.run_pond(run_dir=run_dir, env_file=".env")
            saved = json.loads((run_dir / "results.json").read_text())
            iteration = saved["iterations"][0]

        self.assertEqual(saved["status"], "awaiting_diagnosis")
        self.assertEqual(iteration["pool_stats"]["score_histogram"], {
            "0.9+": 1, "0.8-0.9": 0, "0.7-0.8": 1, "0.6-0.7": 0, "below 0.6": 0,
        })
        self.assertIsNone(iteration["gt_recall"])
        self.assertNotIn("strong_people", saved)
        self.assertNotIn("pool_read", iteration)
        self.assertTrue(iteration["edit_delta"]["traits_added"])

    def test_paid_next_move_is_checkpointed_before_becoming_the_next_query(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            _start(run_dir)
            results = json.loads((run_dir / "results.json").read_text())
            results["status"] = "awaiting_diagnosis"
            results["iterations"] = [{
                "pond_n": 1, "query": results["pending_query"]["query"],
                "pool_stats": {"suggested_diagnosis": "wrong_location", "result_count": 50,
                               "reviewed_count": 50, "score_histogram": {}, "level_mix": {},
                               "geo_mix": {}, "top_companies": {}},
                "shortlist_grades": [], "input": {}, "arm": {}, "cost_usd": 0,
                "diagnosis": None, "human_override": None, "next_move": None,
            }]
            (run_dir / "results.json").write_text(json.dumps(results), encoding="utf-8")
            usage = SimpleNamespace(prompt_tokens=20, completion_tokens=10,
                                    prompt_tokens_details=SimpleNamespace(cached_tokens=5),
                                    completion_tokens_details=SimpleNamespace(reasoning_tokens=2))
            response = SimpleNamespace(
                model="gpt-5.6-luna", service_tier="flex", usage=usage,
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                    "action": "widen_geography", "next_query": "Software engineer in Europe",
                    "rationale": "The reviewed pool was constrained to the wrong geography.",
                })))],
            )
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=mock.Mock(return_value=response))))
            search_v2.decide(run_dir=run_dir, choice=2, diagnosis="wrong_location", client=client)
            saved = json.loads((run_dir / "results.json").read_text())

        self.assertEqual(saved["status"], "ready_to_compile")
        self.assertEqual(saved["pending_query"]["query"], "Software engineer in Europe")
        self.assertEqual(saved["raw_model_responses"][0]["raw"], response.choices[0].message.content)
        self.assertEqual(saved["raw_model_responses"][0]["usage"]["cached_tokens"], 5)

    def test_protocol_caps_retrieval_and_ponds(self) -> None:
        self.assertEqual(search_v2.RETRIEVAL_LIMIT, 1000)
        self.assertEqual(search_v2.MAX_PONDS, 4)


if __name__ == "__main__":
    unittest.main()
