#!/usr/bin/env python3
"""Hydrate a Powerpacks frontier from the checked-in Postgres contract."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID


LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB_DIR))

from postgres_client import fetch_interaction_counts, fetch_person_rows, load_env_file  # noqa: E402
from powerpacks_contracts import normalize_hydrated_context  # noqa: E402


DEFAULT_ENV_FILE = Path(os.getenv("POWERPACKS_ENV_FILE", ".env"))


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
            for key in ["position_title", "company_id"]:
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


def local_rows(conn: Any, table: str, person_ids: list[str]) -> list[dict[str, Any]]:
    columns = table_columns(conn, table)
    if not columns:
        return []
    id_fields = [field for field in ["person_id", "base_id"] if field in columns]
    if not id_fields:
        return []
    conn.execute("drop table if exists temp._hydrate_requested_ids")
    conn.execute("create temporary table _hydrate_requested_ids(id varchar)")
    conn.executemany("insert into _hydrate_requested_ids values (?)", [(pid,) for pid in person_ids])
    predicates = " or ".join(f"cast(t.{field} as varchar) = r.id" for field in id_fields)
    rows = conn.execute(f"select distinct t.* from {table} t join _hydrate_requested_ids r on {predicates}").fetchall()
    return [
        {columns[index]: normalize_local_value(value) for index, value in enumerate(row)}
        for row in rows
    ]


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
        "company": row.get("company_name"),
        "company_name": row.get("company_name"),
        "company_id": row.get("company_id"),
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


def fetch_local_person_rows(person_ids: list[str], env_file: Path | None = None) -> list[dict[str, Any]] | None:
    load_env_file(env_file)
    db_path = os.getenv("POWERPACKS_LOCAL_SEARCH_DB")
    if not db_path:
        return None
    try:
        import duckdb  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("duckdb is required for local DuckDB search") from exc

    with duckdb.connect(str(db_path), read_only=True) as conn:
        position_rows = local_rows(conn, "local_people_positions", person_ids)
        summary_rows = local_rows(conn, "local_summaries", person_ids)
        education_rows = local_rows(conn, "local_people_education", person_ids)

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
        position_source = positions_by_person.get(pid, [])
        summary = summaries_by_person.get(pid, {})
        education_source = education_by_person.get(pid, [])
        if not position_source and not summary and not education_source:
            continue

        positions = [local_position(row) for row in position_source]
        education = [local_education(row) for row in education_source]
        current = next((row for row in position_source if row.get("is_current")), position_source[0] if position_source else {})
        title = current.get("position_title") or current.get("raw_title")
        company = current.get("company_name")
        headline = " at ".join(str(part) for part in [title, company] if part) or title
        location = compact_location(current) if current else None
        years = first_present(position_source, "total_years_experience")

        context = {
            "person_id": pid,
            "name": "",
            "headline": headline,
            "location": location,
            "positions": positions,
            "education": education,
            "tech_skills": summary.get("tech_skills") or [],
            "years_of_experience": years,
        }
        rows.append({
            "id": pid,
            "full_name": "",
            "headline": headline,
            "summary": summary.get("summary"),
            "location_raw": location,
            "city": current.get("city") if current else None,
            "state": current.get("state") if current else None,
            "country": current.get("country") if current else None,
            "hydrated_context": context,
            "x_twitter_followers": first_present(position_source, "x_twitter_followers"),
            "linkedin_followers": first_present(position_source, "linkedin_followers"),
            "linkedin_connections": first_present(position_source, "linkedin_connections"),
            "ig_followers": first_present(position_source, "ig_followers"),
            "inferred_birth_year": first_present(position_source, "inferred_birth_year"),
        })
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
    rows = fetch_local_person_rows(requested, env_file=env_file)
    if rows is None:
        rows = fetch_person_rows(requested, env_file=env_file)
        interaction_counts = fetch_interaction_counts(requested, env_file=env_file)
        source = {
            "type": "postgres_contract",
            "backend": "postgres_supabase",
            "env_file": str(env_file) if env_file else None,
        }
    else:
        interaction_counts = {}
        source = {
            "type": "local_duckdb",
            "backend": "duckdb",
            "duckdb": os.getenv("POWERPACKS_LOCAL_SEARCH_DB"),
        }
    metadata = candidate_metadata(state)
    profiles = []
    for row in rows:
        if interaction_counts.get(str(row.get("id"))):
            row["total_interactions"] = interaction_counts[str(row.get("id"))]
        profile = normalize_hydrated_context(row)
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
    args = parser.parse_args()
    cmd_hydrate(args)


if __name__ == "__main__":
    main()
