"""Behavioral contract tests for typed recruiting; all model adapters are injected and offline."""
from __future__ import annotations

import asyncio
import csv
import json
import random
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.search.pipeline.frontier import CandidateFrontier, CandidateRecord, ProbeMatch
from packs.search.pipeline.models import (
    Backend, HardFilterSet, LocalCorpus, PersonFilters, PowersetCorpus, Profile, RecruitingInput,
    ResolvedSources, RoleIntent, RunnerCapabilities, SearchBounds, SearchSpec,
)
from packs.search.pipeline.recruiting import (
    _production_critic_adapter,
    _production_judge_adapter,
    _production_plan_adapter,
    _run_probes,
    _within_spend_budget,
    run_recruiting,
    SHORTLIST_CSV_FIELDS,
)
from packs.search.pipeline.recruiting_stages import (
    JudgeBudgetExceeded,
    TransientJudgeError,
    apply_deterministic_gates,
    build_review_plan,
    canonical_hash,
    judge_candidate,
    select_exemplars,
)
from packs.search.pipeline.search import run_search

JD = """Senior Backend Infrastructure Engineer
Build and own distributed systems, developer tooling, and reliable cloud infrastructure.
This is a hands-on senior individual contributor role. Candidates need demonstrated ownership
of production backend systems and operational reliability in San Francisco.
"""

EXTRACTED = {
    "job_title": "Senior Backend Infrastructure Engineer",
    "normalized_archetype": "backend infrastructure engineer",
    "hire_stage": "founding_early",
    "target_level": "senior_ic",
    "usable_cutoff": "Senior hands-on IC; current executives are out.",
    "location": "San Francisco, CA, United States",
    "location_filters": {"cities": ["San Francisco"], "countries": ["United States"]},
    "must_have": [
        {"trait": "production backend ownership", "tier": "core"},
        {"trait": "operational reliability", "tier": "core"},
    ],
    "core_groups": [
        {"name": "backend owner", "all_of": ["production backend ownership"], "source": "default"},
        {"name": "reliability owner", "all_of": ["operational reliability"], "source": "default"},
    ],
    "nice_to_have": ["developer tooling"],
    "recruiter_preferences": {},
}


def plan_adapter(jd, spec):
    assert "distributed systems" in jd
    return EXTRACTED


def critic_adapter(jd, plan, spec):
    return {"missing_core_pillars": [], "cutoff_issues": [], "other_issues": [], "verdict": "ok"}


def good_judge(candidate, plan):
    return {
        "score": 0.8,
        "jd_score": 0.8,
        "verdict": "high_potential",
        "seniority_fit": "ideal",
        "rationale": "Strong current systems evidence",
        "must_have": [
            {"trait": "production backend ownership", "status": "experienced", "evidence": "owned APIs"},
            {"trait": "operational reliability", "status": "capable", "evidence": "on-call"},
        ],
        "nice_to_have": [],
    }


def recruiting_spec(**changes):
    value = SearchSpec(
        "search.spec.v1", "recruit backend engineer", Profile.RECRUITING, Backend.LOCAL,
        LocalCorpus("/unused/fake.duckdb"),
        role=RoleIntent(titles=("Backend Infrastructure Engineer",)),
        person_filters=PersonFilters(cities=("San Francisco",), seniority_bands=("senior",), role_tracks=("ic",)),
        bounds=SearchBounds(
            20, 20, 20, max_concurrent_probes=5, per_probe_limit=10, frontier_limit=100,
            triage_threshold=5, judge_candidate_limit=5, judge_call_limit=20,
            exemplar_limit=10, expansion_thread_limit=6, epoch_limit=1, sourced_candidate_limit=100,
        ),
        recruiting=RecruitingInput(JD),
    )
    return replace(value, **changes)


class FakeRunner:
    def __init__(self, count=4, fail=(), expansion_new=False, delays=False, quarantine=()):
        self.count = count
        self.fail = set(fail)
        self.expansion_new = expansion_new
        self.delays = delays
        self.quarantine = set(quarantine)
        self.calls = []
        self.snapshot_hash = "a" * 64
        self.observed_at = "2026-07-31T00:00:00Z"

    def capabilities(self, spec):
        self.calls.append("capabilities")
        return RunnerCapabilities(Backend.LOCAL, ("cities", "seniority_bands", "role_tracks"),
                                  ("role", "summary", "company_signal"), False, True)

    def snapshot_corpus(self, scope, evidence_person_ids):
        self.calls.append("snapshot")
        return {
            "schema_version": "reflect.corpus_snapshot.v1", "backend": "local",
            "verification_status": "verified_comparable", "source": "fake",
            "set_id": "local", "operator_scope_hash": "b" * 64,
            "membership_hash": "c" * 64, "namespace_schema_hashes": {"people": "d" * 64},
            "scoped_records_hash": self.snapshot_hash, "evidence_hashes": {},
            "enumeration_complete": True, "enumeration_truncated": False,
            "enumerated_record_count": self.count, "membership_id_count": self.count,
            "observed_at": self.observed_at,
        }

    def resolve_sources(self, spec):
        self.calls.append("resolve")
        return ResolvedSources()

    def apply_hard_filters(self, spec, sources):
        self.calls.append("filter")
        return HardFilterSet(self.count, tuple(f"p{i}" for i in range(self.count)), {"before_top_k": True})

    def retrieve_people(self, plan, filters, probe_id=None, probe_family=None):
        self.calls.append(("retrieve", probe_id))
        if self.delays:
            time.sleep(random.random() / 100)
        if probe_family in self.fail or probe_id in self.fail:
            raise RuntimeError(f"failed {probe_id}")
        if probe_family == "exemplar_expansion":
            if not self.expansion_new:
                return ()
            ids = [f"new-{probe_id}"]
        else:
            ids = [f"p{i}" for i in range(self.count)]
        rows = []
        for lane_index, lane in enumerate(("role", "summary", "company_signal")):
            for index, person_id in enumerate(ids):
                score = (0.99 if person_id == "p9" else 0.7 - index / 100) - lane_index / 1000
                rows.append(CandidateRecord(
                    person_id, score, matched_position_ids=((f"pos-{probe_id}-{person_id}",) if lane == "role" else ()),
                    source_lanes=(lane,), found_by=(ProbeMatch(lane, index + 1, probe_id, probe_family, score),),
                    backend="local", structured={"position_title": "Senior Backend Engineer"},
                ))
        return tuple(rows)

    def hydrate(self, frontier):
        self.calls.append("hydrate")
        return CandidateFrontier(
            tuple(replace(
                row,
                hydrated_profile={
                    "name": f"Candidate {row.person_id}", "current_title": "Senior Backend Engineer",
                    "current_company": "Example", "city": "San Francisco",
                    "linkedin_url": f"https://linkedin.com/in/{row.person_id}",
                    "positions": [{"seniority_band": "senior", "role_track": "ic", "city": "San Francisco"}],
                },
                hydration_disposition="hydrated",
            ) for row in frontier.candidates),
            frontier.input_count, frontier.output_count, frontier.limit, frontier.truncated,
        )


class RecruitingPipelineTests(unittest.TestCase):
    def run_dir(self):
        root = Path.cwd() / ".powerpacks" / "search-runs"
        root.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(dir=root)

    def prepare(self, spec, runner, root, **kwargs):
        result = run_recruiting(spec, runner, artifact_root=root, plan_adapter=plan_adapter,
                                critic_adapter=critic_adapter, **kwargs)
        self.assertEqual(result.status, "awaiting_review")
        plan = json.loads((Path(root) / "review/plan.json").read_text())
        return replace(spec, recruiting=replace(spec.recruiting, reviewed_plan_hash=canonical_hash(plan)))

    def test_prepare_uses_real_jd_and_stops_before_source(self):
        runner = FakeRunner()
        with self.run_dir() as root:
            result = run_recruiting(recruiting_spec(), runner, artifact_root=root,
                                    plan_adapter=plan_adapter, critic_adapter=critic_adapter)
            self.assertEqual(result.status, "awaiting_review")
            self.assertEqual(runner.calls, ["capabilities", "snapshot"])
            plan = json.loads((Path(root) / "review/plan.json").read_text())
            self.assertEqual(plan["traits"]["must_have"][0]["trait"], "production backend ownership")
            self.assertEqual(plan["search_scope"]["source"], "user")
            self.assertIn("review_seconds", json.loads((Path(root) / "timings.json").read_text()))

    def test_no_unapproved_plan_model_does_not_fake_review(self):
        with self.run_dir() as root:
            result = run_recruiting(recruiting_spec(), FakeRunner(), artifact_root=root)
            self.assertEqual(result.status, "needs_input")
            self.assertFalse((Path(root) / "review/plan.json").exists())

    def test_url_html_is_extracted_not_final_url(self):
        spec = replace(recruiting_spec(), recruiting=RecruitingInput("https://jobs.example/start"))
        html = "<html><title>Role</title><body><h1>Senior Backend Engineer</h1><p>" + ("distributed systems ownership " * 8) + "</p></body></html>"
        with self.run_dir() as root:
            result = run_recruiting(spec, FakeRunner(), artifact_root=root, fetcher=lambda url: (html, "https://jobs.example/final"),
                                    plan_adapter=plan_adapter, critic_adapter=critic_adapter)
            self.assertEqual(result.status, "awaiting_review")
            source = json.loads((Path(root) / "review/source.json").read_text())
            self.assertIn("distributed systems", source["normalized_jd"])
            self.assertEqual(source["source_url"], "https://jobs.example/final")

    def test_thin_or_failed_jd_source_returns_needs_input_before_runner_work(self):
        thin = replace(recruiting_spec(), recruiting=RecruitingInput("too short"))
        runner = FakeRunner()
        result = run_recruiting(thin, runner, plan_adapter=plan_adapter, critic_adapter=critic_adapter)
        self.assertEqual(result.status, "needs_input")
        self.assertEqual(result.stage, "review")
        self.assertEqual(runner.calls, [])

        failed = replace(
            recruiting_spec(), recruiting=RecruitingInput("https://jobs.example/missing")
        )
        result = run_recruiting(
            failed,
            runner,
            fetcher=lambda url: (_ for _ in ()).throw(ValueError("fetch failed")),
            plan_adapter=plan_adapter,
            critic_adapter=critic_adapter,
        )
        self.assertEqual(result.status, "needs_input")
        self.assertIn("fetch failed", result.errors[0])
        self.assertEqual(runner.calls, [])

    def test_ashby_public_posting_uses_extracted_posting_text(self):
        spec = replace(recruiting_spec(), recruiting=RecruitingInput("https://jobs.ashbyhq.com/example/role"))
        with self.run_dir() as root, mock.patch(
            "packs.search.primitives.deep_search.fetch_jd.fetch_ashby",
            return_value=(JD, "Senior Backend Infrastructure Engineer"),
        ) as ashby, mock.patch("packs.search.primitives.deep_search.fetch_jd.fetch") as generic_fetch:
            result = run_recruiting(
                spec,
                FakeRunner(),
                artifact_root=root,
                plan_adapter=plan_adapter,
                critic_adapter=critic_adapter,
            )
            self.assertEqual(result.status, "awaiting_review")
            ashby.assert_called_once_with(spec.recruiting.source)
            generic_fetch.assert_not_called()
            source = json.loads((Path(root) / "review/source.json").read_text())
            self.assertIn("distributed systems", source["normalized_jd"])
            self.assertEqual(source["via"], "ashby_posting_api")

    def test_outside_artifact_root_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "search-runs"):
                run_recruiting(recruiting_spec(), FakeRunner(), artifact_root=root)

    def test_composition_root_rejects_var_tmp_and_other_output_escape(self):
        for root in (Path("/var/tmp/recruiting-output"), Path.cwd() / ".powerpacks" / "other-output"):
            with self.subTest(root=root), self.assertRaisesRegex(ValueError, "search-runs"):
                run_search(recruiting_spec(), output_dir=root)

    def test_fabricated_corpus_hash_rejected(self):
        spec = replace(recruiting_spec(), corpus=LocalCorpus("/unused/fake.duckdb", "f" * 64))
        with self.run_dir() as root:
            result = run_recruiting(spec, FakeRunner(), artifact_root=root,
                                    plan_adapter=plan_adapter, critic_adapter=critic_adapter)
            self.assertEqual(result.status, "needs_input")
            self.assertIn("does not match", result.errors[0])

    def test_binding_rechecks_snapshot_on_resume(self):
        runner = FakeRunner()
        with self.run_dir() as root:
            approved = self.prepare(recruiting_spec(), runner, root)
            runner.snapshot_hash = "e" * 64
            result = run_recruiting(approved, runner, artifact_root=root, judge_adapter=good_judge)
            self.assertEqual(result.status, "failed_binding")

    def test_persisted_powerset_search_spec_resumes_after_recomputed_snapshot(self):
        def offline_call(reservations, stage, model, maximum_tokens, adapter, *args):
            return adapter(*args)

        remote = replace(
            recruiting_spec(),
            backend=Backend.POWERSET,
            corpus=PowersetCorpus("set", ("operator",)),
            recruiting=replace(
                recruiting_spec().recruiting,
                plan_model="gpt-4.1",
                plan_approved=True,
            ),
        )
        namespace_rows = {
            "people": [{"id": "position-1", "base_id": "person-1"}],
            "summaries": [{"id": "person-1", "summary": "summary"}],
            "companies": [],
            "company_signals": [],
            "education": [],
            "schools": [],
        }

        async def enumerate_namespace(name, filters, attributes, *, page_size, max_results=0):
            rows = namespace_rows[name]
            return {
                "rows": rows,
                "completed": True,
                "truncated": False,
                "row_count": len(rows),
            }

        snapshot_patches = (
            mock.patch(
                "packs.search.backends.turbopuffer.runner.postgres_client.fetch_set_operator_ids",
                return_value={"set_id": "set", "operator_ids": ["operator"]},
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.postgres_client.fetch_person_rows",
                return_value=[],
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.enumerate_filter_only_rows_for_namespace",
                new=mock.AsyncMock(side_effect=enumerate_namespace),
            ),
        )
        with self.run_dir() as root:
            with (
                snapshot_patches[0],
                snapshot_patches[1],
                snapshot_patches[2],
                mock.patch(
                    "packs.search.pipeline.recruiting._production_plan_adapter",
                    new=plan_adapter,
                ),
                mock.patch(
                    "packs.search.pipeline.recruiting._production_critic_adapter",
                    new=critic_adapter,
                ),
                mock.patch(
                    "packs.search.pipeline.recruiting._SpendReservations.call",
                    new=offline_call,
                ),
            ):
                prepared = run_search(remote, output_dir=root)
            self.assertEqual(prepared.status, "awaiting_review", prepared.errors)

            persisted_path = Path(root) / "search_spec.json"
            persisted_payload = json.loads(persisted_path.read_text())
            self.assertIsNotNone(persisted_payload["corpus"]["scoped_records_hash"])
            plan = json.loads((Path(root) / "review/plan.json").read_text())
            persisted_payload["recruiting"]["reviewed_plan_hash"] = canonical_hash(plan)
            approved = SearchSpec.from_dict(persisted_payload)

            with (
                mock.patch(
                    "packs.search.backends.turbopuffer.runner.postgres_client.fetch_set_operator_ids",
                    return_value={"set_id": "set", "operator_ids": ["operator"]},
                ),
                mock.patch(
                    "packs.search.backends.turbopuffer.runner.postgres_client.fetch_person_rows",
                    return_value=[],
                ),
                mock.patch(
                    "packs.search.backends.turbopuffer.runner.storage.enumerate_filter_only_rows_for_namespace",
                    new=mock.AsyncMock(side_effect=enumerate_namespace),
                ),
                mock.patch(
                    "packs.search.pipeline.recruiting._production_judge_adapter",
                    return_value=good_judge,
                ),
                mock.patch(
                    "packs.search.backends.turbopuffer.runner.TurboPufferSearchRunner.apply_hard_filters",
                    return_value=HardFilterSet(0, (), {}),
                ),
            ):
                resumed = run_search(approved, output_dir=root)
            self.assertEqual(resumed.status, "completed_empty")

    def test_powerset_recruiting_stays_needs_input_for_unverified_snapshot(self):
        class RemoteRunner(FakeRunner):
            def capabilities(self, spec):
                self.calls.append("capabilities")
                return RunnerCapabilities(
                    Backend.POWERSET,
                    ("cities", "seniority_bands", "role_tracks"),
                    ("role", "summary"),
                    False,
                    False,
                )

            def snapshot_corpus(self, scope, evidence_person_ids):
                self.calls.append("snapshot")
                return {
                    **super().snapshot_corpus(scope, evidence_person_ids),
                    "backend": "powerset",
                    "verification_status": "unverified_non_comparable",
                    "scoped_records_hash": None,
                }

        remote = replace(
            recruiting_spec(),
            backend=Backend.POWERSET,
            corpus=PowersetCorpus("set", ("operator",)),
        )
        result = run_recruiting(
            remote,
            RemoteRunner(),
            plan_adapter=plan_adapter,
            critic_adapter=critic_adapter,
        )
        self.assertEqual(result.status, "needs_input")
        self.assertIn("verified comparable corpus snapshot", result.errors[0])

    def test_binding_ignores_observation_time_metadata(self):
        runner = FakeRunner(1)
        with self.run_dir() as root:
            approved = self.prepare(recruiting_spec(), runner, root)
            runner.observed_at = "2026-07-31T01:00:00Z"
            result = run_recruiting(approved, runner, artifact_root=root, judge_adapter=good_judge)
            self.assertNotEqual(result.status, "failed_binding")

    def test_no_deterministic_production_judge(self):
        with self.run_dir() as root:
            approved = self.prepare(recruiting_spec(), FakeRunner(), root)
            result = run_recruiting(approved, FakeRunner(), artifact_root=root)
            self.assertEqual(result.status, "needs_input")
            self.assertIn("explicit approved judge", result.errors[0])

    def test_production_plan_and_critic_use_approved_instrumented_client(self):
        configured = replace(
            recruiting_spec(),
            recruiting=replace(recruiting_spec().recruiting, plan_model="gpt-test", plan_approved=True),
        )
        create = mock.Mock(
            side_effect=[
                SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(EXTRACTED)))]
                ),
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=json.dumps({"verdict": "ok"}))
                        )
                    ]
                ),
            ]
        )
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with (
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
            mock.patch(
                "packs.search.primitives.shared.openai_client.make_openai_client",
                return_value=client,
            ) as make_client,
        ):
            extracted = _production_plan_adapter(JD, configured)
            critic = _production_critic_adapter(JD, extracted, configured)

        self.assertEqual(extracted["job_title"], EXTRACTED["job_title"])
        self.assertEqual(critic, {"verdict": "ok"})
        self.assertEqual(make_client.call_count, 2)
        self.assertEqual(create.call_args_list[0].kwargs["model"], "gpt-test")
        self.assertEqual(create.call_args_list[0].kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(create.call_args_list[0].kwargs["max_completion_tokens"], 16_000)
        self.assertEqual(create.call_args_list[1].kwargs["max_completion_tokens"], 16_000)
        self.assertIn("JOB DESCRIPTION", create.call_args_list[1].kwargs["messages"][1]["content"])

    def test_oversized_plan_and_critic_inputs_make_no_provider_call(self):
        configured = replace(
            recruiting_spec(),
            recruiting=replace(
                recruiting_spec().recruiting, plan_model="gpt-test", plan_approved=True
            ),
        )
        create = mock.Mock()
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        oversized = "evidence " * 70_000
        with (
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
            mock.patch(
                "packs.search.primitives.shared.openai_client.make_openai_client",
                return_value=client,
            ),
        ):
            with self.assertRaisesRegex(ValueError, "plan input exceeds"):
                _production_plan_adapter(oversized, configured)
            with self.assertRaisesRegex(ValueError, "critic input exceeds"):
                _production_critic_adapter(JD, {"evidence": oversized}, configured)
        create.assert_not_called()

    def test_oversized_production_review_inputs_return_typed_needs_input(self):
        oversized_spec = replace(
            recruiting_spec(),
            recruiting=replace(
                recruiting_spec().recruiting,
                source="evidence " * 70_000,
                plan_model="gpt-4.1",
                plan_approved=True,
            ),
        )
        create = mock.Mock()
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with (
            self.run_dir() as root,
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
            mock.patch(
                "packs.search.primitives.shared.openai_client.make_openai_client",
                return_value=client,
            ),
        ):
            plan_result = run_recruiting(oversized_spec, FakeRunner(), artifact_root=root)
        with (
            self.run_dir() as root,
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
            mock.patch(
                "packs.search.primitives.shared.openai_client.make_openai_client",
                return_value=client,
            ),
        ):
            critic_result = run_recruiting(
                oversized_spec,
                FakeRunner(),
                artifact_root=root,
                plan_adapter=lambda jd, value: EXTRACTED,
            )
        self.assertEqual(plan_result.status, "needs_input")
        self.assertIn("plan input exceeds", plan_result.errors[0])
        self.assertEqual(critic_result.status, "needs_input")
        self.assertIn("critic input exceeds", critic_result.errors[0])
        create.assert_not_called()

    def test_profile_evaluator_request_enforces_estimated_generation_cap(self):
        from packs.search.primitives.evaluate_profile_candidates import evaluate_profile_candidates as evaluator

        create = mock.AsyncMock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))]
            )
        )
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch.object(evaluator, "normalize_evaluation", return_value={}):
            asyncio.run(
                evaluator.evaluate_one(
                    client,
                    asyncio.Semaphore(1),
                    "gpt-test",
                    "medium",
                    {},
                    {"person_id": "p"},
                    {"current_title": "Engineer"},
                    120,
                    0,
                    max_completion_tokens=32_000,
                )
            )
        self.assertEqual(create.await_args.kwargs["max_completion_tokens"], 32_000)

    def test_oversized_profile_stays_unjudged_reviewable_without_provider_call(self):
        configured = replace(
            recruiting_spec(),
            recruiting=replace(
                recruiting_spec().recruiting,
                judge_implementation="profile_evaluator",
                judge_model="gpt-test",
                judge_approved=True,
            ),
        )
        create = mock.AsyncMock()
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
            close=mock.AsyncMock(),
        )
        candidate = CandidateRecord(
            "oversized",
            hydrated_profile={"current_title": "Engineer", "evidence": "profile " * 140_000},
        )
        with (
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
            mock.patch(
                "packs.search.primitives.shared.openai_client.make_async_openai_client",
                return_value=client,
            ),
        ):
            reviewed, attempts = judge_candidate(
                candidate, {}, _production_judge_adapter(configured), max_attempts=1
            )
        self.assertEqual(attempts, 1)
        self.assertEqual(reviewed.judge["status"], "error")
        self.assertTrue(reviewed.judge["reviewable"])
        self.assertIn("prompt ceiling", reviewed.judge["error"])
        create.assert_not_awaited()
        client.close.assert_awaited_once()

    def test_profile_evaluator_production_adapter_closes_client_and_disables_inner_retry(self):
        configured = replace(
            recruiting_spec(),
            recruiting=replace(
                recruiting_spec().recruiting,
                judge_implementation="profile_evaluator",
                judge_model="gpt-test",
                judge_approved=True,
            ),
        )
        client = SimpleNamespace(close=mock.AsyncMock())
        evaluate = mock.AsyncMock(return_value={"jd_score": 0.75, "error": None})
        with (
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
            mock.patch(
                "packs.search.primitives.shared.openai_client.make_async_openai_client",
                return_value=client,
            ),
            mock.patch(
                "packs.search.primitives.evaluate_profile_candidates.evaluate_profile_candidates.evaluate_one",
                new=evaluate,
            ),
        ):
            adapter = _production_judge_adapter(configured)
            result = adapter(CandidateRecord("person", hydrated_profile={"current_title": "Engineer"}), {})

        self.assertEqual(result["score"], 0.75)
        self.assertEqual(result["implementation"], "profile_evaluator")
        self.assertEqual(evaluate.await_count, 1)
        self.assertEqual(evaluate.await_args.args[8], 0)
        self.assertEqual(evaluate.await_args.kwargs["max_completion_tokens"], 32_000)
        self.assertEqual(evaluate.await_args.kwargs["max_prompt_tokens"], 128_000)
        client.close.assert_awaited_once()

    def test_codex_production_adapter_normalizes_valid_json_and_rejects_invalid_json(self):
        configured = replace(
            recruiting_spec(),
            recruiting=replace(
                recruiting_spec().recruiting,
                judge_implementation="codex",
                judge_model="codex-test",
                judge_approved=True,
            ),
        )
        candidate = CandidateRecord("person", hydrated_profile={"current_title": "Engineer"})
        normalized = {"jd_score": 0.72, "verdict": "high_potential", "seniority_fit": "ideal"}
        with (
            mock.patch(
                "packs.search.primitives.deep_search.codex_judge.judge_one",
                return_value=({"seniority_fit": "ideal"}, None),
            ) as judge_one,
            mock.patch(
                "packs.search.primitives.evaluate_profile_candidates.evaluate_profile_candidates.normalize_evaluation",
                return_value=normalized,
            ) as normalize,
        ):
            result = _production_judge_adapter(configured)(candidate, {"traits": {}})
        self.assertEqual(result["score"], 0.72)
        self.assertEqual(result["implementation"], "codex")
        self.assertEqual(judge_one.call_args.args[1:3], ("codex-test", "medium"))
        normalize.assert_called_once()

        with mock.patch(
            "packs.search.primitives.deep_search.codex_judge.judge_one",
            return_value=({}, "empty_or_unparsable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "empty_or_unparsable"):
                _production_judge_adapter(configured)(candidate, {"traits": {}})

        with (
            mock.patch(
                "packs.search.primitives.deep_search.codex_judge.judge_one",
                side_effect=[
                    ({}, "request timed out"),
                    ({"seniority_fit": "ideal"}, None),
                ],
            ) as judge_one,
            mock.patch(
                "packs.search.primitives.evaluate_profile_candidates.evaluate_profile_candidates.normalize_evaluation",
                return_value=normalized,
            ),
        ):
            retried, calls = judge_candidate(
                candidate, {"traits": {}}, _production_judge_adapter(configured)
            )
        self.assertEqual(calls, 2)
        self.assertEqual(judge_one.call_count, 2)
        self.assertEqual(retried.judge["status"], "judged")

    def test_codex_run_counts_judgment_without_provider_usage_reconciliation(self):
        configured = replace(
            recruiting_spec(),
            bounds=replace(
                recruiting_spec().bounds,
                judge_candidate_limit=1,
                judge_call_limit=1,
                spend_limit_usd=0.000001,
            ),
            recruiting=replace(
                recruiting_spec().recruiting,
                judge_implementation="codex",
                judge_model="codex-test",
                judge_approved=True,
            ),
        )
        with self.run_dir() as root:
            approved = self.prepare(configured, FakeRunner(1), root)
            with (
                mock.patch(
                    "packs.search.primitives.deep_search.codex_judge.judge_one",
                    return_value=({"seniority_fit": "ideal"}, None),
                ) as judge_one,
                mock.patch(
                    "packs.search.primitives.evaluate_profile_candidates.evaluate_profile_candidates.normalize_evaluation",
                    side_effect=lambda parsed, plan, profile: good_judge(
                        CandidateRecord("p0", hydrated_profile=profile), plan
                    ),
                ),
            ):
                result = run_recruiting(approved, FakeRunner(1), artifact_root=root)

            self.assertFalse((Path(root) / "usage.jsonl").exists())
        self.assertEqual(judge_one.call_count, 1)
        self.assertEqual(result.counts["judge_calls"], 1)
        self.assertEqual(result.counts["unjudged"], 0)
        self.assertEqual(result.status, "completed_no_anchors")
        self.assertEqual(result.frontier.candidates[0].judge["implementation"], "codex")

    def test_verdict_out_and_invalid_unknown_seniority_fail_shortlist(self):
        with self.run_dir() as root:
            approved = self.prepare(recruiting_spec(), FakeRunner(2), root)
            def judge(candidate, plan):
                row = dict(good_judge(candidate, plan))
                if candidate.person_id == "p0": row["verdict"] = "out"
                if candidate.person_id == "p1": row["seniority_fit"] = "unknown"
                return row
            result = run_recruiting(approved, FakeRunner(2), artifact_root=root, judge_adapter=judge)
            self.assertFalse(any(row.deterministic_gates.get("shortlist") for row in result.frontier.candidates))
            with (Path(root) / "shortlist.csv").open() as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])

    def test_explicit_valid_unknown_seniority_is_shortlisted_but_not_sendable(self):
        plan = build_review_plan(
            recruiting_spec(), EXTRACTED, created_at="2026-07-31T00:00:00Z"
        )
        base = CandidateRecord(
            "unknown-seniority",
            hydrated_profile={"current_title": "Senior Backend Engineer"},
            hard_filter_evidence={"disposition": "accepted"},
        )
        valid = replace(
            base,
            judge={
                **good_judge(base, plan),
                "status": "judged",
                "seniority_fit": "unknown",
                "_seniority_assessment_valid": True,
            },
        )
        gated = apply_deterministic_gates(valid, plan, score_floor=0.4, sendable_score=0.55)
        self.assertTrue(gated.deterministic_gates["seniority_track"])
        self.assertTrue(gated.deterministic_gates["shortlist"])
        self.assertFalse(gated.deterministic_gates["sendable"])

        invalid = replace(valid, judge={**valid.judge, "_seniority_assessment_valid": False})
        gated = apply_deterministic_gates(invalid, plan, score_floor=0.4, sendable_score=0.55)
        self.assertFalse(gated.deterministic_gates["seniority_track"])
        self.assertFalse(gated.deterministic_gates["shortlist"])

    def test_canonical_shortlist_csv_is_safe_shareable_exporter_equivalent(self):
        with self.run_dir() as root:
            approved = self.prepare(recruiting_spec(), FakeRunner(1), root)
            result = run_recruiting(
                approved, FakeRunner(1), artifact_root=root, judge_adapter=good_judge
            )
            self.assertEqual(result.status, "completed_no_anchors")
            with (Path(root) / "shortlist.csv").open() as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
            self.assertEqual(tuple(reader.fieldnames or ()), SHORTLIST_CSV_FIELDS)
            self.assertEqual(rows[0]["Name"], "Candidate p0")
            self.assertEqual(rows[0]["LinkedIn URL"], "https://linkedin.com/in/p0")
            self.assertEqual(rows[0]["Current Title"], "Senior Backend Engineer")
            self.assertEqual(rows[0]["Current Company"], "Example")
            self.assertEqual(rows[0]["Location"], "San Francisco")
            self.assertEqual(rows[0]["Verdict"], "high_potential")
            self.assertEqual(rows[0]["Seniority Fit"], "ideal")
            self.assertEqual(rows[0]["Rationale"], "Strong current systems evidence")
            self.assertEqual(rows[0]["Source/Channels"], "local|role|summary|company_signal")
            self.assertNotIn("person_id", {key.casefold() for key in rows[0]})

    def test_founder_policy_only_gates_non_exec_ic_targets(self):
        extracted = {**EXTRACTED, "target_level": "manager"}
        plan = build_review_plan(
            recruiting_spec(),
            extracted,
            created_at="2026-07-31T00:00:00Z",
        )
        candidate = CandidateRecord(
            "founder-manager",
            hydrated_profile={"current_title": "Founder and CEO"},
            hard_filter_evidence={"disposition": "accepted"},
            judge={**good_judge(CandidateRecord("founder-manager"), plan), "status": "judged"},
        )
        gated = apply_deterministic_gates(candidate, plan, score_floor=0.4, sendable_score=0.55)
        self.assertTrue(gated.deterministic_gates["founder_c_suite_hireable"])
        self.assertTrue(gated.deterministic_gates["shortlist"])

    def test_order_independent_of_future_timing_and_summary_survives(self):
        orders = []
        for _ in range(3):
            with self.run_dir() as root:
                approved = self.prepare(recruiting_spec(), FakeRunner(4), root)
                result = run_recruiting(approved, FakeRunner(4, delays=True), artifact_root=root, judge_adapter=good_judge)
                orders.append([row.person_id for row in result.frontier.candidates])
                self.assertIn("summary", result.frontier.candidates[0].source_lanes)
        self.assertEqual(orders[0], orders[1])
        self.assertEqual(orders[1], orders[2])

    def test_per_probe_limit_is_total_across_lanes(self):
        class LaneDistinctRunner(FakeRunner):
            def retrieve_people(self, plan, filters, probe_id=None, probe_family=None):
                return tuple(
                    CandidateRecord(
                        f"{lane}-person",
                        1.0 - index / 10,
                        source_lanes=(lane,),
                        found_by=(ProbeMatch(lane, 1, probe_id, probe_family),),
                    )
                    for index, lane in enumerate(("role", "summary", "company_signal"))
                )

        bounded = replace(
            recruiting_spec(), bounds=replace(recruiting_spec().bounds, per_probe_limit=1)
        )
        runner = LaneDistinctRunner()
        rows, failures = _run_probes(
            bounded,
            runner,
            runner.capabilities(bounded),
            ResolvedSources(),
            HardFilterSet(3, (), {}),
            ({"probe_id": "one", "probe_family": "title_function", "query": "engineer"},),
        )
        self.assertEqual(failures, [])
        self.assertEqual(len({row.person_id for row in rows}), 1)

    def test_unique_sourcing_budget_applies_before_initial_judging(self):
        bounded = replace(
            recruiting_spec(),
            bounds=replace(
                recruiting_spec().bounds,
                sourced_candidate_limit=1,
                per_probe_limit=10,
            ),
        )
        with self.run_dir() as root:
            approved = self.prepare(bounded, FakeRunner(4), root)
            result = run_recruiting(
                approved, FakeRunner(4), artifact_root=root, judge_adapter=good_judge
            )
        self.assertEqual(result.counts["total_sourced"], 1)
        self.assertEqual(result.status, "completed_capped")
        self.assertEqual(len(result.frontier.candidates), 1)
        self.assertLessEqual(result.counts["judge_calls"], 1)

    def test_expansion_judge_cap_retains_remaining_net_new_as_reviewable(self):
        bounded = replace(
            recruiting_spec(),
            bounds=replace(
                recruiting_spec().bounds,
                judge_candidate_limit=11,
                judge_call_limit=20,
                sourced_candidate_limit=100,
            ),
        )
        with self.run_dir() as root:
            approved = self.prepare(bounded, FakeRunner(10), root)
            result = run_recruiting(
                approved,
                FakeRunner(10, expansion_new=True),
                artifact_root=root,
                judge_adapter=good_judge,
            )
        self.assertEqual(result.status, "completed_capped")
        self.assertEqual(result.counts["total_sourced"], 16)
        self.assertEqual(len(result.frontier.candidates), 16)
        self.assertEqual(result.counts["unjudged"], 5)

    def test_frontier_limit_never_drops_accepted_expansion_candidate(self):
        bounded = replace(
            recruiting_spec(),
            bounds=replace(
                recruiting_spec().bounds,
                per_probe_limit=10,
                frontier_limit=1,
                judge_candidate_limit=11,
                expansion_thread_limit=1,
                sourced_candidate_limit=20,
            ),
        )
        with self.run_dir() as root:
            approved = self.prepare(bounded, FakeRunner(10), root)
            result = run_recruiting(
                approved,
                FakeRunner(10, expansion_new=True),
                artifact_root=root,
                judge_adapter=good_judge,
            )
            persisted = json.loads((Path(root) / "candidate-frontier.json").read_text())
        self.assertEqual(result.counts["total_sourced"], 11)
        self.assertEqual(len(result.frontier.candidates), 11)
        self.assertEqual(len(persisted["candidates"]), 11)

    def test_expansion_requires_ten_and_caps_at_twenty_strong_exemplars(self):
        def strong(index):
            return CandidateRecord(
                f"p{index:02d}",
                deterministic_score=1.0 - index / 100,
                deterministic_gates={"shortlist": True},
                hydrated_profile={
                    "current_company": f"company-{index}",
                    "current_title": f"title-{index}",
                },
            )

        for count in (1, 9):
            self.assertEqual(select_exemplars(tuple(strong(i) for i in range(count)), 20), ())
        self.assertEqual(len(select_exemplars(tuple(strong(i) for i in range(10)), 20)), 10)
        self.assertEqual(len(select_exemplars(tuple(strong(i) for i in range(25)), 25)), 20)

    def test_total_sourced_counts_raw_unique_people_before_presentation_limit(self):
        bounded = replace(
            recruiting_spec(),
            bounds=replace(
                recruiting_spec().bounds,
                frontier_limit=1,
                judge_candidate_limit=4,
                sourced_candidate_limit=10,
            ),
        )
        with self.run_dir() as root:
            approved = self.prepare(bounded, FakeRunner(4), root)
            result = run_recruiting(
                approved, FakeRunner(4), artifact_root=root, judge_adapter=good_judge
            )
        self.assertEqual(result.counts["total_sourced"], 4)
        self.assertEqual(len(result.frontier.candidates), 4)

    def test_strong_candidate_beyond_position_limit_is_judged_and_ranked(self):
        spec = replace(recruiting_spec(), bounds=replace(recruiting_spec().bounds, judge_candidate_limit=3))
        judged = []
        with self.run_dir() as root:
            approved = self.prepare(spec, FakeRunner(10), root)
            def judge(candidate, plan):
                judged.append(candidate.person_id)
                return good_judge(candidate, plan)
            result = run_recruiting(approved, FakeRunner(10), artifact_root=root, judge_adapter=judge)
            self.assertIn("p9", judged)
            self.assertEqual(result.frontier.candidates[0].person_id, "p9")

    def test_all_expansion_failures_fail_source(self):
        spec = replace(recruiting_spec(), bounds=replace(recruiting_spec().bounds, judge_candidate_limit=20))
        with self.run_dir() as root:
            approved = self.prepare(spec, FakeRunner(12), root)
            runner = FakeRunner(12, fail={f"expansion-{i:02d}" for i in range(1, 7)})
            result = run_recruiting(approved, runner, artifact_root=root, judge_adapter=good_judge)
            self.assertEqual(result.status, "failed_source")

    def test_partial_expansion_failure_continues(self):
        spec = replace(recruiting_spec(), bounds=replace(recruiting_spec().bounds, judge_candidate_limit=20))
        with self.run_dir() as root:
            approved = self.prepare(spec, FakeRunner(12), root)
            result = run_recruiting(approved, FakeRunner(12, fail={"expansion-01"}), artifact_root=root,
                                    judge_adapter=good_judge)
            self.assertNotEqual(result.status, "failed_source")

    def test_quarantined_audit_reconciles_hydration_input(self):
        class QuarantineRunner(FakeRunner):
            def hydrate(self, frontier):
                hydrated = super().hydrate(frontier)
                rows = list(hydrated.candidates)
                rows[0] = replace(rows[0], hydrated_profile={"current_title": "Unknown"})
                return replace(hydrated, candidates=tuple(rows))
        with self.run_dir() as root:
            approved = self.prepare(recruiting_spec(), QuarantineRunner(2), root)
            result = run_recruiting(approved, QuarantineRunner(2), artifact_root=root, judge_adapter=good_judge)
            audit = result.hard_filter_validation
            self.assertEqual(audit["reviewed_count"], 2)
            self.assertEqual(audit["violation_count"], 1)

    def test_injected_usage_is_not_falsely_priced(self):
        with self.run_dir() as root:
            approved = self.prepare(recruiting_spec(), FakeRunner(1), root)
            result = run_recruiting(approved, FakeRunner(1), artifact_root=root, judge_adapter=good_judge)
            usage = json.loads((Path(root) / "usage.json").read_text())
            timings = json.loads((Path(root) / "timings.json").read_text())
            self.assertIsNone(usage["cost_usd"])
            self.assertFalse(usage["fully_priced"])
            self.assertEqual(usage["provider_model_calls"], 0)
            self.assertGreater(usage["injected_adapter_calls"], 0)
            self.assertEqual(result.hard_filter_validation["reviewed_count"], 1)
            self.assertIn("source_hydrate_seconds", timings)
            self.assertIn("judge_seconds", timings)

    def test_one_transient_retry_persistent_error_stays_unjudged(self):
        attempts = {}
        def flaky(candidate, plan):
            attempts[candidate.person_id] = attempts.get(candidate.person_id, 0) + 1
            if attempts[candidate.person_id] == 1: raise TransientJudgeError("temporary")
            return good_judge(candidate, plan)
        with self.run_dir() as root:
            approved = self.prepare(recruiting_spec(), FakeRunner(1), root)
            result = run_recruiting(approved, FakeRunner(1), artifact_root=root, judge_adapter=flaky)
            self.assertEqual(result.frontier.candidates[0].judge["attempts"], 2)
        def broken(candidate, plan): raise TransientJudgeError("still broken")
        with self.run_dir() as root:
            approved = self.prepare(recruiting_spec(), FakeRunner(1), root)
            result = run_recruiting(approved, FakeRunner(1), artifact_root=root, judge_adapter=broken)
            self.assertEqual(result.frontier.candidates[0].judge["status"], "error")
            self.assertFalse(result.frontier.candidates[0].deterministic_gates.get("shortlist"))

        attempts = 0
        def transient(candidate, plan):
            nonlocal attempts
            attempts += 1
            raise TransientJudgeError("retry")
        checks = iter((True, False))
        with self.assertRaises(JudgeBudgetExceeded):
            from packs.search.pipeline.recruiting_stages import judge_candidate

            judge_candidate(
                CandidateRecord("budgeted"),
                {},
                transient,
                before_attempt=lambda: next(checks),
            )
        self.assertEqual(attempts, 1)

    def test_spend_budget_prices_usage_and_fails_closed_on_unpriced_rows(self):
        bounds = replace(recruiting_spec().bounds, spend_limit_usd=1.0)
        self.assertFalse(_within_spend_budget(None, bounds))
        with self.run_dir() as root:
            usage = Path(root) / "usage.jsonl"
            usage.write_text(
                json.dumps(
                    {
                        "model": "gpt-4.1",
                        "stage": "recruiting_judge",
                        "prompt_tokens": 1_000_000,
                        "completion_tokens": 0,
                        "reasoning_tokens": 0,
                        "latency_ms": 1,
                    }
                )
                + "\n"
            )
            self.assertFalse(_within_spend_budget(Path(root), bounds))
            usage.write_text(
                json.dumps(
                    {
                        "model": "unpriced-model",
                        "stage": "recruiting_judge",
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "reasoning_tokens": 0,
                        "latency_ms": 1,
                    }
                )
                + "\n"
            )
            self.assertFalse(_within_spend_budget(Path(root), bounds))

    def test_production_spend_cap_stops_before_next_judge_and_retains_unjudged(self):
        configured = replace(
            recruiting_spec(),
            bounds=replace(recruiting_spec().bounds, spend_limit_usd=0.000001),
            recruiting=replace(
                recruiting_spec().recruiting,
                judge_implementation="profile_evaluator",
                judge_model="gpt-4.1",
                judge_approved=True,
            ),
        )
        with self.run_dir() as root:
            approved = self.prepare(configured, FakeRunner(2), root)
            calls = 0

            def paid(candidate, plan):
                nonlocal calls
                calls += 1
                Path(root, "usage.jsonl").write_text(
                    json.dumps(
                        {
                            "model": "gpt-4.1",
                            "stage": "recruiting_judge",
                            "prompt_tokens": 1,
                            "completion_tokens": 0,
                            "reasoning_tokens": 0,
                            "latency_ms": 1,
                        }
                    )
                    + "\n"
                )
                return good_judge(candidate, plan)

            with mock.patch(
                "packs.search.pipeline.recruiting._production_judge_adapter",
                return_value=paid,
            ):
                result = run_recruiting(approved, FakeRunner(2), artifact_root=root)

        self.assertEqual(calls, 0)
        self.assertEqual(result.status, "completed_capped")
        self.assertEqual(result.counts["unjudged"], 2)
        self.assertEqual(len(result.frontier.candidates), 2)

    def test_production_judge_missing_usage_fails_closed_after_reserved_call(self):
        configured = replace(
            recruiting_spec(),
            bounds=replace(recruiting_spec().bounds, spend_limit_usd=10.0),
            recruiting=replace(
                recruiting_spec().recruiting,
                judge_implementation="profile_evaluator",
                judge_model="gpt-4.1",
                judge_approved=True,
            ),
        )
        with self.run_dir() as root:
            approved = self.prepare(configured, FakeRunner(2), root)
            paid = mock.Mock(side_effect=good_judge)
            with mock.patch(
                "packs.search.pipeline.recruiting._production_judge_adapter",
                return_value=paid,
            ):
                result = run_recruiting(approved, FakeRunner(2), artifact_root=root)
        self.assertEqual(paid.call_count, 1)
        self.assertEqual(result.status, "completed_capped")
        self.assertEqual(result.counts["unjudged"], 2)

    def test_plan_and_critic_spend_checks_run_before_production_adapters(self):
        configured = replace(
            recruiting_spec(),
            bounds=replace(recruiting_spec().bounds, spend_limit_usd=0.0001),
            recruiting=replace(
                recruiting_spec().recruiting,
                plan_model="gpt-4.1",
                plan_approved=True,
            ),
        )
        with mock.patch(
            "packs.search.pipeline.recruiting._production_plan_adapter"
        ) as production_plan:
            result = run_recruiting(configured, FakeRunner())
        self.assertEqual(result.status, "needs_input")
        production_plan.assert_not_called()

        with self.run_dir() as root, mock.patch(
            "packs.search.pipeline.recruiting._production_critic_adapter"
        ) as production_critic:
            result = run_recruiting(
                configured,
                FakeRunner(),
                artifact_root=root,
                plan_adapter=plan_adapter,
            )
        self.assertEqual(result.status, "needs_input")
        production_critic.assert_not_called()


if __name__ == "__main__":
    unittest.main()
