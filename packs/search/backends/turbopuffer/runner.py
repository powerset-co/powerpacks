"""Concrete TurboPuffer/Postgres runner; imports no local backend modules."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

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
from ...primitives.lib import postgres_client
from ...primitives.lib.powerpacks_contracts import normalize_hydrated_context
from ...primitives.turbopuffer import turbopuffer_resolve_companies as company_resolution
from ...primitives.turbopuffer import turbopuffer_resolve_education as education_resolution
from ...primitives.turbopuffer import turbopuffer_search_backend as storage
from ...reflect.snapshots import (
    STRICT_VERIFICATION_STATUS,
    TAGGED_METADATA_VERIFICATION_STATUS,
    canonical_hash,
    canonical_json,
    evidence_hash,
    watermark_membership_hash,
)


CONTRACT_ROOT = Path(__file__).resolve().parents[2] / "contracts"
CONTRACTS = CONTRACT_ROOT / "turbopuffer"
SCOPED_RECORDS_HASH_VERSION = "powerset.scoped_records.streaming.v1"


class _NamespaceSnapshotAccumulator:
    """Hash complete canonical rows in stable TurboPuffer id order."""

    def __init__(
        self,
        *,
        retain_membership: bool = False,
        person_identity_source: str | None = None,
    ):
        self._hash = hashlib.sha256()
        self._hash.update(b"powerset.namespace.rows.v1\0")
        self._last_id: str | None = None
        self.count = 0
        self.member_ids: set[str] | None = set() if retain_membership else None
        self._person_identity_source = person_identity_source

    def consume_page(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            row_id = str(row.get("id") or "")
            if not row_id or (self._last_id is not None and row_id <= self._last_id):
                raise RuntimeError("snapshot rows must have unique ids in stable ascending order")
            encoded = canonical_json(row)
            self._hash.update(len(encoded).to_bytes(8, "big"))
            self._hash.update(encoded)
            self._last_id = row_id
            self.count += 1
            if self.member_ids is not None:
                person_id = str(row.get(self._person_identity_source or "") or "")
                if not person_id:
                    raise RuntimeError("snapshot row is missing canonical person identity")
                self.member_ids.add(person_id)

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


def _streaming_scoped_records_hash(namespace_hashes: dict[str, str], counts: dict[str, int]) -> str:
    """Version the aggregate because it replaces the former materialized JSON hash."""
    return canonical_hash({
        "version": SCOPED_RECORDS_HASH_VERSION,
        "namespaces": {
            name: {"count": counts[name], "rows_hash": namespace_hashes[name]}
            for name in sorted(namespace_hashes)
        },
    })


class TurboPufferSearchRunner:
    def __init__(
        self,
        corpus: PowersetCorpus,
        *,
        namespace_schemas: Mapping[str, Iterable[str]] | None = None,
    ):
        if not isinstance(corpus, PowersetCorpus):
            raise ValueError("TurboPuffer runner requires a Powerset corpus")
        self.corpus = corpus
        self._namespace_schemas = {
            str(name): frozenset(str(attribute) for attribute in attributes)
            for name, attributes in (namespace_schemas or {}).items()
        }

    @property
    def namespace_schemas(self) -> dict[str, frozenset[str]]:
        """Return a detached copy of live schemas verified during snapshotting."""
        return dict(self._namespace_schemas)

    @staticmethod
    def _namespace_contract(name: str) -> dict[str, Any]:
        return json.loads((CONTRACTS / f"{name}.namespace.json").read_text())

    def _namespace_attributes(self, name: str) -> frozenset[str]:
        cached = self._namespace_schemas.get(name)
        if cached is not None:
            return cached
        contract = self._namespace_contract(name)
        return frozenset(str(row["name"]) for row in contract.get("attributes") or [])

    def _include_attributes(self, name: str, *requested: str) -> list[str]:
        """Request only attributes present in the selected namespace schema."""
        available = self._namespace_attributes(name)
        return [attribute for attribute in dict.fromkeys(requested) if attribute in available]

    def _person_identity_source(self, name: str) -> str:
        identity = self._namespace_contract(name).get("person_identity") or {}
        source = str(identity.get("source") or "")
        if source == "row_id":
            return "id"
        if source == "attribute":
            attribute = str(identity.get("attribute") or "")
            if attribute:
                return attribute
        raise ValueError(f"{name} contract does not define canonical person identity")

    def _normalize_person_rows(
        self, name: str, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Normalize namespace-owned person identity once and fail closed on gaps."""
        source = self._person_identity_source(name)
        normalized = []
        for row in rows:
            person_id = str(row.get(source) or "")
            if not person_id:
                raise RuntimeError(f"{name} row is missing canonical person identity from {source}")
            normalized.append({**row, "person_id": person_id})
        return normalized

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
            "investor_names": "investor_urns",
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
        return RunnerCapabilities(Backend.POWERSET, supported, tuple(lanes), skills, True, tuple(fields))

    @staticmethod
    def _snapshot_schema_requirements(spec: SearchSpec | None) -> dict[str, set[str]]:
        """Return only fields the snapshot scope and selected search will exercise."""
        required = {
            name: ({"allowed_operator_ids"} if name != "schools" else set())
            for name in (
                "people",
                "summaries",
                "companies",
                "company_signals",
                "education",
                "schools",
            )
        }
        # Complete membership is person-grain even though the people namespace is
        # position-grain. TurboPuffer document ids alone cannot prove membership.
        required["people"].add("base_id")
        if spec is None:
            return required

        people_filters = (
            ("role_ids", spec.role.role_ids),
            ("city", spec.person_filters.cities),
            ("state", spec.person_filters.states),
            ("country", spec.person_filters.countries),
            ("metro_areas", spec.person_filters.metro_areas),
            ("seniority_band", spec.person_filters.seniority_bands),
            ("role_track", spec.person_filters.role_tracks),
            ("is_current", spec.person_filters.is_current_role),
            ("total_years_experience", spec.person_filters.years_experience_min),
            ("total_years_experience", spec.person_filters.years_experience_max),
            ("is_current", spec.company_filters.is_current_company),
        )
        required["people"].update(
            field for field, value in people_filters if value not in (None, (), [])
        )

        company_source_selected = bool(
            spec.company_filters.company_names
            or spec.company_filters.investor_names
            or spec.company_filters.sector_types
            or spec.company_filters.technology_types
            or spec.company_filters.entity_types
            or spec.company_filters.funding_stage_min is not None
            or spec.company_filters.funding_stage_max is not None
            or spec.company_filters.headcount_min is not None
            or spec.company_filters.headcount_max is not None
        )
        if spec.company_filters.company_ids or company_source_selected:
            required["people"].add("company_id")
        if spec.company_filters.investor_names:
            required["people"].add("investor_names")

        company_filters = (
            ("company_name", spec.company_filters.company_names),
            ("investor_urns", spec.company_filters.investor_names),
            ("sector_types", spec.company_filters.sector_types),
            ("technology_types", spec.company_filters.technology_types),
            ("entity_types", spec.company_filters.entity_types),
            ("funding_stage", spec.company_filters.funding_stage_min),
            ("funding_stage", spec.company_filters.funding_stage_max),
            ("headcount", spec.company_filters.headcount_min),
            ("headcount", spec.company_filters.headcount_max),
        )
        required["companies"].update(
            field for field, value in company_filters if value not in (None, (), [])
        )
        if spec.company_filters.company_names:
            required["companies"].add("name_aliases_text")

        if spec.person_filters.education_ids or spec.person_filters.education_names:
            required["education"].update(("person_id", "canonical_education_id"))
        if spec.person_filters.education_names:
            required["schools"].update(("school_name", "person_count"))
        if spec.tech_skills:
            required["summaries"].add("tech_skills")

        queries = tuple(dict.fromkeys((
            *spec.role.bm25_queries,
            *spec.role.titles,
            *(value.replace("_", " ") for value in spec.role.role_ids),
            *((spec.raw_request,) if spec.raw_request.strip() else ()),
        )))
        if queries:
            required["people"].update(("phrase_tokens", "word_tokens"))
            required["summaries"].add("summary_tokens")
        if spec.raw_request.strip():
            required["people"].add("vector")
            required["summaries"].add("vector")
            required["company_signals"].add("vector")
        return required

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
                self._include_attributes("people", "base_id"),
                page_size=min(10000, max(1, len(candidate_ids))),
            )
        )
        return {
            row["person_id"] for row in self._normalize_person_rows("people", rows)
        }

    def _scoped_person_ids(self) -> set[str]:
        if hasattr(self, "_scope_person_ids"):
            return set(self._scope_person_ids)
        import asyncio

        result = asyncio.run(
            storage.enumerate_filter_only_rows_for_namespace(
                "people",
                ("allowed_operator_ids", "ContainsAny", list(self.corpus.operator_ids)),
                self._include_attributes("people", "base_id"),
                page_size=10000,
            )
        )
        if not result["completed"] or result["truncated"]:
            raise RuntimeError("selected Powerset scope enumeration is incomplete")
        values = {
            row["person_id"]
            for row in self._normalize_person_rows("people", result["rows"])
        }
        self._scope_person_ids = frozenset(values)
        return values

    def resolve_sources(self, spec: SearchSpec) -> ResolvedSources:
        import asyncio

        from .resolution import resolve_turbopuffer_investors

        companies = list(spec.company_filters.company_ids)
        education = list(spec.person_filters.education_ids)
        investor_urns: list[str] = []
        resolved_investor_names: list[str] = []
        records: list[dict[str, Any]] = []
        if spec.company_filters.investor_names:
            investor_rows = asyncio.run(
                resolve_turbopuffer_investors(
                    list(spec.company_filters.investor_names),
                    allowed_operator_ids=list(self.corpus.operator_ids),
                    top_k=1,
                )
            )
            for name in spec.company_filters.investor_names:
                matched = [
                    row
                    for row in investor_rows
                    if str(row.get("query_name") or "").casefold() == name.casefold()
                ]
                if not matched:
                    records.append(
                        {
                            "source": "investor",
                            "input": name,
                            "required": True,
                            "disposition": "unresolved",
                        }
                    )
                for row in matched:
                    urn = str(row.get("urn") or "")
                    resolved_name = str(row.get("canonical_name") or row.get("investor_name") or name)
                    if urn and urn not in investor_urns:
                        investor_urns.append(urn)
                    if resolved_name and resolved_name not in resolved_investor_names:
                        resolved_investor_names.append(resolved_name)
                    if urn:
                        records.append(
                            {
                                "source": "investor",
                                "input": name,
                                "required": True,
                                "disposition": "resolved",
                                "resolved_id": urn,
                                "resolved_name": resolved_name,
                                "match_type": str(row.get("match_type") or "exact"),
                            }
                        )
        archetype_payload = {
            "investor_urns": investor_urns,
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
        return ResolvedSources(
            company_ids=tuple(companies),
            education_ids=tuple(education),
            records=tuple(records),
            investor_urns=tuple(investor_urns),
            investor_names=tuple(resolved_investor_names),
        )

    def _company_archetype_filter(self, payload: dict[str, Any]) -> tuple:
        clauses: list[tuple] = [
            ("allowed_operator_ids", "ContainsAny", list(self.corpus.operator_ids))
        ]
        for payload_key, field, op in (
            ("sector_types", "sector_types", "ContainsAny"),
            ("technology_types", "technology_types", "ContainsAny"),
            ("entity_types", "entity_types", "ContainsAny"),
            ("investor_urns", "investor_urns", "ContainsAny"),
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
            ("investor_names", "ContainsAny", sources.investor_names),
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
        tech_skills_by_person: dict[str, tuple[str, ...]] = {}
        if sources.education_ids:
            education = asyncio.run(
                storage.enumerate_filter_only_rows_for_namespace(
                    "education",
                    (
                        "And",
                        [
                            ("allowed_operator_ids", "ContainsAny", list(self.corpus.operator_ids)),
                            ("canonical_education_id", "In", list(sources.education_ids)),
                        ],
                    ),
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
                    self._include_attributes("summaries", "tech_skills"),
                    page_size=10000,
                )
            )
            if skills["truncated"] or not skills["completed"]:
                raise RuntimeError("skills hard-filter enumeration incomplete")
            skill_rows = self._normalize_person_rows("summaries", skills["rows"])
            skill_people = list(
                dict.fromkeys(
                    row["person_id"] for row in skill_rows
                )
            )
            for row in skill_rows:
                person_id = row["person_id"]
                tech_skills_by_person[person_id] = tuple(
                    dict.fromkeys(
                        (*tech_skills_by_person.get(person_id, ()), *(
                            str(value)
                            for value in row.get("tech_skills") or []
                            if str(value)
                        ))
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
                    "people",
                    summary_filter,
                    self._include_attributes("people", "base_id"),
                    page_size=10000,
                )
            )
            if summary_enumeration["truncated"] or not summary_enumeration["completed"]:
                raise RuntimeError("summary eligibility enumeration incomplete")
            summary_eligible_ids = list(dict.fromkeys(
                row["person_id"]
                for row in self._normalize_person_rows(
                    "people", summary_enumeration["rows"]
                )
            ))
            summary_namespace_filter = (
                "And",
                [summary_namespace_filter, ("id", "In", summary_eligible_ids)],
            )
        enumeration = asyncio.run(
            storage.enumerate_filter_only_rows_for_namespace(
                "people",
                filters,
                self._include_attributes("people", "base_id"),
                page_size=10000,
            )
        )
        all_rows = self._normalize_person_rows("people", enumeration["rows"])
        if company_filter is not None:
            company_enumeration = asyncio.run(
                storage.enumerate_filter_only_rows_for_namespace(
                    "people",
                    company_filter,
                    self._include_attributes("people", "base_id"),
                    page_size=10000,
                )
            )
            if company_enumeration["truncated"] or not company_enumeration["completed"]:
                raise RuntimeError("company-union enumeration incomplete")
            all_rows.extend(
                self._normalize_person_rows("people", company_enumeration["rows"])
            )
        ids = tuple(dict.fromkeys(row["person_id"] for row in all_rows))
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
                "tech_skills_by_person": tech_skills_by_person,
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
        attributes = self._include_attributes(
            "people",
            "base_id",
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
        )
        rows = asyncio.run(
            storage.hybrid_role_rows(
                payload,
                filters.compiled["filter"],
                top_k=plan.spec.bounds.retrieval_limit,
                include_attributes=attributes,
            )
        )
        rows = self._normalize_person_rows("people", rows)
        lane_rows = [("role", rows)]
        if "summary" in plan.capabilities.retrieval_lanes:
            summary_rows = asyncio.run(
                storage.hybrid_summary_rows(
                    payload,
                    filters.compiled.get("summary_namespace_filter"),
                    top_k=plan.spec.bounds.retrieval_limit,
                    include_attributes=self._include_attributes(
                        "summaries", "summary", "tech_skills"
                    ),
                )
            )
            summary_rows = self._normalize_person_rows("summaries", summary_rows)
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
                signal_people = self._normalize_person_rows("people", signal_people)
                for row in signal_people:
                    row["score"] = company_scores.get(str(row.get("company_id") or ""), 0)
                lane_rows.append(("company_signal", signal_people))
        if plan.spec.role.search_mode == "COMPANY_UNION" and queries:
            adjacency_rows = asyncio.run(
                storage.bm25_adjacency_rows(
                    queries,
                    filters.compiled["filter"],
                    top_k=plan.spec.bounds.retrieval_limit,
                    include_attributes=attributes,
                )
            )
            lane_rows.append(
                (
                    "adjacency",
                    self._normalize_person_rows("people", adjacency_rows),
                )
            )
        if filters.compiled.get("company_filter") is not None:
            company_rows = asyncio.run(
                storage.enumerate_filter_only_rows_for_namespace(
                    "people",
                    filters.compiled["company_filter"],
                    attributes,
                    page_size=plan.spec.bounds.retrieval_limit,
                )
            )
            lane_rows.append(
                (
                    "company_union",
                    self._normalize_person_rows(
                        "people", company_rows["rows"][: plan.spec.bounds.retrieval_limit]
                    ),
                ),
            )
        out = []
        indexed_skills = filters.compiled.get("tech_skills_by_person") or {}
        for lane, lane_values in lane_rows:
            for rank, row in enumerate(lane_values, start=1):
                person_id = row["person_id"]
                position_id = "" if lane == "summary" else str(row.get("position_id") or row.get("id") or "")
                score = float(row.get("score") or 0)
                actual_skills = tuple(
                    dict.fromkeys(
                        str(value)
                        for value in (
                            row.get("tech_skills")
                            or indexed_skills.get(person_id)
                            or ()
                        )
                        if str(value)
                    )
                )
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
                        tech_skills=actual_skills,
                        hard_filter_evidence=(
                            {
                                "tech_skills": {
                                    "source": "turbopuffer_summaries",
                                    "values": list(actual_skills),
                                }
                            }
                            if actual_skills
                            else {}
                        ),
                    )
                )
        return tuple(out)

    def hydrate(self, frontier: CandidateFrontier) -> CandidateFrontier:
        person_ids = [row.person_id for row in frontier.candidates]
        rows = postgres_client.fetch_person_rows(
            person_ids
        )
        interaction_counts = postgres_client.fetch_interaction_counts(
            person_ids,
            allowed_operator_ids=list(self.corpus.operator_ids),
        )
        source_attribution = postgres_client.fetch_source_attribution(
            person_ids,
            allowed_operator_ids=list(self.corpus.operator_ids),
        )
        by_id = {}
        for raw in rows:
            person_id = str(raw["id"])
            if person_id in interaction_counts:
                raw["total_interactions"] = interaction_counts[person_id]
            profile = normalize_hydrated_context(raw)
            profile.update(
                {key: value for key, value in raw.items() if key != "hydrated_context" and value is not None}
            )
            attribution = source_attribution.get(person_id)
            if attribution:
                profile["source_channels"] = attribution.get("channels", [])
            by_id[person_id] = profile
        hydrated = []
        for row in frontier.candidates:
            profile = by_id.get(row.person_id)
            if profile is not None and row.tech_skills:
                profile = dict(profile)
                profile["tech_skills"] = list(
                    dict.fromkeys(
                        [
                            str(value)
                            for value in profile.get("tech_skills") or []
                            if str(value)
                        ]
                        + list(row.tech_skills)
                    )
                )
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

    def snapshot_corpus(
        self,
        scope: str,
        evidence_person_ids: tuple[str, ...],
        *,
        spec: SearchSpec | None = None,
    ) -> dict[str, Any]:
        """Prove corpus identity — cheaply when the caller supplies a corpus tag.

        One visible decision, one input: `corpus.native_content_version`.

          tag supplied -> live namespace metadata only (row counts + write watermarks +
                          live schemas, seconds); status tagged_metadata_non_comparable.
          no tag       -> the full scoped enumeration strict Reflect comparability needs
                          (minutes on a large set); status verified_comparable.

        Scope, contract, and live-schema proof are identical in both modes; only the
        content/membership identity differs.
        """
        if scope != self.corpus.set_id:
            raise ValueError("snapshot scope must exactly match the selected Powerset set_id")
        operator_resolution = postgres_client.fetch_set_operator_ids(self.corpus.set_id)
        if str(operator_resolution.get("set_id") or "") != self.corpus.set_id:
            raise ValueError("Postgres resolved a different Powerset set_id")
        derived_operators = tuple(sorted(str(value) for value in operator_resolution.get("operator_ids") or []))
        if not derived_operators:
            raise ValueError("selected Powerset set has no Postgres-derived operator scope")
        if derived_operators != tuple(sorted(self.corpus.operator_ids)):
            raise ValueError("Powerset operator_ids do not match the selected set_id")
        contracts = {}
        for path in sorted(CONTRACTS.glob("*.namespace.json")):
            contract = json.loads(path.read_text())
            contracts[str(contract["name"])] = contract
        required_namespaces = (
            "people",
            "summaries",
            "companies",
            "company_signals",
            "education",
            "schools",
        )
        if not set(required_namespaces) <= set(contracts):
            raise ValueError("checked-in TurboPuffer snapshot contracts are incomplete")
        contracts = {name: contracts[name] for name in required_namespaces}
        operator_filter = (
            "allowed_operator_ids",
            "ContainsAny",
            list(self.corpus.operator_ids),
        )
        live_schemas: dict[str, dict[str, Any]] = {}
        schema_requirements = self._snapshot_schema_requirements(spec)
        for name in required_namespaces:
            contract = contracts[name]
            contracted_attributes = {
                str(row["name"]) for row in contract.get("attributes") or []
            }
            if contract.get("vector"):
                contracted_attributes.add("vector")
            missing_contract = sorted(schema_requirements[name] - contracted_attributes)
            if missing_contract:
                raise ValueError(
                    f"{name} checked-in contract is missing operationally required attributes: "
                    + ", ".join(missing_contract)
                )
            live_schema = storage.namespace_schema(name)
            live_schemas[name] = live_schema
            self._namespace_schemas[name] = frozenset(live_schema)
            missing_required = sorted(schema_requirements[name] - set(live_schema))
            if missing_required:
                raise ValueError(
                    f"{name} live schema is missing operationally required attributes: "
                    + ", ".join(missing_required)
                )
        schema_hashes = {name: canonical_hash(live_schemas[name]) for name in required_namespaces}
        operator_hash = canonical_hash(sorted(self.corpus.operator_ids))
        if self.corpus.operator_scope_hash is not None and self.corpus.operator_scope_hash != operator_hash:
            raise ValueError("supplied Powerset operator_scope_hash does not match derived scope")
        if self.corpus.namespace_schema_hashes and dict(self.corpus.namespace_schema_hashes) != schema_hashes:
            raise ValueError("supplied Powerset namespace schema hashes do not match live schemas")
        requested_ids = tuple(dict.fromkeys(str(value) for value in evidence_person_ids))
        identity = (
            self._tagged_metadata_identity(required_namespaces)
            if self.corpus.native_content_version
            else self._enumerated_identity(required_namespaces, operator_filter, requested_ids)
        )
        if self.corpus.membership_hash is not None and self.corpus.membership_hash != identity["membership_hash"]:
            raise ValueError("supplied Powerset membership_hash does not match derived scope")
        return {
            "schema_version": "reflect.corpus_snapshot.v2",
            "backend": "powerset",
            "source": "pr_b_runner_snapshot",
            "set_id": self.corpus.set_id,
            "operator_scope_hash": operator_hash,
            "namespace_schema_hashes": schema_hashes,
            "evidence_hashes": self._evidence_hashes(requested_ids),
            "observed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            **identity,
        }

    def _tagged_metadata_identity(self, required_namespaces: tuple[str, ...]) -> dict[str, Any]:
        """Trust the caller's tag; prove only the cheap live write watermark."""
        namespace_metadata = {name: storage.namespace_metadata(name) for name in required_namespaces}
        return {
            "verification_status": TAGGED_METADATA_VERIFICATION_STATUS,
            "membership_hash": watermark_membership_hash(namespace_metadata),
            "namespace_metadata": namespace_metadata,
            "native_content_version": self.corpus.native_content_version,
        }

    def _enumerated_identity(
        self,
        required_namespaces: tuple[str, ...],
        operator_filter: tuple[str, str, list[str]],
        requested_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        """Enumerate and hash every scoped row: the only strictly comparable identity."""
        import asyncio

        print(
            f"snapshot: enumerating every scoped row across {len(required_namespaces)} namespaces "
            "for strict comparability; supply corpus native_content_version to skip",
            file=sys.stderr,
        )
        namespace_counts: dict[str, int] = {}
        namespace_hashes: dict[str, str] = {}
        member_ids: set[str] = set()
        for name in required_namespaces:
            accumulator = _NamespaceSnapshotAccumulator(
                retain_membership=name == "people",
                person_identity_source=(
                    self._person_identity_source(name) if name == "people" else None
                ),
            )
            enumeration = asyncio.run(
                storage.consume_filter_only_pages_for_namespace(
                    name,
                    None if name == "schools" else operator_filter,
                    True,
                    accumulator.consume_page,
                    page_size=10000,
                )
            )
            if not enumeration.get("completed") or enumeration.get("truncated"):
                raise RuntimeError(f"{name} snapshot enumeration is incomplete")
            if enumeration.get("row_count") != accumulator.count:
                raise RuntimeError(f"{name} snapshot enumeration count mismatch")
            namespace_counts[name] = accumulator.count
            namespace_hashes[name] = accumulator.hexdigest()
            if accumulator.member_ids is not None:
                member_ids.update(accumulator.member_ids)
        if sorted(set(requested_ids) - member_ids):
            raise ValueError("requested evidence person IDs are outside complete Powerset membership")
        scoped_records_hash = _streaming_scoped_records_hash(namespace_hashes, namespace_counts)
        if self.corpus.scoped_records_hash is not None and self.corpus.scoped_records_hash != scoped_records_hash:
            raise ValueError("supplied Powerset content identity does not match derived scope")
        return {
            "verification_status": STRICT_VERIFICATION_STATUS,
            "membership_hash": canonical_hash(sorted(member_ids)),
            "scoped_records_hash": scoped_records_hash,
            "enumeration_complete": True,
            "enumeration_truncated": False,
            "enumerated_record_count": sum(namespace_counts.values()),
            "namespace_record_counts": namespace_counts,
            "membership_id_count": len(member_ids),
        }

    def _evidence_hashes(self, requested_ids: tuple[str, ...]) -> dict[str, str]:
        hydrated_rows = postgres_client.fetch_person_rows(list(requested_ids))
        hydrated_by_id = {str(row.get("id")): row for row in hydrated_rows if row.get("id")}
        if [value for value in requested_ids if value not in hydrated_by_id]:
            raise RuntimeError("requested Powerset evidence hydration is missing")
        evidence = {}
        for person_id in requested_ids:
            raw = hydrated_by_id[person_id]
            profile = normalize_hydrated_context(raw)
            profile.update({
                key: value
                for key, value in raw.items()
                if key != "hydrated_context" and value is not None
            })
            evidence[person_id] = evidence_hash(profile)
        return evidence
