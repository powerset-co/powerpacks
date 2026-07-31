"""Concrete TurboPuffer/Postgres runner; imports no local backend modules."""

from __future__ import annotations

import sys
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...pipeline.frontier import CandidateFrontier, CandidateRecord, ProbeMatch
from ...pipeline.models import (
    Backend,
    HardFilterSet,
    LookupSpec,
    PowersetCorpus,
    ResolvedSources,
    RunnerCapabilities,
    SearchPlan,
    SearchSpec,
)
from ...reflect.snapshots import canonical_hash, evidence_hash

_PRIMITIVES = Path(__file__).resolve().parents[2] / "primitives"
for _path in (_PRIMITIVES / "lib", _PRIMITIVES / "shared", _PRIMITIVES / "turbopuffer"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import postgres_client  # noqa: E402
from powerpacks_contracts import normalize_hydrated_context  # noqa: E402
import turbopuffer_search_backend as storage  # noqa: E402
import turbopuffer_resolve_companies as company_resolution  # noqa: E402
import turbopuffer_resolve_education as education_resolution  # noqa: E402


CONTRACT_ROOT = Path(__file__).resolve().parents[2] / "contracts"
CONTRACTS = CONTRACT_ROOT / "turbopuffer"


class TurboPufferSearchRunner:
    def __init__(self, corpus: PowersetCorpus):
        if not isinstance(corpus, PowersetCorpus):
            raise ValueError("TurboPuffer runner requires a Powerset corpus")
        self.corpus = corpus

    def capabilities(self, spec: SearchSpec) -> RunnerCapabilities:
        people = json.loads((CONTRACTS / "people.namespace.json").read_text())
        summaries = json.loads((CONTRACTS / "summaries.namespace.json").read_text())
        education = json.loads((CONTRACTS / "education.namespace.json").read_text())
        companies = json.loads((CONTRACTS / "companies.namespace.json").read_text())
        people_fields = {row["field"] for row in people["filters"]}
        summary_fields = {row["field"] for row in summaries["filters"]}
        company_fields = {row["field"] for row in companies["filters"]}
        mapping = {
            "role_ids": "role_ids",
            "cities": "city",
            "states": "state",
            "countries": "country",
            "metro_areas": "metro_areas",
            "seniority_bands": "seniority_band",
            "role_tracks": "role_track",
            "is_current_role": "is_current",
            "years_experience_min": "total_years_experience",
            "years_experience_max": "total_years_experience",
            "company_ids": "company_id",
            "is_current_company": "is_current",
        }
        supported = tuple(name for name, field in mapping.items() if field in people_fields)
        company_mapping = {
            "sector_types": "sector_types",
            "technology_types": "technology_types",
            "entity_types": "entity_types",
            "funding_stage_min": "funding_stage",
            "funding_stage_max": "funding_stage",
            "headcount_min": "headcount",
            "headcount_max": "headcount",
        }
        supported += tuple(name for name, field in company_mapping.items() if field in company_fields)
        supported = tuple(dict.fromkeys(supported))
        if "canonical_education_id" in {row["field"] for row in education["filters"]}:
            supported += ("education_ids",)
        skills = "tech_skills" in summary_fields
        if skills:
            supported += ("tech_skills",)
        persons = json.loads((CONTRACT_ROOT / "postgres" / "persons.table.json").read_text())
        person_columns = {row["name"] for row in persons["columns"]}
        fields = ["person_id"]
        if {"full_name", "public_identifier", "public_profile_url"} <= person_columns:
            fields.extend(("name", "handle", "profile_url"))
        lanes = ["role", "summary", "company_signal", "adjacency"]
        return RunnerCapabilities(Backend.POWERSET, supported, tuple(lanes), skills, False, tuple(fields))

    def lookup_person(self, lookup: LookupSpec | None) -> tuple[CandidateRecord, ...]:
        if lookup is None:
            return ()
        if lookup.field == "person_id":
            rows = postgres_client.fetch_person_rows([lookup.value])
        else:
            fixture = postgres_client.fixture_rows("persons")
            aliases = {
                "name": ("full_name",),
                "handle": ("public_identifier", "x_twitter_handle"),
                "profile_url": ("public_profile_url",),
            }
            if lookup.field not in aliases:
                raise ValueError(f"unsupported Powerset lookup field: {lookup.field}")
            if fixture is not None:
                rows = [
                    row
                    for row in fixture
                    if any(
                        str(row.get(field) or "").casefold() == lookup.value.casefold()
                        for field in aliases[lookup.field]
                    )
                ]
            else:
                fields = aliases[lookup.field]
                psycopg2 = postgres_client.ensure_psycopg2()
                clauses = " OR ".join(f"lower(cast({field} as text)) = lower(%s)" for field in fields)
                query = f"SELECT id::text FROM persons WHERE {clauses} ORDER BY id LIMIT 20"
                with psycopg2.connect(postgres_client.database_url()) as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(query, [lookup.value] * len(fields))
                        ids = [str(row[0]) for row in cursor.fetchall()]
                rows = postgres_client.fetch_person_rows(ids)
        candidate_ids = tuple(str(row["id"]) for row in rows)
        allowed = self._scoped_lookup_ids(candidate_ids)
        return tuple(
            CandidateRecord(
                str(row["id"]),
                1.0,
                source_lanes=("lookup",),
                found_by=(ProbeMatch("lookup", rank, probe_family="deterministic_lookup"),),
                backend="powerset",
            )
            for rank, row in enumerate(rows, start=1)
            if str(row["id"]) in allowed
        )

    def _scoped_lookup_ids(self, candidate_ids: tuple[str, ...]) -> set[str]:
        if not candidate_ids:
            return set()
        import asyncio

        rows = asyncio.run(
            storage.filter_only_rows_for_namespace(
                "people",
                (
                    "And",
                    [
                        ("allowed_operator_ids", "ContainsAny", list(self.corpus.operator_ids)),
                        ("base_id", "In", list(candidate_ids)),
                    ],
                ),
                ["base_id"],
                page_size=min(10000, max(1, len(candidate_ids))),
            )
        )
        return {
            str(row.get("person_id") or row.get("base_id"))
            for row in rows
            if row.get("person_id") or row.get("base_id")
        }

    def _scoped_person_ids(self) -> set[str]:
        if hasattr(self, "_scope_person_ids"):
            return set(self._scope_person_ids)
        import asyncio

        result = asyncio.run(
            storage.enumerate_filter_only_rows_for_namespace(
                "people",
                ("allowed_operator_ids", "ContainsAny", list(self.corpus.operator_ids)),
                ["base_id", "person_id"],
                page_size=10000,
            )
        )
        if not result["completed"] or result["truncated"]:
            raise RuntimeError("selected Powerset scope enumeration is incomplete")
        values = {
            str(row.get("person_id") or row.get("base_id"))
            for row in result["rows"]
            if row.get("person_id") or row.get("base_id")
        }
        self._scope_person_ids = frozenset(values)
        return values

    def resolve_sources(self, spec: SearchSpec) -> ResolvedSources:
        import asyncio

        companies = list(spec.company_filters.company_ids)
        education = list(spec.person_filters.education_ids)
        records: list[dict[str, Any]] = []
        archetype_payload = {
            "sector_types": list(spec.company_filters.sector_types),
            "technology_types": list(spec.company_filters.technology_types),
            "entity_types": list(spec.company_filters.entity_types),
            "funding_stage_min": spec.company_filters.funding_stage_min,
            "funding_stage_max": spec.company_filters.funding_stage_max,
            "headcount_min": spec.company_filters.headcount_min,
            "headcount_max": spec.company_filters.headcount_max,
        }
        has_archetype = any(
            value not in (None, [], ())
            for key, value in archetype_payload.items()
        )
        archetype_filter = self._company_archetype_filter(archetype_payload)
        if companies and has_archetype:
            archetype_filter = ("And", [archetype_filter, ("id", "In", list(companies))])
        if spec.company_filters.company_names:
            rows = asyncio.run(
                company_resolution.exact_name_lookup(
                    list(spec.company_filters.company_names), archetype_filter, top_k=10
                )
            )
            companies = []
            for name in spec.company_filters.company_names:
                matched = [
                    row
                    for row in rows
                    if str(row.get("name") or row.get("company_name") or "").casefold() == name.casefold()
                ]
                if not matched:
                    records.append({"source": "company", "input": name, "required": True, "disposition": "unresolved"})
                for row in matched:
                    value = str(row.get("id") or "")
                    if value and value not in companies:
                        companies.append(value)
                    if value:
                        records.append(
                            {
                                "source": "company",
                                "input": name,
                                "required": True,
                                "disposition": "resolved",
                                "resolved_id": value,
                            }
                        )
            if has_archetype:
                records.append(
                    {
                        "source": "company_archetype",
                        "input": archetype_payload,
                        "required": True,
                        "disposition": "resolved" if companies else "unresolved",
                        "resolved_count": len(companies),
                    }
                )
        elif has_archetype:
            enumeration = asyncio.run(
                company_resolution.filter_only_company_rows(
                    archetype_filter, page_size=10000, max_results=10000
                )
            )
            rows = enumeration["rows"]
            companies = []
            for row in rows:
                value = str(row.get("id") or "")
                if value and value not in companies:
                    companies.append(value)
            records.append(
                {
                    "source": "company_archetype",
                    "input": archetype_payload,
                    "required": True,
                    "disposition": (
                        "resolved"
                        if companies and enumeration["completed"] and not enumeration["truncated"]
                        else "unresolved"
                    ),
                    "resolved_count": len(companies),
                    "completed": enumeration["completed"],
                    "truncated": enumeration["truncated"],
                }
            )
        for name in spec.person_filters.education_names:
            resolution = asyncio.run(education_resolution.resolve_name(name, limit=10))
            if not resolution["resolved_ids"]:
                records.append({"source": "education", "input": name, "required": True, "disposition": "unresolved"})
            for value in resolution["resolved_ids"]:
                if value not in education:
                    education.append(value)
                records.append(
                    {
                        "source": "education",
                        "input": name,
                        "required": True,
                        "disposition": "resolved",
                        "resolved_id": value,
                    }
                )
        return ResolvedSources(tuple(companies), tuple(education), tuple(records))

    def _company_archetype_filter(self, payload: dict[str, Any]) -> tuple:
        clauses: list[tuple] = [
            ("allowed_operator_ids", "ContainsAny", list(self.corpus.operator_ids))
        ]
        for payload_key, field, op in (
            ("sector_types", "sector_types", "ContainsAny"),
            ("technology_types", "technology_types", "ContainsAny"),
            ("entity_types", "entity_types", "ContainsAny"),
            ("headcount_min", "headcount", "Gte"),
            ("headcount_max", "headcount", "Lte"),
        ):
            value = payload.get(payload_key)
            if value not in (None, (), []):
                clauses.append((field, op, list(value) if isinstance(value, (tuple, list)) else value))
        minimum = company_resolution.normalize_stage(payload.get("funding_stage_min"))
        maximum = company_resolution.normalize_stage(payload.get("funding_stage_max"))
        if minimum is not None or maximum is not None:
            clauses.append(("funding_stage", "Gt", 0))
        if minimum is not None:
            clauses.append(("funding_stage", "Gte", minimum))
        if maximum is not None:
            clauses.append(("funding_stage", "Lte", maximum))
        return ("And", clauses)

    def _filters(
        self,
        spec: SearchSpec,
        sources: ResolvedSources,
        *,
        include_company: bool = True,
        include_role: bool = True,
        include_currentness: bool = True,
    ) -> tuple:
        clauses: list[tuple] = [("allowed_operator_ids", "ContainsAny", list(self.corpus.operator_ids))]
        values = (
            ("role_ids", "ContainsAny", spec.role.role_ids),
            ("city", "In", spec.person_filters.cities),
            ("state", "In", spec.person_filters.states),
            ("country", "In", spec.person_filters.countries),
            ("metro_areas", "ContainsAny", spec.person_filters.metro_areas),
            ("seniority_band", "In", spec.person_filters.seniority_bands),
            ("role_track", "In", spec.person_filters.role_tracks),
            ("company_id", "In", sources.company_ids),
        )
        clauses.extend(
            (field, op, list(value))
            for field, op, value in values
            if value
            and (include_company or not field.startswith("company_") and field != "company_id")
            and (
                include_role
                or field not in {"role_ids", "seniority_band", "role_track"}
            )
        )
        if include_role and include_currentness and spec.person_filters.is_current_role is not None:
            clauses.append(("is_current", "Eq", spec.person_filters.is_current_role))
        if spec.person_filters.years_experience_min is not None:
            clauses.append(("total_years_experience", "Gte", spec.person_filters.years_experience_min))
        if spec.person_filters.years_experience_max is not None:
            clauses.append(("total_years_experience", "Lte", spec.person_filters.years_experience_max))
        if (
            include_company
            and include_currentness
            and spec.company_filters.is_current_company is not None
            and (not include_role or spec.person_filters.is_current_role is None)
        ):
            clauses.append(("is_current", "Eq", spec.company_filters.is_current_company))
        return ("And", clauses)

    def apply_hard_filters(self, spec: SearchSpec, sources: ResolvedSources) -> HardFilterSet:
        import asyncio

        union = spec.role.search_mode == "COMPANY_UNION" and bool(sources.company_ids)
        filters = self._filters(spec, sources, include_company=not union)
        summary_filter = self._filters(
            spec,
            sources,
            include_company=not union,
        )
        summary_namespace_filter: tuple = (
            "allowed_operator_ids",
            "ContainsAny",
            list(self.corpus.operator_ids),
        )
        signal_filter: tuple = (
            "allowed_operator_ids",
            "ContainsAny",
            list(self.corpus.operator_ids),
        )
        company_filter = self._filters(spec, sources, include_role=False) if union else None
        if sources.education_ids:
            education = asyncio.run(
                storage.enumerate_filter_only_rows_for_namespace(
                    "education",
                    ("canonical_education_id", "In", list(sources.education_ids)),
                    ["person_id"],
                    page_size=10000,
                )
            )
            if education["truncated"]:
                raise RuntimeError("education hard-filter enumeration truncated")
            education_people = list(
                dict.fromkeys(str(row.get("person_id") or "") for row in education["rows"] if row.get("person_id"))
            )
            clause = ("base_id", "In", education_people)
            filters = ("And", [filters, clause])
            summary_filter = ("And", [summary_filter, clause])
            if company_filter is not None:
                company_filter = ("And", [company_filter, clause])
        if spec.tech_skills:
            self._required_skills = spec.tech_skills
            skills = asyncio.run(
                storage.enumerate_filter_only_rows_for_namespace(
                    "summaries",
                    (
                        "And",
                        [
                            ("allowed_operator_ids", "ContainsAny", list(self.corpus.operator_ids)),
                            ("tech_skills", "ContainsAny", list(spec.tech_skills)),
                        ],
                    ),
                    ["base_id", "person_id"],
                    page_size=10000,
                )
            )
            if skills["truncated"] or not skills["completed"]:
                raise RuntimeError("skills hard-filter enumeration incomplete")
            skill_people = list(
                dict.fromkeys(
                    str(row.get("person_id") or row.get("base_id"))
                    for row in skills["rows"]
                    if row.get("person_id") or row.get("base_id")
                )
            )
            clause = ("base_id", "In", skill_people)
            filters = ("And", [filters, clause])
            summary_filter = ("And", [summary_filter, clause])
            if company_filter is not None:
                company_filter = ("And", [company_filter, clause])
            summary_namespace_filter = (
                "And",
                [
                    summary_namespace_filter,
                    ("tech_skills", "ContainsAny", list(spec.tech_skills)),
                ],
            )
        summary_clauses = summary_filter[1] if summary_filter[0] == "And" else [summary_filter]
        if len(summary_clauses) > 1:
            summary_enumeration = asyncio.run(
                storage.enumerate_filter_only_rows_for_namespace(
                    "people", summary_filter, ["base_id", "person_id"], page_size=10000
                )
            )
            if summary_enumeration["truncated"] or not summary_enumeration["completed"]:
                raise RuntimeError("summary eligibility enumeration incomplete")
            summary_eligible_ids = list(
                dict.fromkeys(
                    str(row.get("person_id") or row.get("base_id") or "")
                    for row in summary_enumeration["rows"]
                    if row.get("person_id") or row.get("base_id")
                )
            )
            summary_namespace_filter = (
                "And",
                [summary_namespace_filter, ("id", "In", summary_eligible_ids)],
            )
        enumeration = asyncio.run(
            storage.enumerate_filter_only_rows_for_namespace(
                "people", filters, ["base_id", "person_id"], page_size=10000
            )
        )
        all_rows = list(enumeration["rows"])
        if company_filter is not None:
            company_enumeration = asyncio.run(
                storage.enumerate_filter_only_rows_for_namespace(
                    "people", company_filter, ["base_id", "person_id"], page_size=10000
                )
            )
            if company_enumeration["truncated"] or not company_enumeration["completed"]:
                raise RuntimeError("company-union enumeration incomplete")
            all_rows.extend(company_enumeration["rows"])
        ids = tuple(
            dict.fromkeys(
                str(row.get("person_id") or row.get("base_id") or "")
                for row in all_rows
                if row.get("person_id") or row.get("base_id")
            )
        )
        if enumeration["truncated"]:
            raise RuntimeError("hard-filter pool enumeration truncated")
        return HardFilterSet(
            len(ids),
            ids,
            {
                "filter": filters,
                "summary_filter": summary_filter,
                "summary_namespace_filter": summary_namespace_filter,
                "signal_filter": signal_filter,
                "company_filter": company_filter,
            },
        )

    def retrieve_people(
        self,
        plan: SearchPlan,
        filters: HardFilterSet,
        probe_id: str | None = None,
        probe_family: str | None = None,
    ) -> tuple[CandidateRecord, ...]:
        import asyncio

        role_queries = tuple(value.replace("_", " ") for value in plan.spec.role.role_ids)
        queries = list(
            dict.fromkeys(
                (
                    *plan.spec.role.bm25_queries,
                    *plan.spec.role.titles,
                    *role_queries,
                    *((plan.spec.raw_request,) if plan.spec.raw_request.strip() else ()),
                )
            )
        )
        payload = {
            "semantic_query": plan.spec.raw_request.strip(),
            "bm25_queries": queries,
            "role_ids": list(plan.spec.role.role_ids),
            "search_mode": plan.spec.role.search_mode,
        }
        attributes = [
            "base_id",
            "person_id",
            "position_id",
            "id",
            "position_title",
            "raw_title",
            "role_ids",
            "company_id",
            "city",
            "state",
            "country",
            "metro_areas",
            "seniority_band",
            "role_track",
            "is_current",
            "total_years_experience",
            "company_sector_types",
            "company_entity_types",
            "company_headcount",
            "company_stage",
        ]
        rows = asyncio.run(
            storage.hybrid_role_rows(
                payload,
                filters.compiled["filter"],
                top_k=plan.spec.bounds.retrieval_limit,
                include_attributes=attributes,
            )
        )
        lane_rows = [("role", rows)]
        if "summary" in plan.capabilities.retrieval_lanes:
            summary_rows = asyncio.run(
                storage.hybrid_summary_rows(
                    payload,
                    filters.compiled.get("summary_namespace_filter"),
                    top_k=plan.spec.bounds.retrieval_limit,
                    include_attributes=["base_id", "person_id", "summary", "tech_skills"],
                )
            )
            lane_rows.append(("summary", summary_rows))
        if "company_signal" in plan.capabilities.retrieval_lanes:
            signal_rows = asyncio.run(
                storage.semantic_company_signal_rows(
                    plan.spec.raw_request,
                    filters.compiled.get("signal_filter"),
                    top_k=min(500, plan.spec.bounds.retrieval_limit),
                    include_attributes=["signals_semantic_text"],
                )
            )
            company_scores = {
                str(row.get("company_id") or row.get("id") or ""): float(row.get("score") or 0)
                for row in signal_rows
                if row.get("company_id") or row.get("id")
            }
            if company_scores:
                people_filter = storage.and_filters(
                    filters.compiled["filter"],
                    ("company_id", "In", list(company_scores)),
                )
                signal_people = asyncio.run(
                    storage.enumerate_filter_only_rows_for_namespace(
                        "people",
                        people_filter,
                        attributes,
                        page_size=min(10000, plan.spec.bounds.retrieval_limit),
                        max_results=plan.spec.bounds.retrieval_limit,
                    )
                )["rows"]
                for row in signal_people:
                    row["score"] = company_scores.get(str(row.get("company_id") or ""), 0)
                lane_rows.append(("company_signal", signal_people))
        if plan.spec.role.search_mode == "COMPANY_UNION" and queries:
            lane_rows.append(
                (
                    "adjacency",
                    asyncio.run(
                        storage.bm25_adjacency_rows(
                            queries,
                            filters.compiled["filter"],
                            top_k=plan.spec.bounds.retrieval_limit,
                            include_attributes=attributes,
                        )
                    ),
                )
            )
        if filters.compiled.get("company_filter") is not None:
            company_rows = asyncio.run(
                storage.enumerate_filter_only_rows_for_namespace(
                    "people", filters.compiled["company_filter"], attributes, page_size=plan.spec.bounds.retrieval_limit
                )
            )
            lane_rows.append(("company_union", company_rows["rows"][: plan.spec.bounds.retrieval_limit]))
        out = []
        for lane, lane_values in lane_rows:
            for rank, row in enumerate(lane_values, start=1):
                person_id = str(row.get("person_id") or row.get("base_id") or "")
                if not person_id:
                    continue
                position_id = "" if lane == "summary" else str(row.get("position_id") or row.get("id") or "")
                score = float(row.get("score") or 0)
                out.append(
                    CandidateRecord(
                        person_id,
                        score,
                        matched_position_ids=(position_id,) if position_id else (),
                        source_lanes=(lane,),
                        found_by=(
                            ProbeMatch(lane, rank, probe_id, probe_family or "structured_gtm", score),
                        ),
                        backend="powerset",
                        structured={key: row.get(key) for key in attributes if key in row},
                    )
                )
        return tuple(out)

    def hydrate(self, frontier: CandidateFrontier) -> CandidateFrontier:
        rows = postgres_client.fetch_person_rows(
            [row.person_id for row in frontier.candidates]
        )
        by_id = {}
        for raw in rows:
            profile = normalize_hydrated_context(raw.get("hydrated_context") or {})
            profile.update(
                {key: value for key, value in raw.items() if key != "hydrated_context" and value is not None}
            )
            if getattr(self, "_required_skills", ()):
                profile["tech_skills"] = list(self._required_skills)
            by_id[str(raw["id"])] = profile
        hydrated = []
        for row in frontier.candidates:
            profile = by_id.get(row.person_id)
            position_ids = set(row.matched_position_ids)
            indexes = tuple(
                index
                for index, position in enumerate((profile or {}).get("positions") or [])
                if str(position.get("position_id") or position.get("id") or "") in position_ids
            )
            hydrated.append(
                replace(
                    row,
                    hydrated_profile=profile,
                    matched_position_indexes=indexes,
                    hydration_disposition="hydrated" if profile is not None else "missing_profile",
                )
            )
        return CandidateFrontier(
            tuple(hydrated), frontier.input_count, frontier.output_count, frontier.limit, frontier.truncated
        )

    def snapshot_corpus(self, scope: str, evidence_person_ids: tuple[str, ...]) -> dict[str, Any]:
        operator_resolution = postgres_client.fetch_set_operator_ids(self.corpus.set_id)
        derived_operators = tuple(sorted(str(value) for value in operator_resolution.get("operator_ids") or []))
        if derived_operators and derived_operators != tuple(sorted(self.corpus.operator_ids)):
            raise ValueError("Powerset operator_ids do not match the selected set_id")
        member_ids = sorted(self._scoped_person_ids())
        hydrated = self.hydrate(CandidateFrontier.merge([CandidateRecord(value) for value in evidence_person_ids]))
        evidence = {row.person_id: evidence_hash(dict(row.hydrated_profile or {})) for row in hydrated.candidates}
        schema_hashes = {
            path.stem: canonical_hash(json.loads(path.read_text()))
            for path in sorted(CONTRACTS.glob("*.namespace.json"))
        }
        operator_hash = canonical_hash(sorted(self.corpus.operator_ids))
        membership_hash = canonical_hash(member_ids)
        for supplied, derived, name in (
            (self.corpus.operator_scope_hash, operator_hash, "operator_scope_hash"),
            (self.corpus.membership_hash, membership_hash, "membership_hash"),
        ):
            if supplied is not None and supplied != derived:
                raise ValueError(f"supplied Powerset {name} does not match derived scope")
        if self.corpus.namespace_schema_hashes and dict(self.corpus.namespace_schema_hashes) != schema_hashes:
            raise ValueError("supplied Powerset namespace schema hashes do not match checked-in contracts")
        if self.corpus.native_content_version or self.corpus.scoped_records_hash:
            raise ValueError("caller-supplied Powerset content identity is not accepted")
        return {
            "schema_version": "reflect.corpus_snapshot.v1",
            "backend": "powerset",
            "source": "pr_b_runner_snapshot",
            "verification_status": "unverified_non_comparable",
            "set_id": self.corpus.set_id,
            "operator_scope_hash": operator_hash,
            "membership_hash": membership_hash,
            "namespace_schema_hashes": schema_hashes,
            "scoped_records_hash": None,
            "evidence_hashes": evidence,
            "enumeration_complete": True,
            "enumeration_truncated": False,
            "enumerated_record_count": len(member_ids),
            "membership_id_count": len(member_ids),
            "observed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
