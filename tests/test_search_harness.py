from __future__ import annotations

import asyncio
import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.search.primitives.deep_search import company_context, network_floors, search_harness


def _plan() -> dict:
    return {
        "job_id": "jd-1", "job_title": "Search Engineer",
        "normalized_archetype": "software engineer",
        "pond_prompt_family": "engineering",
        "target_level": "staff_ic",
        "source_url": "https://example.test/job", "set_scope": {"set_id": "set-1"},
        "hiring_company": {"name": "Acme", "website_url": "https://acme.example"},
        "candidate_populations": [{
            "population": "software engineer", "hint_kind": "stated-background",
            "evidence_quote": "We are looking for software engineers.",
        }],
        "comp_band": {"currency": "USD", "minimum": 140000, "maximum": 220000,
                      "period": "year", "evidence_quote": "Base salary is 140000 to 220000."},
        "search_scope": {"location": "San Francisco Bay Area",
                         "filters": {"metro_areas": ["San Francisco Bay Area"]}},
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


def _fit_experts(
    role: str = "strong-fit",
    company: str = "strong",
    craft: str = "strong",
    move: str = "plausible",
) -> dict:
    return {
        "role_fit": {"label": role, "why": "Role evidence.", "applied_precedent_ids": []},
        "company_taste": {
            "label": company, "why": "Company evidence.", "applied_precedent_ids": [],
        },
        "craft_and_potential": {
            "label": craft, "why": "Craft evidence.", "applied_precedent_ids": [],
        },
        "move_feasibility": {
            "label": move, "why": "Move evidence.", "applied_precedent_ids": [],
        },
    }


def _floor_artifact(plan: dict | None = None, count: int = 12) -> dict:
    plan = plan or _plan()
    identity = {"backend": "powerset", "set_id": plan["set_scope"]["set_id"]}
    population = plan["candidate_populations"][0]["population"]
    geography = plan["search_scope"]["location"]
    return {
        "schema_version": "network-floors.v1",
        "binding": network_floors.floor_binding(plan, "powerset", identity),
        "generated_at": "2026-08-25T12:00:00Z",
        "provenance": {"backend": "powerset", "namespace": "aleph_people_v1",
                       "set_id": identity["set_id"]},
        "floors": [{
            "population": population, "geography": geography, "count": count,
            "display_count": str(count), "capped": False,
            "label": f"exact-filter floor (lower bound; semantic availability unknown): {count}",
        }],
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
    (directory / "plan_binding.json").write_text(json.dumps({
        "retrieval": {"backend": "powerset", "set_id": "set-1"},
    }), encoding="utf-8")
    (directory / "network_floors.json").write_text(
        json.dumps(_floor_artifact()), encoding="utf-8")
    return search_harness.initialize_run(run_dir=directory, jd_path=jd,
                                    plan_path=plan, queries_path=queries)


class SearchHarnessTests(unittest.TestCase):
    def test_initial_results_uses_the_plan_for_card_retrieval(self) -> None:
        plan = {**_plan(), "normalized_archetype": "agent experience engineer"}
        plan["traits"]["must_have"].append({
            "trait": "retrieval-aware documentation", "tier": "core",
        })

        results = search_harness.build_initial_results(
            plan, [{"key": "q00", "query": "Technical Writer with AI benchmarks"}],
        )

        self.assertEqual(results["brief"]["occupation"], "agent experience engineer")
        self.assertIn("search systems", results["brief"]["defining_capability"])
        self.assertIn("retrieval-aware documentation",
                      results["brief"]["defining_capability"])

    def test_review_set_annotates_the_whole_floor_set_up_to_the_retrieval_cap(self) -> None:
        rows = [
            {"person_id": f"p{index}", "final_score": .70 if index < 105 else .69}
            for index in range(110)
        ]

        reviewed = search_harness._review_candidates(rows, {})

        # Every row over the floor is annotated (~$0.50 per 1,000 calls) ...
        self.assertEqual(len(reviewed), 105)
        self.assertEqual(reviewed[-1]["person"], "p104")

        # ... bounded by FIT_ANNOTATION_LIMIT.
        flood = [{"person_id": f"f{index}", "final_score": .71} for index in range(510)]
        self.assertEqual(len(search_harness._review_candidates(flood, {})), 500)

        fallback = search_harness._review_candidates([
            {"person_id": "fallback-1", "final_score": .69},
            {"person_id": "fallback-2", "final_score": .30},
            {"person_id": "too-weak", "final_score": .29},
        ], {})
        self.assertEqual([row["person"] for row in fallback], ["fallback-1", "fallback-2"])
        self.assertEqual(search_harness._review_candidates([
            {"person_id": "too-weak", "final_score": .29}], {}), [])

    def test_review_candidates_preserves_static_trajectory_evidence(self) -> None:
        rows = [{"person_id": "p1", "final_score": .9}]
        profiles = {"p1": {
            "positions": [{
                "position_title": "Staff Engineer", "company_name": "Acme",
                "start_date": "2024-01-01", "end_date": None,
                "description": "Promoted twice and led the billing platform.",
                "company_description": "Developer tools company.",
                "company_sector_types": ["Enterprise Software"],
                "company_entity_types": ["venture_backed_startup"],
                "company_stage": "series_b", "company_headcount": 180,
                "role_track": "engineering", "role_ids": ["software_engineer"],
                "seniority_band": "staff", "is_current": True,
            }],
            "education": [{
                "school_name": "Example University", "degree": "BS",
                "field_of_study": "Computer Science", "start_year": 2016,
                "end_year": 2020,
            }],
        }}

        candidate = search_harness._review_candidates(rows, profiles)[0]

        self.assertEqual(candidate["recent_roles"], [{
            "title": "Staff Engineer", "company": "Acme",
            "start_date": "2024-01-01",
            "description": "Promoted twice and led the billing platform.",
            "company_description": "Developer tools company.",
            "company_sector_types": ["Enterprise Software"],
            "company_entity_types": ["venture_backed_startup"],
            "company_stage": "series_b", "company_headcount": 180,
            "role_track": "engineering", "role_ids": ["software_engineer"],
            "seniority_band": "staff",
        }])
        self.assertEqual(candidate["current_role_ids"], ["software_engineer"])
        self.assertEqual(candidate["current_company_description"], "Developer tools company.")
        self.assertEqual(candidate["current_company_sector_types"], ["Enterprise Software"])
        self.assertEqual(candidate["current_company_entity_types"], ["venture_backed_startup"])
        self.assertEqual(candidate["education"], [{
            "school": "Example University", "degree": "BS",
            "field": "Computer Science", "start_year": 2016, "end_year": 2020,
        }])

    def test_company_fit_uses_shared_slots_and_resumes_per_candidate(self) -> None:
        class Completions:
            def __init__(self) -> None:
                self.active = 0
                self.max_active = 0
                self.calls: list[dict] = []

            async def create(self, **kwargs):
                self.calls.append(kwargs)
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                await asyncio.sleep(.01)
                self.active -= 1
                prompt = kwargs["messages"][0]["content"]
                if prompt == company_context.ROLE_FIT_PROMPT:
                    payload = {
                        "label": "strong-fit",
                        "why": "Level and role evidence line up.",
                        "applied_precedent_ids": [],
                    }
                elif prompt == company_context.COMPANY_TASTE_PROMPT:
                    payload = {
                        "label": "neutral",
                        "why": "Ordinary employer history for this family.",
                        "applied_precedent_ids": [],
                    }
                elif prompt == company_context.CRAFT_POTENTIAL_PROMPT:
                    payload = {
                        "label": "strong",
                        "why": "Repeated high-quality individual work.",
                        "applied_precedent_ids": [],
                    }
                elif prompt == company_context.MOVE_FEASIBILITY_PROMPT:
                    payload = {
                        "label": "plausible",
                        "why": "The move is plausible now.",
                        "applied_precedent_ids": [],
                    }
                else:
                    payload = {
                        "group": "chat_worthy",
                        "why": "The candidate is plausible but needs role calibration.",
                        "applied_precedent_ids": [],
                    }
                return SimpleNamespace(
                    model="gpt-5.6-luna", service_tier="flex",
                    usage=SimpleNamespace(
                        prompt_tokens=100, completion_tokens=20,
                        prompt_tokens_details=SimpleNamespace(cached_tokens=80),
                        completion_tokens_details=SimpleNamespace(reasoning_tokens=5),
                    ),
                    choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))],
                )

        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            _start(run_dir)
            results = json.loads((run_dir / "results.json").read_text())
            candidates = [{
                "person": f"p{index}", "title": "Senior Software Engineer",
                "company": f"Company {index}", "score": .9 - index * .01,
                "trait_scores": {"Software Engineer": {
                    "score": .9, "reason": "Built production systems."}},
            } for index in range(3)]
            completions = Completions()
            client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
            with (mock.patch.object(search_harness, "FIT_CONCURRENCY", 2),
                  mock.patch.object(search_harness, "retrieve_fit_precedents", return_value=[{
                      "id": "direct-product-work", "dimension": "role_fit",
                      "candidate_context": "Direct product work",
                      "judgment": {"label": "strong-fit"},
                      "reason": "Strong evidence.",
                  }])):
                first = search_harness._annotate_company_fit(
                    candidates=candidates, results=results, run_dir=run_dir,
                    pond_n=1, plan=_plan(), client=client)
                second = search_harness._annotate_company_fit(
                    candidates=candidates, results=results, run_dir=run_dir,
                    pond_n=1, plan=_plan(), client=client)

            checkpoints = sorted((run_dir / "ponds/pond-01/company-fit").glob("*.json"))
            payloads = [json.loads(call["messages"][1]["content"])
                        for call in completions.calls]
            systems = [call["messages"][0]["content"] for call in completions.calls]

        self.assertEqual(len(completions.calls), 15)
        self.assertEqual(completions.max_active, 2)
        self.assertEqual(len(checkpoints), 15)
        self.assertEqual([row["person"] for row in first], ["p0", "p1", "p2"])
        self.assertEqual(first, second)
        expert_payloads = [payload for payload in payloads if "candidate" in payload]
        decision_payloads = [payload for payload in payloads if "fit_experts" in payload]
        self.assertEqual(len(expert_payloads), 12)
        self.assertEqual(len(decision_payloads), 3)
        self.assertTrue(all(list(payload)[-1] == "candidate" for payload in expert_payloads))
        static_prefixes = [{key: value for key, value in payload.items() if key != "candidate"}
                           for payload in expert_payloads]
        self.assertTrue(all(prefix == static_prefixes[0] for prefix in static_prefixes))
        for prompt in (company_context.ROLE_FIT_PROMPT, company_context.COMPANY_TASTE_PROMPT,
                       company_context.CRAFT_POTENTIAL_PROMPT,
                       company_context.MOVE_FEASIBILITY_PROMPT, company_context.COMPANY_FIT_PROMPT):
            self.assertEqual(systems.count(prompt), 3)

    def test_summary_dedupes_ponds_and_uses_model_groups(self) -> None:
        def candidate(person, score, group, move="plausible", company="strong"):
            return {
                "person": person, "name": person, "title": "Engineer", "company": "Acme",
                "score": score, "fit_experts": _fit_experts(company=company, move=move),
                "group": group,
                "why": f"Model put {person} in {group}.", "months_in_seat": 24,
                "fit_annotation_source": "luna",
            }

        summary = search_harness.build_search_summary({"iterations": [
            {"pond_n": 1, "query": "Software engineers", "diagnosis": "weak_quality",
             "next_move": {"action": "add_adjacent_pond"}, "result_count": 100,
             "cost_usd": .4, "shortlist_grades": [
                 candidate("duplicate", .75, "chat_worthy"),
                 candidate("passed", .8, "passed", "comp-mismatch"),
                 candidate("chat-score", .68, "chat_worthy"),
                 candidate("send", .9, "send_worthy"),
                 candidate("chat-company", .9, "chat_worthy", company="weak"),
            ]},
            {"pond_n": 2, "query": "Adjacent engineers", "diagnosis": "enough_strong",
             "next_move": {"action": "stop"}, "below_threshold": True,
             "result_count": 50, "cost_usd": .5,
             "shortlist_grades": [candidate(
                 "duplicate", .85, "wrong_timing_relationship", "wrong-timing")]},
        ]}, 1.2345678)

        self.assertEqual(summary["deduped_candidate_count"], 5)
        self.assertEqual(summary["counts"], {
            "send_worthy": 1, "chat_worthy": 2,
            "wrong_timing_relationship": 1, "passed": 1,
        })
        duplicate = summary["groups"]["wrong_timing_relationship"][0]
        self.assertEqual(duplicate["ponds"], [1, 2])
        self.assertNotIn("anchored_score", duplicate)
        self.assertEqual(duplicate["rerank_score"], .85)
        self.assertEqual(duplicate["runs"], ["current"])
        self.assertEqual(
            duplicate["fit_experts"], _fit_experts(company="strong", move="wrong-timing"))
        self.assertEqual(summary["pond_chain"][1]["move"], "stop")
        self.assertTrue(summary["pond_chain"][1]["below_threshold"])
        self.assertEqual(summary["total_cost_usd"], 1.234568)

    def test_summary_preserves_model_group_and_why_then_sorts_by_rerank_score(self) -> None:
        def candidate(person, score, group, why, company="neutral", move="plausible"):
            return {
                "person": person, "name": person, "score": score,
                "fit_experts": _fit_experts(company=company, move=move),
                "group": group, "why": why,
            }

        summary = search_harness.build_search_summary({"iterations": [{
            "pond_n": 1, "query": "Engineers", "shortlist_grades": [
                candidate("generic", .8, "send_worthy",
                          "The model chose send despite generic evidence."),
                candidate("direct", .9, "send_worthy",
                          "Shipped work is direct evidence."),
                candidate("relationship", .95, "wrong_timing_relationship",
                          "Destination pull makes this a relationship for later.",
                          company="strong", move="destination-pull"),
            ],
        }]}, 0)

        self.assertEqual(summary["counts"], {
            "send_worthy": 2, "chat_worthy": 0,
            "wrong_timing_relationship": 1, "passed": 0,
        })
        self.assertEqual([row["name"] for row in summary["groups"]["send_worthy"]],
                         ["direct", "generic"])
        self.assertEqual(summary["groups"]["send_worthy"][1]["why"],
                         "The model chose send despite generic evidence.")
        relationship = summary["groups"]["wrong_timing_relationship"][0]
        self.assertEqual(relationship["fit_experts"]["move_feasibility"]["label"],
                         "destination-pull")

    def test_summary_merges_same_jd_frames_and_exports_canonical_csvs(self) -> None:
        current = {"iterations": [{
            "pond_n": 1, "query": "Designers", "shortlist_grades": [{
                "person": "p1", "name": "Current", "score": .71,
                "group": "chat_worthy", "why": "Needs calibration.",
                "fit_experts": _fit_experts(company="neutral"),
                "title": "Designer", "company": "Acme",
                "linkedin_url": "https://linkedin.com/in/current",
            }],
        }]}
        related = {"run": "title-frame", "cost_usd": .2, "results": {"iterations": [{
            "pond_n": 1, "query": "Design engineers", "shortlist_grades": [{
                "person": "duplicate-id", "name": "Current", "score": .82,
                "group": "send_worthy", "why": "Direct design craft evidence.",
                "fit_experts": _fit_experts(company="neutral"),
                "title": "Design Engineer", "company": "Acme",
                "linkedin_url": "https://linkedin.com/in/duplicate-current",
            }, {
                "person": "p2", "name": "Second", "score": .8,
                "group": "send_worthy", "why": "Direct frontend craft evidence.",
                "fit_experts": _fit_experts(company="neutral"),
                "title": "Frontend Engineer", "company": "Beta",
            }],
        }]}}
        summary = search_harness.build_search_summary(
            current, .1, run_name="design-frame", related_runs=[related])

        with tempfile.TemporaryDirectory() as raw:
            paths = search_harness.export_search_summary(summary, Path(raw))
            with Path(paths["shortlist_csv"]).open() as handle:
                rows = list(csv.DictReader(handle))
            with Path(paths["relationship_csv"]).open() as handle:
                relationship_rows = list(csv.DictReader(handle))

        self.assertEqual(summary["deduped_candidate_count"], 2)
        self.assertEqual(summary["groups"]["send_worthy"][0]["rerank_score"], .82)
        self.assertEqual(summary["groups"]["send_worthy"][0]["runs"],
                         ["design-frame", "title-frame"])
        self.assertEqual(list(rows[0]), [
            "Rank", "Name", "LinkedIn URL", "Current Role", "Current Company",
            "Source", "Channel", "Rationale",
        ])
        self.assertEqual([row["Name"] for row in rows], ["Current", "Second"])
        self.assertEqual(relationship_rows, [])

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
            (run_dir / "network_floors.json").write_text(
                json.dumps(_floor_artifact()), encoding="utf-8")
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
            saved = json.loads(Path(result["results"]).read_text())

        self.assertEqual(result["status"], "ready_to_compile")
        self.assertTrue(Path(result["results"]).name == "results.json")
        self.assertIsNotNone(saved["network_floors"])

    def test_draft_probe_precedes_query_generation_and_flags_sparse_populations(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            plan_path = run_dir / "epoch0" / "plan.json"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(json.dumps(_plan()), encoding="utf-8")
            jd = run_dir / "jd.txt"
            jd.write_text("Synthetic complete job description", encoding="utf-8")
            args = SimpleNamespace(
                approved_plan=None, queries_file=None, plan_approved=False,
                jd_file=str(jd), jd_url=None, backend="powerset", set_id="set-1",
                db="unused.duckdb", env_file=".env", query_model="gpt-5.6-luna",
                query_reasoning_effort="medium",
            )
            order: list[str] = []

            def fake_probe(*_args, **_kwargs):
                order.append("probe")
                artifact = _floor_artifact(count=0)
                artifact["floors"][0]["display_count"] = "0"
                return artifact

            def fake_run(_command, *, expected_paths=None, description=None):
                order.append("query")
                self.assertEqual(description, "generate initial search queries")
                self.assertTrue((run_dir / "network_floors.json").is_file())
                (run_dir / "queries.json").write_text(json.dumps([{
                    "key": "literal_search",
                    "query": "Software engineer in San Francisco Bay Area",
                }]), encoding="utf-8")

            with mock.patch.object(search_harness, "run_checked", side_effect=fake_run):
                result = search_harness.run_search_harness(
                    args, run_dir, None,
                    validate_plan=lambda *_args, **_kwargs: _plan(),
                    resolve_identity=lambda *_args: (
                        {"backend": "powerset", "set_id": "set-1"},
                        "set-1", "unused.duckdb"),
                    bind_plan=mock.Mock(), probe_floors=fake_probe,
                )

        self.assertEqual(order, ["probe", "query"])
        self.assertEqual(result["status"], "awaiting_plan_approval")
        self.assertIn(
            "exact-title floor: 0 for software engineer in San Francisco Bay Area — "
            "semantic availability unknown; expect a thin pond.",
            result["review"],
        )

    def test_changed_population_binding_regenerates_queries_and_returns_to_review(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            jd = run_dir / "jd.txt"
            jd.write_text("Synthetic complete job description", encoding="utf-8")
            plan_path = run_dir / "epoch0" / "plan.json"
            plan_path.parent.mkdir(parents=True)
            original = _plan()
            (run_dir / "network_floors.json").write_text(
                json.dumps(_floor_artifact(original)), encoding="utf-8")
            approved = _plan()
            approved["candidate_populations"].append({
                "population": "retrieval engineer", "hint_kind": "capability-adjacent",
                "evidence_quote": "Search or retrieval engineers are relevant.",
            })
            plan_path.write_text(json.dumps(approved), encoding="utf-8")
            queries_path = run_dir / "queries.json"
            queries_path.write_text(json.dumps([{
                "key": "literal_search", "query": "Old reviewed query",
            }]), encoding="utf-8")
            args = SimpleNamespace(
                approved_plan=None, queries_file=None, plan_approved=True,
                jd_file=str(jd), jd_url=None, backend="powerset", set_id="set-1",
                db="unused.duckdb", env_file=".env", query_model="gpt-5.6-luna",
                query_reasoning_effort="medium",
            )
            bind_plan = mock.Mock()

            def fake_probe(*_args, **_kwargs):
                artifact = _floor_artifact(approved)
                artifact["binding"] = network_floors.floor_binding(
                    approved, "powerset", {"backend": "powerset", "set_id": "set-1"})
                return artifact

            def fake_run(_command, *, expected_paths=None, description=None):
                self.assertEqual(description, "regenerate changed-binding queries")
                queries_path.write_text(json.dumps([{
                    "key": "q00", "query": "Retrieval engineer in San Francisco Bay Area",
                }]), encoding="utf-8")

            with mock.patch.object(search_harness, "run_checked", side_effect=fake_run):
                result = search_harness.run_search_harness(
                    args, run_dir, None,
                    validate_plan=lambda *_args, **_kwargs: approved,
                    resolve_identity=lambda *_args: (
                        {"backend": "powerset", "set_id": "set-1"},
                        "set-1", "unused.duckdb"),
                    bind_plan=bind_plan, probe_floors=fake_probe,
                )
            results_exists = (run_dir / "results.json").exists()

        self.assertEqual(result["status"], "awaiting_query_review")
        self.assertEqual(result["query_arms"][0]["query"],
                         "Retrieval engineer in San Francisco Bay Area")
        bind_plan.assert_not_called()
        self.assertFalse(results_exists)

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
            "shortlist_csv": None, "relationship_csv": None,
            "schema_version": "search-harness.manifest.v1", "status": "ready_to_compile",
        })

    def test_compile_uses_native_expansion_traits_without_evaluation_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            _start(run_dir)
            env_file = run_dir / "test.env"
            env_file.write_text("", encoding="utf-8")
            expanded = run_dir / "expanded.json"
            expanded.write_text(json.dumps(_payload()), encoding="utf-8")
            with mock.patch.object(search_harness, "_run_command", return_value={
                    "payload_json": str(expanded),
                  }) as run, mock.patch.object(
                      search_harness, "_ensure_hiring_company_context"), mock.patch.object(
                      search_harness, "_llm_pattern_defaults",
                      side_effect=lambda **kwargs: (kwargs["payload"], [])):
                search_harness.compile_pond(run_dir=run_dir, env_file=str(env_file))
            saved = json.loads((run_dir / "results.json").read_text())

        command = run.call_args.args[0]
        self.assertNotIn("--evaluation-query", command)
        self.assertNotIn("--evaluation-traits-json", command)
        self.assertEqual(saved["pending_payload"]["payload"]["traits"], _payload()["traits"])
        self.assertFalse((run_dir / "evaluation-traits.json").exists())

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
                 "current_titles": "Senior Software Engineer", "current_companies": "Alpha",
                 "trait_scores": json.dumps({"Software Engineer": {
                     "score": .9, "reason": "Built production systems."}})},
                {"person_id": "p2", "name": "Casey Delta", "final_score": .74,
                 "current_titles": "Software Engineer", "current_companies": "Beta"},
                {"person_id": "p3", "name": "Morgan Echo", "final_score": .65,
                 "current_titles": "Engineering Manager", "current_companies": "Gamma"},
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
                return [{**dict(row), "fit_experts": _fit_experts(),
                         "group": "send_worthy",
                         "why": "Direct evidence and a plausible move support outreach.",
                         "fit_annotation_source": "luna"}
                        for row in kwargs["candidates"]]

            with (mock.patch.object(search_harness, "_run_command", return_value={
                    "artifacts": {"jsonl": str(rows_path)},
                  }) as run, mock.patch.object(search_harness, "_ensure_hiring_company_context"),
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

        command = run.call_args.args[0]
        self.assertNotIn("--evaluation-query", command)
        self.assertNotIn("--evaluation-traits-json", command)
        self.assertEqual(saved["status"], "awaiting_diagnosis")
        self.assertEqual(iteration["pool_stats"]["score_histogram"], {
            "0.9+": 1, "0.8-0.9": 0, "0.7-0.8": 1, "0.6-0.7": 1, "below 0.6": 0,
        })
        self.assertEqual(iteration["pool_stats"]["level_mix"], {"Senior": 1, "Unspecified": 1,
                                                                  "Manager": 1})
        self.assertEqual(iteration["reviewed_count"], 2)
        self.assertEqual(iteration["result_count"], 3)
        self.assertFalse(iteration["below_threshold"])
        self.assertIsNone(iteration["gt_recall"])
        self.assertNotIn("strong_people", saved)
        self.assertNotIn("pool_read", iteration)
        self.assertNotIn("suggested_diagnosis", iteration["pool_stats"])
        self.assertTrue(iteration["edit_delta"]["traits_added"])
        self.assertEqual(
            iteration["shortlist_grades"][0]["fit_experts"]["move_feasibility"]["label"],
            "plausible",
        )
        self.assertIn("Software Engineer", iteration["shortlist_grades"][0]["trait_scores"])
        self.assertEqual(iteration["shortlist_grades"][0]["current_company_headcount"], 40)
        self.assertIsNone(iteration["shortlist_grades"][0]["company_card_id"])

    def test_run_reapplies_approved_set_and_plan_filters_after_review(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            _start(run_dir)
            plan_path = run_dir / "epoch0" / "plan.json"
            plan = json.loads(plan_path.read_text())
            plan["filters"] = [{"filter": "7+ YOE", "source": "user"}]
            plan["retrieval_filters"] = {"years_experience_min": 7}
            plan_path.write_text(json.dumps(plan))
            payload = _payload()
            payload["role_search_filters"].update({
                "set_id": "edited-set", "years_experience_min": 2,
            })
            payload_path = run_dir / "ponds/pond-01/payload.json"
            payload_path.parent.mkdir(parents=True)
            payload_path.write_text(json.dumps(payload))
            rows_path = run_dir / "rows.jsonl"
            rows_path.write_text("")
            results = json.loads((run_dir / "results.json").read_text())
            results["status"] = "ready_to_run"
            results["pending_payload"] = {
                "pond_n": 1, "query": results["pending_query"]["query"],
                "payload_json": str(payload_path), "ledger": "ledger", "payload": payload,
                "rerank_exclusions": [], "rerank_only": False, "pattern_default_edits": [],
            }
            (run_dir / "results.json").write_text(json.dumps(results))

            with mock.patch.object(search_harness, "_run_command", return_value={
                    "artifacts": {"jsonl": str(rows_path)}}), \
                 mock.patch.object(search_harness, "_ensure_hiring_company_context"):
                search_harness.run_pond(run_dir=run_dir, env_file=".env")

            reviewed = json.loads(payload_path.read_text())["role_search_filters"]

        self.assertEqual(reviewed["set_id"], "set-1")
        self.assertEqual(reviewed["years_experience_min"], 7)

    def test_local_continuation_uses_the_bound_db_instead_of_a_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            db = run_dir / "approved.duckdb"
            db.write_bytes(b"synthetic duckdb")
            stat = db.stat()
            (run_dir / "plan_binding.json").write_text(json.dumps({"retrieval": {
                "backend": "local", "db_path": str(db.resolve()),
                "db_size": stat.st_size, "db_mtime_ns": stat.st_mtime_ns,
            }}))

            set_id, resolved = search_harness._approved_retrieval(
                run_dir, _plan(), "local", search_harness.DEFAULT_LOCAL_DB)

        self.assertIsNone(set_id)
        self.assertEqual(resolved, str(db.resolve()))

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
            results["network_floors"] = _floor_artifact(count=0)
            results["network_floors"]["floors"][0]["display_count"] = "0"
            results["iterations"] = [{
                "pond_n": 1, "query": results["pending_query"]["query"],
                "pool_stats": {"result_count": 50, "reviewed_count": 0,
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
        context = json.loads(client.chat.completions.create.call_args.kwargs["messages"][1]["content"])
        self.assertIn(
            "Backend Engineer or Frontend Engineer",
            client.chat.completions.create.call_args.kwargs["messages"][0]["content"],
        )
        self.assertEqual(context["pond_chain"][0]["reviewed_count"], 0)
        self.assertEqual(context["network_floors"], [
            "exact-filter floor (lower bound; semantic availability unknown): 0",
        ])
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

    def test_interactive_decide_retries_a_conflicting_model_diagnosis(self) -> None:
        def response(diagnosis: str, action: str, query: str) -> SimpleNamespace:
            usage = SimpleNamespace(prompt_tokens=20, completion_tokens=10,
                                    prompt_tokens_details=SimpleNamespace(cached_tokens=5),
                                    completion_tokens_details=SimpleNamespace(reasoning_tokens=2))
            return SimpleNamespace(
                model="gpt-5.6-luna", service_tier="flex", usage=usage,
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                    "diagnosis": diagnosis, "action": action, "next_query": query,
                    "source": "software engineer",
                    "rationale": "Address the diagnosed problem.",
                })))],
            )

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
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=mock.Mock(side_effect=[
                    response("wrong_specialty", "add_adjacent_pond",
                             "Product designer in the Bay Area"),
                    response("wrong_location", "widen_geography",
                             "Software engineer in Europe"),
                ]))))

            search_harness.decide(
                run_dir=run_dir, choice=2, diagnosis="wrong_location", client=client)
            saved = json.loads((run_dir / "results.json").read_text())

        self.assertEqual(client.chat.completions.create.call_count, 2)
        retry = client.chat.completions.create.call_args_list[1].kwargs["messages"][-1]["content"]
        self.assertIn("human selected diagnosis 'wrong_location'", retry)
        self.assertEqual(saved["iterations"][0]["diagnosis"], "wrong_location")
        self.assertEqual(saved["iterations"][0]["next_move"]["action"], "widen_geography")
        self.assertEqual(saved["pending_query"]["query"], "Software engineer in Europe")

    def test_next_move_accepts_retrieved_card_family_as_source(self) -> None:
        usage = SimpleNamespace(
            prompt_tokens=20, completion_tokens=10,
            prompt_tokens_details=SimpleNamespace(cached_tokens=5),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
        )
        response = SimpleNamespace(
            model="gpt-5.6-luna", service_tier="flex", usage=usage,
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                "diagnosis": "too_few", "action": "add_adjacent_pond",
                "next_query": "Developer advocates with documentation experience",
                "source": "technical writer developer documentation",
                "rationale": "Use the card's adjacent population.",
            })))],
        )
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=mock.Mock(return_value=response))))
        context = {
            "pond_chain": [{"query": "Technical Writers"}],
            "candidate_populations": [],
            "retrieved_precedents": [{
                "job": "Technical Writer",
                "family": "technical writer developer documentation",
                "chain": [],
            }],
        }

        proposal, _raw, _usage = search_harness.propose_next_move(
            context, selected="too_few", user_continue=False,
            iteration={"query": "Technical Writers"}, prompt="next pond",
            client=client,
        )

        self.assertEqual(proposal["source"],
                         "technical writer developer documentation")
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_user_continue_retries_stops_then_widens_geography(self) -> None:
        usage = SimpleNamespace(
            prompt_tokens=20, completion_tokens=10,
            prompt_tokens_details=SimpleNamespace(cached_tokens=5),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
        )
        response = SimpleNamespace(
            model="gpt-5.6-luna", service_tier="flex", usage=usage,
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                "diagnosis": "exhausted", "action": "stop", "next_query": None,
                "source": None, "rationale": "The current results are exhausted.",
            })))],
        )
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            _start(run_dir)
            results = json.loads((run_dir / "results.json").read_text())
            results["status"] = "awaiting_diagnosis"
            results["iterations"] = [{
                "pond_n": 1, "query": "Software Engineer in the Bay Area",
                "pool_stats": {"result_count": 20, "reviewed_count": 5,
                               "score_histogram": {}, "level_mix": {}, "geo_mix": {},
                               "top_companies": {}},
                "shortlist_grades": [], "input": {}, "arm": {}, "cost_usd": 0,
                "diagnosis": None, "human_override": None, "next_move": None,
            }]
            (run_dir / "results.json").write_text(json.dumps(results), encoding="utf-8")
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=mock.Mock(side_effect=[response, response]))))

            search_harness.decide(run_dir=run_dir, choice=2, client=client)
            saved = json.loads((run_dir / "results.json").read_text())

        self.assertEqual(client.chat.completions.create.call_count, 2)
        context = client.chat.completions.create.call_args_list[0].kwargs["messages"][1]["content"]
        self.assertIn('"user_requested_another_round": true', context)
        retry = client.chat.completions.create.call_args_list[1].kwargs["messages"][-1]["content"]
        self.assertIn("stop and corpus_sparse are not allowed", retry)
        self.assertEqual(saved["status"], "ready_to_compile")
        self.assertEqual(saved["pending_query"]["query"], "Software Engineer")
        self.assertEqual(saved["iterations"][0]["next_move"]["action"], "widen_geography")

    def test_user_continue_reopens_a_completed_model_stop(self) -> None:
        usage = SimpleNamespace(
            prompt_tokens=20, completion_tokens=10,
            prompt_tokens_details=SimpleNamespace(cached_tokens=5),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
        )
        response = SimpleNamespace(
            model="gpt-5.6-luna", service_tier="flex", usage=usage,
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                "diagnosis": "wrong_specialty", "action": "add_adjacent_pond",
                "next_query": "Product Designer in the Bay Area", "source": "inferred",
                "rationale": "Try a different occupation.",
            })))],
        )
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            _start(run_dir)
            results = json.loads((run_dir / "results.json").read_text())
            results["status"] = "completed"
            results["iterations"] = [{
                "pond_n": 1, "query": "Software Engineer in the Bay Area",
                "pool_stats": {"result_count": 20, "reviewed_count": 5,
                               "score_histogram": {}, "level_mix": {}, "geo_mix": {},
                               "top_companies": {}},
                "shortlist_grades": [], "input": {}, "arm": {}, "cost_usd": 0,
                "diagnosis": "exhausted", "human_override": None,
                "next_move": {"action": "stop", "next_query": None},
            }]
            (run_dir / "results.json").write_text(json.dumps(results), encoding="utf-8")
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=mock.Mock(return_value=response))))

            search_harness.decide(run_dir=run_dir, choice=2, client=client)
            saved = json.loads((run_dir / "results.json").read_text())

        self.assertEqual(saved["status"], "ready_to_compile")
        self.assertEqual(saved["pending_query"]["query"], "Product Designer in the Bay Area")
        self.assertEqual(saved["iterations"][0]["human_override"]["choice"], 2)
        self.assertEqual(saved["iterations"][0]["next_move"]["action"], "add_adjacent_pond")

    def test_stop_can_reject_an_already_proposed_payload_edit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            _start(run_dir)
            results = json.loads((run_dir / "results.json").read_text())
            results["status"] = "awaiting_payload_review"
            results["iterations"] = [{
                "pond_n": 1, "query": results["pending_query"]["query"],
                "diagnosis": "weak_quality", "human_override": None,
                "next_move": {"action": "ranking_fix", "next_query": None,
                              "source": None, "rationale": "Rerank the same pond."},
            }]
            (run_dir / "results.json").write_text(json.dumps(results), encoding="utf-8")

            search_harness.decide(
                run_dir=run_dir, choice=3, diagnosis="weak_quality",
                note="The proposed rerank does not change the payload.")
            saved = json.loads((run_dir / "results.json").read_text())

        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["iterations"][0]["next_move"]["action"], "stop")
        self.assertEqual(saved["iterations"][0]["human_override"]["choice"], 3)

    def test_next_query_retries_only_an_exact_prior_query(self) -> None:
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

        with tempfile.TemporaryDirectory() as raw:
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
                    response("  Software Engineer in the Bay Area  "),
                    response("Risk Systems Engineer in the Bay Area"),
                ]))))

            search_harness.decide(run_dir=run_dir, autonomous=True, client=client)
            saved = json.loads((run_dir / "results.json").read_text())

        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(saved["status"], "ready_to_compile")
        self.assertEqual(saved["pending_query"]["query"],
                         "Risk Systems Engineer in the Bay Area")

    def test_duplicate_query_checks_every_prior_pond_then_widens(self) -> None:
        def response() -> SimpleNamespace:
            usage = SimpleNamespace(prompt_tokens=20, completion_tokens=10,
                                    prompt_tokens_details=SimpleNamespace(cached_tokens=5),
                                    completion_tokens_details=SimpleNamespace(reasoning_tokens=2))
            return SimpleNamespace(
                model="gpt-5.6-luna", service_tier="flex", usage=usage,
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                    "diagnosis": "wrong_specialty", "action": "add_adjacent_pond",
                    "next_query": "Product Designer in London",
                    "source": "inferred", "rationale": "Try a different craft.",
                })))],
            )

        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            _start(run_dir)
            results = json.loads((run_dir / "results.json").read_text())
            results["status"] = "awaiting_diagnosis"
            results["iterations"] = [
                {"pond_n": 1, "query": "Frontend Engineer in London",
                 "diagnosis": "wrong_specialty", "next_move": {"action": "add_adjacent_pond"}},
                {"pond_n": 2, "query": "Product Designer in London",
                 "diagnosis": "wrong_level", "next_move": {"action": "add_adjacent_pond"}},
                {"pond_n": 3, "query": "Software Engineer in London",
                 "pool_stats": {"result_count": 50, "reviewed_count": 50,
                                "score_histogram": {}, "level_mix": {},
                                "geo_mix": {}, "top_companies": {}},
                 "shortlist_grades": [],
                 "input": {"filters": {"metro_areas": ["London Metropolitan Area"]}},
                 "arm": {}, "cost_usd": 0, "diagnosis": None,
                 "human_override": None, "next_move": None},
            ]
            (run_dir / "results.json").write_text(json.dumps(results), encoding="utf-8")
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=mock.Mock(side_effect=[response(), response()]))))

            search_harness.decide(
                run_dir=run_dir, choice=2, diagnosis="wrong_specialty", client=client)
            saved = json.loads((run_dir / "results.json").read_text())

        self.assertEqual(client.chat.completions.create.call_count, 2)
        context = client.chat.completions.create.call_args_list[0].kwargs["messages"][1]["content"]
        self.assertIn('"pond_chain"', context)
        self.assertIn('"pond_n": 1', context)
        retry = client.chat.completions.create.call_args_list[1].kwargs["messages"][-1]["content"]
        self.assertIn("duplicates a query already in pond_chain", retry)
        self.assertEqual(saved["status"], "ready_to_compile")
        self.assertEqual(saved["pending_query"]["query"], "Software Engineer")
        self.assertEqual(saved["iterations"][2]["next_move"]["action"], "widen_geography")

    def test_wrong_specialty_may_return_to_a_prior_population_at_wider_geography(self) -> None:
        usage = SimpleNamespace(
            prompt_tokens=20, completion_tokens=10,
            prompt_tokens_details=SimpleNamespace(cached_tokens=5),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
        )
        response = SimpleNamespace(
            model="gpt-5.6-luna", service_tier="flex", usage=usage,
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                "diagnosis": "wrong_specialty", "action": "widen_geography",
                "next_query": "Executive Assistant in Europe", "source": "inferred",
                "rationale": "Return to the right occupation and widen the thin local market.",
            })))],
        )
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            _start(run_dir)
            results = json.loads((run_dir / "results.json").read_text())
            results["status"] = "awaiting_diagnosis"
            results["iterations"] = [
                {"pond_n": 1, "query": "Executive Assistant in Stockholm",
                 "diagnosis": "too_few", "next_move": {"action": "add_adjacent_pond"}},
                {"pond_n": 2, "query": "Operations professional in Europe",
                 "pool_stats": {"result_count": 34, "reviewed_count": 0,
                                "score_histogram": {}, "level_mix": {}, "geo_mix": {},
                                "top_companies": {}},
                 "shortlist_grades": [], "input": {}, "arm": {}, "cost_usd": 0,
                 "diagnosis": None, "human_override": None, "next_move": None},
            ]
            (run_dir / "results.json").write_text(json.dumps(results), encoding="utf-8")
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=mock.Mock(return_value=response))))

            search_harness.decide(
                run_dir=run_dir, choice=2, diagnosis="wrong_specialty",
                note="Keep Executive Assistant and widen to Europe.", client=client)
            saved = json.loads((run_dir / "results.json").read_text())

        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual(saved["pending_query"]["query"], "Executive Assistant in Europe")
        context = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        self.assertIn("network is predominantly US-based", context)
        self.assertIn("widen country to region to global early", context)

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
        self.assertIn("Choose one next pond", search_harness.NEXT_SEARCH_PROMPT)
        self.assertIn("highest retrieval_score card wins", search_harness.NEXT_SEARCH_PROMPT)
        self.assertIn("Keep the current US metro, Europe, or other non-US country unchanged",
                      search_harness.NEXT_SEARCH_PROMPT)
        self.assertIn("user_requested_another_round", search_harness.NEXT_SEARCH_PROMPT)
        self.assertIn("Return strict JSON only with exactly diagnosis, action, next_query,",
                      search_harness.NEXT_SEARCH_PROMPT)


if __name__ == "__main__":
    unittest.main()
