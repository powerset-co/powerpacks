#!/usr/bin/env python3
"""Hydrate a Powerpacks frontier from the checked-in Postgres contract."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID


PRIMITIVES_DIR = Path(__file__).resolve().parents[1]
LIB_DIR = PRIMITIVES_DIR / "lib"
SHARED_DIR = PRIMITIVES_DIR / "shared"
LOCAL_DIR = PRIMITIVES_DIR / "local"
TURBOPUFFER_DIR = PRIMITIVES_DIR / "turbopuffer"
for _path in [LIB_DIR, SHARED_DIR, LOCAL_DIR, TURBOPUFFER_DIR]:
    sys.path.insert(0, str(_path))

from postgres_client import fetch_interaction_counts, fetch_person_rows, fetch_source_attribution, load_env_file  # noqa: E402
from powerpacks_contracts import normalize_hydrated_context  # noqa: E402


DEFAULT_ENV_FILE = Path(os.getenv("POWERPACKS_ENV_FILE", ".env"))
DEFAULT_LOCAL_BATCH_SIZE = 1000
DEFAULT_LOCAL_WORKERS = min(8, max(1, os.cpu_count() or 1))
LOCAL_PROFILE_HYDRATE_COLUMNS = [
    "id",
    "person_id",
    "base_id",
    "public_identifier",
    "linkedin_url",
    "public_profile_url",
    "first_name",
    "last_name",
    "full_name",
    "headline",
    "summary",
    "city",
    "state",
    "country",
    "location_raw",
    "profile_picture_url",
    "current_title",
    "current_company",
    "current_company_urn",
    "primary_email",
    "all_emails",
    "primary_phone",
    "all_phones",
    "source_channels",
    "source_artifacts",
    "total_interactions",
    "twitter_handle",
    "x_twitter_handle",
    "x_twitter_followers",
    "linkedin_followers",
    "linkedin_connections",
    "ig_followers",
    "inferred_birth_year",
    "work_experiences",
    "education",
    "hydrated_context",
]
LOCAL_INTERACTION_SUMMARY_TABLES = ("local_person_source_summary", "person_source_summary")
LOCAL_POSITION_HYDRATE_COLUMNS = [
    "id",
    "position_id",
    "person_id",
    "base_id",
    "position_title",
    "raw_title",
    "description",
    "dense_text",
    "company_name",
    "company_id",
    "company_domain",
    "company_linkedin_url",
    "company_description",
    "company_sector_types",
    "company_entity_types",
    "company_headcount",
    "company_funding_total",
    "company_stage",
    "investor_names",
    "city",
    "state",
    "country",
    "is_current",
    "start_date_epoch",
    "end_date_epoch",
    "tenure_years",
    "seniority_band",
    "role_track",
    "total_years_experience",
    "x_twitter_followers",
    "linkedin_followers",
    "linkedin_connections",
    "ig_followers",
    "inferred_birth_year",
]
LOCAL_SUMMARY_HYDRATE_COLUMNS = [
    "id",
    "person_id",
    "base_id",
    "summary",
    "tech_skills",
]
LOCAL_EDUCATION_HYDRATE_COLUMNS = [
    "id",
    "person_id",
    "base_id",
    "education_id",
    "school_name",
    "degree",
    "degree_normalized",
    "field_of_study",
    "start_year",
    "end_year",
    "graduation_year",
    "canonical_education_id",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "wt") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def append_event(state_path: Path, event: dict[str, Any]) -> None:
    event_path = state_path.with_suffix(state_path.suffix + ".events.jsonl")
    event_path.parent.mkdir(parents=True, exist_ok=True)
    with event_path.open("a") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def step_output(state: dict[str, Any], step_id: str) -> dict[str, Any]:
    for step in reversed(state.get("steps", [])):
        if step.get("id") == step_id:
            return step.get("output", {}) or {}
    return {}


def frontier_ids(state: dict[str, Any]) -> list[str]:
    llm_filter = step_output(state, "llm_filter_candidates")
    ids = llm_filter.get("passed_candidate_ids") or []
    if ids:
        return list(dict.fromkeys(str(pid) for pid in ids if pid))

    merge = step_output(state, "merge_candidate_frontier")
    ids = merge.get("frontier_candidate_ids") or []
    if ids:
        return list(dict.fromkeys(str(pid) for pid in ids if pid))

    role_search = step_output(state, "execute_role_search")
    ids = role_search.get("candidate_ids") or []
    if ids:
        return list(dict.fromkeys(str(pid) for pid in ids if pid))

    slice_search = step_output(state, "execute_search_slice")
    ids = slice_search.get("candidate_ids") or []
    if ids:
        return list(dict.fromkeys(str(pid) for pid in ids if pid))

    direct = step_output(state, "direct_execute")
    ids = direct.get("person_ids") or direct.get("candidate_ids") or []
    if ids:
        return list(dict.fromkeys(str(pid) for pid in ids if pid))

    hydrate = step_output(state, "hydrate_people")
    return list(dict.fromkeys(str(p["person_id"]) for p in hydrate.get("profiles", []) or [] if p.get("person_id")))


def candidate_metadata(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return retrieval metadata keyed by base person id for hydration handoff."""
    out: dict[str, dict[str, Any]] = {}
    for step_id in ["merge_candidate_frontier", "execute_role_search", "execute_search_slice", "direct_execute"]:
        step = step_output(state, step_id)
        for raw in step.get("candidates") or []:
            if not isinstance(raw, dict):
                continue
            person_id = str(raw.get("person_id") or raw.get("base_id") or "")
            if not person_id:
                continue
            existing = out.setdefault(person_id, {"vertical_sources": [], "matched_position_ids": []})
            if raw.get("score") is not None and existing.get("base_score") is None:
                existing["base_score"] = raw.get("score")
            for source in raw.get("vertical_sources") or []:
                if source not in existing["vertical_sources"]:
                    existing["vertical_sources"].append(source)
            for pos_id in [raw.get("position_id"), *(raw.get("matched_position_ids") or [])]:
                if pos_id and pos_id not in existing["matched_position_ids"]:
                    existing["matched_position_ids"].append(pos_id)
            for key in ["position_title", "company_id", "agentic_sql_evidence"]:
                if raw.get(key) and not existing.get(key):
                    existing[key] = raw.get(key)
    return out


def position_identifier(position: dict[str, Any]) -> str | None:
    for key in ["id", "position_id", "linkedin_position_id", "urn"]:
        if position.get(key):
            return str(position[key])
    return None


def matched_indexes(profile: dict[str, Any], meta: dict[str, Any]) -> list[int]:
    positions = profile.get("positions") or []
    ids = {str(value) for value in meta.get("matched_position_ids") or [] if value}
    indexes: list[int] = []
    for idx, pos in enumerate(positions):
        if not isinstance(pos, dict):
            continue
        pos_id = position_identifier(pos)
        if pos_id and pos_id in ids:
            indexes.append(idx)
    if indexes:
        return indexes
    title = str(meta.get("position_title") or "").strip().lower()
    company_id = str(meta.get("company_id") or "").strip().lower()
    if not title and not company_id:
        return []
    for idx, pos in enumerate(positions):
        if not isinstance(pos, dict):
            continue
        pos_title = str(pos.get("title") or pos.get("position_title") or "").strip().lower()
        pos_company = str(pos.get("company_id") or pos.get("company_urn") or pos.get("company") or "").strip().lower()
        if title and pos_title and title != pos_title:
            continue
        if company_id and pos_company and company_id != pos_company:
            continue
        indexes.append(idx)
    return indexes


def apply_candidate_metadata(profile: dict[str, Any], meta: dict[str, Any] | None) -> dict[str, Any]:
    if not meta:
        return profile
    profile = dict(profile)
    if meta.get("base_score") is not None:
        profile["base_score"] = meta.get("base_score")
        profile["score"] = meta.get("base_score")
    sources = list(profile.get("vertical_sources") or [])
    for source in meta.get("vertical_sources") or []:
        if source not in sources:
            sources.append(source)
    profile["vertical_sources"] = sources
    if meta.get("agentic_sql_evidence") and not profile.get("agentic_sql_evidence"):
        profile["agentic_sql_evidence"] = meta.get("agentic_sql_evidence")
    existing = list(profile.get("matched_position_indexes") or [])
    for idx in matched_indexes(profile, meta):
        if idx not in existing:
            existing.append(idx)
    profile["matched_position_indexes"] = existing
    return profile


def base_person_id(value: str) -> str:
    parts = str(value).split("-")
    if len(parts) == 6 and parts[5].isdigit():
        return "-".join(parts[:5])
    return str(value)


def normalize_local_value(value: Any) -> Any:
    if hasattr(value, "tolist"):
        try:
            value = value.tolist()
        except Exception:
            pass
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, tuple):
        return [normalize_local_value(item) for item in value]
    if isinstance(value, list):
        return [normalize_local_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): normalize_local_value(item) for key, item in value.items()}
    return value


def table_exists(conn: Any, table: str) -> bool:
    row = conn.execute(
        "select count(*) from information_schema.tables where table_schema in ('main', 'temp') and table_name = ?",
        [table],
    ).fetchone()
    return bool(row and row[0])


def table_columns(conn: Any, table: str) -> list[str]:
    if not table_exists(conn, table):
        return []
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def quote_ident(value: str) -> str:
    if not value.replace("_", "").isalnum() or not (value[0].isalpha() or value[0] == "_"):
        raise RuntimeError(f"unsafe DuckDB identifier: {value!r}")
    return f'"{value}"'


def prepare_requested_ids(conn: Any, person_ids: list[str]) -> None:
    conn.execute("drop table if exists temp._hydrate_requested_ids")
    conn.execute("create temporary table _hydrate_requested_ids(id varchar)")
    if person_ids:
        conn.executemany("insert into _hydrate_requested_ids values (?)", [(pid,) for pid in person_ids])


def local_rows(conn: Any, table: str, person_ids: list[str] | None = None, select_columns: list[str] | None = None) -> list[dict[str, Any]]:
    columns = table_columns(conn, table)
    if not columns:
        return []
    id_fields = [field for field in ["person_id", "base_id"] if field in columns]
    if not id_fields:
        return []
    selected_columns = [field for field in (select_columns or columns) if field in columns]
    for field in id_fields:
        if field not in selected_columns:
            selected_columns.append(field)
    if not selected_columns:
        return []
    if person_ids is not None:
        prepare_requested_ids(conn, person_ids)
    predicates = " or ".join(f"cast(t.{quote_ident(field)} as varchar) = r.id" for field in id_fields)
    selected_sql = ", ".join(f"t.{quote_ident(field)}" for field in selected_columns)
    rows = conn.execute(f"select distinct {selected_sql} from {quote_ident(table)} t join _hydrate_requested_ids r on {predicates}").fetchall()
    return [
        {selected_columns[index]: normalize_local_value(value) for index, value in enumerate(row)}
        for row in rows
    ]


def local_interaction_counts(conn: Any, person_ids: list[str]) -> dict[str, int]:
    """Aggregate local DuckDB interaction counts using the prod person_source_summary contract."""
    if not person_ids:
        return {}
    prepare_requested_ids(conn, person_ids)
    counts: dict[str, int] = {}
    for table in LOCAL_INTERACTION_SUMMARY_TABLES:
        columns = set(table_columns(conn, table))
        if {"person_id", "total_interactions"} - columns:
            continue
        rows = conn.execute(
            f"""
            select cast(t.person_id as varchar) as person_id,
                   sum(coalesce(try_cast(t.total_interactions as bigint), 0)) as total
            from {quote_ident(table)} t
            join _hydrate_requested_ids r on cast(t.person_id as varchar) = r.id
            group by 1
            """
        ).fetchall()
        for person_id, total in rows:
            # Prefer the explicit local table when both local and restored prod-shaped
            # tables coexist, but let later tables fill IDs that were absent upstream.
            counts.setdefault(str(person_id), int(total or 0))
    return counts


def epoch_date(value: Any) -> str | None:
    try:
        ts = int(value or 0)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def compact_location(row: dict[str, Any]) -> str | None:
    parts = [str(row.get(key)) for key in ["city", "state", "country"] if row.get(key)]
    return ", ".join(parts) if parts else None


def local_position(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("position_id") or row.get("id"),
        "position_id": row.get("position_id") or row.get("id"),
        "title": row.get("position_title") or row.get("raw_title"),
        "position_title": row.get("position_title") or row.get("raw_title"),
        "description": row.get("description"),
        "dense_text": row.get("dense_text") or " ".join(
            str(part)
            for part in [row.get("position_title") or row.get("raw_title"), row.get("company_name"), row.get("description")]
            if part
        ) or None,
        "company": row.get("company_name"),
        "company_name": row.get("company_name"),
        "company_id": row.get("company_id"),
        "company_domain": row.get("company_domain"),
        "company_linkedin_url": row.get("company_linkedin_url"),
        "company_description": row.get("company_description"),
        "company_sector_types": row.get("company_sector_types") or [],
        "company_entity_types": row.get("company_entity_types") or [],
        "company_headcount": row.get("company_headcount"),
        "company_funding_total": row.get("company_funding_total"),
        "company_stage": row.get("company_stage"),
        "investor_names": row.get("investor_names") or [],
        "city": row.get("city"),
        "state": row.get("state"),
        "country": row.get("country"),
        "location": compact_location(row),
        "is_current": row.get("is_current"),
        "start_date": epoch_date(row.get("start_date_epoch")),
        "end_date": epoch_date(row.get("end_date_epoch")),
        "tenure_years": row.get("tenure_years"),
        "seniority_band": row.get("seniority_band"),
        "role_track": row.get("role_track"),
    }


def local_education(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("education_id") or row.get("id"),
        "school_name": row.get("school_name"),
        "degree": row.get("degree"),
        "degree_normalized": row.get("degree_normalized"),
        "field_of_study": row.get("field_of_study"),
        "start_year": row.get("start_year") or None,
        "end_year": row.get("end_year") or None,
        "graduation_year": row.get("graduation_year") or None,
        "canonical_education_id": row.get("canonical_education_id"),
    }


def first_present(rows: list[dict[str, Any]], field: str) -> Any:
    for row in rows:
        value = row.get(field)
        if value not in (None, ""):
            return value
    return None


def _list_value(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _dict_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def build_local_person_rows(
    person_ids: list[str],
    profile_rows: list[dict[str, Any]],
    position_rows: list[dict[str, Any]],
    summary_rows: list[dict[str, Any]],
    education_rows: list[dict[str, Any]],
    interaction_counts: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    interaction_counts = interaction_counts or {}
    profiles_by_person = {
        str(row.get("person_id") or row.get("base_id") or row.get("id")): row
        for row in profile_rows
        if row.get("person_id") or row.get("base_id") or row.get("id")
    }
    positions_by_person: dict[str, list[dict[str, Any]]] = {}
    for row in position_rows:
        pid = str(row.get("person_id") or row.get("base_id") or "")
        if pid:
            positions_by_person.setdefault(pid, []).append(row)
    for rows in positions_by_person.values():
        rows.sort(key=lambda row: (not bool(row.get("is_current")), -(int(row.get("start_date_epoch") or 0))))

    summaries_by_person = {
        str(row.get("person_id") or row.get("base_id")): row
        for row in summary_rows
        if row.get("person_id") or row.get("base_id")
    }
    education_by_person: dict[str, list[dict[str, Any]]] = {}
    for row in education_rows:
        pid = str(row.get("person_id") or row.get("base_id") or "")
        if pid:
            education_by_person.setdefault(pid, []).append(row)

    rows: list[dict[str, Any]] = []
    for pid in person_ids:
        profile = profiles_by_person.get(pid, {})
        position_source = positions_by_person.get(pid, [])
        summary = summaries_by_person.get(pid, {})
        education_source = education_by_person.get(pid, [])
        profile_context = _dict_value(profile.get("hydrated_context"))
        if not position_source and not summary and not education_source and not profile:
            continue

        positions = [local_position(row) for row in position_source]
        if not positions:
            positions = _list_value(profile.get("work_experiences")) or _list_value(profile_context.get("positions"))
        education = [local_education(row) for row in education_source]
        if not education:
            education = _list_value(profile.get("education")) or _list_value(profile_context.get("education"))
        current = next((row for row in position_source if row.get("is_current")), position_source[0] if position_source else {})
        title = profile.get("current_title") or current.get("position_title") or current.get("raw_title")
        company = profile.get("current_company") or current.get("company_name")
        headline = profile.get("headline") or " at ".join(str(part) for part in [title, company] if part) or title
        location = profile.get("location_raw") or profile_context.get("location") or (compact_location(current) if current else None)
        years = first_present(position_source, "total_years_experience") or profile_context.get("years_of_experience")
        total_interactions = interaction_counts.get(pid)
        if total_interactions is None:
            total_interactions = profile.get("total_interactions")
        if total_interactions is None:
            total_interactions = profile_context.get("total_interactions")

        context = {
            "person_id": pid,
            "name": profile.get("full_name") or profile_context.get("name") or "",
            "headline": headline,
            "summary": profile.get("summary") or summary.get("summary") or profile_context.get("summary"),
            "location": location,
            "linkedin_url": profile.get("linkedin_url") or profile.get("public_profile_url") or profile_context.get("linkedin_url"),
            "profile_picture_url": profile.get("profile_picture_url") or profile_context.get("profile_picture_url"),
            "positions": positions,
            "education": education,
            "tech_skills": summary.get("tech_skills") or profile_context.get("tech_skills") or [],
            "years_of_experience": years,
            "total_interactions": total_interactions,
        }
        rows.append({
            "id": pid,
            "full_name": profile.get("full_name") or profile_context.get("name") or "",
            "headline": headline,
            "summary": profile.get("summary") or summary.get("summary") or profile_context.get("summary"),
            "location_raw": location,
            "city": profile.get("city") or current.get("city") if current else profile.get("city"),
            "state": profile.get("state") or current.get("state") if current else profile.get("state"),
            "country": profile.get("country") or current.get("country") if current else profile.get("country"),
            "public_profile_url": profile.get("public_profile_url") or profile.get("linkedin_url") or profile_context.get("linkedin_url"),
            "profile_picture_url": profile.get("profile_picture_url") or profile_context.get("profile_picture_url"),
            "hydrated_context": context,
            "x_twitter_handle": profile.get("x_twitter_handle") or profile.get("twitter_handle"),
            "x_twitter_followers": profile.get("x_twitter_followers") or first_present(position_source, "x_twitter_followers"),
            "linkedin_followers": profile.get("linkedin_followers") or first_present(position_source, "linkedin_followers"),
            "linkedin_connections": profile.get("linkedin_connections") or first_present(position_source, "linkedin_connections"),
            "ig_followers": profile.get("ig_followers") or first_present(position_source, "ig_followers"),
            "inferred_birth_year": profile.get("inferred_birth_year") or first_present(position_source, "inferred_birth_year"),
            "total_interactions": total_interactions,
        })
    return rows


def fetch_local_person_batch(db_path: str, person_ids: list[str]) -> list[dict[str, Any]]:
    import duckdb  # type: ignore

    with duckdb.connect(str(db_path), read_only=True) as conn:
        prepare_requested_ids(conn, person_ids)
        profile_table = "local_person_profiles" if table_exists(conn, "local_person_profiles") else "local_people_profiles" if table_exists(conn, "local_people_profiles") else ""
        profile_rows = local_rows(conn, profile_table, select_columns=LOCAL_PROFILE_HYDRATE_COLUMNS) if profile_table else []
        position_rows = local_rows(conn, "local_people_positions", select_columns=LOCAL_POSITION_HYDRATE_COLUMNS)
        summary_rows = local_rows(conn, "local_summaries", select_columns=LOCAL_SUMMARY_HYDRATE_COLUMNS)
        education_rows = local_rows(conn, "local_people_education", select_columns=LOCAL_EDUCATION_HYDRATE_COLUMNS)
        interaction_counts = local_interaction_counts(conn, person_ids)
    return build_local_person_rows(person_ids, profile_rows, position_rows, summary_rows, education_rows, interaction_counts)


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), max(1, size))]


def positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def fetch_local_person_rows(
    person_ids: list[str],
    env_file: Path | None = None,
    *,
    db_path: str | None = None,
    workers: int | None = None,
    batch_size: int | None = None,
) -> list[dict[str, Any]] | None:
    load_env_file(env_file)
    if db_path is None and os.getenv("POWERPACKS_ENABLE_LEGACY_LOCAL_SEARCH_ENV") == "1":
        db_path = os.getenv("POWERPACKS_LOCAL_SEARCH_DB")
    if not db_path:
        return None
    if not person_ids:
        return []
    try:
        import duckdb  # noqa: F401  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("duckdb is required for local DuckDB search") from exc

    effective_batch_size = batch_size or positive_int(os.getenv("POWERPACKS_LOCAL_HYDRATE_BATCH_SIZE"), DEFAULT_LOCAL_BATCH_SIZE)
    requested_workers = workers or positive_int(os.getenv("POWERPACKS_LOCAL_HYDRATE_WORKERS"), DEFAULT_LOCAL_WORKERS)
    batches = chunked(person_ids, effective_batch_size)
    effective_workers = min(max(1, requested_workers), len(batches))
    if effective_workers <= 1:
        rows: list[dict[str, Any]] = []
        for batch in batches:
            rows.extend(fetch_local_person_batch(db_path, batch))
        return rows

    rows = []
    with ThreadPoolExecutor(max_workers=effective_workers) as pool:
        for batch_rows in pool.map(lambda batch: fetch_local_person_batch(db_path, batch), batches):
            rows.extend(batch_rows)
    return rows


def artifact_dir(state_path: Path, state: dict[str, Any]) -> Path:
    existing = state.get("artifacts") or {}
    if existing.get("artifact_dir"):
        return Path(str(existing["artifact_dir"]))
    return state_path.parent / "artifacts" / str(state.get("task_id") or state_path.stem)


def llm_profile_view(profile: dict[str, Any]) -> dict[str, Any]:
    """Compact view for LLM filter/rerank handoff."""
    positions = profile.get("positions") or []
    matched = set(profile.get("matched_position_indexes") or [])
    selected = []
    for idx, pos in enumerate(positions):
        if not isinstance(pos, dict):
            continue
        if pos.get("is_current") or idx in matched:
            selected.append(pos)
    if not selected and positions:
        selected = [positions[0]]
    return {
        "person_id": profile.get("person_id"),
        "name": profile.get("name"),
        "headline": profile.get("headline"),
        "location": profile.get("location"),
        "linkedin_url": profile.get("linkedin_url"),
        "positions": selected,
        "education": (profile.get("education") or [])[:3],
        "tech_skills": profile.get("tech_skills"),
        "total_interactions": profile.get("total_interactions"),
        "matched_position_indexes": profile.get("matched_position_indexes") or [],
        "base_score": profile.get("base_score"),
        "score": profile.get("score"),
        "tags": profile.get("tags"),
        "vertical_sources": profile.get("vertical_sources"),
        "agentic_sql_evidence": profile.get("agentic_sql_evidence"),
    }


def record_step(state_path: Path, state: dict[str, Any], output: dict[str, Any], elapsed_ms: int) -> None:
    now = now_iso()
    state.setdefault("steps", []).append({
        "id": "hydrate_people",
        "status": "completed",
        "recorded_at": now,
        "elapsed_ms": elapsed_ms,
        "output": output,
    })
    state["updated_at"] = now
    write_json(state_path, state)
    append_event(state_path, {
        "event": "record_step",
        "task_id": state.get("task_id"),
        "state": str(state_path),
        "step_id": "hydrate_people",
        "status": "completed",
        "timestamp": now,
        "elapsed_ms": elapsed_ms,
        "requested": output.get("requested"),
        "hydrated": output.get("hydrated"),
    })


def cmd_hydrate(args: argparse.Namespace) -> None:
    started = time.time()
    state_path = Path(args.state)
    state = read_json(state_path)
    requested = list(dict.fromkeys(base_person_id(pid) for pid in frontier_ids(state)))
    if args.limit:
        requested = requested[: args.limit]

    if args.dry_run:
        print(json.dumps({
            "state": str(state_path),
            "env_file": str(Path(args.env_file)) if args.env_file else None,
            "requested": len(requested),
            "sample_ids": requested[:10],
            "would_write_state": args.write_state,
        }, indent=2, sort_keys=True))
        return

    env_file = Path(args.env_file) if args.env_file else None
    rows = fetch_local_person_rows(requested, env_file=env_file, db_path=args.local_db, workers=args.local_workers, batch_size=args.local_batch_size)
    if rows is None:
        rows = fetch_person_rows(requested, env_file=env_file)
        interaction_counts = fetch_interaction_counts(requested, env_file=env_file)
        source_attribution = fetch_source_attribution(requested, env_file=env_file)
        source = {
            "type": "postgres_contract",
            "backend": "postgres_supabase",
            "env_file": str(env_file) if env_file else None,
        }
    else:
        interaction_counts = {}
        source_attribution = {}
        source = {
            "type": "local_duckdb",
            "backend": "duckdb",
            "duckdb": args.local_db,
            "batch_size": args.local_batch_size or positive_int(os.getenv("POWERPACKS_LOCAL_HYDRATE_BATCH_SIZE"), DEFAULT_LOCAL_BATCH_SIZE),
            "workers": args.local_workers or positive_int(os.getenv("POWERPACKS_LOCAL_HYDRATE_WORKERS"), DEFAULT_LOCAL_WORKERS),
        }
    metadata = candidate_metadata(state)
    profiles = []
    for row in rows:
        if interaction_counts.get(str(row.get("id"))):
            row["total_interactions"] = interaction_counts[str(row.get("id"))]
        profile = normalize_hydrated_context(row)
        attribution = source_attribution.get(str(profile.get("person_id")))
        if attribution:
            profile["source_operators"] = attribution.get("operators", [])
            profile["source_channels"] = attribution.get("channels", [])
            profile["primary_source_operator"] = attribution.get("primary_operator")
            profile["primary_source_channel"] = attribution.get("primary_channel")
        profiles.append(apply_candidate_metadata(profile, metadata.get(str(profile.get("person_id")))))
    order = {pid: idx for idx, pid in enumerate(requested)}
    profiles.sort(key=lambda profile: order.get(str(profile.get("person_id")), len(order)))

    out_dir = artifact_dir(state_path, state) / "hydrate_people"
    profiles_jsonl = out_dir / ("profiles.jsonl" if args.no_compress_profiles else "profiles.jsonl.gz")
    llm_profiles_jsonl = out_dir / "llm_profiles.jsonl"
    write_jsonl(profiles_jsonl, profiles)
    write_jsonl(llm_profiles_jsonl, [llm_profile_view(profile) for profile in profiles])

    artifacts: dict[str, Any] = {}
    if args.dump_profiles:
        profiles_json = out_dir / "profiles.json"
        write_json(profiles_json, {"profiles": profiles})
        artifacts = {"profiles_json": str(profiles_json)}

    output = {
        "requested": len(requested),
        "hydrated": len(profiles),
        "profile_ids": [profile.get("person_id") for profile in profiles if profile.get("person_id")],
        "profiles_path": str(profiles_jsonl),
        "llm_profiles_path": str(llm_profiles_jsonl),
        "profiles_compressed": not args.no_compress_profiles,
        "artifacts": artifacts,
        "source": source,
    }
    elapsed_ms = int((time.time() - started) * 1000)
    if args.write_state:
        record_step(state_path, state, output, elapsed_ms)
    print(json.dumps(output, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Hydrate Powerpacks candidate IDs through the Postgres/Supabase contract")
    parser.add_argument("--state", required=True)
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--write-state", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dump-profiles", action="store_true", help="Write full hydration inspection artifacts for debugging")
    parser.add_argument("--no-compress-profiles", action="store_true", help="Write raw profiles.jsonl instead of the default profiles.jsonl.gz")
    parser.add_argument("--local-workers", type=int, help="Read-only DuckDB hydration worker count for local backend")
    parser.add_argument("--local-batch-size", type=int, help="Candidate IDs per read-only DuckDB hydration batch")
    parser.add_argument("--local-db", help=argparse.SUPPRESS)
    args = parser.parse_args()
    cmd_hydrate(args)


if __name__ == "__main__":
    main()
