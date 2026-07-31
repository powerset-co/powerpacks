"""Concrete local DuckDB runner; imports no remote backend modules."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...pipeline.frontier import CandidateFrontier, CandidateRecord, ProbeMatch
from ...pipeline.models import (
    Backend,
    HardFilterSet,
    LookupSpec,
    ResolvedSources,
    RunnerCapabilities,
    SearchPlan,
    SearchSpec,
)
from ...primitives.local.local_duckdb_store import LocalDuckDBSearchStore
from ...reflect.snapshots import canonical_hash, evidence_hash

FILTER_COLUMNS = {
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
    "sector_types": "company_sector_types",
    "technology_types": "company_technology_types",
    "entity_types": "company_entity_types",
    "headcount_min": "company_headcount",
    "headcount_max": "company_headcount",
    "is_current_company": "is_current",
}


def _and(clauses: list[tuple]) -> tuple | None:
    return None if not clauses else clauses[0] if len(clauses) == 1 else ("And", clauses)


class LocalSearchRunner:
    def __init__(self, db_path: str):
        self.db_path = str(Path(db_path).resolve())

    def _store(self) -> LocalDuckDBSearchStore:
        return LocalDuckDBSearchStore(self.db_path)

    def capabilities(self, spec: SearchSpec) -> RunnerCapabilities:
        store = self._store()
        try:
            relevant_columns: set[str] = set()
            for table in (
                "local_person_profiles",
                "local_people_profiles",
                "local_people_positions",
                "local_people_education",
                "local_summaries",
            ):
                if store._table_exists(table):
                    relevant_columns.update(store._table_columns(table))
            skills = "tech_skills" in relevant_columns
            education = "canonical_education_id" in relevant_columns
            lookup_columns: set[str] = set()
            for table in ("local_person_profiles", "local_people_profiles", "local_people_positions"):
                if store._table_exists(table):
                    lookup_columns.update(store._table_columns(table))
            lanes = ["role", "sql"]
            if store.namespace_exists("summaries"):
                lanes.append("summary")
            if store.namespace_exists("company_signals"):
                lanes.append("company_signal")
            lanes.append("adjacency")
        finally:
            store.conn.close()
        supported = tuple(name for name, column in FILTER_COLUMNS.items() if column in relevant_columns)
        if education:
            supported += ("education_ids",)
        if skills:
            supported += ("tech_skills",)
        aliases = {
            "name": {"full_name"},
            "handle": {"public_identifier", "twitter_handle", "x_twitter_handle"},
            "profile_url": {"linkedin_url", "public_profile_url"},
            "person_id": {"person_id", "id", "base_id"},
            "email": {"primary_email", "all_emails"},
            "phone": {"primary_phone", "all_phones"},
        }
        return RunnerCapabilities(
            Backend.LOCAL,
            supported,
            tuple(lanes),
            skills,
            True,
            tuple(name for name, columns in aliases.items() if columns & lookup_columns),
        )

    def lookup_person(self, lookup: LookupSpec | None) -> tuple[CandidateRecord, ...]:
        if lookup is None:
            return ()
        import duckdb

        aliases = {
            "name": ("full_name",),
            "handle": ("public_identifier", "twitter_handle", "x_twitter_handle"),
            "profile_url": ("linkedin_url", "public_profile_url"),
            "person_id": ("person_id", "id", "base_id"),
            "email": ("primary_email", "all_emails"),
            "phone": ("primary_phone", "all_phones"),
        }
        with duckdb.connect(self.db_path, read_only=True) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "select table_name from information_schema.tables where table_schema='main'"
                ).fetchall()
            }
            table = next(
                name
                for name in ("local_person_profiles", "local_people_profiles", "local_people_positions")
                if name in tables
            )
            columns = {row[1] for row in conn.execute(f"pragma table_info('{table}')").fetchall()}
            fields = [field for field in aliases[lookup.field] if field in columns]
            clauses = [
                f'list_contains(list_transform("{field}", x -> lower(cast(x as varchar))), lower(?))'
                if field.startswith("all_")
                else f'lower(cast("{field}" as varchar)) = lower(?)'
                for field in fields
            ]
            if not clauses:
                return ()
            rows = conn.execute(
                f'SELECT * FROM "{table}" WHERE {" OR ".join(clauses)} ORDER BY 1 LIMIT 20',
                [lookup.value] * len(fields),
            ).fetchall()
            names = [item[0] for item in conn.description]
        out = []
        for rank, values in enumerate(rows, 1):
            row = dict(zip(names, values))
            person_id = str(row.get("person_id") or row.get("base_id") or row.get("id") or "")
            if person_id:
                out.append(
                    CandidateRecord(
                        person_id,
                        1.0,
                        source_lanes=("lookup",),
                        found_by=(ProbeMatch("lookup", rank, probe_family="deterministic_lookup"),),
                        backend="local",
                    )
                )
        return tuple(out)

    def resolve_sources(self, spec: SearchSpec) -> ResolvedSources:
        companies, education = list(spec.company_filters.company_ids), list(spec.person_filters.education_ids)
        records: list[dict[str, Any]] = []
        store = self._store()
        try:
            for logical, names, source, ids in (
                ("companies", spec.company_filters.company_names, "company", companies),
                ("schools", spec.person_filters.education_names, "education", education),
            ):
                if not names:
                    continue
                if not store.namespace_exists(logical):
                    records.extend(
                        {
                            "source": source,
                            "input": name,
                            "required": True,
                            "disposition": "unresolved",
                            "reason": "namespace_unavailable",
                        }
                        for name in names
                    )
                    continue
                table = store._table_for_namespace(logical)
                columns = set(store._table_columns(table))
                name_col = next(
                    (value for value in ("name", "company_name", "school_name", "display_value") if value in columns),
                    None,
                )
                candidates = (
                    ("canonical_education_id", "education_id", "id")
                    if source == "education"
                    else ("company_id", "company_urn", "id")
                )
                id_col = next((value for value in candidates if value in columns), None)
                for name in names:
                    rows = (
                        []
                        if not name_col or not id_col
                        else store.conn.execute(
                            f'SELECT "{id_col}" FROM "{table}" WHERE lower("{name_col}") = lower(?)', [name]
                        ).fetchall()
                    )
                    if not rows:
                        records.append({"source": source, "input": name, "required": True, "disposition": "unresolved"})
                    for (value,) in rows:
                        if str(value) not in ids:
                            ids.append(str(value))
                        records.append(
                            {
                                "source": source,
                                "input": name,
                                "required": True,
                                "disposition": "resolved",
                                "resolved_id": str(value),
                            }
                        )
        finally:
            store.conn.close()
        return ResolvedSources(tuple(companies), tuple(education), tuple(records))

    def _filters(
        self,
        spec: SearchSpec,
        sources: ResolvedSources,
        *,
        include_company: bool = True,
        include_role: bool = True,
        include_currentness: bool = True,
    ) -> tuple | None:
        clauses: list[tuple] = []
        values = (
            ("role_ids", "ContainsAny", spec.role.role_ids),
            ("city", "In", spec.person_filters.cities),
            ("state", "In", spec.person_filters.states),
            ("country", "In", spec.person_filters.countries),
            ("metro_areas", "ContainsAny", spec.person_filters.metro_areas),
            ("seniority_band", "In", spec.person_filters.seniority_bands),
            ("role_track", "In", spec.person_filters.role_tracks),
            ("company_id", "In", sources.company_ids),
            ("company_sector_types", "ContainsAny", spec.company_filters.sector_types),
            ("company_technology_types", "ContainsAny", spec.company_filters.technology_types),
            ("company_entity_types", "ContainsAny", spec.company_filters.entity_types),
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
        for field, op, value in (
            (
                "is_current",
                "Eq",
                spec.person_filters.is_current_role
                if include_role and include_currentness
                else None,
            ),
            (
                "is_current",
                "Eq",
                spec.company_filters.is_current_company
                if include_company
                and include_currentness
                and (not include_role or spec.person_filters.is_current_role is None)
                else None,
            ),
            ("total_years_experience", "Gte", spec.person_filters.years_experience_min),
            ("total_years_experience", "Lte", spec.person_filters.years_experience_max),
            ("company_headcount", "Gte", spec.company_filters.headcount_min if include_company else None),
            ("company_headcount", "Lte", spec.company_filters.headcount_max if include_company else None),
        ):
            if value is not None:
                clauses.append((field, op, value))
        return _and(clauses)

    def apply_hard_filters(self, spec: SearchSpec, sources: ResolvedSources) -> HardFilterSet:
        store = self._store()
        try:
            union = spec.role.search_mode == "COMPANY_UNION" and bool(sources.company_ids)
            filters = self._filters(spec, sources, include_company=not union)
            summary_filter = self._filters(
                spec,
                sources,
                include_company=not union,
            )
            company_filter = self._filters(spec, sources, include_role=False) if union else None
            for namespace, field, wanted in (
                ("education", "canonical_education_id", sources.education_ids),
                ("summaries", "tech_skills", spec.tech_skills),
            ):
                if wanted:
                    rows = store.filter_only_rows_for_namespace(
                        namespace,
                        (field, "ContainsAny" if field == "tech_skills" else "In", list(wanted)),
                        ["base_id", "person_id"],
                        10000,
                        0,
                    )
                    ids = list(
                        dict.fromkeys(
                            str(row.get("person_id") or row.get("base_id"))
                            for row in rows
                            if row.get("person_id") or row.get("base_id")
                        )
                    )
                    clause = ("base_id", "In", ids)
                    filters = clause if filters is None else ("And", [filters, clause])
                    summary_filter = clause if summary_filter is None else ("And", [summary_filter, clause])
                    if company_filter is not None:
                        company_filter = ("And", [company_filter, clause])
            count = store.filtered_people_count(filters)
            rows = store.filter_only_rows_for_namespace(
                "people", filters or ("id", "NotEq", ""), ["base_id", "person_id"], 10000, 0
            )
            if company_filter is not None:
                company_rows = store.filter_only_rows_for_namespace(
                    "people", company_filter, ["base_id", "person_id"], 10000, 0
                )
                rows.extend(company_rows)
                count["matched_people"] = len({str(row.get("person_id") or row.get("base_id")) for row in rows})
            ids = tuple(
                dict.fromkeys(
                    str(row.get("person_id") or row.get("base_id"))
                    for row in rows
                    if row.get("person_id") or row.get("base_id")
                )
            )
        finally:
            store.conn.close()
        return HardFilterSet(
            count["matched_people"],
            ids,
            {"filter": filters, "summary_filter": summary_filter, "company_filter": company_filter},
        )

    def retrieve_people(
        self,
        plan: SearchPlan,
        filters: HardFilterSet,
        probe_id: str | None = None,
        probe_family: str | None = None,
    ) -> tuple[CandidateRecord, ...]:
        store = self._store()
        spec = plan.spec
        try:
            role_queries = tuple(value.replace("_", " ") for value in spec.role.role_ids)
            queries = list(
                dict.fromkeys(
                    (
                        *spec.role.bm25_queries,
                        *spec.role.titles,
                        *role_queries,
                        *((spec.raw_request,) if spec.raw_request.strip() else ()),
                    )
                )
            )
            payload = {
                "bm25_queries": queries,
                "role_ids": list(spec.role.role_ids),
                "tech_skills": list(spec.tech_skills),
            }
            attrs = [
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
                "company_technology_types",
                "company_entity_types",
                "company_headcount",
                "company_stage",
            ]
            available = set(store._table_columns(store._table_for_namespace("people")))
            attrs = [name for name in attrs if name in available]
            lanes = [
                (
                    "role",
                    asyncio.run(
                        store.hybrid_role_rows(
                            payload, filters.compiled.get("filter"), spec.bounds.retrieval_limit, attrs
                        )
                    ),
                )
            ]
            if "summary" in plan.capabilities.retrieval_lanes:
                lanes.append(
                    (
                        "summary",
                        store.summary_search_rows(
                            payload,
                            filters.compiled.get("summary_filter"),
                            spec.bounds.retrieval_limit,
                            attrs,
                        ),
                    )
                )
            if "company_signal" in plan.capabilities.retrieval_lanes:
                lanes.append(
                    (
                        "company_signal",
                        store.company_signal_rows(
                            payload, filters.compiled.get("filter"), spec.bounds.retrieval_limit, attrs
                        ),
                    )
                )
            if spec.role.search_mode == "COMPANY_UNION" and queries:
                lanes.append(
                    (
                        "adjacency",
                        store.bm25_adjacency_rows(
                            queries, filters.compiled.get("filter"), spec.bounds.retrieval_limit, attrs
                        ),
                    )
                )
            if filters.compiled.get("company_filter") is not None:
                company_rows = store.filter_only_rows_for_namespace(
                    "people", filters.compiled["company_filter"], attrs, 10000, spec.bounds.retrieval_limit
                )
                lanes.append(("company_union", company_rows))
        finally:
            store.conn.close()
        out = []
        for lane, rows in lanes:
            for rank, row in enumerate(rows, 1):
                person_id = str(row.get("person_id") or row.get("base_id") or "")
                position_id = "" if lane == "summary" else str(row.get("position_id") or row.get("id") or "")
                if person_id:
                    score = float(row.get("score") or 1 / rank)
                    out.append(
                        CandidateRecord(
                            person_id,
                            score,
                            matched_position_ids=(position_id,) if position_id else (),
                            source_lanes=(lane,),
                            found_by=(ProbeMatch(lane, rank, probe_id, probe_family or "structured_gtm", score),),
                            backend="local",
                            structured={key: row.get(key) for key in attrs},
                        )
                    )
        return tuple(out)

    def hydrate(self, frontier: CandidateFrontier) -> CandidateFrontier:
        if not frontier.candidates:
            return frontier
        import duckdb

        wanted = [row.person_id for row in frontier.candidates]
        profiles = {person_id: {"positions": []} for person_id in wanted}
        with duckdb.connect(self.db_path, read_only=True) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "select table_name from information_schema.tables where table_schema='main'"
                ).fetchall()
            }
            profile_table = next(
                (table for table in ("local_person_profiles", "local_people_profiles") if table in tables),
                None,
            )
            selected = []
            if profile_table:
                selected.append((profile_table, None))
            selected.extend(
                (
                    ("local_people_positions", "positions"),
                    ("local_people_education", "education"),
                    ("local_summaries", "summary"),
                )
            )
            for table, target in selected:
                if table not in tables:
                    continue
                columns = [row[1] for row in conn.execute(f"pragma table_info('{table}')").fetchall()]
                id_col = "person_id" if "person_id" in columns else "base_id" if "base_id" in columns else "id"
                rows = conn.execute(
                    f'SELECT * FROM "{table}" WHERE cast("{id_col}" as varchar) IN (SELECT * FROM unnest(?))', [wanted]
                ).fetchall()
                for values in rows:
                    item = dict(zip(columns, values))
                    profile = profiles[str(item[id_col])]
                    if target in {"positions", "education"}:
                        profile.setdefault(target, []).append(item)
                    elif target == "summary":
                        profile["tech_skills"] = item.get("tech_skills") or []
                    else:
                        profile.update(item)
        hydrated = []
        for row in frontier.candidates:
            profile = profiles.get(row.person_id)
            position_ids = set(row.matched_position_ids)
            indexes = tuple(
                index
                for index, position in enumerate((profile or {}).get("positions") or [])
                if str(position.get("position_id") or position.get("id") or "") in position_ids
            )
            disposition = (
                "hydrated" if profile and (profile.get("positions") or profile.get("full_name")) else "missing_profile"
            )
            hydrated.append(
                replace(
                    row,
                    hydrated_profile=profile if disposition == "hydrated" else None,
                    matched_position_indexes=indexes,
                    hydration_disposition=disposition,
                )
            )
        return CandidateFrontier(
            tuple(hydrated), frontier.input_count, frontier.output_count, frontier.limit, frontier.truncated
        )

    def snapshot_corpus(self, scope: str, evidence_person_ids: tuple[str, ...]) -> dict[str, Any]:
        import duckdb

        with duckdb.connect(self.db_path, read_only=True) as conn:
            tables = sorted(
                row[0]
                for row in conn.execute(
                    "select table_name from information_schema.tables where table_schema='main'"
                ).fetchall()
            )
            schema = {table: conn.execute(f"pragma table_info('{table}')").fetchall() for table in tables}
            content = {table: conn.execute(f'SELECT * FROM "{table}" ORDER BY ALL').fetchall() for table in tables}
            positions = next((table for table in tables if table == "local_people_positions"), None)
            columns = (
                []
                if not positions
                else [row[1] for row in conn.execute(f"pragma table_info('{positions}')").fetchall()]
            )
            id_col = "person_id" if "person_id" in columns else "base_id"
            member_ids = (
                []
                if not positions
                else [
                    str(row[0])
                    for row in conn.execute(f'SELECT DISTINCT "{id_col}" FROM "{positions}" ORDER BY 1').fetchall()
                ]
            )
        hydrated = self.hydrate(CandidateFrontier.merge([CandidateRecord(value) for value in evidence_person_ids]))
        return {
            "schema_version": "reflect.corpus_snapshot.v1",
            "backend": "local",
            "source": "local_deterministic_snapshot",
            "verification_status": "verified_comparable",
            "set_id": scope,
            "operator_scope_hash": canonical_hash([]),
            "membership_hash": canonical_hash(member_ids),
            "namespace_schema_hashes": {key: canonical_hash(value) for key, value in schema.items()},
            "scoped_records_hash": canonical_hash(content),
            "evidence_hashes": {
                row.person_id: evidence_hash(dict(row.hydrated_profile or {})) for row in hydrated.candidates
            },
            "enumeration_complete": True,
            "enumeration_truncated": False,
            "enumerated_record_count": len(member_ids),
            "membership_id_count": len(member_ids),
            "observed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
