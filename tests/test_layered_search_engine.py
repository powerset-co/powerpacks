from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import jsonschema

from packs.search.pipeline.artifacts import persist_result
from packs.search.pipeline.frontier import CandidateFrontier, CandidateRecord, ProbeMatch, StageResult
from packs.search.pipeline.filters import validation_findings
from packs.search.pipeline.gtm import run_with_runner
from packs.search.pipeline.models import (
    Backend,
    CompanyFilters,
    EvidenceCriterion,
    HardFilterSet,
    LocalCorpus,
    LookupSpec,
    PersonFilters,
    Profile,
    RankMode,
    ResolvedSources,
    RoleIntent,
    RunnerCapabilities,
    SearchBounds,
    SearchPlan,
    SearchSpec,
    SqlCandidate,
)
from packs.search.pipeline.routing import SearchRoute
from packs.search.pipeline.ranking import SemanticOutcome, production_semantic_adapter
from packs.search.reflect.snapshots import validate_snapshot
from packs.search.primitives.persist_search_results.results_io import result_rows
from tests.local_search_fixture import PERSON_OTHER, PERSON_STANFORD, write_local_search_db

ROOT = Path(__file__).resolve().parents[1]
HASH = "a" * 64


def spec(**changes) -> SearchSpec:
    base = SearchSpec(
        "search.spec.v1",
        "senior engineers",
        Profile.GTM,
        Backend.LOCAL,
        LocalCorpus("/var/tmp/test.duckdb"),
        role=RoleIntent(("engineer",), (), ()),
        person_filters=PersonFilters(seniority_bands=("senior",)),
        bounds=SearchBounds(10, 10, 10),
    )
    return replace(base, **changes)


class FakeRunner:
    def __init__(self, *, eligible=2, records=(), eligible_ids=None, supported=("role_ids", "seniority_bands")):
        self.eligible = eligible
        self.records = tuple(records)
        self.supported = supported
        self.eligible_ids = eligible_ids
        self.calls = []

    def capabilities(self, value):
        self.calls.append("capabilities")
        return RunnerCapabilities(
            Backend.LOCAL,
            self.supported,
            ("role",),
            "tech_skills" in self.supported,
            True,
            ("name", "email", "phone", "handle", "profile_url", "person_id"),
        )

    def lookup_person(self, value):
        self.calls.append("lookup")
        return self.records

    def resolve_sources(self, value):
        self.calls.append("resolve")
        return ResolvedSources()

    def apply_hard_filters(self, value, sources):
        self.calls.append("filter")
        ids = tuple(row.person_id for row in self.records) if self.eligible_ids is None else tuple(self.eligible_ids)
        return HardFilterSet(self.eligible, ids, {"synthetic": True})

    def retrieve_people(self, plan, filters):
        self.calls.append("retrieve")
        return self.records

    def hydrate(self, frontier):
        self.calls.append("hydrate")
        rows = [
            replace(
                row,
                hydrated_profile=row.hydrated_profile
                or {"positions": [{"role_ids": ["engineer"], "seniority_band": "senior"}]},
                hydration_disposition="hydrated",
            )
            for row in frontier.candidates
        ]
        return CandidateFrontier(
            tuple(rows), frontier.input_count, frontier.output_count, frontier.limit, frontier.truncated
        )


class ContractTests(unittest.TestCase):
    def test_strict_parse_unknown_and_backend_mismatch(self):
        with self.assertRaisesRegex(ValueError, "unknown SearchRoute"):
            SearchRoute.from_dict({"target": "engine", "profile": "gtm", "backend": "local", "reason": "x", "extra": 1})
        value = spec().to_dict()
        value["corpus"] = {
            "kind": "powerset",
            "set_id": "s",
            "operator_ids": ["o"],
            "operator_scope_hash": HASH,
            "membership_hash": HASH,
            "namespace_schema_hashes": {"people": HASH},
            "native_content_version": "v1",
            "scoped_records_hash": None,
        }
        with self.assertRaisesRegex(ValueError, "do not match"):
            SearchSpec.from_dict(value)

    def test_parser_and_direct_constructor_reject_coercions_and_bad_hashes(self):
        with self.assertRaisesRegex(ValueError, "integers"):
            SearchBounds.from_dict({"retrieval_limit": "10", "output_limit": 2, "semantic_rank_limit": 2})
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            LocalCorpus("/var/tmp/a.duckdb", "not-a-hash")
        with self.assertRaisesRegex(ValueError, "boolean"):
            PersonFilters(is_current_role=1)
        with self.assertRaisesRegex(ValueError, "semantic rank limit"):
            SearchBounds(2, 2, 3)

    def test_schema_parser_and_constructor_reject_currentness_and_empty_scope(self):
        schema = json.loads((ROOT / "packs/search/schemas/search-spec.schema.json").read_text())
        value = spec().to_dict()
        value["person_filters"]["is_current_role"] = True
        value["company_filters"]["is_current_company"] = False
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(value, schema)
        with self.assertRaisesRegex(ValueError, "cannot conflict"):
            SearchSpec.from_dict(value)
        remote = value | {
            "backend": "powerset",
            "corpus": {
                "kind": "powerset",
                "set_id": "s",
                "operator_ids": [],
                "namespace_schema_hashes": {},
                "operator_scope_hash": None,
                "membership_hash": None,
                "native_content_version": None,
                "scoped_records_hash": None,
            },
        }
        remote["person_filters"] = {**value["person_filters"], "is_current_role": None}
        remote["company_filters"] = {**value["company_filters"], "is_current_company": None}
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(remote, schema)
        from packs.search.pipeline.models import PowersetCorpus

        with self.assertRaisesRegex(ValueError, "operator_ids"):
            PowersetCorpus("s", ())

    def test_persisted_schemas_and_unknown_fields(self):
        schemas = ROOT / "packs/search/schemas"
        jsonschema.validate(spec().to_dict(), json.loads((schemas / "search-spec.schema.json").read_text()))
        frontier = CandidateFrontier.merge([CandidateRecord("p", backend="local")]).to_dict()
        jsonschema.validate(frontier, json.loads((schemas / "candidate-frontier.schema.json").read_text()))
        frontier["extra"] = True
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(frontier, json.loads((schemas / "candidate-frontier.schema.json").read_text()))

    def test_provenance_union_matched_positions_and_probe_family(self):
        one = CandidateRecord(
            "p",
            0.4,
            matched_position_ids=("pos-1",),
            matched_position_indexes=(0,),
            source_lanes=("role",),
            found_by=(ProbeMatch("role", 2, "p1", "title"),),
        )
        two = CandidateRecord(
            "p",
            0.7,
            matched_position_ids=("pos-2",),
            matched_position_indexes=(1,),
            source_lanes=("summary",),
            found_by=(ProbeMatch("summary", 1, "p2", "systems"),),
        )
        merged = CandidateFrontier.merge([one, two]).candidates[0]
        self.assertEqual(merged.retrieval_score, 0.7)
        self.assertEqual(merged.matched_position_ids, ("pos-1", "pos-2"))
        self.assertEqual(merged.source_lanes, ("role", "summary"))
        self.assertEqual({probe.probe_family for probe in merged.found_by}, {"title", "systems"})

    def test_lane_yield_counts_report_unique_and_marginal_people(self):
        from packs.search.pipeline.frontier import lane_yield_counts

        counts = lane_yield_counts(
            [
                CandidateRecord("role-only", source_lanes=("role",)),
                CandidateRecord("shared", source_lanes=("role",)),
                CandidateRecord("shared", source_lanes=("summary",)),
                CandidateRecord("signal-only", source_lanes=("company_signal",)),
            ]
        )
        self.assertEqual(counts["lane_role_people"], 2)
        self.assertEqual(counts["lane_role_marginal"], 1)
        self.assertEqual(counts["lane_summary_people"], 1)
        self.assertEqual(counts["lane_summary_marginal"], 0)
        self.assertEqual(counts["lane_company_signal_marginal"], 1)


class PipelineTests(unittest.TestCase):
    def test_local_runner_deterministic_gtm_end_to_end(self):
        from packs.search.pipeline.search import run_search

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "local.duckdb"
            write_local_search_db(db)
            local = spec(
                corpus=LocalCorpus(str(db)),
                role=RoleIntent(("software_engineer",), (), ()),
                person_filters=PersonFilters(cities=("San Francisco",), seniority_bands=("senior",)),
            )
            result = run_search(local)
            self.assertEqual(result.status, "completed")
            self.assertIn(PERSON_STANFORD, {row.person_id for row in result.frontier.candidates})
            self.assertTrue(all(row.hydration_disposition == "hydrated" for row in result.frontier.candidates))
            candidate = next(row for row in result.frontier.candidates if row.person_id == PERSON_STANFORD)
            self.assertTrue(candidate.hydrated_profile["positions"])
            self.assertTrue(candidate.hydrated_profile["education"])
            self.assertIn("Python", candidate.hydrated_profile["tech_skills"])

    def test_nonsense_bm25_does_not_return_entire_eligible_corpus(self):
        from packs.search.pipeline.search import run_search

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "local.duckdb"
            write_local_search_db(db)
            value = spec(
                corpus=LocalCorpus(str(db)),
                raw_request="zzzxxyy-no-match",
                role=RoleIntent(bm25_queries=("zzzxxyy-no-match",)),
                person_filters=PersonFilters(),
            )
            result = run_search(value)
            self.assertEqual(result.status, "completed_empty")
            self.assertEqual(result.frontier.output_count, 0)

    def test_hard_filter_before_cap_recovers_eligible_candidate(self):
        eligible = CandidateRecord("eligible-below-unconstrained-top-k", 0.1)
        runner = FakeRunner(records=(eligible,))
        result = run_with_runner(spec(bounds=SearchBounds(1, 1, 1)), runner)
        self.assertEqual(runner.calls[:4], ["capabilities", "resolve", "filter", "retrieve"])
        self.assertEqual(result.frontier.candidates[0].person_id, eligible.person_id)

    def test_unsupported_required_skill_and_zero_pool_exit_before_retrieval(self):
        runner = FakeRunner()
        result = run_with_runner(spec(tech_skills=("Rust",)), runner)
        self.assertEqual(result.status, "unsupported_capability")
        self.assertEqual(runner.calls, ["capabilities"])
        runner = FakeRunner(eligible=0)
        result = run_with_runner(spec(), runner)
        self.assertEqual(result.status, "completed_empty")
        self.assertNotIn("retrieve", runner.calls)

    def test_structured_gtm_is_model_free_and_semantic_rank_is_explicit_and_bounded(self):
        records = tuple(CandidateRecord(f"p{i}", 1 / (i + 1)) for i in range(4))
        runner = FakeRunner(records=records)
        self.assertEqual(run_with_runner(spec(), runner).status, "completed")
        with self.assertRaisesRegex(ValueError, "require semantic"):
            spec(soft_criteria=(EvidenceCriterion("fit", "soft company fit"),), bounds=SearchBounds(4, 2, 2))
        semantic = spec(
            soft_criteria=(EvidenceCriterion("fit", "soft company fit"),),
            rank_mode=RankMode.SEMANTIC,
            bounds=SearchBounds(4, 2, 3),
        )
        result = run_with_runner(semantic, runner)
        self.assertEqual(result.status, "needs_input")
        calls = []
        configured = replace(semantic, rank_model="gpt-test", rank_approved=True)

        def adapter(value, candidates):
            calls.append(tuple(row.person_id for row in candidates))
            return [
                SemanticOutcome(row.person_id, 1 - index / 10)
                for index, row in enumerate(candidates)
            ]

        result = run_with_runner(configured, runner, semantic_adapter=adapter)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]), 3)
        self.assertEqual(len(result.frontier.candidates), 2)

        def partial(value, candidates):
            return [
                SemanticOutcome(candidates[0].person_id, 0.9),
                *(SemanticOutcome(row.person_id, error="adapter failed") for row in candidates[1:]),
            ]

        partial_result = run_with_runner(configured, runner, semantic_adapter=partial)
        self.assertEqual(partial_result.status, "completed")
        self.assertEqual(partial_result.counts["semantic_rank_failures"], 2)
        self.assertTrue(any("adapter failed" in warning for warning in partial_result.warnings))

        failed = run_with_runner(
            configured,
            runner,
            semantic_adapter=lambda value, candidates: [
                SemanticOutcome(row.person_id, error="adapter failed") for row in candidates
            ],
        )
        self.assertEqual(failed.status, "failed_rank")
        self.assertEqual(len(failed.errors), 3)

        raised = run_with_runner(
            configured,
            runner,
            semantic_adapter=lambda value, candidates: (_ for _ in ()).throw(
                RuntimeError("ranking provider unavailable")
            ),
        )
        self.assertEqual(raised.status, "failed_rank")
        self.assertIn("ranking provider unavailable", raised.errors[0])

    def test_production_semantic_adapter_uses_established_trait_shape_and_preserves_errors(self):
        configured = replace(
            spec(
                soft_criteria=(EvidenceCriterion("fit", "company and role evidence"),),
                rank_mode=RankMode.SEMANTIC,
            ),
            rank_model="gpt-test",
            rank_approved=True,
        )
        rerank = mock.AsyncMock(
            return_value=[
                SimpleNamespace(id="one", score=0.8, error=None),
                SimpleNamespace(id="two", score=0.0, error="timeout"),
            ]
        )
        with (
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test"}),
            mock.patch(
                "packs.search.primitives.llm_rerank_candidates.llm_rerank_candidates.rerank_all",
                new=rerank,
            ),
        ):
            outcomes = production_semantic_adapter(
                configured, (CandidateRecord("one"), CandidateRecord("two"))
            )
        self.assertEqual(
            rerank.await_args.kwargs["traits"],
            [{"value": "fit", "temporal": "all", "meaning": "general"}],
        )
        self.assertEqual(outcomes[1], SemanticOutcome("two", None, "timeout"))

    def test_sql_fan_in_before_single_hydration_and_missing_disposition(self):
        class MissingRunner(FakeRunner):
            def hydrate(self, frontier):
                self.calls.append("hydrate")
                return CandidateFrontier(
                    tuple(replace(row, hydration_disposition="missing_profile") for row in frontier.candidates),
                    frontier.input_count,
                    frontier.output_count,
                    frontier.limit,
                    frontier.truncated,
                )

        runner = MissingRunner(records=(CandidateRecord("retrieved"),), eligible_ids=("retrieved", "sql"))
        result = run_with_runner(spec(sql_candidates=(SqlCandidate("sql", "joined"),)), runner)
        self.assertEqual(runner.calls.count("hydrate"), 1)
        self.assertEqual(result.status, "completed_empty")
        self.assertFalse(result.frontier.candidates)
        self.assertEqual(result.hard_filter_validation["violation_count"], 2)

    def test_company_union_validates_role_and_company_branches_independently(self):
        union = spec(
            role=RoleIntent(("engineer",), search_mode="COMPANY_UNION"),
            company_filters=CompanyFilters(company_ids=("target",)),
        )
        sources = ResolvedSources(company_ids=("target",))
        role_profile = {"positions": [{"role_ids": ["engineer"], "seniority_band": "senior", "company_id": "other"}]}
        company_profile = {"positions": [{"role_ids": ["founder"], "seniority_band": "senior", "company_id": "target"}]}
        self.assertEqual(validation_findings(role_profile, union, sources, ("role",))["violations"], ())
        self.assertEqual(validation_findings(company_profile, union, sources, ("company_union",))["violations"], ())
        merged = validation_findings(role_profile, union, sources, ("role", "company_union"))
        self.assertEqual(merged["violations"], ())

        constrained = replace(
            union,
            person_filters=PersonFilters(
                cities=("San Francisco",), seniority_bands=("senior",),
                role_tracks=("ic",), is_current_role=True,
            ),
            company_filters=CompanyFilters(company_ids=("target",), is_current_company=True),
        )
        from packs.search.backends.local.runner import LocalSearchRunner
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        remote_spec = replace(
            constrained, backend=Backend.POWERSET,
            corpus=PowersetCorpus("set", ("operator",)),
        )
        compiled_filters = (
            LocalSearchRunner("/unused")._filters(constrained, sources, include_role=False),
            TurboPufferSearchRunner(remote_spec.corpus)._filters(
                remote_spec, sources, include_role=False
            ),
        )
        for compiled in compiled_filters:
            text = str(compiled)
            self.assertIn("city", text)
            self.assertIn("company_id", text)
            self.assertIn("is_current", text)
            self.assertNotIn("role_ids", text)
            self.assertNotIn("seniority_band", text)
            self.assertNotIn("role_track", text)

    def test_summary_branch_does_not_use_historical_role_for_current_requirement(self):
        union = spec(
            role=RoleIntent(("engineer",), bm25_queries=("engineer",), search_mode="COMPANY_UNION"),
            person_filters=PersonFilters(is_current_role=True, seniority_bands=("senior",)),
            company_filters=CompanyFilters(company_ids=("target",), is_current_company=True),
        )
        profile = {
            "positions": [
                {"role_ids": ["founder"], "seniority_band": "c_suite", "company_id": "current", "is_current": True},
                {"role_ids": ["engineer"], "seniority_band": "senior", "company_id": "old", "is_current": False},
            ]
        }
        findings = validation_findings(profile, union, ResolvedSources(company_ids=("target",)), ("summary",))
        self.assertIn("role_ids_mismatch", findings["violations"])
        self.assertIn("seniority_band_mismatch", findings["violations"])
        from packs.search.backends.local.runner import LocalSearchRunner

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "local.duckdb"
            write_local_search_db(db)
            runner = LocalSearchRunner(str(db))
            local_union = replace(
                union,
                corpus=LocalCorpus(str(db)),
                role=RoleIntent(("software_engineer",), bm25_queries=("engineer",), search_mode="COMPANY_UNION"),
                company_filters=CompanyFilters(company_ids=("linkedin:company:one",), is_current_company=True),
            )
            compiled = runner.apply_hard_filters(
                local_union,
                ResolvedSources(company_ids=("linkedin:company:one",)),
            ).compiled["summary_filter"]
        self.assertIn("role_ids", str(compiled))
        self.assertNotIn("company_id", str(compiled))
        self.assertIn("is_current", str(compiled))

    def test_local_summary_filters_company_and_current_role_before_top_k(self):
        from packs.search.backends.local.runner import LocalSearchRunner

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "local.duckdb"
            write_local_search_db(db)
            import duckdb

            with duckdb.connect(str(db)) as conn:
                conn.execute(
                    "UPDATE local_summaries SET summary = CASE WHEN person_id = ? "
                    "THEN 'needle' WHEN person_id = ? THEN 'needle needle needle needle' ELSE summary END",
                    [PERSON_STANFORD, PERSON_OTHER],
                )
            runner = LocalSearchRunner(str(db))
            local = spec(
                corpus=LocalCorpus(str(db)),
                raw_request="needle",
                role=RoleIntent(bm25_queries=("needle",)),
                person_filters=PersonFilters(is_current_role=True),
                company_filters=CompanyFilters(company_ids=("linkedin:company:one",), is_current_company=True),
                bounds=SearchBounds(1, 1, 1),
            )
            sources = ResolvedSources(company_ids=("linkedin:company:one",))
            filters = runner.apply_hard_filters(local, sources)
            plan = SearchPlan(local, runner.capabilities(local), sources, ("retrieve",))
            rows = runner.retrieve_people(plan, filters)
        summary_ids = [row.person_id for row in rows if row.source_lanes == ("summary",)]
        self.assertEqual(summary_ids, [PERSON_STANFORD])

    def test_lookup_early_exit_local_and_remote_shaped(self):
        lookup = spec(
            profile=Profile.LOOKUP,
            lookup=LookupSpec("person_id", "p"),
            role=RoleIntent(),
            person_filters=PersonFilters(),
        )
        from packs.search.pipeline.models import PowersetCorpus

        for backend, corpus in ((Backend.LOCAL, lookup.corpus), (Backend.POWERSET, PowersetCorpus("s", ("o",)))):
            runner = FakeRunner(records=(CandidateRecord("p", backend=backend.value),))
            runner.capabilities = lambda value, backend=backend: RunnerCapabilities(
                backend, (), ("lookup",), False, False, ("person_id",)
            )
            result = run_with_runner(replace(lookup, backend=backend, corpus=corpus), runner)
            self.assertEqual(result.frontier.candidates[0].person_id, "p")
            self.assertNotIn("filter", runner.calls)

    def test_composition_root_lookup_does_not_snapshot_corpus(self):
        from packs.search.pipeline.search import run_search

        lookup_spec = spec(
            profile=Profile.LOOKUP,
            lookup=LookupSpec("person_id", "p"),
            role=RoleIntent(),
            person_filters=PersonFilters(),
        )
        with mock.patch("packs.search.backends.local.runner.LocalSearchRunner") as cls:
            runner = cls.return_value
            runner.capabilities.return_value = RunnerCapabilities(
                Backend.LOCAL, (), ("lookup",), False, True, ("person_id",)
            )
            runner.lookup_person.return_value = (CandidateRecord("p"),)
            runner.hydrate.side_effect = lambda frontier: frontier
            result = run_search(lookup_spec)
        self.assertEqual(result.status, "completed")
        runner.snapshot_corpus.assert_not_called()

    def test_local_lookup_executes_against_selected_duckdb(self):
        from packs.search.backends.local.runner import LocalSearchRunner

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "local.duckdb"
            write_local_search_db(db)
            runner = LocalSearchRunner(str(db))
            local = spec(
                profile=Profile.LOOKUP,
                corpus=LocalCorpus(str(db)),
                lookup=LookupSpec("person_id", PERSON_STANFORD),
                role=RoleIntent(),
                person_filters=PersonFilters(),
            )
            result = run_with_runner(local, runner)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.frontier.candidates[0].person_id, PERSON_STANFORD)

    def test_canonical_csv_equivalence(self):
        row = CandidateRecord(
            "p",
            source_lanes=("role",),
            matched_position_ids=("pos",),
            backend="local",
            hydrated_profile={"full_name": "Person", "headline": "Engineer", "location_raw": "SF"},
            hydration_disposition="hydrated",
            deterministic_score=1.2,
        )
        result = StageResult("gtm", "completed", CandidateFrontier.merge([row]))
        root = ROOT / ".powerpacks" / "test-layered-artifacts"
        left, right = root / "left", root / "right"
        import shutil

        shutil.rmtree(root, ignore_errors=True)
        try:
            persist_result(left, spec(), result)
            persist_result(right, spec(), result)
            self.assertEqual(Path(left, "candidates.csv").read_bytes(), Path(right, "candidates.csv").read_bytes())
            with Path(left, "candidates.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["rank"], "1")
            self.assertNotIn("person_id", rows[0])
            self.assertNotIn("full_name", rows[0])
            persisted = json.loads(Path(left, "result.json").read_text())
            self.assertEqual(
                set(persisted["artifact_paths"]),
                {
                    "search_spec_json",
                    "result_json",
                    "candidates_jsonl",
                    "candidates_csv",
                    "hard_filter_validation_json",
                    "manifest_json",
                },
            )
            validation = json.loads(Path(left, "hard-filter-validation.json").read_text())
            schema = json.loads((ROOT / "packs/search/schemas/reflect-hard-filter-validation.schema.json").read_text())
            jsonschema.validate(validation, schema, format_checker=jsonschema.FormatChecker())
            from packs.search.reflect.bench import _validate_hard_filter

            self.assertEqual(
                _validate_hard_filter(
                    validation,
                    case_id=validation["case_id"],
                    case_hash=validation["case_hash"],
                    corpus_hash=validation["corpus_snapshot_hash"],
                    reviewed_count=0,
                ),
                0,
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_sql_fan_in_is_eligible_and_survives_full_retrieval_limit(self):
        records = (CandidateRecord("retrieved", 10),)
        runner = FakeRunner(records=records, eligible_ids=("retrieved", "sql"))
        result = run_with_runner(spec(sql_candidates=(SqlCandidate("sql"),), bounds=SearchBounds(1, 2, 1)), runner)
        self.assertEqual({row.person_id for row in result.frontier.candidates}, {"retrieved", "sql"})
        excluded = run_with_runner(
            spec(sql_candidates=(SqlCandidate("outside"),)), FakeRunner(records=records, eligible_ids=("retrieved",))
        )
        self.assertNotIn("outside", {row.person_id for row in excluded.frontier.candidates})

    def test_unresolved_required_source_fails_closed(self):
        class Unresolved(FakeRunner):
            def resolve_sources(self, value):
                self.calls.append("resolve")
                return ResolvedSources(
                    records=(
                        {"source": "company", "input": "Missing Co", "required": True, "disposition": "unresolved"},
                    )
                )

        result = run_with_runner(
            spec(company_filters=CompanyFilters(company_names=("Missing Co",))),
            Unresolved(supported=("role_ids", "seniority_bands", "company_ids")),
        )
        self.assertEqual(result.status, "needs_input")
        self.assertNotIn("filter", result.errors)

    def test_powerset_lookup_excludes_unscoped_global_person(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        with (
            mock.patch.object(runner, "_scoped_lookup_ids", return_value={"allowed"}),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.postgres_client.fetch_person_rows",
                return_value=[{"id": "global"}],
            ),
        ):
            rows = runner.lookup_person(LookupSpec("person_id", "global"))
        self.assertEqual(rows, ())

    def test_powerset_supported_handle_lookup_is_set_scoped(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        fixture = [
            {"id": "allowed", "public_identifier": "person-handle"},
            {"id": "global", "public_identifier": "person-handle"},
        ]
        with (
            mock.patch.object(runner, "_scoped_lookup_ids", return_value={"allowed"}),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.postgres_client.fixture_rows",
                return_value=fixture,
            ),
        ):
            rows = runner.lookup_person(LookupSpec("handle", "person-handle"))
        self.assertEqual([row.person_id for row in rows], ["allowed"])

    def test_powerset_lookup_checks_only_matched_ids_without_scope_enumeration(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        with (
            mock.patch.object(
                runner,
                "_scoped_person_ids",
                side_effect=AssertionError("lookup must not enumerate the whole scope"),
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.postgres_client.fetch_person_rows",
                return_value=[{"id": "matched"}],
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.filter_only_rows_for_namespace",
                new=mock.AsyncMock(return_value=[{"base_id": "matched"}]),
            ) as scoped,
        ):
            rows = runner.lookup_person(LookupSpec("person_id", "matched"))
        self.assertEqual([row.person_id for row in rows], ["matched"])
        self.assertIn("base_id", str(scoped.await_args.args[1]))

    def test_remote_unresolved_education_preserves_input_disposition(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        remote = spec(
            backend=Backend.POWERSET,
            corpus=PowersetCorpus("set", ("operator",)),
            person_filters=PersonFilters(education_names=("Missing School",)),
        )
        with mock.patch(
            "packs.search.backends.turbopuffer.runner.education_resolution.resolve_name",
            new=mock.AsyncMock(return_value={"resolved_ids": []}),
        ):
            sources = runner.resolve_sources(remote)
        self.assertEqual(sources.unresolved_required_inputs, ("Missing School",))

    def test_remote_capabilities_do_not_depend_on_fixture_rows(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        remote = spec(backend=Backend.POWERSET, corpus=runner.corpus)
        with mock.patch(
            "packs.search.backends.turbopuffer.runner.postgres_client.fixture_rows",
            side_effect=AssertionError("fixtures are not capability contracts"),
        ):
            capabilities = runner.capabilities(remote)
        self.assertNotIn("email", capabilities.lookup_fields)
        self.assertNotIn("phone", capabilities.lookup_fields)
        self.assertFalse(capabilities.supports_complete_snapshot)
        self.assertIn("summary", capabilities.retrieval_lanes)
        self.assertIn("company_signal", capabilities.retrieval_lanes)

    def test_remote_email_and_phone_lookup_are_unsupported_before_sql(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        remote = spec(
            profile=Profile.LOOKUP,
            backend=Backend.POWERSET,
            corpus=runner.corpus,
            lookup=LookupSpec("email", "person@example.com"),
            role=RoleIntent(),
            person_filters=PersonFilters(),
        )
        with mock.patch.object(runner, "lookup_person") as lookup:
            result = run_with_runner(remote, runner)
        self.assertEqual(result.status, "unsupported_capability")
        lookup.assert_not_called()

    def test_remote_company_archetype_resolves_to_company_ids(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        remote = spec(
            backend=Backend.POWERSET,
            corpus=runner.corpus,
            company_filters=CompanyFilters(
                sector_types=("software",),
                technology_types=("ai",),
                entity_types=("startup",),
                funding_stage_min="seed",
                headcount_max=100,
            ),
        )
        with (
            mock.patch.dict("os.environ", {"POWERPACKS_LOCAL_SEARCH_DB": "/should/not/be/read"}),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.company_resolution.allowed_operator_ids_from_payload",
                side_effect=AssertionError("typed remote resolution must not use ambient operator scope"),
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.company_resolution.filter_only_company_rows",
                new=mock.AsyncMock(
                    return_value={
                        "rows": [{"id": "company-1"}],
                        "completed": True,
                        "truncated": False,
                    }
                ),
            ) as resolve,
        ):
            sources = runner.resolve_sources(remote)
        self.assertEqual(sources.company_ids, ("company-1",))
        compiled = resolve.await_args.args[0]
        self.assertIn("sector_types", str(compiled))
        self.assertIn("technology_types", str(compiled))
        self.assertIn("funding_stage", str(compiled))
        self.assertIn("headcount", str(compiled))
        self.assertIn("allowed_operator_ids", str(compiled))
        findings = validation_findings(
            {"positions": [{"company_id": "company-1", "is_current": True}]},
            remote,
            sources,
            ("role",),
        )
        self.assertNotIn("sector_types_unknown", findings["unknowns"])
        self.assertNotIn("technology_types_unknown", findings["unknowns"])

        with mock.patch(
            "packs.search.backends.turbopuffer.runner.company_resolution.filter_only_company_rows",
            new=mock.AsyncMock(
                return_value={
                    "rows": [{"id": "company-1"}],
                    "completed": False,
                    "truncated": True,
                }
            ),
        ):
            incomplete = runner.resolve_sources(remote)
        self.assertEqual(len(incomplete.unresolved_required_inputs), 1)
        self.assertTrue(incomplete.records[-1]["truncated"])

    def test_remote_company_name_only_resolution_is_explicitly_operator_scoped(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator-explicit",)))
        remote = spec(
            backend=Backend.POWERSET,
            corpus=runner.corpus,
            company_filters=CompanyFilters(company_names=("Scoped Co",)),
        )
        with (
            mock.patch.dict("os.environ", {"POWERPACKS_LOCAL_SEARCH_DB": "/ambient/local.duckdb"}),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.company_resolution.allowed_operator_ids_from_payload",
                side_effect=AssertionError("ambient operator resolution must not run"),
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.company_resolution.exact_name_lookup",
                new=mock.AsyncMock(return_value=[{"id": "company-1", "name": "Scoped Co"}]),
            ) as exact,
        ):
            sources = runner.resolve_sources(remote)
        self.assertEqual(sources.company_ids, ("company-1",))
        compiled = exact.await_args.args[1]
        self.assertIsNotNone(compiled)
        self.assertIn("allowed_operator_ids", str(compiled))
        self.assertIn("operator-explicit", str(compiled))

    def test_remote_retrieval_preserves_structured_row_evidence(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus, SearchPlan

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        remote = spec(
            backend=Backend.POWERSET, corpus=runner.corpus, role=RoleIntent(("engineer",), bm25_queries=("engineer",))
        )
        row = {
            "base_id": "p",
            "position_id": "pos",
            "position_title": "Engineer",
            "raw_title": "Eng",
            "role_ids": ["engineer"],
            "company_id": "co",
            "city": "San Francisco",
            "state": "California",
            "country": "United States",
            "metro_areas": ["Bay Area"],
            "seniority_band": "senior",
            "role_track": "engineering",
            "is_current": True,
            "total_years_experience": 8,
            "company_sector_types": ["software"],
            "company_entity_types": ["startup"],
            "company_headcount": 50,
            "company_stage": "series_a",
            "score": 0.9,
        }
        capabilities = runner.capabilities(remote)
        plan = SearchPlan(remote, capabilities, ResolvedSources(), ("retrieve",))
        with (
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.hybrid_role_rows",
                new=mock.AsyncMock(return_value=[row]),
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.hybrid_summary_rows",
                new=mock.AsyncMock(return_value=[]),
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.semantic_company_signal_rows",
                new=mock.AsyncMock(return_value=[]),
            ),
        ):
            candidate = runner.retrieve_people(plan, HardFilterSet(1, ("p",), {"filter": ("And", [])}))[0]
        self.assertEqual(candidate.structured["raw_title"], "Eng")
        self.assertEqual(candidate.structured["company_stage"], "series_a")
        self.assertEqual(candidate.matched_position_ids, ("pos",))

    def test_remote_summary_and_company_signal_lanes_preserve_grain_and_filters(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus, SearchPlan

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        remote = spec(
            backend=Backend.POWERSET,
            corpus=runner.corpus,
            role=RoleIntent(("engineer",), bm25_queries=("distributed systems",)),
        )
        plan = SearchPlan(remote, runner.capabilities(remote), ResolvedSources(), ("retrieve",))
        compiled = {
            "filter": ("And", [("allowed_operator_ids", "ContainsAny", ["operator"]), ("role_ids", "ContainsAny", ["engineer"])]),
            "summary_namespace_filter": (
                "And",
                [("allowed_operator_ids", "ContainsAny", ["operator"]), ("id", "In", ["summary-person"])],
            ),
            "signal_filter": ("allowed_operator_ids", "ContainsAny", ["operator"]),
        }
        summary = {"id": "summary-person", "person_id": "summary-person", "score": 0.7}
        signal = {"company_id": "signal-company", "score": 0.8}
        signal_person = {
            "id": "signal-position",
            "base_id": "signal-person",
            "company_id": "signal-company",
            "position_title": "Infrastructure Engineer",
        }
        enumerate_rows = mock.AsyncMock(
            return_value={"rows": [signal_person], "completed": True, "truncated": False}
        )
        with (
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.hybrid_role_rows",
                new=mock.AsyncMock(return_value=[]),
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.hybrid_summary_rows",
                new=mock.AsyncMock(return_value=[summary]),
            ) as summary_search,
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.semantic_company_signal_rows",
                new=mock.AsyncMock(return_value=[signal]),
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.enumerate_filter_only_rows_for_namespace",
                new=enumerate_rows,
            ),
        ):
            rows = runner.retrieve_people(plan, HardFilterSet(2, ("summary-person", "signal-person"), compiled))

        by_lane = {row.source_lanes[0]: row for row in rows}
        self.assertEqual(by_lane["summary"].matched_position_ids, ())
        self.assertEqual(by_lane["summary"].person_id, "summary-person")
        self.assertEqual(by_lane["company_signal"].matched_position_ids, ("signal-position",))
        self.assertEqual(by_lane["company_signal"].person_id, "signal-person")
        self.assertEqual(summary_search.await_args.args[1], compiled["summary_namespace_filter"])
        people_filter = enumerate_rows.await_args.args[1]
        self.assertIn(("role_ids", "ContainsAny", ["engineer"]), people_filter[1][0][1])
        self.assertEqual(people_filter[1][1], ("company_id", "In", ["signal-company"]))

    def test_remote_summary_eligible_people_are_compiled_before_top_k(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        remote = spec(
            backend=Backend.POWERSET,
            corpus=runner.corpus,
            role=RoleIntent(("engineer",), bm25_queries=("engineer",)),
            person_filters=PersonFilters(
                cities=("San Francisco",), seniority_bands=("senior",), is_current_role=True
            ),
            company_filters=CompanyFilters(company_ids=("target",), is_current_company=True),
        )
        enumerations = mock.AsyncMock(
            side_effect=[
                {
                    "rows": [{"base_id": "summary-eligible"}],
                    "completed": True,
                    "truncated": False,
                },
                {
                    "rows": [{"base_id": "role-eligible"}],
                    "completed": True,
                    "truncated": False,
                },
            ]
        )
        with mock.patch(
            "packs.search.backends.turbopuffer.runner.storage.enumerate_filter_only_rows_for_namespace",
            new=enumerations,
        ):
            filters = runner.apply_hard_filters(remote, ResolvedSources(company_ids=("target",)))

        summary_filter = filters.compiled["summary_namespace_filter"]
        self.assertEqual(summary_filter[1][1], ("id", "In", ["summary-eligible"]))
        people_summary_filter = enumerations.await_args_list[0].args[1]
        self.assertIn(("city", "In", ["San Francisco"]), people_summary_filter[1])
        self.assertIn(("seniority_band", "In", ["senior"]), people_summary_filter[1])
        self.assertIn(("role_ids", "ContainsAny", ["engineer"]), people_summary_filter[1])
        self.assertIn(("company_id", "In", ["target"]), people_summary_filter[1])
        self.assertIn(("is_current", "Eq", True), people_summary_filter[1])

    def test_snapshot_cli_restricts_output_to_reflect_state(self):
        script = ROOT / "packs/search/reflect/capture_snapshot.py"
        rejected = subprocess.run(
            [
                sys.executable,
                str(script),
                "--spec",
                "/missing",
                "--scope",
                "local",
                "--evidence-person-ids",
                "/missing",
                "--out",
                "/var/tmp/out.json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("--out must remain", rejected.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "local.duckdb"
            write_local_search_db(db)
            spec_path = Path(tmp) / "spec.json"
            ids_path = Path(tmp) / "ids.json"
            spec_path.write_text(json.dumps(spec(corpus=LocalCorpus(str(db))).to_dict()))
            ids_path.write_text(json.dumps([PERSON_STANFORD]))
            out = ROOT / ".powerpacks/reflect/test-capture-snapshot.json"
            try:
                allowed = subprocess.run(
                    [
                        sys.executable,
                        str(script),
                        "--spec",
                        str(spec_path),
                        "--scope",
                        "local",
                        "--evidence-person-ids",
                        str(ids_path),
                        "--out",
                        str(out),
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(allowed.returncode, 0, allowed.stderr)
                self.assertTrue(out.exists())
            finally:
                out.unlink(missing_ok=True)

    def test_nanoclaw_result_reader_shape_uses_canonical_frontier(self):
        row = CandidateRecord(
            "p",
            backend="local",
            source_lanes=("role",),
            hydrated_profile={"full_name": "Person"},
            hydration_disposition="hydrated",
            deterministic_score=1.0,
        )
        state = StageResult("gtm", "completed", CandidateFrontier.merge([row])).to_dict()
        rendered = result_rows(state)
        self.assertEqual(rendered[0]["name"], "Person")
        self.assertEqual(rendered[0]["vertical_sources"], "role")

    def test_snapshot_truncation_fails_closed(self):
        snapshot = {
            "backend": "powerset",
            "source": "pr_b_runner_snapshot",
            "verification_status": "verified_comparable",
            "set_id": "s",
            "operator_scope_hash": HASH,
            "membership_hash": HASH,
            "namespace_schema_hashes": {"people": HASH},
            "native_content_version": "v1",
            "evidence_hashes": {},
            "enumeration_complete": False,
            "enumeration_truncated": True,
            "enumerated_record_count": 100,
        }
        errors = validate_snapshot(snapshot)
        self.assertTrue(any("incomplete" in error for error in errors))
        self.assertTrue(any("truncated" in error for error in errors))
        from packs.search.pipeline.models import PowersetCorpus
        from packs.search.pipeline.search import run_search

        remote = spec(
            backend=Backend.POWERSET,
            corpus=PowersetCorpus("s", ("o",)),
            role=RoleIntent(),
            person_filters=PersonFilters(),
        )
        derived = {
            "set_id": "s",
            "operator_scope_hash": "b" * 64,
            "membership_hash": "c" * 64,
            "namespace_schema_hashes": {"people": "d" * 64},
            "native_content_version": None,
            "scoped_records_hash": None,
            "verification_status": "unverified_non_comparable",
            "source": "pr_b_runner_snapshot",
            "observed_at": "2026-07-31T00:00:00Z",
        }
        output = ROOT / ".powerpacks/search-runs/test-remote-derived-corpus"
        import shutil

        shutil.rmtree(output, ignore_errors=True)
        try:
            with mock.patch("packs.search.backends.turbopuffer.runner.TurboPufferSearchRunner") as cls:
                runner = cls.return_value
                runner.snapshot_corpus.return_value = derived
                runner.capabilities.return_value = RunnerCapabilities(Backend.POWERSET, (), ("role",), False, False)
                runner.resolve_sources.return_value = ResolvedSources()
                runner.apply_hard_filters.return_value = HardFilterSet(0, (), {})
                result = run_search(remote, output_dir=output)
            observed_spec = runner.capabilities.call_args.args[0]
            self.assertEqual(observed_spec.corpus.operator_scope_hash, "b" * 64)
            persisted = json.loads((output / "search_spec.json").read_text())
            self.assertEqual(persisted["corpus"]["membership_hash"], "c" * 64)
            self.assertEqual(result.corpus_observation["verification_status"], "unverified_non_comparable")
        finally:
            shutil.rmtree(output, ignore_errors=True)


class ImportFirewallTests(unittest.TestCase):
    def test_each_runner_imports_with_opposite_backend_physically_blocked(self):
        scripts = [
            ("packs.search.backends.local.runner", {"turbopuffer", "turbopuffer_search_backend", "postgres_client"}),
            ("packs.search.backends.turbopuffer.runner", {"duckdb", "local_duckdb_store", "local_search_backend"}),
        ]
        for module, blocked in scripts:
            code = f"""\nimport importlib, sys\nblocked={blocked!r}\nclass Blocker:\n def find_spec(self, fullname, path=None, target=None):\n  if fullname.split('.')[0] in blocked: raise ImportError(fullname)\nsys.meta_path.insert(0, Blocker())\nimportlib.import_module({module!r})\n"""
            process = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)

    def test_composition_root_imports_only_selected_branch(self):
        code = """
import sys
from unittest import mock
from packs.search.pipeline.models import *
from packs.search.pipeline import search
blocked={'packs.search.backends.turbopuffer.runner'}
class Blocker:
 def find_spec(self, fullname, path=None, target=None):
  if fullname in blocked: raise ImportError(fullname)
sys.meta_path.insert(0, Blocker())
corpus=LocalCorpus('/var/tmp/no-open.duckdb')
spec=SearchSpec('search.spec.v1','q',Profile.GTM,Backend.LOCAL,corpus)
with mock.patch('packs.search.backends.local.runner.LocalSearchRunner') as runner:
 runner.return_value.capabilities.return_value=RunnerCapabilities(Backend.LOCAL,(),('role',),False,True)
 runner.return_value.snapshot_corpus.return_value={'scoped_records_hash':'a'*64,'namespace_schema_hashes':{},'membership_hash':'a'*64}
 runner.return_value.resolve_sources.return_value=ResolvedSources()
 runner.return_value.apply_hard_filters.return_value=HardFilterSet(0,(),{})
 search.run_search(spec)
"""
        process = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)


if __name__ == "__main__":
    unittest.main()
