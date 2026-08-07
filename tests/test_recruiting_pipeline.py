"""Behavioral contract tests for typed recruiting; all model adapters are injected and offline."""
from __future__ import annotations

import asyncio
import csv
import json
import random
import tempfile
import time
import unittest

import jsonschema
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.search.pipeline.frontier import CandidateFrontier, CandidateRecord, ProbeMatch
from packs.search.pipeline.artifacts import persist_result
from packs.search.pipeline.models import (
    Backend, DEFAULT_JUDGE_MODEL, DEFAULT_JUDGE_REASONING_EFFORT, HardFilterSet, LocalCorpus,
    PersonFilters, PowersetCorpus, Profile, RecruitingInput, ResolvedSources, RoleIntent,
    RunnerCapabilities, SearchBounds, SearchSpec,
)
from packs.search.pipeline.recruiting import (
    DEFAULT_PLAN_MODEL,
    JUDGE_CALL_MAX_TOKENS,
    PLAN_EXTRACTION_ATTEMPTS,
    _production_critic_adapter,
    _production_judge_adapter,
    _production_plan_adapter,
    _run_probes,
    _SpendReservations,
    _validate_hydrated,
    _within_spend_budget,
    plan_model,
    run_recruiting as production_run_recruiting,
    shortlist_csv_row,
    SHORTLIST_CSV_FIELDS,
)
from packs.search.primitives.deep_search.build_eval_inputs import (
    MAX_MUST_HAVE,
    MAX_NICE_TO_HAVE,
    PLAN_EXTRACTION_SCHEMA,
    PLAN_RESPONSE_FORMAT,
    PLAN_SCHEMA,
    PLAN_SYSTEM,
    VALID_TARGET_LEVELS,
    VALID_TIERS,
)
from packs.search.primitives.deep_search import recruiter_policy
from packs.search.primitives.evaluate_profile_candidates import (
    evaluate_profile_candidates as profile_evaluator,
)
from packs.search.pipeline.stage_membership import build_stage_membership
from packs.search.pipeline.recruiting_stages import (
    CONTRACT_MESSAGE_CHARS,
    JudgeBudgetExceeded,
    PlanContractError,
    TransientJudgeError,
    apply_deterministic_gates,
    build_review_plan,
    canonical_hash,
    judge_candidate,
    select_exemplars,
    validate_review_plan,
)
from packs.search.pipeline.search import run_search

TEST_POWERPACKS_ROOT = (Path.cwd() / ".powerpacks").resolve()
TEST_SEARCH_RUNS_ROOT = TEST_POWERPACKS_ROOT / "search-runs"


def run_recruiting(*args, **kwargs):
    """Run production recruiting with this fixture's explicit private root."""
    if kwargs.get("artifact_root") is not None:
        kwargs.setdefault("allowed_artifact_root", TEST_SEARCH_RUNS_ROOT)
    return production_run_recruiting(*args, **kwargs)


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


OVER_CAP_EXTRACTED = {
    **EXTRACTED,
    "must_have": [
        *EXTRACTED["must_have"],
        *(
            {"trait": f"synthetic capability {index}", "tier": "table_stakes"}
            for index in range(MAX_MUST_HAVE)
        ),
    ],
}


def plan_adapter(jd, spec, repair=None):
    assert "distributed systems" in jd
    assert repair is None
    return EXTRACTED


class ScriptedPlanAdapter:
    """Injected plan adapter that answers each attempt in order and records the repair it saw."""

    def __init__(self, *responses):
        self.responses = responses
        self.calls: list[str | None] = []

    def __call__(self, jd, spec, repair):
        self.calls.append(repair)
        return self.responses[min(len(self.calls), len(self.responses)) - 1]


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

    def snapshot_corpus(self, scope, evidence_person_ids, *, spec=None):
        self.calls.append("snapshot")
        evidence = {
            person_id: canonical_hash({"person_id": person_id})
            for person_id in evidence_person_ids
        }
        return {
            "schema_version": "reflect.corpus_snapshot.v2", "backend": "local",
            "verification_status": "verified_comparable", "source": "local_deterministic_snapshot",
            "set_id": "local", "operator_scope_hash": "b" * 64,
            "membership_hash": "c" * 64, "namespace_schema_hashes": {"people": "d" * 64},
            "scoped_records_hash": self.snapshot_hash, "evidence_hashes": evidence,
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

    def test_review_pool_missing_or_substituted_evidence_fails_closed(self):
        requested = ("synthetic-reviewed-person",)
        run_spec = recruiting_spec(recruiting=replace(
            recruiting_spec().recruiting, review_pool_person_ids=requested
        ))
        runner = FakeRunner()
        for evidence in ({}, {requested[0]: "a" * 64, "synthetic-substitute": "b" * 64}):
            snapshot = runner.snapshot_corpus("local", requested)
            snapshot["evidence_hashes"] = evidence
            result = run_recruiting(
                run_spec,
                runner,
                plan_adapter=plan_adapter,
                critic_adapter=critic_adapter,
                corpus_snapshot=snapshot,
            )
            self.assertEqual(result.status, "failed_binding")
            self.assertIn("exactly match", result.errors[0])

    def test_intermediate_v1_corpus_snapshot_fails_binding(self):
        runner = FakeRunner()
        snapshot = runner.snapshot_corpus("local", ())
        snapshot["schema_version"] = "reflect.corpus_snapshot.v1"

        result = run_recruiting(
            recruiting_spec(),
            runner,
            plan_adapter=plan_adapter,
            critic_adapter=critic_adapter,
            corpus_snapshot=snapshot,
        )

        self.assertEqual(result.status, "failed_binding")
        self.assertIn("unsupported corpus snapshot schema", result.errors[0])

    def test_review_pool_drift_on_resume_fails_binding(self):
        first_ids = ("synthetic-reviewed-person-a",)
        first = recruiting_spec(recruiting=replace(
            recruiting_spec().recruiting, review_pool_person_ids=first_ids
        ))
        runner = FakeRunner()
        with self.run_dir() as root:
            approved = self.prepare(
                first,
                runner,
                root,
                corpus_snapshot=runner.snapshot_corpus("local", first_ids),
            )
            drifted_ids = ("synthetic-reviewed-person-b",)
            drifted = replace(
                approved,
                recruiting=replace(approved.recruiting, review_pool_person_ids=drifted_ids),
            )
            result = run_recruiting(
                drifted,
                runner,
                artifact_root=root,
                judge_adapter=good_judge,
                corpus_snapshot=runner.snapshot_corpus("local", drifted_ids),
            )
        self.assertEqual(result.status, "failed_binding")
        self.assertIn("binding drifted", result.errors[0])

    def test_missing_profile_is_not_hydrated_or_hard_filtered(self):
        spec = recruiting_spec()
        source = CandidateRecord(
            "missing",
            found_by=(ProbeMatch("role", 1, "probe", "title", 0.5),),
            hydration_disposition="missing_profile",
            hydrated_profile={"name": "stale placeholder"},
        )
        survivors, reviewed = _validate_hydrated(
            spec, ResolvedSources(), CandidateFrontier((source,), 1, 1, None, False)
        )
        self.assertEqual(survivors, ())
        self.assertEqual(reviewed[0].hard_filter_evidence, {})
        membership = build_stage_membership(
            sourced=[source], hydrated=list(reviewed), triaged=[], ranked=(),
            shortlist_person_ids=set(), status="completed_empty", epochs=0, bounds=spec.bounds,
        )
        row = membership.candidates[0]
        self.assertFalse(row.hydrated)
        self.assertFalse(row.hard_filter_passed)
        self.assertEqual(row.disposition, "hydration_missing")

    def test_zero_eligible_pool_persists_scoreable_empty_contract(self):
        class EmptyPoolRunner(FakeRunner):
            def apply_hard_filters(self, spec, sources):
                self.calls.append("filter")
                return HardFilterSet(0, (), {"before_top_k": True})

        runner = EmptyPoolRunner(0)
        requested = ("synthetic-gt-miss",)
        run_spec = recruiting_spec(recruiting=replace(
            recruiting_spec().recruiting, review_pool_person_ids=requested
        ))
        snapshot = runner.snapshot_corpus("local", requested)
        with self.run_dir() as root:
            prepared = run_recruiting(
                run_spec, runner, artifact_root=root, plan_adapter=plan_adapter,
                critic_adapter=critic_adapter, corpus_snapshot=snapshot,
            )
            plan = json.loads((Path(root) / "review/plan.json").read_text())
            approved = replace(
                run_spec,
                recruiting=replace(run_spec.recruiting, reviewed_plan_hash=canonical_hash(plan)),
            )
            result = run_recruiting(
                approved, runner, artifact_root=root, judge_adapter=good_judge,
                corpus_snapshot=snapshot,
            )
            self.assertEqual(result.status, "completed_empty")
            persist_result(root, approved, result, allowed_root=TEST_POWERPACKS_ROOT)
            membership = json.loads((Path(root) / "stage-membership.json").read_text())
            frontier = json.loads((Path(root) / "candidate-frontier.json").read_text())
            manifest = json.loads((Path(root) / "manifest.json").read_text())
            self.assertEqual(membership["candidates"], [])
            self.assertEqual(frontier["candidates"], [])
            self.assertFalse(frontier["truncated"])
            self.assertTrue({"stage-membership.json", "candidate-frontier.json",
                             "hard_filter_validation_json", "review_evidence_json"}.issubset(
                                 manifest["artifacts"]
                             ))

    def test_successful_empty_probes_persist_scoreable_empty_contract(self):
        class EmptyProbeRunner(FakeRunner):
            def retrieve_people(self, plan, filters, probe_id=None, probe_family=None):
                self.calls.append(("retrieve", probe_id))
                return ()

        runner = EmptyProbeRunner(1)
        requested = ("synthetic-gt-miss",)
        run_spec = recruiting_spec(recruiting=replace(
            recruiting_spec().recruiting, review_pool_person_ids=requested
        ))
        snapshot = runner.snapshot_corpus("local", requested)
        with self.run_dir() as root:
            prepared = run_recruiting(
                run_spec, runner, artifact_root=root, plan_adapter=plan_adapter,
                critic_adapter=critic_adapter, corpus_snapshot=snapshot,
            )
            plan = json.loads((Path(root) / "review/plan.json").read_text())
            approved = replace(
                run_spec,
                recruiting=replace(run_spec.recruiting, reviewed_plan_hash=canonical_hash(plan)),
            )
            result = run_recruiting(
                approved, runner, artifact_root=root, judge_adapter=good_judge,
                corpus_snapshot=snapshot,
            )
            self.assertEqual(result.status, "completed_empty")
            self.assertEqual(result.counts["probe_failures"], 0)
            persist_result(root, approved, result, allowed_root=TEST_POWERPACKS_ROOT)
            self.assertTrue((Path(root) / "manifest.json").exists())
            self.assertEqual(json.loads((Path(root) / "candidate-frontier.json").read_text())["candidates"], [])

    def test_membership_producer_rejects_contradictory_deterministic_gates(self):
        with self.run_dir() as root:
            approved = self.prepare(recruiting_spec(), FakeRunner(1), root)
            result = run_recruiting(
                approved, FakeRunner(1), artifact_root=root, judge_adapter=good_judge
            )
        candidate = result.frontier.candidates[0]
        contradictory = replace(
            candidate,
            deterministic_gates={**candidate.deterministic_gates, "shortlist": False},
        )
        with self.assertRaisesRegex(ValueError, "shortlist gate contradicts"):
            build_stage_membership(
                sourced=[contradictory], hydrated=[contradictory], triaged=[contradictory],
                ranked=(contradictory,), shortlist_person_ids=set(), status="completed_capped",
                epochs=0, bounds=approved.bounds,
            )

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
            with self.assertRaisesRegex(ValueError, "explicitly allowed private root"):
                production_run_recruiting(
                    recruiting_spec(),
                    FakeRunner(),
                    artifact_root=root,
                    allowed_artifact_root=TEST_SEARCH_RUNS_ROOT,
                )

    def test_private_root_accepts_resolved_symlink_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp) / "private" / "var"
            allowed = private / "search-runs"
            allowed.mkdir(parents=True)
            alias = Path(tmp) / "var"
            alias.symlink_to(private, target_is_directory=True)
            artifact = alias / "search-runs" / "run"
            result = production_run_recruiting(
                recruiting_spec(),
                FakeRunner(),
                artifact_root=artifact,
                allowed_artifact_root=allowed,
                plan_adapter=plan_adapter,
                critic_adapter=critic_adapter,
            )
            self.assertEqual(result.status, "awaiting_review")
            self.assertTrue((allowed / "run" / "review" / "plan.json").exists())

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

        def live_schema(name):
            contract = json.loads(
                (
                    Path(__file__).resolve().parents[1]
                    / "packs/search/contracts/turbopuffer"
                    / f"{name}.namespace.json"
                ).read_text()
            )
            schema = {row["name"]: {"type": row["type"]} for row in contract["attributes"]}
            if contract.get("vector"):
                schema["vector"] = {"type": "vector"}
            return schema

        async def enumerate_namespace(name, filters, attributes, consume_page, *, page_size, max_results=0):
            rows = namespace_rows[name]
            consume_page(rows)
            return {
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
                "packs.search.backends.turbopuffer.runner.storage.consume_filter_only_pages_for_namespace",
                new=mock.AsyncMock(side_effect=enumerate_namespace),
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.namespace_schema",
                side_effect=live_schema,
            ),
        )
        with self.run_dir() as root:
            with (
                snapshot_patches[0],
                snapshot_patches[1],
                snapshot_patches[2],
                snapshot_patches[3],
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
                    "packs.search.backends.turbopuffer.runner.storage.consume_filter_only_pages_for_namespace",
                    new=mock.AsyncMock(side_effect=enumerate_namespace),
                ),
                mock.patch(
                    "packs.search.backends.turbopuffer.runner.storage.namespace_schema",
                    side_effect=live_schema,
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

            def snapshot_corpus(self, scope, evidence_person_ids, *, spec=None):
                self.calls.append("snapshot")
                return {
                    **super().snapshot_corpus(scope, evidence_person_ids, spec=spec),
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
        self.assertIn("verified comparable or tagged corpus snapshot", result.errors[0])

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
            extracted = _production_plan_adapter(JD, configured, None)
            critic = _production_critic_adapter(JD, extracted, configured)

        self.assertEqual(extracted["job_title"], EXTRACTED["job_title"])
        self.assertEqual(critic, {"verdict": "ok"})
        self.assertEqual(make_client.call_count, 2)
        self.assertEqual(create.call_args_list[0].kwargs["model"], "gpt-test")
        self.assertEqual(create.call_args_list[0].kwargs["response_format"], PLAN_RESPONSE_FORMAT)
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
                _production_plan_adapter(oversized, configured, None)
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
                plan_adapter=lambda jd, value, repair: EXTRACTED,
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

    def test_default_judge_effort_survives_the_provider_reasoning_gate(self):
        """gpt-5.6-luna is a reasoning model, so the default "none" must reach the request."""
        self.assertTrue(profile_evaluator.supports_reasoning_effort(DEFAULT_JUDGE_MODEL))
        create = mock.AsyncMock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))]
            )
        )
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch.object(profile_evaluator, "normalize_evaluation", return_value={}):
            asyncio.run(
                profile_evaluator.evaluate_one(
                    client,
                    asyncio.Semaphore(1),
                    DEFAULT_JUDGE_MODEL,
                    DEFAULT_JUDGE_REASONING_EFFORT,
                    {},
                    {"person_id": "p"},
                    {"current_title": "Engineer"},
                    120,
                    0,
                )
            )
        self.assertEqual(create.await_args.kwargs["model"], DEFAULT_JUDGE_MODEL)
        self.assertEqual(create.await_args.kwargs["reasoning_effort"], "none")

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
        self.assertEqual(judge_one.call_args.args[1:3], ("codex-test", "none"))
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

    def test_configured_judge_effort_reaches_both_judge_implementations(self):
        def configure(**changes):
            return replace(
                recruiting_spec(),
                recruiting=replace(
                    recruiting_spec().recruiting, judge_approved=True, **changes
                ),
            )

        candidate = CandidateRecord("person", hydrated_profile={"current_title": "Engineer"})
        client = SimpleNamespace(close=mock.AsyncMock())
        evaluate = mock.AsyncMock(return_value={"jd_score": 0.5, "error": None})
        for effort in (DEFAULT_JUDGE_REASONING_EFFORT, "medium"):
            changes = {"judge_implementation": "profile_evaluator"}
            if effort != DEFAULT_JUDGE_REASONING_EFFORT:
                changes["judge_reasoning_effort"] = effort
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
                _production_judge_adapter(configure(**changes))(candidate, {})
            self.assertEqual(evaluate.await_args.args[2], DEFAULT_JUDGE_MODEL)
            self.assertEqual(evaluate.await_args.args[3], effort)

            changes["judge_implementation"] = "codex"
            with (
                mock.patch(
                    "packs.search.primitives.deep_search.codex_judge.judge_one",
                    return_value=({"seniority_fit": "ideal"}, None),
                ) as judge_one,
                mock.patch(
                    "packs.search.primitives.evaluate_profile_candidates.evaluate_profile_candidates.normalize_evaluation",
                    return_value={"jd_score": 0.5},
                ),
            ):
                _production_judge_adapter(configure(**changes))(candidate, {})
            self.assertEqual(judge_one.call_args.args[1:3], (DEFAULT_JUDGE_MODEL, effort))

    def test_default_judge_model_is_priced_for_the_worst_case_reservation(self):
        with self.run_dir() as root:
            reservations = _SpendReservations(Path(root), recruiting_spec().bounds)
            estimate, prior_rows, stage = reservations.reserve(
                "recruiting_judge", DEFAULT_JUDGE_MODEL, JUDGE_CALL_MAX_TOKENS
            )
        self.assertGreater(estimate, 0)
        self.assertEqual((prior_rows, stage), (0, "recruiting_judge"))
        self.assertEqual(reservations.reserved_usd, estimate)
        with self.run_dir() as root:
            with self.assertRaisesRegex(JudgeBudgetExceeded, "cannot price model"):
                _SpendReservations(Path(root), recruiting_spec().bounds).reserve(
                    "recruiting_judge", "synthetic-unpriced-model", JUDGE_CALL_MAX_TOKENS
                )

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
            self.assertEqual(rows[0]["Interactions"], "")
            self.assertEqual(rows[0]["Source/Channels"], "local|role|summary|company_signal")
            self.assertNotIn("person_id", {key.casefold() for key in rows[0]})

    def test_remote_shortlist_csv_uses_scoped_channels_without_operator_identity(self):
        candidate = CandidateRecord(
            "synthetic-person",
            source_lanes=("role", "summary"),
            backend="powerset",
            hydrated_profile={
                "name": "Jordan Bravo",
                "source_operators": ["Owner User", "Team User"],
                "source_channels": ["gmail", "linkedin"],
                "total_interactions": 12,
            },
        )
        row = shortlist_csv_row(1, candidate)
        self.assertEqual(row["Interactions"], 12)
        self.assertEqual(row["Source/Channels"], "gmail|linkedin")
        self.assertNotIn("Owner User", row["Source/Channels"])
        self.assertNotIn("Team User", row["Source/Channels"])
        self.assertNotIn("powerset", row["Source/Channels"])
        self.assertNotIn("role", row["Source/Channels"])

    def test_remote_shortlist_csv_preserves_zero_scoped_interactions(self):
        candidate = CandidateRecord(
            "synthetic-person",
            backend="powerset",
            hydrated_profile={"name": "Jordan Bravo", "total_interactions": 0},
        )

        self.assertEqual(shortlist_csv_row(1, candidate)["Interactions"], 0)

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
            streamed = [
                json.loads(line)
                for line in (Path(root) / "candidate-frontier.jsonl").read_text().splitlines()
                if line.strip()
            ]
            membership = json.loads((Path(root) / "stage-membership.json").read_text())
        self.assertEqual(result.counts["total_sourced"], 11)
        self.assertEqual(len(result.frontier.candidates), 11)
        self.assertEqual(len(persisted["candidates"]), 11)
        self.assertEqual(streamed, persisted["candidates"])
        self.assertEqual(membership["schema_version"], "search.stage_membership.v1")
        self.assertEqual(membership["total_sourced"], 11)
        self.assertEqual(len(membership["candidates"]), 11)
        self.assertEqual(
            [row["person_id"] for row in persisted["candidates"]],
            [row.person_id for row in result.frontier.candidates],
        )

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

    def test_wrong_current_role_families_do_not_consume_judge_slots(self):
        class RoleFamilyRunner(FakeRunner):
            def capabilities(self, spec):
                capabilities = super().capabilities(spec)
                return replace(
                    capabilities,
                    supported_hard_filters=(*capabilities.supported_hard_filters, "is_current_role"),
                )

            def hydrate(self, frontier):
                hydrated = super().hydrate(frontier)
                families = {"p0": "Engineering", "p1": "investing", "p2": "Human Resources"}
                return replace(
                    hydrated,
                    candidates=tuple(
                        replace(
                            row,
                            hydrated_profile={
                                **row.hydrated_profile,
                                "positions": [{
                                    "is_current": True,
                                    "role_track": families[row.person_id],
                                    "seniority_band": "senior",
                                    "city": "San Francisco",
                                }],
                            },
                        )
                        for row in hydrated.candidates
                    ),
                )

        run_spec = replace(
            recruiting_spec(),
            role=RoleIntent(titles=("Senior Backend Engineer",)),
            person_filters=PersonFilters(
                cities=("San Francisco",), seniority_bands=("senior",), is_current_role=True
            ),
        )
        judged = []
        with self.run_dir() as root:
            approved = self.prepare(run_spec, RoleFamilyRunner(3), root)

            def judge(candidate, plan):
                judged.append(candidate.person_id)
                return good_judge(candidate, plan)

            result = run_recruiting(
                approved, RoleFamilyRunner(3), artifact_root=root, judge_adapter=judge
            )

        self.assertEqual(judged, ["p0"])
        self.assertEqual(result.counts["judge_calls"], 1)
        self.assertEqual(result.hard_filter_validation["reviewed_count"], 3)
        self.assertEqual(result.hard_filter_validation["violation_count"], 2)
        self.assertEqual(
            {row["reason_code"] for row in result.hard_filter_validation["violations"]},
            {"current_role_family_mismatch"},
        )

    def test_wrong_current_role_family_expansion_is_not_judged(self):
        class ExpansionRoleFamilyRunner(FakeRunner):
            def capabilities(self, spec):
                capabilities = super().capabilities(spec)
                return replace(
                    capabilities,
                    supported_hard_filters=(*capabilities.supported_hard_filters, "is_current_role"),
                )

            def hydrate(self, frontier):
                hydrated = super().hydrate(frontier)
                return replace(
                    hydrated,
                    candidates=tuple(
                        replace(
                            row,
                            hydrated_profile={
                                **row.hydrated_profile,
                                "positions": [{
                                    "is_current": True,
                                    "role_track": "investing" if row.person_id.startswith("new-") else "Engineering",
                                    "seniority_band": "senior",
                                    "city": "San Francisco",
                                }],
                            },
                        )
                        for row in hydrated.candidates
                    ),
                )

        base = recruiting_spec()
        run_spec = replace(
            base,
            role=RoleIntent(titles=("Senior Backend Engineer",)),
            person_filters=PersonFilters(
                cities=("San Francisco",), seniority_bands=("senior",), is_current_role=True
            ),
            bounds=replace(base.bounds, judge_candidate_limit=20),
        )
        judged = []
        with self.run_dir() as root:
            approved = self.prepare(run_spec, ExpansionRoleFamilyRunner(10), root)

            def judge(candidate, plan):
                judged.append(candidate.person_id)
                return good_judge(candidate, plan)

            result = run_recruiting(
                approved,
                ExpansionRoleFamilyRunner(10, expansion_new=True),
                artifact_root=root,
                judge_adapter=judge,
            )

        self.assertEqual(set(judged), {f"p{index}" for index in range(10)})
        self.assertEqual(result.counts["judge_calls"], 10)
        self.assertGreater(result.hard_filter_validation["violation_count"], 0)
        self.assertTrue(
            all(
                row["reason_code"] == "current_role_family_mismatch"
                for row in result.hard_filter_validation["violations"]
            )
        )

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

    def test_plan_prompt_states_every_legal_policy_value_and_trait_cap(self):
        """Drift guard: the prompt is the only place the model learns the contract's values."""
        for value in (
            *recruiter_policy.PEDIGREE_POLICIES,
            *recruiter_policy.FOUNDER_C_SUITE_POLICIES,
            *recruiter_policy.EXCELLENCE_DIMENSIONS,
            *recruiter_policy.CANONICAL_HIRE_STAGES,
        ):
            self.assertIn(value, PLAN_SYSTEM)
        self.assertIn(f"AT MOST {MAX_MUST_HAVE} must_have traits", PLAN_SYSTEM)
        self.assertIn(f"AT MOST {MAX_NICE_TO_HAVE} nice_to_have traits", PLAN_SYSTEM)

    def test_plan_contract_constants_track_the_schema_file_not_a_hardcoded_copy(self):
        """Anchor the derived constants to a FRESH parse of the canonical schema.

        Every other guard here compares derived artifacts against these constants, so this is the
        one test that fails when someone "helpfully" inlines a literal and the schema later moves.
        The path is rebuilt from the repo root rather than imported, so pointing PLAN_SCHEMA_PATH
        at a copy fails too.
        """
        document = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "packs/search/schemas/search-network-jd-plan.schema.json"
            ).read_text(encoding="utf-8")
        )
        traits = document["properties"]["traits"]["properties"]
        self.assertEqual(PLAN_SCHEMA, document)
        self.assertEqual(MAX_MUST_HAVE, traits["must_have"]["maxItems"])
        self.assertEqual(MAX_NICE_TO_HAVE, traits["nice_to_have"]["maxItems"])
        self.assertEqual(list(VALID_TARGET_LEVELS), document["properties"]["target_level"]["enum"])
        self.assertEqual(
            list(VALID_TIERS), document["$defs"]["mustTrait"]["properties"]["tier"]["enum"]
        )

    def test_plan_extraction_schema_constrains_generation_to_the_canonical_contract(self):
        def strict(node, path="<root>"):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False, path)
                    self.assertEqual(list(node["required"]), list(node["properties"]), path)
                for key, child in node.items():
                    strict(child, f"{path}/{key}")
            elif isinstance(node, list):
                for index, child in enumerate(node):
                    strict(child, f"{path}[{index}]")

        strict(PLAN_EXTRACTION_SCHEMA)
        properties = PLAN_EXTRACTION_SCHEMA["properties"]
        preferences = properties["recruiter_preferences"]["anyOf"][0]["properties"]
        self.assertEqual(properties["must_have"]["maxItems"], MAX_MUST_HAVE)
        self.assertEqual(properties["nice_to_have"]["maxItems"], MAX_NICE_TO_HAVE)
        self.assertEqual(
            preferences["pedigree_policy"]["anyOf"][0]["enum"],
            sorted(recruiter_policy.PEDIGREE_POLICIES),
        )
        self.assertEqual(
            preferences["current_founder_c_suite_for_non_exec_ic"]["anyOf"][0]["enum"],
            sorted(recruiter_policy.FOUNDER_C_SUITE_POLICIES),
        )
        self.assertTrue(PLAN_RESPONSE_FORMAT["json_schema"]["strict"])
        self.assertIs(PLAN_RESPONSE_FORMAT["json_schema"]["schema"], PLAN_EXTRACTION_SCHEMA)
        # Both live failures are refused at generation time, not after the corpus snapshot.
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(OVER_CAP_EXTRACTED["must_have"], properties["must_have"])
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                "not_a_policy", preferences["current_founder_c_suite_for_non_exec_ic"]
            )

    def test_plan_model_resolves_explicit_then_env_then_priced_pinned_default(self):
        explicit = recruiting_spec(
            recruiting=replace(recruiting_spec().recruiting, plan_model="gpt-4.1")
        )
        with mock.patch.dict("os.environ", {"RECRUIT_PLAN_MODEL": "gpt-4.1-mini"}):
            self.assertEqual(plan_model(explicit), "gpt-4.1")
            self.assertEqual(plan_model(recruiting_spec()), "gpt-4.1-mini")
        for blank in ("", "   "):
            with mock.patch.dict("os.environ", {"RECRUIT_PLAN_MODEL": blank}):
                self.assertEqual(plan_model(recruiting_spec()), DEFAULT_PLAN_MODEL)
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(plan_model(recruiting_spec()), DEFAULT_PLAN_MODEL)
        prices = json.loads(
            (Path(__file__).resolve().parents[1] / "packs/search/data/model-prices.json").read_text()
        )
        self.assertIsNotNone(prices.get(DEFAULT_PLAN_MODEL), "the pinned plan model must be priced")

    def test_over_cap_must_have_is_repaired_on_one_retry_and_the_run_proceeds(self):
        adapter = ScriptedPlanAdapter(OVER_CAP_EXTRACTED, EXTRACTED)
        with self.run_dir() as root:
            result = run_recruiting(
                recruiting_spec(), FakeRunner(), artifact_root=root,
                plan_adapter=adapter, critic_adapter=critic_adapter,
            )
            self.assertEqual(result.status, "awaiting_review", result.errors)
            plan = json.loads((Path(root) / "review/plan.json").read_text())
        self.assertEqual(adapter.calls[0], None)
        self.assertEqual(len(adapter.calls), 2)
        self.assertIn("traits/must_have", adapter.calls[1])
        self.assertIn("maxItems", adapter.calls[1])
        self.assertEqual(len(plan["traits"]["must_have"]), len(EXTRACTED["must_have"]))

    def test_cross_field_location_violation_is_repaired_like_a_schema_violation(self):
        broken = {
            **EXTRACTED,
            "location_filters": {"cities": ["San Francisco"], "countries": ["United States", "Canada"]},
        }
        adapter = ScriptedPlanAdapter(broken, EXTRACTED)
        with self.run_dir() as root:
            result = run_recruiting(
                recruiting_spec(), FakeRunner(), artifact_root=root,
                plan_adapter=adapter, critic_adapter=critic_adapter,
            )
        self.assertEqual(result.status, "awaiting_review", result.errors)
        self.assertIn("exactly one country", adapter.calls[1])

    def test_model_controlled_text_in_a_repair_turn_is_bounded(self):
        """A plain normalization ValueError echoes the model's own location string; bound it too."""
        flooded = {
            **EXTRACTED,
            "location": "San Francisco, CA, " + "Nowhere " * 900,
            "location_filters": {
                "cities": ["San Francisco"],
                "states": ["California"],
                "countries": ["United States"],
            },
        }
        adapter = ScriptedPlanAdapter(flooded, EXTRACTED)
        with self.run_dir() as root:
            result = run_recruiting(
                recruiting_spec(), FakeRunner(), artifact_root=root,
                plan_adapter=adapter, critic_adapter=critic_adapter,
            )
        self.assertEqual(result.status, "awaiting_review", result.errors)
        self.assertLessEqual(len(adapter.calls[1]), CONTRACT_MESSAGE_CHARS)
        self.assertIn("conflict with or broaden", adapter.calls[1])

    def test_persistently_invalid_plan_returns_typed_needs_input_not_a_traceback(self):
        adapter = ScriptedPlanAdapter(OVER_CAP_EXTRACTED)
        with self.run_dir() as root:
            result = run_recruiting(
                recruiting_spec(), FakeRunner(), artifact_root=root,
                plan_adapter=adapter, critic_adapter=critic_adapter,
            )
            self.assertFalse((Path(root) / "review/plan.json").exists())
            self.assertTrue((Path(root) / "review/source.json").exists())
        self.assertEqual(result.status, "needs_input")
        self.assertEqual(result.stage, "review")
        self.assertEqual(len(adapter.calls), PLAN_EXTRACTION_ATTEMPTS)
        self.assertIn("traits/must_have", result.errors[0])

    def test_reviewed_plan_edited_out_of_contract_returns_typed_needs_input(self):
        runner = FakeRunner()
        with self.run_dir() as root:
            approved = self.prepare(recruiting_spec(), runner, root)
            plan_path = Path(root) / "review/plan.json"
            plan = json.loads(plan_path.read_text())
            plan["traits"]["must_have"] = [
                {"trait": f"synthetic capability {index}", "tier": "table_stakes", "source": "jd"}
                for index in range(MAX_MUST_HAVE + 1)
            ]
            plan_path.write_text(json.dumps(plan))
            result = run_recruiting(approved, runner, artifact_root=root, judge_adapter=good_judge)
        self.assertEqual(result.status, "needs_input")
        self.assertEqual(result.stage, "review")
        self.assertIn("traits/must_have", result.errors[0])

    def test_invalid_resolved_policy_in_a_plan_is_typed_not_a_raw_schema_error(self):
        plan = build_review_plan(recruiting_spec(), EXTRACTED, created_at="2026-07-31T00:00:00Z")
        plan["recruiter_policy"]["preferences"]["current_founder_c_suite_for_non_exec_ic"] = "maybe"
        with self.assertRaises(PlanContractError) as raised:
            validate_review_plan(recruiting_spec(), plan)
        self.assertIn("current_founder_c_suite_for_non_exec_ic", str(raised.exception))

    def test_plan_repair_retry_uses_the_same_spend_reservation_door(self):
        calls = []

        def offline_call(reservations, stage, model, maximum_tokens, adapter, *args):
            calls.append((stage, model))
            return adapter(*args)

        configured = recruiting_spec(
            recruiting=replace(recruiting_spec().recruiting, plan_approved=True)
        )
        adapter = ScriptedPlanAdapter(OVER_CAP_EXTRACTED, EXTRACTED)
        with (
            self.run_dir() as root,
            mock.patch("packs.search.pipeline.recruiting._production_plan_adapter", new=adapter),
            mock.patch(
                "packs.search.pipeline.recruiting._production_critic_adapter", new=critic_adapter
            ),
            mock.patch("packs.search.pipeline.recruiting._SpendReservations.call", new=offline_call),
        ):
            result = run_recruiting(configured, FakeRunner(), artifact_root=root)
        self.assertEqual(result.status, "awaiting_review", result.errors)
        self.assertEqual(
            [stage for stage, _ in calls],
            ["recruiting_plan", "recruiting_plan", "recruiting_critic"],
        )
        self.assertEqual({model for _, model in calls}, {DEFAULT_PLAN_MODEL})


if __name__ == "__main__":
    unittest.main()
