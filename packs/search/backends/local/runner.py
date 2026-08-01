"""Concrete local DuckDB runner; imports no remote backend modules."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import date, datetime, timezone
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

_HYDRATION_INDEX_FIELDS = ("vector", "embedding", "token")


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _without_index_fields(value: Any) -> Any:
    """Remove retrieval-only payloads before evidence leaves the local runner."""
    if isinstance(value, dict):
        return {
            str(key): _without_index_fields(item)
            for key, item in value.items()
            if not any(marker in str(key).casefold() for marker in _HYDRATION_INDEX_FIELDS)
        }
    if isinstance(value, (list, tuple)):
        return [_without_index_fields(item) for item in value]
    return value


def _position_id(position: dict[str, Any]) -> str:
    return str(position.get("position_id") or position.get("id") or position.get("linkedin_position_id") or "")


def _canonical_position(raw: dict[str, Any]) -> dict[str, Any]:
    evidence_fields = {
        "id",
        "position_id",
        "linkedin_position_id",
        "title",
        "position_title",
        "raw_title",
        "description",
        "dense_text",
        "company",
        "company_name",
        "company_id",
        "company_domain",
        "company_linkedin_url",
        "company_description",
        "company_sector_types",
        "company_technology_types",
        "company_entity_types",
        "company_headcount",
        "company_funding_total",
        "company_stage",
        "investor_names",
        "city",
        "state",
        "country",
        "metro_areas",
        "location",
        "is_current",
        "start_date",
        "end_date",
        "start_date_epoch",
        "end_date_epoch",
        "tenure_years",
        "seniority_band",
        "role_track",
        "role_ids",
        "total_years_experience",
        "inferred_birth_year",
    }
    position = dict(_without_index_fields({key: value for key, value in raw.items() if key in evidence_fields}))
    identifier = _position_id(position)
    title = position.get("position_title") or position.get("raw_title") or position.get("title")
    company = position.get("company_name") or position.get("company")
    if identifier:
        position["id"] = identifier
        position["position_id"] = identifier
    if title:
        position["title"] = title
        position["position_title"] = title
    if company:
        position["company"] = company
        position["company_name"] = company
    if not position.get("location"):
        location = ", ".join(
            str(position.get(field)) for field in ("city", "state", "country") if position.get(field)
        )
        if location:
            position["location"] = location
    return position


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
        profile_rows: dict[str, dict[str, Any]] = {}
        position_rows: dict[str, list[dict[str, Any]]] = {person_id: [] for person_id in wanted}
        education_rows: dict[str, list[dict[str, Any]]] = {person_id: [] for person_id in wanted}
        summary_rows: dict[str, dict[str, Any]] = {}
        interaction_counts: dict[str, int] = {}
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
            selected: list[tuple[str, str | None]] = []
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
                    person_id = str(item[id_col])
                    if target == "positions":
                        position_rows[person_id].append(item)
                    elif target == "education":
                        education_rows[person_id].append(item)
                    elif target == "summary":
                        summary_rows[person_id] = item
                    else:
                        profile_rows[person_id] = item

            if "local_person_source_summary" in tables:
                columns = {
                    row[1] for row in conn.execute("pragma table_info('local_person_source_summary')").fetchall()
                }
                if {"person_id", "total_interactions"} <= columns:
                    rows = conn.execute(
                        """
                        SELECT cast(person_id AS varchar),
                               sum(coalesce(try_cast(total_interactions AS bigint), 0))
                        FROM local_person_source_summary
                        WHERE cast(person_id AS varchar) IN (SELECT * FROM unnest(?))
                        GROUP BY 1
                        """,
                        [wanted],
                    ).fetchall()
                    interaction_counts = {str(person_id): int(total or 0) for person_id, total in rows}

        hydrated = []
        for row in frontier.candidates:
            raw_profile = profile_rows.get(row.person_id, {})
            context = _json_mapping(raw_profile.get("hydrated_context"))
            raw_positions = position_rows.get(row.person_id) or _json_list(
                raw_profile.get("work_experiences")
            ) or _json_list(context.get("positions"))
            positions = [_canonical_position(item) for item in raw_positions if isinstance(item, dict)]
            positions.sort(
                key=lambda item: (
                    not bool(item.get("is_current")),
                    -int(item.get("start_date_epoch") or 0),
                )
            )
            education = [
                _without_index_fields(item)
                for item in (
                    education_rows.get(row.person_id)
                    or _json_list(raw_profile.get("education"))
                    or _json_list(context.get("education"))
                )
                if isinstance(item, dict)
            ]
            summary = summary_rows.get(row.person_id, {})
            current = next((item for item in positions if item.get("is_current") is True), positions[0] if positions else {})
            birth_year = raw_profile.get("inferred_birth_year") or context.get("inferred_birth_year")
            if not birth_year:
                birth_year = next((item.get("inferred_birth_year") for item in positions if item.get("inferred_birth_year")), None)
            try:
                birth_year = int(birth_year) if birth_year else None
            except (TypeError, ValueError):
                birth_year = None
            total_interactions = interaction_counts.get(row.person_id)
            if total_interactions is None:
                total_interactions = raw_profile.get("total_interactions")
            if total_interactions is None:
                total_interactions = context.get("total_interactions")
            location = raw_profile.get("location_raw") or context.get("location")
            if not location:
                location = ", ".join(
                    str(raw_profile.get(field) or current.get(field))
                    for field in ("city", "state", "country")
                    if raw_profile.get(field) or current.get(field)
                ) or None
            years = context.get("years_of_experience") or raw_profile.get("total_years_experience")
            if years is None:
                years = next(
                    (item.get("total_years_experience") for item in positions if item.get("total_years_experience") is not None),
                    None,
                )
            title = raw_profile.get("current_title") or current.get("position_title") or current.get("title")
            company = raw_profile.get("current_company") or current.get("company_name") or current.get("company")
            headline = raw_profile.get("headline") or context.get("headline") or " at ".join(
                str(value) for value in (title, company) if value
            ) or title
            profile = _without_index_fields(
                {
                    "person_id": row.person_id,
                    "name": raw_profile.get("full_name") or context.get("name") or "",
                    "full_name": raw_profile.get("full_name") or context.get("name") or "",
                    "headline": headline,
                    "summary": raw_profile.get("summary") or summary.get("summary") or context.get("summary"),
                    "location": location,
                    "location_raw": location,
                    "city": raw_profile.get("city") or current.get("city"),
                    "state": raw_profile.get("state") or current.get("state"),
                    "country": raw_profile.get("country") or current.get("country"),
                    "linkedin_url": raw_profile.get("linkedin_url")
                    or raw_profile.get("public_profile_url")
                    or context.get("linkedin_url"),
                    "positions": positions,
                    "education": education,
                    "tech_skills": summary.get("tech_skills") or context.get("tech_skills") or [],
                    "inferred_birth_year": birth_year,
                    "inferred_age": date.today().year - birth_year if birth_year else None,
                    "years_of_experience": years,
                    "total_years_experience": years,
                    "total_interactions": total_interactions,
                }
            )
            position_ids = set(row.matched_position_ids)
            indexes = tuple(
                index
                for index, position in enumerate(positions)
                if _position_id(position) in position_ids
            )
            if not indexes:
                matched_title = str(row.structured.get("position_title") or "").strip().casefold()
                matched_company = str(row.structured.get("company_id") or "").strip().casefold()
                indexes = tuple(
                    index
                    for index, position in enumerate(positions)
                    if (not matched_title or str(position.get("position_title") or "").strip().casefold() == matched_title)
                    and (not matched_company or str(position.get("company_id") or "").strip().casefold() == matched_company)
                ) if matched_title or matched_company else ()
            indexes = tuple(dict.fromkeys((*row.matched_position_indexes, *indexes)))
            disposition = "hydrated" if positions or raw_profile else "missing_profile"
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
