from __future__ import annotations

import asyncio
import csv
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.search.primitives.deep_search import legacy, search_harness


def _context() -> dict:
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
        "traits": [{"trait": "search systems", "kind": "capability",
                    "evidence_quote": "built production search systems"}],
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


def _start(directory: Path) -> Path:
    jd = directory / "source-jd.txt"
    jd.write_text("Synthetic complete job description", encoding="utf-8")
    queries = directory / "queries.json"
    queries.write_text(json.dumps([
        {"key": "literal_search", "query": "Software engineer with search systems experience in San Francisco Bay Area"},
        {"key": "adjacent_search", "query": "Infrastructure engineer with retrieval systems experience in San Francisco Bay Area"},
    ]), encoding="utf-8")
    (directory / "decision.json").write_text(json.dumps({
        "surface": "people", "backend": "powerset", "depth": "deep",
    }), encoding="utf-8")
    source = {"source_title": "Search Engineer", "source_url": "https://example.test/job",
              "company_name": "Acme", "company_website_url": "https://acme.example"}
    (directory / "source.json").write_text(json.dumps(source))
    path = search_harness.initialize_run(run_dir=directory, jd_path=jd, queries_path=queries,
                                        retrieval={"backend": "powerset", "set_id": "set-1"})
    return path


class SearchHarnessTests(unittest.TestCase):
    def test_move_likelihood_gates_native_rating_at_three(self) -> None:
        for score, eligible in [(1.0, False), (2.99, False), (3.0, True), (4.5, True)]:
            with self.subTest(score=score):
                self.assertEqual(search_harness._move_likelihood_eligible({
                    "cross_encoder_status": "ok", "cross_encoder_score": score,
                    "cross_encoder_score_1_to_5": score,
                }), eligible)

    def test_move_likelihood_uses_only_scored_finite_ce_margin_without_a_cap(self) -> None:
        eligible = [{
            "person": f"p{index}", "score": .01,
            "cross_encoder_score": 0 if index == 0 else 2.5,
            "cross_encoder_score_1_to_5": 3 if index == 0 else 4.7,
            "cross_encoder_status": "ok", "rating": 1,
            "current_company_headcount": 40, "current_company_stage": "seed",
        } for index in range(510)]
        skipped = [{"person": f"skip{index}", "score": .99,
                    "cross_encoder_score": score, "cross_encoder_status": status}
                   for index, (score, status) in enumerate([
                       (-.01, "ok"), (None, "ok"), (10, "failed"),
                       (10, None), (float("nan"), "ok"),
                       (float("inf"), "ok"), (-float("inf"), "ok"),
                   ])]
        candidates = [eligible[0], *skipped, *eligible[1:]]
        profiles = {candidate["person"]: {
            "person_id": candidate["person"], "name": "Jordan Bravo",
            "positions": [{"title": "Engineer", "description": "Original evidence " * 200}
                          for _ in range(5)],
            "education": [{"school_name": "Example University"}] * 4,
            "tech_skills": ["Python"],
        } for candidate in eligible}
        calls = []

        async def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(usage=SimpleNamespace(), choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps({"label": "unlikely", "why": "Recent move."})))])

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            (run_dir / "jd.txt").write_text("Complete synthetic JD")
            with (mock.patch.object(search_harness, "_save"),
                  mock.patch.object(search_harness, "_price_usage_log"),
                  mock.patch.object(search_harness, "move_likelihood_messages", create=True,
                                    return_value=[{"role": "user", "content": "move"}]) as messages):
                annotated = search_harness._annotate_move_likelihood(
                    candidates=candidates, profiles=profiles,
                    results={"created_at": "2026-09-14T12:00:00Z"}, run_dir=run_dir,
                    pond_n=1, context=_context(), pond_query="Exact pond query", client=client)
        self.assertEqual(len(calls), 510)
        self.assertEqual([row["person"] for row in annotated],
                         [row["person"] for row in candidates])
        for before, after in zip(candidates, annotated):
            self.assertEqual({key: after[key] for key in before}, before)
            if before["person"].startswith("skip"):
                self.assertIsNone(after["move_likelihood"])
            else:
                self.assertEqual(after["move_likelihood"],
                                 {"label": "unlikely", "why": "Recent move."})
        self.assertEqual(messages.call_count, 510)
        for call, candidate in zip(messages.call_args_list, eligible):
            self.assertEqual(call.kwargs["candidate"], {
                **profiles[candidate["person"]], "current_company_headcount": 40,
                "current_company_stage": "seed"})
            self.assertEqual(call.kwargs["pond_query"], "Exact pond query")
            self.assertEqual(call.kwargs["jd"], "Complete synthetic JD")

    def test_move_likelihood_skips_without_reading_jd_or_creating_a_client(self) -> None:
        with mock.patch.object(search_harness, "make_async_openai_client") as client:
            self.assertEqual(search_harness._annotate_move_likelihood(
                candidates=[{"person": "p1", "score": .99}], profiles={}, results={},
                run_dir=Path("unused"), pond_n=1, context={}, pond_query="pond"),
                [{"person": "p1", "score": .99, "move_likelihood": None}])
        client.assert_not_called()

    def test_move_likelihood_resumes_shared_slots_and_preserves_usage_and_full_evidence(self) -> None:
        class Completions:
            def __init__(self):
                self.calls = []
                self.active = self.max_active = 0

            async def create(self, **kwargs):
                self.calls.append(kwargs)
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                await asyncio.sleep(.01)
                self.active -= 1
                return SimpleNamespace(
                    model="gpt-5.6-luna", service_tier="flex",
                    usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
                    choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                        "label": "plausible", "why": "The candidate described a move."})))])

        candidates = [{"person": f"p{index}", "score": .9 - index * .1,
                       "cross_encoder_score": index, "cross_encoder_status": "ok"}
                      for index in range(3)]
        profiles = {row["person"]: {
            "person_id": row["person"],
            "positions": [{"description": f"Original work {index}"} for index in range(5)],
        } for row in candidates}
        completions = Completions()
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        results = {"created_at": "2026-09-14T12:00:00Z"}
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            (run_dir / "jd.txt").write_text("Complete synthetic JD")
            with (mock.patch.object(search_harness, "_save"),
                  mock.patch.object(search_harness, "_price_usage_log") as price,
                  mock.patch.object(search_harness, "MOVE_LIKELIHOOD_CONCURRENCY", 2)):
                def annotate():
                    return search_harness._annotate_move_likelihood(
                        candidates=candidates, profiles=profiles, results=results, run_dir=run_dir,
                        pond_n=1, context=_context(), pond_query="Exact pond query", client=client)

                first = annotate()
                second = annotate()
                self.assertEqual(len(completions.calls), 3)
                self.assertTrue(all(row["cached"] for row in
                                    results["raw_model_responses"][0]["checkpoints"]))
                profiles["p0"]["positions"][4]["description"] = "Changed older original evidence"
                annotate()
                self.assertEqual(price.call_count, 3)
            records = [json.loads(path.read_text()) for path in
                       sorted((run_dir / "ponds/pond-01/move-likelihood").glob("*.json"))]
        self.assertEqual(first, second)
        self.assertEqual(len(completions.calls), 4)
        self.assertEqual(completions.max_active, 2)
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["usage"]["input_tokens"], 100)
        self.assertEqual(len(results["raw_model_responses"]), 1)
        self.assertEqual(results["raw_model_responses"][0]["kind"], "move_likelihood")
        for call in completions.calls:
            self.assertEqual(call["model"], "gpt-5.6-luna")
            self.assertEqual(call["reasoning_effort"], "medium")
            self.assertEqual(call["service_tier"], "flex")
            payload = json.loads(call["messages"][1]["content"])
            self.assertEqual(payload["as_of"], "2026-09-14")

    def test_move_likelihood_failure_does_not_penalize_qualifications_or_rebill_bad_cache(self) -> None:
        candidate = {"person": "p1", "score": .99, "cross_encoder_score": 4.25,
                     "cross_encoder_status": "ok", "trait_scores": {"Engineer": .9},
                     "fit_override": {"human": {"score": 1}, "note": "Saved human note"}}
        original = deepcopy(candidate)
        calls = []

        async def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(usage=SimpleNamespace(), choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"label":"invalid","why":""}'))])

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        results = {"created_at": "2026-09-14T12:00:00Z"}
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            (run_dir / "jd.txt").write_text("Synthetic JD")
            with (mock.patch.object(search_harness, "_save"),
                  mock.patch.object(search_harness, "_price_usage_log")):
                for _ in range(2):
                    annotated = search_harness._annotate_move_likelihood(
                        candidates=[candidate], profiles={"p1": {}}, results=results,
                        run_dir=run_dir, pond_n=1, context={}, pond_query="pond", client=client)
                client.chat.completions.create = mock.AsyncMock(side_effect=TimeoutError("Synthetic timeout"))
                failed = search_harness._annotate_move_likelihood(
                    candidates=[candidate], profiles={"p1": {}}, results=results,
                    run_dir=run_dir, pond_n=1, context={}, pond_query="Changed pond", client=client)
                self.assertEqual(failed, annotated)
                client.chat.completions.create.assert_awaited_once()
        self.assertEqual(len(calls), 1)
        self.assertEqual(candidate, original)
        self.assertEqual(annotated, [{**original, "move_likelihood": {
            "label": "unclear", "why": "Move likelihood could not be assessed."}}])
        self.assertIn("error", results["raw_model_responses"][0]["checkpoints"][0])

    def test_summary_preserves_move_judgment_across_ponds_without_group_arbitration(self) -> None:
        results = {"iterations": [
            {"pond_n": 1, "query": "First pond", "shortlist_grades": [
                {"person": "p1", "score": .95, "cross_encoder_score": -.5,
                 "cross_encoder_status": "ok", "move_likelihood": None,
                 "fit_override": {"note": "Saved human note", "human": {"score": 1}}},
                {"person": "p2", "score": .99, "move_likelihood": None},
            ]},
            {"pond_n": 2, "query": "Second pond", "shortlist_grades": [
                {"person": "p1", "score": .1, "cross_encoder_score": 0,
                 "cross_encoder_status": "ok", "move_likelihood": {
                     "label": "unlikely", "why": "Recently started the current role."}},
            ]},
        ]}
        original = deepcopy(results)
        summary = search_harness.build_search_summary(results, 0)
        self.assertEqual(results, original)
        self.assertEqual(summary["counts"], {"": 2})
        self.assertEqual([row["person"] for row in summary["groups"][""]], ["p2", "p1"])
        person = summary["groups"][""][1]
        self.assertEqual(person["rerank_score"], .95)
        self.assertEqual(person["cross_encoder_score"], -.5)
        self.assertEqual(person["move_likelihood"], {
            "label": "unlikely", "why": "Recently started the current role."})
        self.assertEqual(person["ponds"], [1, 2])
        self.assertNotIn("jd_fit_order", summary)
        self.assertNotIn("fit_experts", person)
        self.assertNotIn("jd_fit", person)

    def test_reannotate_keeps_saved_feedback_profiles_and_company_context_without_new_calls(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            results_path = _start(run_dir)
            rows_path, profiles_path = run_dir / "rows.jsonl", run_dir / "profiles.jsonl"
            rows_path.write_text(json.dumps({
                "person_id": "p1", "final_score": .99, "cross_encoder_score": -.25,
                "cross_encoder_status": "ok", "current_companies": "Acme"}) + "\n")
            profiles_path.write_text(json.dumps({
                "person_id": "p1", "positions": [
                    {"description": f"Full original role {index}", "company_name": "Acme"}
                    for index in range(5)]}) + "\n")
            feedback = {"human": {"score": 1}, "note": "Keep this exact human note"}
            labels_path = run_dir / "fit-labels.jsonl"
            labels_path.write_text(json.dumps({"person": "p1", **feedback}) + "\n")
            results = json.loads(results_path.read_text())
            results["hiring_company_context"] = {"headcount": 100, "name": "Acme"}
            results["iterations"] = [{
                "pond_n": 1, "query": "Saved exact pond", "shortlist_grades": [{
                    "person": "p1", "score": .99, "fit_override": feedback,
                    "fit_experts": {"role_fit": {"label": "strong-fit"}},
                    "jd_fit": {"coverage": 1}, "group": "send_worthy",
                    "current_company_headcount": 80,
                }], "arm": {"artifacts": {
                    "jsonl": str(rows_path), "profiles_path": str(profiles_path)}}}]
            results_path.write_text(json.dumps(results))
            originals = {path: path.read_bytes() for path in (rows_path, profiles_path, labels_path)}
            with (mock.patch.object(search_harness, "resolve_company_contexts") as companies,
                  mock.patch.object(search_harness, "make_async_openai_client") as client,
                  mock.patch.object(search_harness, "_run_command") as pipeline):
                search_harness.reannotate_saved(run_dir=run_dir, env_file="unused")
            for path, original in originals.items():
                self.assertEqual(path.read_bytes(), original)
            updated = json.loads(results_path.read_text())
        candidate = updated["iterations"][0]["shortlist_grades"][0]
        self.assertEqual(candidate["fit_override"], feedback)
        self.assertEqual(candidate["current_company_headcount"], 80)
        self.assertEqual(updated["hiring_company_context"], {"headcount": 100, "name": "Acme"})
        self.assertIsNone(candidate["move_likelihood"])
        self.assertEqual(candidate["score"], .99)
        self.assertEqual(candidate["cross_encoder_score"], -.25)
        self.assertNotIn("fit_experts", candidate)
        self.assertNotIn("jd_fit", candidate)
        self.assertNotIn("group", candidate)
        companies.assert_not_called()
        client.assert_not_called()
        pipeline.assert_not_called()

    def test_ce_eligible_without_company_ref_still_loads_hiring_context(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            results_path = _start(run_dir)
            payload_path = run_dir / "payload.json"
            payload_path.write_text(json.dumps(_payload()))
            rows_path = run_dir / "rows.jsonl"
            rows_path.write_text(json.dumps({
                "person_id": "p1", "final_score": .01,
                "cross_encoder_score": 0, "cross_encoder_status": "ok"}) + "\n")
            results = json.loads(results_path.read_text())
            results["status"] = "ready_to_run"
            results["pending_payload"] = {
                "pond_n": 1, "query": "Exact saved pond", "payload_json": str(payload_path),
                "ledger": "ledger", "limit": 1000,
            }
            results_path.write_text(json.dumps(results))

            def ensure_hiring(state):
                state["hiring_company_context"] = {"name": "Acme", "headcount": 100}

            completion = mock.AsyncMock(return_value=SimpleNamespace(
                usage=SimpleNamespace(), choices=[SimpleNamespace(message=SimpleNamespace(
                    content='{"label":"unclear","why":"Sparse current-role evidence."}'))]))
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
            with (mock.patch.object(search_harness, "_run_command", return_value={
                    "artifacts": {"jsonl": str(rows_path)}}),
                  mock.patch.object(search_harness, "current_company_ref", return_value={}),
                  mock.patch.object(search_harness, "_ensure_hiring_company_context",
                                    side_effect=ensure_hiring) as hiring,
                  mock.patch.object(search_harness, "resolve_company_contexts",
                                    return_value=([{}], {})) as companies):
                for command in (search_harness.run_pond, search_harness.reannotate_saved):
                    with self.subTest(command=command.__name__):
                        command(run_dir=run_dir, env_file="unused", client=client)
                        hiring.assert_called_once()
                        companies.assert_called_once_with([{}])
                        hiring.reset_mock()
                        companies.reset_mock()
            updated = json.loads(results_path.read_text())
        completion.assert_awaited_once()
        self.assertEqual(updated["iterations"][0]["shortlist_grades"][0]["move_likelihood"],
                         {"label": "unclear", "why": "Sparse current-role evidence."})
        payload = json.loads(completion.call_args.kwargs["messages"][1]["content"])
        self.assertEqual(payload["hiring_company"], {"name": "Acme", "headcount": 100})

    def test_review_candidates_preserves_all_rows_and_cross_encoder_fields(self) -> None:
        rows = [{"person_id": f"p{index}", "final_score": .01,
                 "cross_encoder_score": index, "cross_encoder_status": "ok",
                 "cross_encoder_score_1_to_5": 4.5, "cross_encoder_model": "trained"}
                for index in range(510)]
        candidates = search_harness._review_candidates(rows, {})
        self.assertEqual(len(candidates), len(rows))
        self.assertEqual([row["person"] for row in candidates],
                         [row["person_id"] for row in rows])
        for row, candidate in zip(rows, candidates):
            for key in ("cross_encoder_score", "cross_encoder_score_1_to_5",
                        "cross_encoder_status", "cross_encoder_model"):
                self.assertEqual(candidate[key], row[key])

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

    def test_summary_and_export_retain_unjudged_candidates(self) -> None:
        candidates = [{"person": "p1", "name": "Jordan Bravo", "score": .91,
                       "group": "", "fit_experts": {}, "jd_fit": {"traits": []}},
                      {"person": "p2", "name": "Casey Delta", "score": .85,
                       "group": "", "fit_experts": {}, "jd_fit": {"traits": []}}]
        summary = search_harness.build_search_summary({"iterations": [
            {"pond_n": 1, "shortlist_grades": candidates},
            {"pond_n": 2, "shortlist_grades": [{**candidates[0], "score": .8}]},
        ]}, 0)
        self.assertEqual(summary["deduped_candidate_count"], 2)
        self.assertEqual(summary["counts"][""], 2)
        self.assertEqual([row["rerank_score"] for row in summary["groups"][""]], [.91, .85])
        self.assertNotIn("jd_fit_order", summary)
        with tempfile.TemporaryDirectory() as raw:
            paths = search_harness.export_search_summary(summary, Path(raw))
            with Path(paths["shortlist_csv"]).open() as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual([row["Name"] for row in rows], ["Jordan Bravo", "Casey Delta"])
        self.assertEqual([row["Rationale"] for row in rows], ["", ""])

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
        self.assertEqual(summary["groups"][""][0]["rerank_score"], .82)
        self.assertEqual(summary["groups"][""][0]["runs"],
                         ["design-frame", "title-frame"])
        self.assertEqual(list(rows[0]), [
            "Rank", "Name", "LinkedIn URL", "Current Role", "Current Company",
            "Source", "Channel", "Rationale",
        ])
        self.assertEqual([row["Name"] for row in rows], ["Current", "Second"])
        self.assertEqual(relationship_rows, [])


    def test_fixed_artifacts_use_search_harness_schema(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            results_path = _start(run_dir)
            results = json.loads(results_path.read_text())
            manifest = json.loads((run_dir / "manifest.json").read_text())

        self.assertEqual(results["schema_version"], "search-harness.v1")
        self.assertEqual(results["status"], "ready_to_compile")
        self.assertEqual(results["pending_query"], results["frozen_initial_queries"][0])
        self.assertNotIn("candidate_populations", results)
        self.assertNotIn("network_floors", results)
        self.assertEqual(manifest, {
            "cost_usd": 0.0, "gt_recall": None, "jd_id": run_dir.name, "ponds_run": 0,
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

    def test_compile_pond_caps_retrieval_at_the_requested_limit(self) -> None:
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
                search_harness.compile_pond(run_dir=run_dir, env_file=str(env_file), limit=100)
            saved = json.loads((run_dir / "results.json").read_text())

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--limit") + 1], "100")
        self.assertEqual(saved["pending_payload"]["limit"], 100)

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
        edited, changes = search_harness._pattern_defaults(payload, _context())

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
                payload=payload, results=results, run_dir=run_dir,
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
                "rerank_only": False, "limit": 1000, "pattern_default_edits": [],
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

    def test_legacy_results_get_the_default_retrieval_limit(self) -> None:
        results = {
            "iterations": [
                {"pond_n": 1, "arm": {"payload_json": "p", "ledger": "l"}},
                {"pond_n": 2, "arm": {"payload_json": "p", "ledger": "l", "limit": 100}},
            ],
            "pending_payload": {"pond_n": 3, "payload_json": "p", "ledger": "l"},
        }
        scrubbed = legacy.scrub_results(results, default_limit=1000)
        self.assertEqual([row["arm"]["limit"] for row in scrubbed["iterations"]], [1000, 100])
        self.assertEqual(scrubbed["pending_payload"]["limit"], 1000)
        self.assertIsNone(legacy.scrub_results(
            {"iterations": [], "pending_payload": None}, default_limit=1000)["pending_payload"])

    def test_run_pond_executes_a_pre_limit_pending_payload_at_the_default_cap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            _start(run_dir)
            payload_path = run_dir / "ponds" / "pond-01" / "payload.json"
            payload_path.parent.mkdir(parents=True)
            payload_path.write_text(json.dumps(_payload()), encoding="utf-8")
            rows_path = run_dir / "rows.jsonl"
            rows_path.write_text(json.dumps({
                "person_id": "p1", "name": "Jordan Bravo", "final_score": .91,
                "current_titles": "Senior Software Engineer", "current_companies": "Alpha",
            }) + "\n", encoding="utf-8")
            results = json.loads((run_dir / "results.json").read_text())
            results["status"] = "ready_to_run"
            results["pending_payload"] = {  # written before compile-pond --limit existed
                "pond_n": 1, "query": results["pending_query"]["query"],
                "payload_json": str(payload_path), "ledger": "ledger", "payload": _payload(),
                "rerank_exclusions": [], "rerank_only": False, "pattern_default_edits": [],
            }
            (run_dir / "results.json").write_text(json.dumps(results), encoding="utf-8")

            with (mock.patch.object(search_harness, "_run_command", return_value={
                    "artifacts": {"jsonl": str(rows_path)},
                  }) as run, mock.patch.object(search_harness, "_ensure_hiring_company_context") as hiring,
                  mock.patch.object(search_harness, "make_async_openai_client") as fit_client,
                  mock.patch.object(search_harness, "resolve_company_contexts", return_value=(
                    [{"name": "Alpha", "headcount": 40, "stage": "SEED", "funding": 2_000_000}],
                    {"cache_hits": 1, "cache_misses": 0, "live_lookups": 0, "unresolved": 0,
                     "cost_usd": 0.0, "unit_cost_usd": 0.0,
                     "billing_basis": "unit_price_not_configured"})) as companies):
                search_harness.run_pond(run_dir=run_dir, env_file=".env")
            saved = json.loads((run_dir / "results.json").read_text())
            self.assertEqual(saved["traits"], [])

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--limit") + 1], "1000")
        self.assertEqual(saved["iterations"][0]["arm"]["limit"], 1000)
        self.assertEqual(saved["iterations"][0]["arm"]["traits"], _payload()["traits"])
        self.assertIsNone(saved["iterations"][0]["shortlist_grades"][0]["move_likelihood"])
        self.assertNotIn("fit_experts", saved["iterations"][0]["shortlist_grades"][0])
        self.assertNotIn("group", saved["iterations"][0]["shortlist_grades"][0])
        self.assertEqual(saved["iterations"][0]["shortlist_grades"][0]["score"], .91)
        self.assertIsNone(saved["brief"]["defining_capability"])
        self.assertNotIn("jd_fit_order", saved["summary"])
        self.assertFalse(hasattr(search_harness, "_jd_traits"))
        fit_client.assert_not_called()
        hiring.assert_not_called()
        companies.assert_not_called()

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
                 "cross_encoder_score": 1.25, "cross_encoder_status": "ok",
                 "current_titles": "Senior Software Engineer", "current_companies": "Alpha",
                 "trait_scores": json.dumps({"Software Engineer": {
                     "score": .9, "reason": "Built production systems."}})},
                {"person_id": "p2", "name": "Casey Delta", "final_score": .74,
                 "cross_encoder_score": -.5, "cross_encoder_status": "ok",
                 "current_titles": "Software Engineer", "current_companies": "Beta"},
                {"person_id": "p3", "name": "Morgan Echo", "final_score": .65,
                 "cross_encoder_score": 0, "cross_encoder_status": "ok",
                 "current_titles": "Engineering Manager", "current_companies": "Gamma"},
            ]), encoding="utf-8")
            results = json.loads((run_dir / "results.json").read_text())
            results["status"] = "ready_to_run"
            results["pending_payload"] = {
                "pond_n": 1, "query": results["pending_query"]["query"],
                "payload_json": str(payload_path), "ledger": "ledger", "payload": _payload(),
                "rerank_exclusions": [], "rerank_only": False, "limit": 1000,
                "pattern_default_edits": [{"pattern": "retune_seniority"}],
            }
            (run_dir / "results.json").write_text(json.dumps(results), encoding="utf-8")
            def annotate(**kwargs):
                self.assertEqual(kwargs["pond_query"], results["pending_payload"]["query"])
                return [{**dict(row), "move_likelihood": {
                         "label": "plausible", "why": "A plausible move."}}
                        for row in kwargs["candidates"]]

            with (mock.patch.object(search_harness, "_run_command", return_value={
                    "artifacts": {"jsonl": str(rows_path)},
                  }) as run, mock.patch.object(search_harness, "_ensure_hiring_company_context"),
                  mock.patch.object(search_harness, "_annotate_move_likelihood", side_effect=annotate),
                  mock.patch.object(search_harness, "resolve_company_contexts", return_value=(
                    [{"name": "Alpha", "headcount": 40, "stage": "SEED", "funding": 2_000_000},
                     {}, {"name": "Gamma", "headcount": 500, "stage": "SERIES_C", "funding": 80_000_000}],
                    {"cache_hits": 2, "cache_misses": 0, "live_lookups": 0, "unresolved": 0,
                     "cost_usd": 0.0, "unit_cost_usd": 0.0,
                     "billing_basis": "unit_price_not_configured"})) as companies):
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
        self.assertEqual(iteration["reviewed_count"], 3)
        self.assertEqual(iteration["result_count"], 3)
        self.assertFalse(iteration["below_threshold"])
        self.assertIsNone(iteration["gt_recall"])
        self.assertNotIn("strong_people", saved)
        self.assertNotIn("pool_read", iteration)
        self.assertNotIn("suggested_diagnosis", iteration["pool_stats"])
        self.assertTrue(iteration["edit_delta"]["traits_added"])
        self.assertEqual(
            iteration["shortlist_grades"][0]["move_likelihood"]["label"],
            "plausible",
        )
        self.assertIn("Software Engineer", iteration["shortlist_grades"][0]["trait_scores"])
        self.assertEqual(iteration["shortlist_grades"][0]["current_company_headcount"], 40)
        self.assertEqual([ref.get("name") for ref in companies.call_args.args[0]],
                         ["Alpha", None, "Gamma"])
        self.assertEqual([row["cross_encoder_score"] for row in iteration["shortlist_grades"]],
                         [1.25, -.5, 0])
        self.assertIsNone(iteration["shortlist_grades"][0]["company_card_id"])

    def test_run_preserves_reviewed_filters_and_enforces_set(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            _start(run_dir)
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
                "rerank_exclusions": [], "rerank_only": False, "limit": 1000, "pattern_default_edits": [],
            }
            (run_dir / "results.json").write_text(json.dumps(results))

            with mock.patch.object(search_harness, "_run_command", return_value={
                    "artifacts": {"jsonl": str(rows_path)}}), \
                 mock.patch.object(search_harness, "_ensure_hiring_company_context"):
                search_harness.run_pond(run_dir=run_dir, env_file=".env")

            reviewed = json.loads(payload_path.read_text())["role_search_filters"]

        self.assertEqual(reviewed["set_id"], "set-1")
        self.assertEqual(reviewed["years_experience_min"], 2)

    def test_local_continuation_uses_the_bound_db_instead_of_a_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            db = run_dir / "approved.duckdb"
            db.write_bytes(b"synthetic duckdb")
            stat = db.stat()
            (run_dir / "results.json").write_text(json.dumps({"retrieval": {
                "backend": "local", "db_path": str(db.resolve()),
                "db_size": stat.st_size, "db_mtime_ns": stat.st_mtime_ns,
            }}))

            set_id, resolved = search_harness._approved_retrieval(
                run_dir, "local", search_harness.DEFAULT_LOCAL_DB)

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
                "rerank_only": True, "limit": 1000, "pattern_default_edits": [],
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
                    "source": "inferred",
                    "rationale": "The reviewed pool was constrained to the wrong geography.",
                })))],
            )
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=mock.Mock(return_value=response))))
            with mock.patch.object(search_harness, "retrieve_next_moves", return_value=[]) as retrieve:
                search_harness.decide(run_dir=run_dir, choice=2, diagnosis="wrong_location", client=client)
                self.assertEqual(retrieve.call_args.kwargs["brief"], {
                    **results["brief"], "defining_capability": (run_dir / "jd.txt").read_text()})
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
        self.assertNotIn("network_floors", context)
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
                    "source": "inferred",
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
        self.assertEqual(search_harness.load_next_search_prompt(),
                         search_harness.NEXT_SEARCH_PROMPT_PATH.read_text().rstrip())
        self.assertIn("Choose one next pond", search_harness.NEXT_SEARCH_PROMPT)
        self.assertIn("highest retrieval_score card wins", search_harness.NEXT_SEARCH_PROMPT)
        self.assertIn("Keep every current location unchanged, including OR alternatives",
                      search_harness.NEXT_SEARCH_PROMPT)
        self.assertIn("user_requested_another_round", search_harness.NEXT_SEARCH_PROMPT)
        self.assertIn("Return strict JSON only with exactly diagnosis, action, next_query,",
                      search_harness.NEXT_SEARCH_PROMPT)


if __name__ == "__main__":
    unittest.main()
