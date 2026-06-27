"""Local profile hydration helpers for Powerpacks DuckDB/profile artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load_json_profile(row: dict[str, Any]) -> dict[str, Any]:
    profile = row.get("profile_json")
    if isinstance(profile, str) and profile.strip():
        parsed = json.loads(profile)
        if isinstance(parsed, dict):
            return parsed
    return dict(row)


def _profile_id(profile: dict[str, Any]) -> str:
    return str(profile.get("person_id") or profile.get("base_id") or profile.get("id") or "")


def load_profiles_from_jsonl(path: str | Path, requested: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    wanted = set(str(pid) for pid in requested)
    by_id: dict[str, dict[str, Any]] = {}
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            pid = _profile_id(row)
            if pid in wanted:
                by_id[pid] = row
    return [by_id[pid] for pid in requested if pid in by_id], {
        "type": "local_profiles",
        "backend": "jsonl",
        "profiles_path": str(path),
        "interaction_counts": _interaction_status(by_id.values()),
    }


def load_profiles_from_duckdb(db_path: str | Path, requested: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        import duckdb  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError("duckdb is required for local profile hydration") from exc

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        table_exists = con.execute(
            "select count(*) from information_schema.tables where table_schema in ('main', 'temp') and table_name = 'local_profiles'"
        ).fetchone()[0]
        if not table_exists:
            raise RuntimeError("local hydration requires missing table 'local_profiles'")
        by_id: dict[str, dict[str, Any]] = {}
        for pid in requested:
            row = con.execute(
                "select * from local_profiles where person_id = ? or base_id = ? limit 1",
                [str(pid), str(pid)],
            ).fetchone()
            if not row:
                continue
            cols = [desc[0] for desc in con.description or []]
            profile = _load_json_profile(dict(zip(cols, row)))
            by_id[str(pid)] = profile
        return [by_id[pid] for pid in requested if pid in by_id], {
            "type": "local_profiles",
            "backend": "duckdb",
            "db_path": str(db_path),
            "profiles_table": "local_profiles",
            "interaction_counts": _interaction_status(by_id.values()),
        }
    finally:
        con.close()


def _interaction_status(profiles: Any) -> str:
    values = [profile.get("total_interactions") for profile in profiles if isinstance(profile, dict)]
    return "available" if any(value is not None for value in values) else "unavailable"
