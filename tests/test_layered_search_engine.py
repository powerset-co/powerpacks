from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
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
    RecruitingInput,
    REVIEW_POOL_MAX_PERSON_IDS,
    REVIEW_POOL_PERSON_ID_MAX_LENGTH,
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
from packs.search.reflect.snapshots import canonical_hash, validate_snapshot
from adapters.nanoclaw.primitives.view_search_results.search_tui import result_rows
from tests.local_search_fixture import (
    COMPANY_ONE,
    PERSON_CONTEXT_ONLY,
    PERSON_OTHER,
    PERSON_PROFILE_ONLY,
    PERSON_STANFORD,
    STANFORD_ID,
    write_local_search_db,
)

ROOT = Path(__file__).resolve().parents[1]
HASH = "a" * 64


def turbopuffer_contract_schema(name):
    contract = json.loads(
        (ROOT / "packs/search/contracts/turbopuffer" / f"{name}.namespace.json").read_text()
    )
    schema = {row["name"]: {"type": row["type"]} for row in contract["attributes"]}
    if contract.get("vector"):
        schema["vector"] = {"type": "vector"}
    return schema


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

    def test_review_pool_limits_match_schema_and_reject_instead_of_truncating(self):
        schema = json.loads((ROOT / "packs/search/schemas/search-spec.schema.json").read_text())
        pool_schema = schema["properties"]["recruiting"]["oneOf"][1]["properties"]["review_pool_person_ids"]
        bounds = pool_schema["allOf"][1]
        self.assertEqual(bounds["maxItems"], REVIEW_POOL_MAX_PERSON_IDS)
        self.assertEqual(bounds["items"]["maxLength"], REVIEW_POOL_PERSON_ID_MAX_LENGTH)

        accepted = tuple(f"synthetic-person-{index}" for index in range(REVIEW_POOL_MAX_PERSON_IDS))
        self.assertEqual(len(RecruitingInput("Synthetic role", review_pool_person_ids=accepted).review_pool_person_ids), 500)
        self.assertEqual(
            RecruitingInput("Synthetic role", review_pool_person_ids=("x" * REVIEW_POOL_PERSON_ID_MAX_LENGTH,)).review_pool_person_ids,
            ("x" * REVIEW_POOL_PERSON_ID_MAX_LENGTH,),
        )
        for rejected, message in (
            (accepted + ("synthetic-person-over-limit",), "cannot exceed 500 IDs"),
            (("x" * (REVIEW_POOL_PERSON_ID_MAX_LENGTH + 1),), "cannot exceed 256 characters"),
        ):
            with self.assertRaisesRegex(ValueError, message):
                RecruitingInput("Synthetic role", review_pool_person_ids=rejected)
            value = SearchSpec(
                "search.spec.v1", "synthetic recruiting", Profile.RECRUITING, Backend.LOCAL,
                LocalCorpus("/var/tmp/synthetic.duckdb"), recruiting=RecruitingInput("Synthetic role"),
            ).to_dict()
            value["recruiting"]["review_pool_person_ids"] = list(rejected)
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(value, schema)

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

    def test_investor_intent_and_luna_rank_defaults_are_typed_and_persisted(self):
        value = spec(company_filters=CompanyFilters(investor_names=("a16z", "a16z")))
        self.assertEqual(value.company_filters.investor_names, ("a16z",))
        self.assertEqual(value.rank_model, "gpt-5.6-luna")
        self.assertEqual(value.rank_reasoning_effort, "medium")
        parsed = SearchSpec.from_dict(value.to_dict())
        self.assertEqual(parsed.company_filters.investor_names, ("a16z",))
        schema = json.loads((ROOT / "packs/search/schemas/search-spec.schema.json").read_text())
        jsonschema.validate(parsed.to_dict(), schema)

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
            self.assertEqual(candidate.matched_position_indexes, (0,))
            self.assertEqual(candidate.hydrated_profile["total_interactions"], 12)
            self.assertEqual(
                candidate.hydrated_profile["years_of_experience"],
                candidate.hydrated_profile["total_years_experience"],
            )
            self.assertTrue(candidate.hard_filter_evidence["validated"])
            self.assertEqual(result.hard_filter_validation["violation_count"], 0)

    def test_shared_current_role_family_gate_protects_gtm_ranking(self):
        engineering = CandidateRecord(
            "engineering",
            0.8,
            hydrated_profile={
                "positions": [{"is_current": True, "role_track": "Engineering", "seniority_band": "senior"}]
            },
        )
        investor = CandidateRecord(
            "investor",
            0.9,
            hydrated_profile={
                "positions": [
                    {"is_current": True, "role_track": "investing", "seniority_band": "senior"},
                    {"is_current": False, "role_track": "Engineering", "seniority_band": "senior"},
                ]
            },
        )
        run_spec = spec(
            role=RoleIntent(titles=("Senior Backend Engineer",)),
            person_filters=PersonFilters(seniority_bands=("senior",), is_current_role=True),
        )
        result = run_with_runner(
            run_spec,
            FakeRunner(records=(investor, engineering), supported=("seniority_bands", "is_current_role")),
        )

        self.assertEqual([row.person_id for row in result.frontier.candidates], ["engineering"])
        self.assertEqual(result.hard_filter_validation["violation_count"], 1)
        self.assertEqual(
            result.hard_filter_validation["violations"][0]["reason_code"],
            "current_role_family_mismatch",
        )

    def test_current_role_family_gate_is_conservative_and_branch_aware(self):
        target = spec(
            role=RoleIntent(titles=("Senior Backend Engineer",)),
            person_filters=PersonFilters(is_current_role=True),
        )
        concurrent = {
            "positions": [
                {"is_current": True, "role_track": "Finance"},
                {"is_current": True, "role_track": "Engineering"},
            ]
        }
        unknown = {"positions": [{"is_current": True, "role_track": "Unmapped Hybrid"}]}
        self.assertNotIn(
            "current_role_family_mismatch",
            validation_findings(concurrent, target, ResolvedSources())["violations"],
        )
        self.assertNotIn(
            "current_role_family_mismatch",
            validation_findings(unknown, target, ResolvedSources())["violations"],
        )
        semantic_only = replace(target, role=RoleIntent(bm25_queries=("backend infrastructure AI",)))
        finance = {"positions": [{"is_current": True, "role_track": "Finance"}]}
        self.assertNotIn(
            "current_role_family_mismatch",
            validation_findings(finance, semantic_only, ResolvedSources())["violations"],
        )
        historical = replace(target, person_filters=PersonFilters(is_current_role=False))
        self.assertNotIn(
            "current_role_family_mismatch",
            validation_findings(finance, historical, ResolvedSources())["violations"],
        )
        company_union = replace(target, role=replace(target.role, search_mode="COMPANY_UNION"))
        self.assertNotIn(
            "current_role_family_mismatch",
            validation_findings(finance, company_union, ResolvedSources(), ("company_union",))["violations"],
        )

    def test_current_role_family_gate_preserves_target_hybrids_and_ambiguous_target_titles(self):
        target = spec(
            role=RoleIntent(titles=("Senior Backend Engineer",)),
            person_filters=PersonFilters(is_current_role=True),
        )
        partial = {
            "positions": [
                {"is_current": True, "role_track": "investing"},
                {"is_current": True, "position_title": "Founding Engineer"},
            ]
        }
        self.assertNotIn(
            "current_role_family_mismatch",
            validation_findings(partial, target, ResolvedSources())["violations"],
        )
        for title in (
            "Marketing Operations Manager",
            "People Operations Manager",
            "Financial Advisor",
            "Air Traffic Controller",
        ):
            ambiguous = replace(target, role=RoleIntent(titles=(title,)))
            self.assertNotIn(
                "current_role_family_mismatch",
                validation_findings(
                    {"positions": [{"is_current": True, "role_track": "Engineering"}]},
                    ambiguous,
                    ResolvedSources(),
                )["violations"],
            )

    def test_current_engineering_titles_override_mislabeled_role_track(self):
        target = spec(
            role=RoleIntent(titles=("Senior Backend Engineer",)),
            person_filters=PersonFilters(is_current_role=True),
        )
        for title in (
            "Staff Engineer",
            "Principal Engineer",
            "VP of Engineering",
            "VP Engineering",
            "Vice President of Engineering",
            "Director of Engineering",
            "Engineering Director",
            "Head of Engineering",
            "Software Engineer II",
            "Sr. Engineer",
        ):
            with self.subTest(title=title):
                profile = {
                    "positions": [{
                        "is_current": True,
                        "position_title": title,
                        "role_track": "marketing",
                    }]
                }
                self.assertNotIn(
                    "current_role_family_mismatch",
                    validation_findings(profile, target, ResolvedSources())["violations"],
                )

    def test_marketing_only_title_still_fails_engineering_family_gate(self):
        target = spec(
            role=RoleIntent(titles=("Senior Backend Engineer",)),
            person_filters=PersonFilters(is_current_role=True),
        )
        profile = {
            "positions": [{
                "is_current": True,
                "position_title": "Marketing Manager",
                "role_track": "marketing",
            }]
        }
        self.assertIn(
            "current_role_family_mismatch",
            validation_findings(profile, target, ResolvedSources())["violations"],
        )

    def test_unmapped_concurrent_role_does_not_override_positive_mismatch_evidence(self):
        target = spec(
            role=RoleIntent(titles=("Senior Backend Engineer",)),
            person_filters=PersonFilters(is_current_role=True),
        )
        profile = {
            "positions": [
                {"is_current": True, "role_track": "investing"},
                {"is_current": True, "position_title": "Sr Principal"},
            ]
        }
        self.assertIn(
            "current_role_family_mismatch",
            validation_findings(profile, target, ResolvedSources())["violations"],
        )

    def test_current_role_family_gate_ignores_malformed_position_rows(self):
        target = spec(
            role=RoleIntent(titles=("Senior Backend Engineer",)),
            person_filters=PersonFilters(is_current_role=True),
        )
        profile = {
            "positions": [
                "malformed-row",
                {"is_current": True, "role_track": "investing"},
            ]
        }

        findings = validation_findings(profile, target, ResolvedSources())

        self.assertEqual(findings["violations"], ("current_role_family_mismatch",))

    def test_matching_structured_current_role_completes_missing_position_family(self):
        target = spec(
            role=RoleIntent(titles=("Senior Backend Engineer",)),
            person_filters=PersonFilters(is_current_role=True),
        )
        profile = {
            "positions": [
                {
                    "is_current": True,
                    "position_title": "Lead Technical Recruiter",
                    "role_track": "",
                },
                {"is_current": True, "position_title": "Board Member", "role_track": "Education"},
            ]
        }
        structured = {
            "is_current": True,
            "position_title": "Lead Technical Recruiter",
            "role_track": "people_hr",
            "role_ids": ["recruiter"],
        }
        self.assertIn(
            "current_role_family_mismatch",
            validation_findings(
                profile, target, ResolvedSources(), structured=structured
            )["violations"],
        )
        self.assertNotIn(
            "current_role_family_mismatch",
            validation_findings(
                {"positions": [profile["positions"][0]]},
                target,
                ResolvedSources(),
                structured={**structured, "position_title": "Different Current Role"},
            )["violations"],
        )

    def test_frontier_merge_preserves_role_evidence_across_summary_overlap(self):
        role = CandidateRecord(
            "same-person",
            matched_position_ids=("role-position",),
            source_lanes=("role",),
            structured={
                "position_id": "role-position",
                "position_title": "Lead Technical Recruiter",
                "company_id": "company-a",
                "is_current": True,
                "role_track": "people_hr",
                "role_ids": ["recruiter"],
            },
        )
        summary = CandidateRecord(
            "same-person",
            source_lanes=("summary",),
            structured={
                "position_title": None,
                "role_track": None,
                "role_ids": None,
                "is_current": None,
            },
        )
        target = spec(
            role=RoleIntent(titles=("Senior Backend Engineer",)),
            person_filters=PersonFilters(is_current_role=True),
        )
        profile = {
            "positions": [{
                "position_id": "role-position",
                "position_title": "Lead Technical Recruiter",
                "company_id": "company-a",
                "is_current": True,
            }]
        }
        for records in ((role, summary), (summary, role)):
            merged = CandidateFrontier.merge(records).candidates[0]
            self.assertIn(
                "current_role_family_mismatch",
                validation_findings(
                    profile, target, ResolvedSources(), structured=merged.structured
                )["violations"],
            )

    def test_frontier_merge_preserves_concurrent_target_role_in_any_order(self):
        target_role = CandidateRecord(
            "same-person",
            matched_position_ids=("engineering-position",),
            structured={
                "position_id": "engineering-position",
                "position_title": "Founding Engineer",
                "company_id": "company-a",
                "is_current": True,
                "role_track": "engineering",
                "role_ids": ["software_engineer"],
            },
        )
        investor_role = CandidateRecord(
            "same-person",
            matched_position_ids=("investor-position",),
            structured={
                "position_id": "investor-position",
                "position_title": "Investor",
                "company_id": "company-b",
                "is_current": True,
                "role_track": "investing",
                "role_ids": ["angel_investor"],
            },
        )
        target = spec(
            role=RoleIntent(titles=("Senior Backend Engineer",)),
            person_filters=PersonFilters(is_current_role=True),
        )
        profile = {
            "positions": [
                {
                    "position_id": "engineering-position",
                    "position_title": "Founding Engineer",
                    "company_id": "company-a",
                    "is_current": True,
                },
                {
                    "position_id": "investor-position",
                    "position_title": "Investor",
                    "company_id": "company-b",
                    "is_current": True,
                },
            ]
        }
        for records in ((target_role, investor_role), (investor_role, target_role)):
            merged = CandidateFrontier.merge(records).candidates[0]
            self.assertNotIn(
                "current_role_family_mismatch",
                validation_findings(
                    profile, target, ResolvedSources(), structured=merged.structured
                )["violations"],
            )

    def test_associated_target_contribution_overrides_coarse_non_target_track(self):
        target = spec(
            role=RoleIntent(titles=("Senior Backend Engineer",)),
            person_filters=PersonFilters(is_current_role=True),
        )
        profile = {
            "positions": [{
                "position_id": "hybrid-position",
                "position_title": "Investor and Technical Builder",
                "company_id": "company-a",
                "is_current": True,
                "role_track": "investing",
            }]
        }
        structured = {
            "position_id": "hybrid-position",
            "position_title": "Investor and Technical Builder",
            "company_id": "company-a",
            "is_current": True,
            "role_track": "investing",
            "role_ids": ["angel_investor", "software_engineer"],
        }
        self.assertNotIn(
            "current_role_family_mismatch",
            validation_findings(
                profile, target, ResolvedSources(), structured=structured
            )["violations"],
        )

    def test_duplicate_taxonomy_role_preserves_all_target_families(self):
        target = spec(
            role=RoleIntent(role_ids=("developer_advocate",)),
            person_filters=PersonFilters(is_current_role=True),
        )
        engineering = {
            "positions": [{"is_current": True, "role_track": "Engineering", "role_ids": ["developer_advocate"]}]
        }
        self.assertNotIn(
            "current_role_family_mismatch",
            validation_findings(engineering, target, ResolvedSources())["violations"],
        )

    def test_exact_taxonomy_titles_resolve_before_seniority_stripping(self):
        finance = {"positions": [{"is_current": True, "role_track": "Finance"}]}
        for title in (
            "Account Manager",
            "Creative Director",
            "Chief Technology Officer",
            "Senior Engineering Manager",
        ):
            target = spec(
                role=RoleIntent(titles=(title,)),
                person_filters=PersonFilters(is_current_role=True),
            )
            self.assertIn(
                "current_role_family_mismatch",
                validation_findings(finance, target, ResolvedSources())["violations"],
            )

    def test_local_hydration_restores_profile_only_position_fallbacks(self):
        from packs.search.backends.local.runner import LocalSearchRunner

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "local.duckdb"
            write_local_search_db(db)
            runner = LocalSearchRunner(str(db))
            frontier = CandidateFrontier.merge(
                [
                    CandidateRecord(PERSON_PROFILE_ONLY, matched_position_ids=("profile-role",), backend="local"),
                    CandidateRecord(PERSON_CONTEXT_ONLY, matched_position_ids=("context-role",), backend="local"),
                ]
            )

            hydrated = runner.hydrate(frontier)

            self.assertTrue(all(isinstance(row, CandidateRecord) for row in hydrated.candidates))
            by_id = {row.person_id: row for row in hydrated.candidates}
            self.assertEqual(by_id[PERSON_PROFILE_ONLY].hydrated_profile["positions"][0]["title"], "Founder")
            self.assertEqual(by_id[PERSON_PROFILE_ONLY].matched_position_indexes, (0,))
            self.assertEqual(by_id[PERSON_CONTEXT_ONLY].hydrated_profile["positions"][0]["title"], "Staff Engineer")
            self.assertEqual(by_id[PERSON_CONTEXT_ONLY].matched_position_indexes, (0,))
            findings = validation_findings(
                by_id[PERSON_PROFILE_ONLY].hydrated_profile,
                spec(
                    role=RoleIntent(("founder",), (), ()),
                    person_filters=PersonFilters(seniority_bands=("c_suite",), is_current_role=True),
                ),
                ResolvedSources(),
                ("role",),
            )
            self.assertEqual(findings, {"violations": (), "unknowns": ()})

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

    def test_lookup_gtm_runner_rejects_recruiting_before_capability_access(self):
        runner = FakeRunner()
        recruiting = spec(
            profile=Profile.RECRUITING,
            recruiting=RecruitingInput(
                "Synthetic recruiting role brief with enough content for deterministic validation."
            ),
        )
        with self.assertRaisesRegex(ValueError, "does not accept recruiting"):
            run_with_runner(recruiting, runner)
        self.assertEqual(runner.calls, [])

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
        configured = replace(semantic, rank_approved=True)

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
            [
                {
                    "value": "fit: company and role evidence",
                    "temporal": "all",
                    "meaning": "general",
                }
            ],
        )
        self.assertEqual(rerank.await_args.kwargs["model"], "gpt-5.6-luna")
        self.assertEqual(rerank.await_args.kwargs["reasoning_effort"], "medium")
        self.assertEqual(outcomes[1], SemanticOutcome("two", None, "timeout"))

    def test_evidence_criterion_rejects_unsupported_weight(self):
        with self.assertRaisesRegex(ValueError, "weight must be 1.0"):
            EvidenceCriterion("fit", "company and role evidence", 2.0)
        with self.assertRaisesRegex(ValueError, "weight must be 1.0"):
            EvidenceCriterion.from_dict(
                {"name": "fit", "description": "company and role evidence", "weight": 0.5}
            )

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
                company_filters=CompanyFilters(company_ids=(COMPANY_ONE,), is_current_company=True),
            )
            compiled = runner.apply_hard_filters(
                local_union,
                ResolvedSources(company_ids=(COMPANY_ONE,)),
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
                company_filters=CompanyFilters(company_ids=(COMPANY_ONE,), is_current_company=True),
                bounds=SearchBounds(1, 1, 1),
            )
            sources = ResolvedSources(company_ids=(COMPANY_ONE,))
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
            persist_result(left, spec(), result, allowed_root=root)
            persist_result(right, spec(), result, allowed_root=root)
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
        self.assertEqual(scoped.await_args.args[2], ["base_id"])

    def test_powerset_scope_enumeration_uses_live_base_id_identity(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        runner._namespace_schemas["people"] = frozenset(
            {"base_id", "allowed_operator_ids"}
        )
        enumeration = mock.AsyncMock(return_value={
            "rows": [{"base_id": "person-1"}],
            "completed": True,
            "truncated": False,
        })
        with mock.patch(
            "packs.search.backends.turbopuffer.runner.storage.enumerate_filter_only_rows_for_namespace",
            new=enumeration,
        ):
            self.assertEqual(runner._scoped_person_ids(), {"person-1"})
        self.assertEqual(enumeration.await_args.args[2], ["base_id"])

    def test_remote_hydration_exposes_experience_for_strict_validation(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        remote = spec(
            backend=Backend.POWERSET,
            corpus=runner.corpus,
            role=RoleIntent(),
            person_filters=PersonFilters(years_experience_min=3),
        )
        frontier = CandidateFrontier.merge((CandidateRecord("synthetic-person", backend="powerset"),))
        with mock.patch(
            "packs.search.backends.turbopuffer.runner.postgres_client.fetch_person_rows",
            return_value=[{
                "id": "synthetic-person",
                "hydrated_context": {
                    "positions": [{
                        "title": "Engineer",
                        "start_date": "2018-01-01",
                        "end_date": "2024-01-01",
                    }],
                },
            }],
        ):
            hydrated = runner.hydrate(frontier).candidates[0]

        profile = hydrated.hydrated_profile
        self.assertEqual(profile["years_of_experience"], profile["total_years_experience"])
        self.assertGreater(profile["total_years_experience"], 3)
        self.assertEqual(
            validation_findings(profile, remote, ResolvedSources(), hydrated.source_lanes),
            {"violations": (), "unknowns": ()},
        )

        below_minimum = {**profile, "years_of_experience": 2.0, "total_years_experience": 2.0}
        self.assertEqual(
            validation_findings(below_minimum, remote, ResolvedSources())["violations"],
            ("years_experience_min_mismatch",),
        )
        truly_unknown = {"positions": [{"title": "Engineer"}]}
        self.assertEqual(
            validation_findings(truly_unknown, remote, ResolvedSources())["unknowns"],
            ("years_experience_min_unknown",),
        )
        with mock.patch(
            "packs.search.backends.turbopuffer.runner.postgres_client.fetch_person_rows",
            return_value=[{
                "id": "synthetic-person",
                "hydrated_context": {
                    "positions": [{
                        "title": "Engineer",
                        "start_date": "2018-01-01",
                        "end_date": "2024-01-01",
                    }],
                    "years_of_experience": 0,
                },
            }],
        ):
            zero = runner.hydrate(frontier).candidates[0].hydrated_profile
        self.assertEqual(zero["years_of_experience"], 0)
        self.assertEqual(zero["total_years_experience"], 0)

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

    def test_remote_investor_resolution_preserves_exact_alias_unresolved_and_scope(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator-a", "operator-b")))
        remote = spec(
            backend=Backend.POWERSET,
            corpus=runner.corpus,
            company_filters=CompanyFilters(
                investor_names=("Sequoia Capital", "a16z", "Missing Investor")
            ),
        )
        resolved = mock.AsyncMock(
            return_value=[
                {
                    "query_name": "Sequoia Capital",
                    "investor_name": "Sequoia Capital",
                    "canonical_name": "Sequoia Capital",
                    "urn": "urn:sequoia",
                    "match_type": "exact",
                },
                {
                    "query_name": "a16z",
                    "investor_name": "a16z",
                    "canonical_name": "Andreessen Horowitz",
                    "urn": "urn:a16z",
                    "match_type": "alias",
                },
            ]
        )
        companies = mock.AsyncMock(
            return_value={
                "rows": [{"id": "company-1"}],
                "completed": True,
                "truncated": False,
            }
        )
        with (
            mock.patch(
                "packs.search.backends.turbopuffer.resolution.resolve_turbopuffer_investors",
                new=resolved,
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.company_resolution.filter_only_company_rows",
                new=companies,
            ),
        ):
            sources = runner.resolve_sources(remote)

        self.assertEqual(
            resolved.await_args.kwargs["allowed_operator_ids"], ["operator-a", "operator-b"]
        )
        self.assertEqual(sources.investor_urns, ("urn:sequoia", "urn:a16z"))
        self.assertEqual(
            sources.investor_names, ("Sequoia Capital", "Andreessen Horowitz")
        )
        self.assertEqual(sources.unresolved_required_inputs, ("Missing Investor",))
        investor_records = [row for row in sources.records if row["source"] == "investor"]
        self.assertEqual(
            [row["disposition"] for row in investor_records],
            ["resolved", "resolved", "unresolved"],
        )
        self.assertEqual(
            [row.get("match_type") for row in investor_records],
            ["exact", "alias", None],
        )
        company_filter = companies.await_args.args[0]
        self.assertIn(
            ("allowed_operator_ids", "ContainsAny", ["operator-a", "operator-b"]),
            company_filter[1],
        )
        self.assertIn(("investor_urns", "ContainsAny", ["urn:sequoia", "urn:a16z"]), company_filter[1])

    def test_remote_investor_names_compile_into_scoped_people_filter(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        remote = spec(
            backend=Backend.POWERSET,
            corpus=runner.corpus,
            company_filters=CompanyFilters(investor_names=("a16z",)),
        )
        enumerated = mock.AsyncMock(
            return_value={"rows": [], "completed": True, "truncated": False}
        )
        sources = ResolvedSources(
            company_ids=("company-1",),
            investor_urns=("urn:a16z",),
            investor_names=("Andreessen Horowitz",),
        )
        with mock.patch(
            "packs.search.backends.turbopuffer.runner.storage.enumerate_filter_only_rows_for_namespace",
            new=enumerated,
        ):
            hard_filters = runner.apply_hard_filters(remote, sources)
        people_filter = enumerated.await_args_list[-1].args[1]
        self.assertIn(("allowed_operator_ids", "ContainsAny", ["operator"]), people_filter[1])
        self.assertIn(
            ("investor_names", "ContainsAny", ["Andreessen Horowitz"]), people_filter[1]
        )
        role_rows = mock.AsyncMock(return_value=[])
        with (
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.hybrid_role_rows",
                new=role_rows,
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
            runner.retrieve_people(
                SearchPlan(remote, runner.capabilities(remote), sources, ("retrieve",)),
                hard_filters,
            )
        self.assertIn(
            ("investor_names", "ContainsAny", ["Andreessen Horowitz"]),
            role_rows.await_args.args[1][1],
        )

    def test_remote_required_skills_preserve_actual_match_any_evidence(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        remote = spec(
            backend=Backend.POWERSET,
            corpus=runner.corpus,
            tech_skills=("Rust", "Python"),
        )
        enumerations = mock.AsyncMock(
            side_effect=[
                {
                    "rows": [
                        {
                            "id": "person-1",
                            "tech_skills": ["Rust", "Go"],
                        }
                    ],
                    "completed": True,
                    "truncated": False,
                },
                {
                    "rows": [{"base_id": "person-1", "person_id": "person-1"}],
                    "completed": True,
                    "truncated": False,
                },
                {
                    "rows": [{"base_id": "person-1", "person_id": "person-1"}],
                    "completed": True,
                    "truncated": False,
                },
            ]
        )
        runner._namespace_schemas["summaries"] = frozenset(
            {"summary", "summary_tokens", "tech_skills", "allowed_operator_ids", "vector"}
        )
        with mock.patch(
            "packs.search.backends.turbopuffer.runner.storage.enumerate_filter_only_rows_for_namespace",
            new=enumerations,
        ):
            hard_filters = runner.apply_hard_filters(remote, ResolvedSources())
        self.assertEqual(
            hard_filters.compiled["tech_skills_by_person"]["person-1"],
            ("Rust", "Go"),
        )
        self.assertEqual(enumerations.await_args_list[0].args[2], ["tech_skills"])

        role_rows = [{"base_id": "person-1", "position_id": "position-1", "score": 1.0}]
        with (
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.hybrid_role_rows",
                new=mock.AsyncMock(return_value=role_rows),
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.hybrid_summary_rows",
                new=mock.AsyncMock(return_value=[]),
            ) as summary_rows,
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.semantic_company_signal_rows",
                new=mock.AsyncMock(return_value=[]),
            ),
        ):
            retrieved = runner.retrieve_people(
                SearchPlan(remote, runner.capabilities(remote), ResolvedSources(), ("retrieve",)),
                hard_filters,
            )
        self.assertEqual(retrieved[0].tech_skills, ("Rust", "Go"))
        self.assertEqual(
            summary_rows.await_args.kwargs["include_attributes"],
            ["summary", "tech_skills"],
        )
        self.assertEqual(
            retrieved[0].hard_filter_evidence["tech_skills"],
            {"source": "turbopuffer_summaries", "values": ["Rust", "Go"]},
        )

        with mock.patch(
            "packs.search.backends.turbopuffer.runner.postgres_client.fetch_person_rows",
            return_value=[
                {
                    "id": "person-1",
                    "hydrated_context": {"positions": [], "tech_skills": ["Go"]},
                }
            ],
        ):
            hydrated = runner.hydrate(CandidateFrontier.merge(retrieved)).candidates[0]
        self.assertEqual(hydrated.hydrated_profile["tech_skills"], ["Go", "Rust"])
        self.assertNotIn("Python", hydrated.hydrated_profile["tech_skills"])
        self.assertEqual(
            validation_findings(
                hydrated.hydrated_profile, remote, ResolvedSources(), hydrated.source_lanes
            )["violations"],
            (),
        )

        mismatched = replace(hydrated, tech_skills=("Go",), hydrated_profile={"tech_skills": ["Go"]})
        self.assertEqual(
            validation_findings(
                mismatched.hydrated_profile, remote, ResolvedSources(), mismatched.source_lanes
            )["violations"],
            ("tech_skills_mismatch",),
        )

    def test_remote_education_hard_filter_is_operator_scoped(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        remote = spec(
            backend=Backend.POWERSET,
            corpus=runner.corpus,
            person_filters=PersonFilters(education_ids=("school",)),
        )
        enumerate_rows = mock.AsyncMock(
            return_value={
                "rows": [{"person_id": "person-1", "base_id": "person-1"}],
                "completed": True,
                "truncated": False,
            }
        )
        with mock.patch(
            "packs.search.backends.turbopuffer.runner.storage.enumerate_filter_only_rows_for_namespace",
            new=enumerate_rows,
        ):
            runner.apply_hard_filters(remote, ResolvedSources(education_ids=("school",)))
        education_filter = enumerate_rows.await_args_list[0].args[1]
        self.assertIn(
            ("allowed_operator_ids", "ContainsAny", ["operator"]), education_filter[1]
        )
        self.assertIn(("canonical_education_id", "In", ["school"]), education_filter[1])

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
        self.assertTrue(capabilities.supports_complete_snapshot)
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
        runner._namespace_schemas["people"] = frozenset(
            set(row) | {"base_id", "position_id"}
        )
        with (
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.hybrid_role_rows",
                new=mock.AsyncMock(return_value=[row]),
            ) as role_search,
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
        self.assertNotIn("person_id", role_search.await_args.kwargs["include_attributes"])

    def test_remote_summary_and_company_signal_lanes_preserve_grain_and_filters(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus, SearchPlan

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        runner._namespace_schemas["people"] = frozenset(
            {"base_id", "company_id", "position_title", "allowed_operator_ids"}
        )
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
        summary = {"id": "summary-person", "score": 0.7}
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
        self.assertEqual(
            summary_search.await_args.kwargs["include_attributes"],
            ["summary", "tech_skills"],
        )
        people_filter = enumerate_rows.await_args.args[1]
        self.assertIn(("role_ids", "ContainsAny", ["engineer"]), people_filter[1][0][1])
        self.assertEqual(people_filter[1][1], ("company_id", "In", ["signal-company"]))
        self.assertNotIn("person_id", enumerate_rows.await_args.args[2])

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

    def test_remote_hard_filter_people_enumerations_use_live_base_id_identity(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        runner._namespace_schemas["people"] = frozenset({
            "allowed_operator_ids",
            "base_id",
            "company_id",
            "role_ids",
            "seniority_band",
        })
        remote = spec(
            backend=Backend.POWERSET,
            corpus=runner.corpus,
            role=RoleIntent(
                ("engineer",),
                bm25_queries=("engineer",),
                search_mode="COMPANY_UNION",
            ),
            company_filters=CompanyFilters(company_ids=("target",)),
        )
        enumerations = mock.AsyncMock(side_effect=[
            {"rows": [{"base_id": "summary-person"}], "completed": True, "truncated": False},
            {"rows": [{"base_id": "role-person"}], "completed": True, "truncated": False},
            {"rows": [{"base_id": "company-person"}], "completed": True, "truncated": False},
        ])
        with mock.patch(
            "packs.search.backends.turbopuffer.runner.storage.enumerate_filter_only_rows_for_namespace",
            new=enumerations,
        ):
            filters = runner.apply_hard_filters(
                remote, ResolvedSources(company_ids=("target",))
            )

        self.assertEqual(
            filters.eligible_person_ids,
            ("role-person", "company-person"),
        )
        self.assertEqual(len(enumerations.await_args_list), 3)
        for call in enumerations.await_args_list:
            self.assertEqual(call.args[0], "people")
            self.assertEqual(call.args[2], ["base_id"])

    def test_remote_summary_identity_fails_closed_when_row_id_is_missing(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        with self.assertRaisesRegex(
            RuntimeError, "summaries row is missing canonical person identity from id"
        ):
            runner._normalize_person_rows(
                "summaries", [{"tech_skills": ["Synthetic Skill"]}]
            )

    def test_remote_people_identity_fails_closed_without_base_id(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        with self.assertRaisesRegex(
            RuntimeError, "people row is missing canonical person identity from base_id"
        ):
            runner._normalize_person_rows(
                "people", [{"id": "position-1", "person_id": "alias-only"}]
            )

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

    def test_public_search_cli_round_trips_nonempty_review_pool_through_composition_root(self):
        from packs.search.pipeline import search

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "local.duckdb"
            write_local_search_db(db)
            requested = (PERSON_STANFORD,)
            run_spec = SearchSpec(
                "search.spec.v1",
                "synthetic recruiting evaluation",
                Profile.RECRUITING,
                Backend.LOCAL,
                LocalCorpus(str(db)),
                role=RoleIntent(titles=("Synthetic Engineer",)),
                bounds=SearchBounds(10, 10, 10),
                recruiting=RecruitingInput(
                    "Synthetic role brief with enough deterministic content for Review.",
                    review_pool_person_ids=requested,
                ),
            )
            spec_path = Path(tmp) / "search_spec.json"
            spec_path.write_text(json.dumps(run_spec.to_dict()))
            output = ROOT / ".powerpacks/search-runs" / f"test-review-pool-{uuid.uuid4().hex}"
            import shutil

            shutil.rmtree(output, ignore_errors=True)
            try:
                with (
                    mock.patch.object(sys, "argv", ["search", "--spec", str(spec_path), "--output-dir", str(output)]),
                    mock.patch("packs.search.pipeline.recruiting.run_recruiting") as recruiting,
                    mock.patch("builtins.print"),
                ):
                    recruiting.return_value = StageResult(
                        "review",
                        "awaiting_review",
                        CandidateFrontier(
                            (
                                CandidateRecord(
                                    PERSON_STANFORD,
                                    backend="local",
                                    hydrated_profile={"person_id": PERSON_STANFORD},
                                    hydration_disposition="hydrated",
                                ),
                            ),
                            1,
                            1,
                            None,
                            False,
                        ),
                    )
                    search.main()
                recruiting.assert_called_once()
                received_spec = recruiting.call_args.args[0]
                snapshot = recruiting.call_args.kwargs["corpus_snapshot"]
                self.assertEqual(received_spec.recruiting.review_pool_person_ids, requested)
                self.assertEqual(set(snapshot["evidence_hashes"]), set(requested))
                persisted = json.loads((output / "search_spec.json").read_text())
                self.assertEqual(persisted["recruiting"]["review_pool_person_ids"], list(requested))
                json.loads((output / "result.json").read_text())
                candidates = [
                    json.loads(line)
                    for line in (output / "candidates.jsonl").read_text().splitlines()
                ]
                self.assertEqual(candidates[0]["person_id"], PERSON_STANFORD)
                manifest = json.loads((output / "manifest.json").read_text())
                self.assertEqual(manifest["artifacts"]["search_spec_json"]["path"], "search_spec.json")
            finally:
                shutil.rmtree(output, ignore_errors=True)

    def test_local_snapshot_rejects_out_of_scope_review_pool_id(self):
        from packs.search.backends.local.runner import LocalSearchRunner

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "local.duckdb"
            write_local_search_db(db)
            with self.assertRaisesRegex(ValueError, "outside complete local membership"):
                LocalSearchRunner(str(db)).snapshot_corpus("local", ("synthetic-out-of-scope-person",))

    def test_local_snapshot_accepts_profile_only_review_pool_member(self):
        from packs.search.backends.local.runner import LocalSearchRunner

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "local.duckdb"
            write_local_search_db(db)
            runner = LocalSearchRunner(str(db))
            snapshot = runner.snapshot_corpus("local", (PERSON_PROFILE_ONLY,))
            rerun = runner.snapshot_corpus("local", (PERSON_PROFILE_ONLY,))
            serialized = json.dumps(snapshot, sort_keys=True)
        self.assertEqual(set(snapshot["evidence_hashes"]), {PERSON_PROFILE_ONLY})
        self.assertIn(PERSON_PROFILE_ONLY, serialized)
        self.assertEqual(snapshot["schema_version"], "reflect.corpus_snapshot.v2")
        self.assertGreater(snapshot["membership_id_count"], 0)
        self.assertEqual(
            {key: value for key, value in snapshot.items() if key != "observed_at"},
            {key: value for key, value in rerun.items() if key != "observed_at"},
        )

    def test_local_store_normalizes_uuid_values_recursively(self):
        from packs.search.primitives.local.local_duckdb_store import LocalDuckDBSearchStore

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "local.duckdb"
            write_local_search_db(db)
            with LocalDuckDBSearchStore(str(db)) as store:
                self.assertEqual(store.table_columns("local_people_positions")["person_id"], "UUID")
                self.assertEqual(store.table_columns("local_people_positions")["company_id"], "UUID")
                self.assertEqual(store.table_columns("local_people_positions")["allowed_operator_ids"], "UUID[]")
                self.assertEqual(store.table_columns("local_companies")["id"], "UUID")
                self.assertEqual(store.table_columns("local_companies")["company_urn"], "UUID")
                self.assertEqual(store.table_columns("local_education")["id"], "UUID")
                self.assertEqual(
                    store.table_columns("local_people_education")["canonical_education_id"],
                    "UUID",
                )
                self.assertTrue(store.table_columns("local_person_profiles")["hydrated_context"].startswith("STRUCT("))
                row = store.query_rows(
                    """
                    SELECT person_id,
                           [person_id] AS nested_ids,
                           struct_pack(id := person_id, ids := [person_id]) AS nested_record
                    FROM local_people_positions
                    WHERE cast(person_id AS varchar) = ?
                    LIMIT 1
                    """,
                    [PERSON_STANFORD],
                )[0]
        self.assertEqual(row["person_id"], PERSON_STANFORD)
        self.assertEqual(row["nested_ids"], [PERSON_STANFORD])
        self.assertEqual(row["nested_record"], {"id": PERSON_STANFORD, "ids": [PERSON_STANFORD]})

    def test_local_source_resolution_uses_uuid_company_and_school_tables(self):
        from packs.search.backends.local.runner import LocalSearchRunner

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "local.duckdb"
            write_local_search_db(db)
            runner = LocalSearchRunner(str(db))
            resolved = runner.resolve_sources(spec(
                company_filters=CompanyFilters(company_names=("Company One",)),
                person_filters=PersonFilters(education_names=("Stanford University",)),
            ))

        self.assertEqual(resolved.company_ids, (COMPANY_ONE,))
        self.assertEqual(resolved.education_ids, (STANFORD_ID,))
        self.assertFalse(resolved.unresolved_required_inputs)

    def test_local_run_with_output_dir_serializes_uuid_backed_rows(self):
        from packs.search.pipeline.search import run_search

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "local.duckdb"
            write_local_search_db(db)
            local_spec = spec(
                corpus=LocalCorpus(str(db)),
                role=RoleIntent(("software_engineer",), ("Senior Software Engineer",), ("software engineer",)),
                person_filters=PersonFilters(
                    cities=("San Francisco",), seniority_bands=("senior",), is_current_role=True
                ),
            )
            output = ROOT / ".powerpacks/search-runs" / f"test-local-uuid-{uuid.uuid4().hex}"
            import shutil

            shutil.rmtree(output, ignore_errors=True)
            try:
                result = run_search(local_spec, output_dir=output)
                persisted = json.loads((output / "result.json").read_text())
                jsonl = [json.loads(line) for line in (output / "candidates.jsonl").read_text().splitlines()]
                manifest = json.loads((output / "manifest.json").read_text())
            finally:
                shutil.rmtree(output, ignore_errors=True)
        self.assertEqual(result.status, "completed")
        self.assertTrue(jsonl)
        self.assertIsInstance(jsonl[0]["person_id"], str)
        self.assertIsInstance(persisted["frontier"]["candidates"][0]["person_id"], str)
        self.assertEqual(manifest["schema_version"], "search.manifest.v1")

    def test_local_lookup_with_output_dir_serializes_uuid_backed_rows(self):
        from packs.search.pipeline.search import run_search

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "local.duckdb"
            write_local_search_db(db)
            lookup_spec = SearchSpec(
                "search.spec.v1",
                "synthetic lookup",
                Profile.LOOKUP,
                Backend.LOCAL,
                LocalCorpus(str(db)),
                lookup=LookupSpec("person_id", PERSON_STANFORD),
                bounds=SearchBounds(20, 20, 20),
            )
            output = ROOT / ".powerpacks/search-runs" / f"test-local-lookup-uuid-{uuid.uuid4().hex}"
            import shutil

            shutil.rmtree(output, ignore_errors=True)
            try:
                result = run_search(lookup_spec, output_dir=output)
                candidates = [
                    json.loads(line)
                    for line in (output / "candidates.jsonl").read_text().splitlines()
                ]
                persisted = json.loads((output / "result.json").read_text())
                manifest = json.loads((output / "manifest.json").read_text())
            finally:
                shutil.rmtree(output, ignore_errors=True)
        self.assertEqual(result.status, "completed")
        self.assertEqual(candidates[0]["person_id"], PERSON_STANFORD)
        self.assertEqual(persisted["frontier"]["candidates"][0]["person_id"], PERSON_STANFORD)
        self.assertEqual(manifest["schema_version"], "search.manifest.v1")

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
                snapshot_runner = mock.Mock()
                snapshot_runner.corpus = remote.corpus
                snapshot_runner.namespace_schemas = {"people": frozenset({"id", "base_id"})}
                snapshot_runner.snapshot_corpus.return_value = derived
                execution_runner = mock.Mock()
                execution_runner.capabilities.return_value = RunnerCapabilities(
                    Backend.POWERSET, (), ("role",), False, False
                )
                execution_runner.resolve_sources.return_value = ResolvedSources()
                execution_runner.apply_hard_filters.return_value = HardFilterSet(0, (), {})
                cls.side_effect = [snapshot_runner, execution_runner]
                result = run_search(remote, output_dir=output)
            observed_spec = execution_runner.capabilities.call_args.args[0]
            self.assertEqual(observed_spec.corpus.operator_scope_hash, "b" * 64)
            self.assertIs(snapshot_runner.corpus, remote.corpus)
            self.assertEqual(cls.call_count, 2)
            self.assertIs(cls.call_args_list[0].args[0], remote.corpus)
            self.assertIs(cls.call_args_list[1].args[0], observed_spec.corpus)
            self.assertEqual(
                cls.call_args_list[1].kwargs["namespace_schemas"],
                snapshot_runner.namespace_schemas,
            )
            persisted = json.loads((output / "search_spec.json").read_text())
            self.assertEqual(persisted["corpus"]["membership_hash"], "c" * 64)
            self.assertEqual(result.corpus_observation["verification_status"], "unverified_non_comparable")
        finally:
            shutil.rmtree(output, ignore_errors=True)

    def test_remote_snapshot_hashes_complete_scoped_query_corpus_and_evidence(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        calls = []

        def live_schema(name):
            return turbopuffer_contract_schema(name)

        async def enumerate_namespace(name, filters, attributes, consume_page, *, page_size, max_results=0):
            calls.append((name, filters, attributes))
            rows = {
                "people": [{"id": "position-1", "base_id": "person-1", "vector": [0.1]}],
                "summaries": [{"id": "person-1", "summary": "summary", "vector": [0.2]}],
                "companies": [{"id": "company-1", "company_name": "Company", "vector": [0.3]}],
                "company_signals": [{"id": "company-1", "signals_semantic_text": "signal", "vector": [0.4]}],
                "education": [{"id": "education-1", "person_id": "person-1"}],
                "schools": [{"id": "school-1", "school_name": "School"}],
            }[name]
            consume_page(rows)
            return {
                "completed": True,
                "truncated": False,
                "row_count": len(rows),
            }

        hydrated = {
            "id": "person-1",
            "full_name": "Person",
            "hydrated_context": {"positions": [{"title": "Engineer"}]},
        }
        with (
            mock.patch(
                "packs.search.backends.turbopuffer.runner.postgres_client.fetch_set_operator_ids",
                return_value={"set_id": "set", "operator_ids": ["operator"]},
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.postgres_client.fetch_person_rows",
                return_value=[hydrated],
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.consume_filter_only_pages_for_namespace",
                new=mock.AsyncMock(side_effect=enumerate_namespace),
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.namespace_schema",
                side_effect=live_schema,
            ),
        ):
            snapshot = runner.snapshot_corpus("set", ("person-1",))

        self.assertEqual(snapshot["verification_status"], "verified_comparable")
        self.assertEqual(snapshot["membership_id_count"], 1)
        self.assertEqual(snapshot["enumerated_record_count"], 6)
        self.assertEqual(set(snapshot["namespace_record_counts"]), {
            "people", "summaries", "companies", "company_signals", "education", "schools"
        })
        self.assertEqual(validate_snapshot(snapshot, ("person-1",)), [])
        self.assertEqual(calls[-1][0:2], ("schools", None))
        for name, filters, attributes in calls[:-1]:
            self.assertEqual(
                filters, ("allowed_operator_ids", "ContainsAny", ["operator"]), name
            )
            self.assertIs(attributes, True)

    def test_remote_snapshot_accepts_absent_optional_attribute_and_hashes_live_schema(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        selected = spec(
            backend=Backend.POWERSET,
            corpus=PowersetCorpus("set", ("operator",)),
            tech_skills=("Synthetic Skill",),
        )

        def live_schema(name):
            contract = json.loads(
                (ROOT / "packs/search/contracts/turbopuffer" / f"{name}.namespace.json").read_text()
            )
            omitted = {
                "company_description",
                "ig_followers",
                "investor_names",
                "linkedin_connections",
                "linkedin_followers",
                "x_twitter_followers",
            }
            if name == "summaries":
                omitted.update(("base_id", "person_id"))
            if name == "people":
                omitted.add("person_id")
            fields = {
                row["name"]: {"type": row["type"]}
                for row in contract["attributes"]
                if row["name"] not in omitted
            }
            if contract.get("vector"):
                fields["vector"] = {"type": "vector"}
            fields["live_only_attribute"] = {"type": "string"}
            return fields

        async def enumerate_namespace(name, filters, attributes, consume_page, *, page_size, max_results=0):
            self.assertIs(attributes, True)
            rows = (
                [{"id": "position-1", "base_id": "person-1", "live_only_attribute": "covered"}]
                if name == "people"
                else []
            )
            consume_page(rows)
            return {"completed": True, "truncated": False, "row_count": len(rows)}

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
                "packs.search.backends.turbopuffer.runner.storage.namespace_schema",
                side_effect=live_schema,
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.consume_filter_only_pages_for_namespace",
                new=mock.AsyncMock(side_effect=enumerate_namespace),
            ),
        ):
            snapshot = TurboPufferSearchRunner(selected.corpus).snapshot_corpus(
                "set", (), spec=selected
            )

        people_schema = live_schema("people")
        self.assertNotIn("company_description", people_schema)
        self.assertNotIn("person_id", people_schema)
        self.assertNotIn("person_id", live_schema("summaries"))
        self.assertNotIn("base_id", live_schema("summaries"))
        self.assertEqual(snapshot["namespace_schema_hashes"]["people"], canonical_hash(people_schema))
        self.assertRegex(snapshot["scoped_records_hash"], r"^[a-f0-9]{64}$")

    def test_remote_streaming_snapshot_hash_is_page_invariant_and_complete(self):
        from packs.search.backends.turbopuffer.runner import (
            _NamespaceSnapshotAccumulator,
            _streaming_scoped_records_hash,
        )

        rows = [
            {"id": "a", "base_id": "person-1", "live_only": "first", "vector": [0.1, 0.2]},
            {"id": "b", "base_id": "person-2", "live_only": "second", "vector": [0.3, 0.4]},
        ]
        combined = _NamespaceSnapshotAccumulator(
            retain_membership=True, person_identity_source="base_id"
        )
        combined.consume_page(rows)
        paged = _NamespaceSnapshotAccumulator(
            retain_membership=True, person_identity_source="base_id"
        )
        paged.consume_page(rows[:1])
        paged.consume_page(rows[1:])

        self.assertEqual(combined.hexdigest(), paged.hexdigest())
        self.assertEqual(
            combined.hexdigest(),
            "cd87932ddf36eb2682e6d5acd186ea1f23e1a999aaaed9d816a5f2031eb07be4",
        )
        self.assertEqual(combined.member_ids, {"person-1", "person-2"})
        self.assertEqual(
            _streaming_scoped_records_hash({"people": combined.hexdigest()}, {"people": 2}),
            "1ca28a48f2fd97eb087b2662412ccea186b9cb4397c138c9cb78d69665eb333e",
        )
        empty = _NamespaceSnapshotAccumulator().hexdigest()
        self.assertEqual(
            _streaming_scoped_records_hash(
                {"people": combined.hexdigest(), "schools": empty},
                {"people": 2, "schools": 0},
            ),
            _streaming_scoped_records_hash(
                {"schools": empty, "people": combined.hexdigest()},
                {"schools": 0, "people": 2},
            ),
        )
        for changed in (
            [rows[0], {**rows[1], "live_only": "changed"}],
            [rows[0], {**rows[1], "vector": [0.3, 0.5]}],
        ):
            accumulator = _NamespaceSnapshotAccumulator()
            accumulator.consume_page(changed)
            self.assertNotEqual(accumulator.hexdigest(), combined.hexdigest())

        out_of_order = _NamespaceSnapshotAccumulator()
        with self.assertRaisesRegex(RuntimeError, "stable ascending order"):
            out_of_order.consume_page(list(reversed(rows)))

    def test_remote_snapshot_requires_selected_filter_query_and_vector_fields(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        selected = spec(
            backend=Backend.POWERSET,
            corpus=PowersetCorpus("set", ("operator",)),
            person_filters=PersonFilters(cities=("Synthetic City",)),
        )

        def drifted_schema(name):
            schema = turbopuffer_contract_schema(name)
            if name == "people":
                for field in ("city", "phrase_tokens", "vector"):
                    schema.pop(field)
            return schema

        enumeration = mock.AsyncMock()
        with (
            mock.patch(
                "packs.search.backends.turbopuffer.runner.postgres_client.fetch_set_operator_ids",
                return_value={"set_id": "set", "operator_ids": ["operator"]},
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.namespace_schema",
                side_effect=drifted_schema,
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.consume_filter_only_pages_for_namespace",
                new=enumeration,
            ),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "people live schema is missing operationally required attributes: city, phrase_tokens, vector",
            ):
                TurboPufferSearchRunner(selected.corpus).snapshot_corpus(
                    "set", (), spec=selected
                )
        enumeration.assert_not_awaited()

    def test_remote_snapshot_does_not_require_query_fields_or_vectors_for_filter_only_spec(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        selected = spec(
            raw_request="",
            backend=Backend.POWERSET,
            corpus=PowersetCorpus("set", ("operator",)),
            role=RoleIntent(),
            person_filters=PersonFilters(cities=("Synthetic City",)),
        )
        required = TurboPufferSearchRunner._snapshot_schema_requirements(selected)

        self.assertIn("city", required["people"])
        self.assertNotIn("phrase_tokens", required["people"])
        self.assertNotIn("word_tokens", required["people"])
        self.assertNotIn("vector", required["people"])
        self.assertNotIn("summary_tokens", required["summaries"])
        self.assertNotIn("vector", required["summaries"])
        self.assertNotIn("vector", required["company_signals"])

    def test_remote_snapshot_rejects_missing_required_live_attribute_before_enumeration(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        def drifted_schema(name):
            schema = turbopuffer_contract_schema(name)
            if name == "people":
                schema.pop("allowed_operator_ids")
            return schema

        enumeration = mock.AsyncMock()
        with (
            mock.patch(
                "packs.search.backends.turbopuffer.runner.postgres_client.fetch_set_operator_ids",
                return_value={"set_id": "set", "operator_ids": ["operator"]},
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.namespace_schema",
                side_effect=drifted_schema,
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.consume_filter_only_pages_for_namespace",
                new=enumeration,
            ),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "people live schema is missing operationally required attributes: allowed_operator_ids",
            ):
                TurboPufferSearchRunner(
                    PowersetCorpus("set", ("operator",))
                ).snapshot_corpus("set", ())
        enumeration.assert_not_awaited()

    def test_remote_snapshot_fails_closed_on_scope_enumeration_and_evidence_gaps(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        set_resolution = mock.patch(
            "packs.search.backends.turbopuffer.runner.postgres_client.fetch_set_operator_ids",
            return_value={"set_id": "set", "operator_ids": ["operator"]},
        )
        complete = {
            "rows": [{"id": "position-1", "base_id": "person-1"}],
            "completed": True,
            "truncated": False,
            "row_count": 1,
        }
        with (
            set_resolution,
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.namespace_schema",
                side_effect=turbopuffer_contract_schema,
            ),
            mock.patch(
                "packs.search.backends.turbopuffer.runner.storage.consume_filter_only_pages_for_namespace",
                new=mock.AsyncMock(return_value={**complete, "completed": False, "truncated": True}),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "enumeration is incomplete"):
                runner.snapshot_corpus("set", ())

        async def complete_namespaces(name, filters, attributes, consume_page, *, page_size, max_results=0):
            rows = complete["rows"] if name == "people" else []
            consume_page(rows)
            return {"completed": True, "truncated": False, "row_count": len(rows)}

        for evidence_ids, hydrated, error in (
            (("outside",), [], "outside complete Powerset membership"),
            (("person-1",), [], "evidence hydration is missing"),
        ):
            with (
                mock.patch(
                    "packs.search.backends.turbopuffer.runner.postgres_client.fetch_set_operator_ids",
                    return_value={"set_id": "set", "operator_ids": ["operator"]},
                ),
                mock.patch(
                    "packs.search.backends.turbopuffer.runner.postgres_client.fetch_person_rows",
                    return_value=hydrated,
                ),
                mock.patch(
                    "packs.search.backends.turbopuffer.runner.storage.consume_filter_only_pages_for_namespace",
                    new=mock.AsyncMock(side_effect=complete_namespaces),
                ),
                mock.patch(
                    "packs.search.backends.turbopuffer.runner.storage.namespace_schema",
                    side_effect=turbopuffer_contract_schema,
                ),
            ):
                with self.assertRaisesRegex((ValueError, RuntimeError), error):
                    runner.snapshot_corpus("set", evidence_ids)

    def test_remote_snapshot_requires_exact_nonempty_postgres_scope(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        runner = TurboPufferSearchRunner(PowersetCorpus("set", ("operator",)))
        for resolution, error in (
            ({"set_id": "other", "operator_ids": ["operator"]}, "different Powerset set_id"),
            ({"set_id": "set", "operator_ids": []}, "no Postgres-derived operator scope"),
            ({"set_id": "set", "operator_ids": ["other"]}, "operator_ids do not match"),
        ):
            with mock.patch(
                "packs.search.backends.turbopuffer.runner.postgres_client.fetch_set_operator_ids",
                return_value=resolution,
            ):
                with self.assertRaisesRegex(ValueError, error):
                    runner.snapshot_corpus("set", ())

    def test_remote_snapshot_accepts_matching_persisted_content_identity_only(self):
        from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
        from packs.search.pipeline.models import PowersetCorpus

        rows = {
            "people": [{"id": "position-1", "base_id": "person-1"}],
            "summaries": [{"id": "person-1", "summary": "summary"}],
            "companies": [],
            "company_signals": [],
            "education": [],
            "schools": [],
        }

        async def enumerate_namespace(name, filters, attributes, consume_page, *, page_size, max_results=0):
            values = rows[name]
            consume_page(values)
            return {
                "completed": True,
                "truncated": False,
                "row_count": len(values),
            }

        def snapshot(corpus):
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
                    side_effect=turbopuffer_contract_schema,
                ),
            ):
                return TurboPufferSearchRunner(corpus).snapshot_corpus("set", ())

        initial = snapshot(PowersetCorpus("set", ("operator",)))
        snapshot_schema = json.loads(
            (ROOT / "packs/search/schemas/reflect-corpus-snapshot.schema.json").read_text()
        )
        jsonschema.validate(initial, snapshot_schema)
        content_hash = initial["scoped_records_hash"]
        rerun = snapshot(
            PowersetCorpus("set", ("operator",), scoped_records_hash=content_hash)
        )
        self.assertEqual(rerun["scoped_records_hash"], content_hash)
        native = snapshot(
            PowersetCorpus("set", ("operator",), native_content_version=content_hash)
        )
        self.assertEqual(native["native_content_version"], content_hash)
        self.assertNotIn("scoped_records_hash", native)
        jsonschema.validate(native, snapshot_schema)
        with self.assertRaisesRegex(ValueError, "content identity does not match"):
            snapshot(PowersetCorpus("set", ("operator",), scoped_records_hash="f" * 64))


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
