from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.search.primitives.deep_search import search_harness


def _plan() -> dict:
    return {
        "job_id": "jd-1", "job_title": "Search Engineer",
        "normalized_archetype": "software engineer",
        "target_level": "staff_ic",
        "source_url": "https://example.test/job", "set_scope": {"set_id": "set-1"},
        "hiring_company": {"name": "Acme", "website_url": "https://acme.example"},
        "candidate_populations": [{
            "population": "software engineer", "hint_kind": "stated-background",
            "evidence_quote": "We are looking for software engineers.",
        }],
        "comp_band": {"currency": "USD", "minimum": 140000, "maximum": 220000,
                      "period": "year", "evidence_quote": "Base salary is 140000 to 220000."},
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
    return search_harness.initialize_run(run_dir=directory, jd_path=jd,
                                    plan_path=plan, queries_path=queries)


class SearchHarnessTests(unittest.TestCase):
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

            result = search_harness.run_search_harness(
                args, run_dir, run_dir / "decision.json",
                validate_plan=lambda path, **_kwargs: _plan(),
                resolve_identity=lambda *_args: ({"backend": "powerset", "set_id": "set-1"},
                                                 "set-1", "unused.duckdb"),
                bind_plan=lambda _run, path, _identity, _jd, **_kwargs: (path, "digest"),
            )

        self.assertEqual(result["status"], "ready_to_compile")
        self.assertTrue(Path(result["results"]).name == "results.json")

    def test_fixed_artifacts_use_search_harness_schema(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            results_path = _start(run_dir)
            results = json.loads(results_path.read_text())
            manifest = json.loads((run_dir / "manifest.json").read_text())

        self.assertEqual(results["schema_version"], "search-harness.v1")
        self.assertEqual(results["status"], "ready_to_compile")
        self.assertEqual(results["pending_query"], results["frozen_initial_queries"][0])
        self.assertEqual(results["candidate_populations"][0]["population"], "software engineer")
        self.assertEqual(results["comp_band"]["maximum"], 220000)
        self.assertEqual(manifest, {
            "cost_usd": 0.0, "gt_recall": None, "jd_id": "jd-1", "ponds_run": 0,
            "rapidapi": {"billing_basis": "unit_price_not_configured", "cache_hits": 0,
                         "cache_misses": 0, "cost_usd": 0.0, "live_lookups": 0,
                         "unit_cost_usd": 0.0, "unresolved": 0},
            "results": str(run_dir / "results.json"),
            "schema_version": "search-harness.manifest.v1", "status": "ready_to_compile",
        })

    def test_evaluation_contract_uses_brief_core_and_demotes_jd_checklist(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            _start(run_dir)
            results = json.loads((run_dir / "results.json").read_text())
            plan = _plan()
            plan["traits"]["must_have"].append({
                "trait": "Specific domain syntax", "tier": "core"})
            plan["traits"]["nice_to_have"] = [{"trait": "Specific framework"}]

            text, path = search_harness._evaluation_contract(results, plan, run_dir)
            traits = json.loads(path.read_text())

        self.assertIn("Target occupation: software engineer", text)
        self.assertEqual(
            [(row["value"], row["meaning"]) for row in traits],
            [("software engineer", "core"), ("search systems", "core"),
             ("Specific domain syntax", "nice-to-have"),
             ("Specific framework", "nice-to-have")],
        )

    def test_query_review_accepts_one_or_two_clean_population_queries(self) -> None:
        one = [{"key": "literal_search", "query": " Software engineer in Europe "}]
        self.assertEqual(search_harness.validate_query_arms(one)[0]["query"], "Software engineer in Europe")
        with self.assertRaisesRegex(ValueError, "1 or 2"):
            search_harness.validate_query_arms([])
        with self.assertRaisesRegex(ValueError, "only key and query"):
            search_harness.validate_query_arms([{"key": "q", "query": "x", "filters": {}}])

    def test_pattern_defaults_are_logged_and_reviewable(self) -> None:
        payload = _payload()
        payload["role_search_filters"].update({
            "fields_of_study": ["Computer Science"],
            "seniority_bands": ["junior", "manager"],
        })
        edited, changes = search_harness._pattern_defaults(payload, _plan())

        self.assertNotIn("fields_of_study", edited["role_search_filters"])
        self.assertEqual(edited["role_search_filters"]["seniority_bands"],
                         ["mid", "senior", "staff", "principal"])
        self.assertEqual({row["pattern"] for row in changes},
                         {"drop_duplicate_hard_filter", "retune_seniority"})

    def test_llm_pattern_defaults_use_terra_and_checkpoint_before_apply(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            _start(run_dir)
            usage = SimpleNamespace(prompt_tokens=20, completion_tokens=8,
                                    prompt_tokens_details=SimpleNamespace(cached_tokens=4),
                                    completion_tokens_details=SimpleNamespace(reasoning_tokens=1))
            response = SimpleNamespace(
                model="gpt-5.6-terra", service_tier="flex", usage=usage,
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                    "edits": [{"pattern": "prune_keyword_fanout", "field": "bm25_queries",
                               "to": ["software engineer"],
                               "reason": "Keep the on-target population phrase."}],
                })))],
            )
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=mock.Mock(return_value=response))))
            results = json.loads((run_dir / "results.json").read_text())
            payload = _payload()
            payload["role_search_filters"]["bm25_queries"] = [
                "software engineer", "backend engineer"]

            edited, changes = search_harness._llm_pattern_defaults(
                payload=payload, plan=_plan(), results=results, run_dir=run_dir,
                pond_n=1, query="Software engineer", client=client)

            call = client.chat.completions.create.call_args.kwargs
            self.assertEqual(call["model"], "gpt-5.6-terra")
            self.assertEqual(call["reasoning_effort"], "medium")
            self.assertEqual(call["service_tier"], "flex")
            self.assertTrue((run_dir / "ponds/pond-01/pattern-defaults.raw.json").is_file())
            self.assertEqual(edited["role_search_filters"]["bm25_queries"], ["software engineer"])
            self.assertEqual(changes[0]["source"], "llm_precedent")

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
            search_harness.review_payload(run_dir=run_dir,
                                     rerank_exclusions=["chip design", "mechanical design"])
            reviewed = json.loads((run_dir / "results.json").read_text())

        self.assertEqual(reviewed["status"], "ready_to_run")
        self.assertFalse(reviewed["pending_payload"]["payload"]["role_search_filters"]["is_current_role"])
        self.assertEqual(reviewed["pending_payload"]["rerank_exclusions"],
                         ["chip design", "mechanical design"])
        self.assertEqual(reviewed["pending_payload"]["human_edit_delta"]["rerank_exclusions"]["to"],
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
            def annotate(**kwargs):
                return [{**dict(row), "level_read": row["title"],
                         "move_plausibility": "promising step-up", "move_why": "Plausible move.",
                         "move_annotation_source": "luna"}
                        for row in kwargs["candidates"]]

            with (mock.patch.object(search_harness, "_run_command", return_value={
                    "artifacts": {"jsonl": str(rows_path)},
                  }), mock.patch.object(search_harness, "_ensure_hiring_company_context"),
                  mock.patch.object(search_harness, "_annotate_company_fit", side_effect=annotate),
                  mock.patch.object(search_harness, "resolve_company_contexts", return_value=(
                    [{"name": "Alpha", "headcount": 40, "stage": "SEED", "funding": 2_000_000},
                     {"name": "Beta", "headcount": 500, "stage": "SERIES_C", "funding": 80_000_000}],
                    {"cache_hits": 2, "cache_misses": 0, "live_lookups": 0, "unresolved": 0,
                     "cost_usd": 0.0, "unit_cost_usd": 0.0,
                     "billing_basis": "unit_price_not_configured"}))):
                search_harness.run_pond(run_dir=run_dir, env_file=".env")
            saved = json.loads((run_dir / "results.json").read_text())
            iteration = saved["iterations"][0]

        self.assertEqual(saved["status"], "awaiting_diagnosis")
        self.assertEqual(iteration["pool_stats"]["score_histogram"], {
            "0.9+": 1, "0.8-0.9": 0, "0.7-0.8": 1, "0.6-0.7": 0, "below 0.6": 0,
        })
        self.assertIsNone(iteration["gt_recall"])
        self.assertNotIn("strong_people", saved)
        self.assertNotIn("pool_read", iteration)
        self.assertNotIn("suggested_diagnosis", iteration["pool_stats"])
        self.assertTrue(iteration["edit_delta"]["traits_added"])
        self.assertEqual(iteration["shortlist_grades"][0]["move_plausibility"], "promising step-up")
        self.assertEqual(iteration["shortlist_grades"][0]["current_company_headcount"], 40)
        self.assertIsNone(iteration["shortlist_grades"][0]["company_card_id"])

    def test_ranking_fix_forces_only_llm_stages(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            _start(run_dir)
            payload_path = run_dir / "ponds" / "pond-01" / "payload.json"
            payload_path.parent.mkdir(parents=True)
            payload_path.write_text(json.dumps(_payload()), encoding="utf-8")
            rows_path = run_dir / "rows.jsonl"
            rows_path.write_text("", encoding="utf-8")
            results = json.loads((run_dir / "results.json").read_text())
            results["status"] = "ready_to_rerank"
            results["pending_payload"] = {
                "pond_n": 1, "query": results["pending_query"]["query"],
                "payload_json": str(payload_path), "ledger": "ledger",
                "payload": _payload(), "rerank_exclusions": [],
                "rerank_only": True, "pattern_default_edits": [],
            }
            (run_dir / "results.json").write_text(json.dumps(results), encoding="utf-8")
            with mock.patch.object(search_harness, "_run_command", return_value={
                    "artifacts": {"jsonl": str(rows_path)},
                  }) as run, mock.patch.object(search_harness, "_ensure_hiring_company_context"):
                search_harness.run_pond(run_dir=run_dir, env_file=".env")

        command = run.call_args.args[0]
        self.assertIn("--force-llm", command)
        self.assertNotIn("--force", command)

    def test_paid_next_move_is_checkpointed_before_becoming_the_next_query(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            _start(run_dir)
            results = json.loads((run_dir / "results.json").read_text())
            results["status"] = "awaiting_diagnosis"
            results["iterations"] = [{
                "pond_n": 1, "query": results["pending_query"]["query"],
                "pool_stats": {"result_count": 50, "reviewed_count": 50,
                               "score_histogram": {}, "level_mix": {},
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
                    "diagnosis": "wrong_location",
                    "action": "widen_geography", "next_query": "Software engineer in Europe",
                    "source": "software engineer",
                    "rationale": "The reviewed pool was constrained to the wrong geography.",
                })))],
            )
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=mock.Mock(return_value=response))))
            search_harness.decide(run_dir=run_dir, choice=2, diagnosis="wrong_location", client=client)
            search_harness.update_pending_query(
                run_dir=run_dir, query="Backend engineer in Europe")
            saved = json.loads((run_dir / "results.json").read_text())

        self.assertEqual(saved["status"], "ready_to_compile")
        self.assertEqual(saved["pending_query"]["query"], "Backend engineer in Europe")
        self.assertEqual(saved["raw_model_responses"][0]["raw"], response.choices[0].message.content)
        self.assertEqual(saved["raw_model_responses"][0]["usage"]["cached_tokens"], 5)
        self.assertEqual(saved["iterations"][0]["proposal_delta"]["actual"]["next_query"],
                         "Backend engineer in Europe")
        self.assertTrue(saved["iterations"][0]["proposal_delta"]["changed"])

    def test_autonomous_decide_records_model_diagnosis_without_a_human_override(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            _start(run_dir)
            results = json.loads((run_dir / "results.json").read_text())
            results["status"] = "awaiting_diagnosis"
            results["iterations"] = [{
                "pond_n": 1, "query": results["pending_query"]["query"],
                "pool_stats": {"result_count": 20, "reviewed_count": 20,
                               "score_histogram": {}, "level_mix": {}, "geo_mix": {},
                               "top_companies": {}},
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
                    "diagnosis": "exhausted", "action": "add_adjacent_pond",
                    "next_query": "Product designer in the Bay Area",
                    "source": "inferred",
                    "rationale": "The direct pond is exhausted; broaden to transferable systems work.",
                })))],
            )
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=mock.Mock(return_value=response))))

            search_harness.decide(run_dir=run_dir, autonomous=True, client=client)
            saved = json.loads((run_dir / "results.json").read_text())

        iteration = saved["iterations"][0]
        self.assertEqual(iteration["diagnosis"], "exhausted")
        self.assertIsNone(iteration["human_override"])
        self.assertFalse(iteration["proposal_delta"]["changed"])
        self.assertEqual(saved["pending_query"]["query"], "Product designer in the Bay Area")
        self.assertEqual(client.chat.completions.create.call_args.kwargs["service_tier"], "flex")

    def test_adjacent_population_requires_new_head_or_career_stage(self) -> None:
        self.assertFalse(search_harness._adjacent_population_changed(
            "Software Engineer in the Bay Area", "Risk Systems Engineer in the Bay Area"))
        self.assertTrue(search_harness._adjacent_population_changed(
            "Software Engineer in the Bay Area", "Product Designer in the Bay Area"))
        self.assertTrue(search_harness._adjacent_population_changed(
            "Software Engineer in the Bay Area", "Staff Software Engineer in the Bay Area"))

    def test_adjacent_population_retries_once_then_accepts_or_stops(self) -> None:
        def response(query: str) -> SimpleNamespace:
            usage = SimpleNamespace(prompt_tokens=20, completion_tokens=10,
                                    prompt_tokens_details=SimpleNamespace(cached_tokens=5),
                                    completion_tokens_details=SimpleNamespace(reasoning_tokens=2))
            return SimpleNamespace(
                model="gpt-5.6-luna", service_tier="flex", usage=usage,
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                    "diagnosis": "weak_quality", "action": "add_adjacent_pond",
                    "next_query": query, "source": "inferred",
                    "rationale": "Change the candidate population.",
                })))],
            )

        for second, expected_status in (
            ("Product Designer in the Bay Area", "ready_to_compile"),
            ("Payments Software Engineer in the Bay Area", "completed"),
        ):
            with self.subTest(second=second), tempfile.TemporaryDirectory() as raw:
                run_dir = Path(raw)
                _start(run_dir)
                results = json.loads((run_dir / "results.json").read_text())
                results["status"] = "awaiting_diagnosis"
                results["iterations"] = [{
                    "pond_n": 1, "query": "Software Engineer in the Bay Area",
                    "pool_stats": {"result_count": 50, "reviewed_count": 50,
                                   "score_histogram": {}, "level_mix": {},
                                   "geo_mix": {}, "top_companies": {}},
                    "shortlist_grades": [], "input": {}, "arm": {}, "cost_usd": 0,
                    "diagnosis": None, "human_override": None, "next_move": None,
                }]
                (run_dir / "results.json").write_text(json.dumps(results), encoding="utf-8")
                client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                    create=mock.Mock(side_effect=[
                        response("Risk Systems Engineer in the Bay Area"), response(second)]))))

                search_harness.decide(run_dir=run_dir, autonomous=True, client=client)
                saved = json.loads((run_dir / "results.json").read_text())

            self.assertEqual(client.chat.completions.create.call_count, 2)
            self.assertEqual(saved["status"], expected_status)
            if expected_status == "ready_to_compile":
                self.assertEqual(saved["pending_query"]["query"], second)
            else:
                self.assertEqual(saved["iterations"][0]["next_move"]["action"], "stop")

    def test_next_move_retries_requirement_language_once_then_accepts_or_stops(self) -> None:
        def response(query: str) -> SimpleNamespace:
            usage = SimpleNamespace(prompt_tokens=20, completion_tokens=10,
                                    prompt_tokens_details=SimpleNamespace(cached_tokens=5),
                                    completion_tokens_details=SimpleNamespace(reasoning_tokens=2))
            return SimpleNamespace(
                model="gpt-5.6-luna", service_tier="flex", usage=usage,
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                    "diagnosis": "wrong_specialty", "action": "add_adjacent_pond",
                    "next_query": query, "source": "inferred",
                    "rationale": "Change the candidate population.",
                })))],
            )

        bad = "Frontend Engineer with polished landing pages in New York"
        for second, expected_status in (
            ("Designer who can code in New York", "ready_to_compile"),
            ("Design Engineer with polished landing pages in New York", "completed"),
        ):
            with self.subTest(second=second), tempfile.TemporaryDirectory() as raw:
                run_dir = Path(raw)
                _start(run_dir)
                plan_path = run_dir / "epoch0" / "plan.json"
                plan = json.loads(plan_path.read_text())
                plan["traits"] = {"must_have": [{
                    "trait": "Shipping polished landing pages and interactive web experiences",
                    "tier": "core",
                }]}
                plan_path.write_text(json.dumps(plan), encoding="utf-8")
                results = json.loads((run_dir / "results.json").read_text())
                results["status"] = "awaiting_diagnosis"
                results["iterations"] = [{
                    "pond_n": 1, "query": results["pending_query"]["query"],
                    "pool_stats": {"result_count": 50, "reviewed_count": 50,
                                   "score_histogram": {}, "level_mix": {},
                                   "geo_mix": {}, "top_companies": {}},
                    "shortlist_grades": [], "input": {}, "arm": {}, "cost_usd": 0,
                    "diagnosis": None, "human_override": None, "next_move": None,
                }]
                (run_dir / "results.json").write_text(json.dumps(results), encoding="utf-8")
                client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                    create=mock.Mock(side_effect=[response(bad), response(second)]))))

                search_harness.decide(run_dir=run_dir, autonomous=True, client=client)
                saved = json.loads((run_dir / "results.json").read_text())

            self.assertEqual(client.chat.completions.create.call_count, 2)
            self.assertEqual(len(saved["raw_model_responses"]), 2)
            self.assertEqual(saved["status"], expected_status)
            if expected_status == "ready_to_compile":
                self.assertEqual(saved["pending_query"]["query"], second)
            else:
                self.assertEqual(saved["iterations"][0]["next_move"]["action"], "stop")

    def test_requirement_overlap_ignores_location_outside_plan_traits(self) -> None:
        plan = {"traits": {"must_have": [{
            "trait": "Shipping polished landing pages and interactive web experiences",
        }]}}
        self.assertEqual(
            search_harness._shared_requirement_ngram(
                "Frontend Engineer with polished landing pages in New York", plan),
            "polished landing pages",
        )
        self.assertIsNone(search_harness._shared_requirement_ngram(
            "Designer who can code in New York Metropolitan Area", plan))

    def test_interactive_diagnosis_is_saved_before_the_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            _start(run_dir)
            results = json.loads((run_dir / "results.json").read_text())
            results["status"] = "awaiting_diagnosis"
            results["iterations"] = [{
                "pond_n": 1, "query": results["pending_query"]["query"],
                "pool_stats": {"result_count": 20, "reviewed_count": 20,
                               "score_histogram": {}, "level_mix": {}, "geo_mix": {},
                               "top_companies": {}},
                "shortlist_grades": [], "diagnosis": None, "human_override": None,
            }]
            (run_dir / "results.json").write_text(json.dumps(results), encoding="utf-8")
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=mock.Mock(side_effect=RuntimeError("synthetic failure")))))

            with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                search_harness.decide(
                    run_dir=run_dir, choice=2, diagnosis="weak_quality", client=client)
            saved = json.loads((run_dir / "results.json").read_text())

        self.assertEqual(saved["iterations"][0]["diagnosis"], "weak_quality")
        self.assertEqual(saved["iterations"][0]["human_override"]["diagnosis"], "weak_quality")

    def test_protocol_caps_retrieval_and_ponds(self) -> None:
        self.assertEqual(search_harness.RETRIEVAL_LIMIT, 1000)
        self.assertEqual(search_harness.MAX_PONDS, 4)
        self.assertIn("For too_few, weak_quality, or exhausted, never narrow",
                      search_harness.NEXT_SEARCH_PROMPT)
        self.assertIn("For wrong_specialty, the next query must name a different source occupation",
                      search_harness.NEXT_SEARCH_PROMPT)
        self.assertIn("Never widen geography", search_harness.NEXT_SEARCH_PROMPT)
        self.assertIn("rich in in-band candidates from credible companies",
                      search_harness.NEXT_SEARCH_PROMPT)
        self.assertIn("candidate_populations as the JD-grounded pond menu",
                      search_harness.NEXT_SEARCH_PROMPT)
        self.assertIn("Return diagnosis, action, next_query,", search_harness.NEXT_SEARCH_PROMPT)


if __name__ == "__main__":
    unittest.main()
