#!/usr/bin/env python3
"""Validate deterministic content parity for a local Powerpacks index run."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from packs.indexing.lib.duckdb_artifacts import (  # noqa: E402
    LOCAL_TABLES,
    _float,
    _int,
    _list,
    _str,
    _tokens,
    _vector,
    table_checksum,
)
from packs.indexing.lib.fingerprints import sha256_file, sha256_json  # noqa: E402
from packs.indexing.lib.io import iter_jsonl  # noqa: E402


def _csv_rows(path: Path) -> int:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        return sum(1 for _ in reader) if header is not None else 0


def _jsonl_checksum(path: Path, key: str = "id") -> str | None:
    if not path.exists():
        return None
    rows = list(iter_jsonl(path))
    rows.sort(key=lambda row: str(row.get(key) or row.get("person_id") or row.get("base_id") or row))
    return sha256_json(rows)


def _jsonl_rows(path: Path, key: str = "id") -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = list(iter_jsonl(path))
    rows.sort(key=lambda row: str(row.get(key) or row.get("person_id") or row.get("base_id") or row))
    return rows


TABLE_COLUMNS: dict[str, list[str]] = {
    "local_people_positions": [
        "id",
        "position_id",
        "person_id",
        "base_id",
        "position_title",
        "company_id",
        "company_name",
        "city",
        "state",
        "country",
        "macro_region",
        "metro_areas",
        "role_track",
        "seniority_band",
        "role_ids",
        "is_current",
        "total_years_experience",
        "allowed_operator_ids",
        "start_date_epoch",
        "end_date_epoch",
        "inferred_birth_year",
        "x_twitter_followers",
        "linkedin_followers",
        "linkedin_connections",
        "ig_followers",
        "phrase_tokens",
        "word_tokens",
        "char_tokens",
        "d2q_tokens",
        "vector",
    ],
    "local_summaries": [
        "id",
        "person_id",
        "base_id",
        "summary",
        "tech_skills",
        "allowed_operator_ids",
        "phrase_tokens",
        "word_tokens",
        "vector",
    ],
    "local_people_education": [
        "id",
        "person_id",
        "base_id",
        "canonical_education_id",
        "school_name",
        "degree",
        "degree_normalized",
        "field_of_study",
        "start_year",
        "end_year",
        "graduation_year",
        "allowed_operator_ids",
    ],
    "local_education": [
        "id",
        "canonical_education_id",
        "school_name_tokens",
        "school_name",
        "display_value",
        "person_count",
    ],
    "local_profiles": ["person_id", "base_id", "name", "profile_json", "total_interactions"],
}


def _normalize_content(value: Any) -> Any:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, tuple):
        return [_normalize_content(item) for item in value]
    if isinstance(value, list):
        return [_normalize_content(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _normalize_content(item) for key, item in sorted(value.items())}
    if isinstance(value, float):
        return round(value, 12)
    return value


def _row(columns: list[str], values: list[Any] | tuple[Any, ...]) -> dict[str, Any]:
    return {column: _normalize_content(value) for column, value in zip(columns, values)}


def _rows_by_id(path: Path, *keys: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in iter_jsonl(path):
        for key in keys:
            value = row.get(key)
            if value:
                out[str(value)] = row
    return out


def _table_checksum(rows: list[dict[str, Any]], key: str = "id") -> str:
    return sha256_json(sorted(rows, key=lambda row: str(row.get(key) or row.get("person_id") or row)))


def expected_content_checksums(run_dir: Path) -> dict[str, str]:
    people = [
        _row(
            TABLE_COLUMNS["local_people_positions"],
            [
                _str(row.get("id")),
                _str(row.get("position_id") or row.get("id")),
                _str(row.get("person_id") or row.get("base_id")),
                _str(row.get("base_id") or row.get("person_id")),
                _str(row.get("position_title")),
                _str(row.get("company_id")),
                _str(row.get("company_name")),
                _str(row.get("city")),
                _str(row.get("state")),
                _str(row.get("country")),
                _str(row.get("macro_region")),
                [str(v) for v in _list(row.get("metro_areas"))],
                _str(row.get("role_track")),
                _str(row.get("seniority_band")),
                [str(v) for v in _list(row.get("role_ids"))],
                bool(row.get("is_current")),
                _float(row.get("total_years_experience")) or 0.0,
                [str(v) for v in _list(row.get("allowed_operator_ids"))],
                _int(row.get("start_date_epoch")) or 0,
                _int(row.get("end_date_epoch")) or 0,
                _int(row.get("inferred_birth_year")) or 0,
                _int(row.get("x_twitter_followers")) or 0,
                _int(row.get("linkedin_followers")) or 0,
                _int(row.get("linkedin_connections")) or 0,
                _int(row.get("ig_followers")) or 0,
                [str(v) for v in _list(row.get("phrase_tokens"))],
                [str(v) for v in _list(row.get("word_tokens"))],
                [str(v) for v in _list(row.get("char_tokens"))],
                [str(v) for v in _list(row.get("d2q_tokens"))],
                _vector(row),
            ],
        )
        for row in _jsonl_rows(run_dir / "records/people.records.jsonl")
    ]
    summaries_text = _rows_by_id(run_dir / "summaries/summary_records.jsonl", "person_id", "base_id", "id")
    summaries = []
    for row in _jsonl_rows(run_dir / "records/summaries.records.jsonl"):
        pid = _str(row.get("person_id") or row.get("base_id") or row.get("id"))
        text_row = summaries_text.get(pid) or summaries_text.get(_str(row.get("id"))) or {}
        text = _str(text_row.get("text") or row.get("summary") or "")
        summaries.append(
            _row(
                TABLE_COLUMNS["local_summaries"],
                [
                    _str(row.get("id") or pid),
                    pid,
                    pid,
                    text,
                    [str(v) for v in _list(row.get("tech_skills"))],
                    [str(v) for v in _list(row.get("allowed_operator_ids"))],
                    _tokens(text),
                    _tokens(text) + [str(v).lower() for v in _list(row.get("tech_skills"))],
                    _vector(row),
                ],
            )
        )
    education = [
        _row(
            TABLE_COLUMNS["local_people_education"],
            [
                _str(row.get("id")),
                _str(row.get("person_id") or row.get("base_id")),
                _str(row.get("base_id") or row.get("person_id")),
                _str(row.get("canonical_education_id")),
                _str(row.get("school_name")),
                _str(row.get("degree")),
                _str(row.get("degree_normalized")),
                _str(row.get("field_of_study")),
                _int(row.get("start_year")),
                _int(row.get("end_year")),
                _int(row.get("graduation_year")),
                [str(v) for v in _list(row.get("allowed_operator_ids"))],
            ],
        )
        for row in _jsonl_rows(run_dir / "records/education.records.jsonl")
    ]
    schools = [
        _row(
            TABLE_COLUMNS["local_education"],
            [
                _str(row.get("id") or row.get("canonical_education_id")),
                _str(row.get("canonical_education_id") or row.get("id")),
                _tokens(row.get("school_name")),
                _str(row.get("school_name")),
                _str(row.get("display_value") or row.get("school_name")),
                _int(row.get("person_count")) or 0,
            ],
        )
        for row in _jsonl_rows(run_dir / "records/schools.records.jsonl")
    ]
    profiles = [
        _row(
            TABLE_COLUMNS["local_profiles"],
            [
                _str(row.get("person_id") or row.get("base_id") or row.get("id")),
                _str(row.get("base_id") or row.get("person_id") or row.get("id")),
                _str(row.get("name")),
                json.dumps(row, ensure_ascii=False, sort_keys=True),
                _int(row.get("total_interactions")),
            ],
        )
        for row in _jsonl_rows(run_dir / "profiles/hydrated_profiles.jsonl", "person_id")
    ]
    return {
        "local_people_positions": _table_checksum(people),
        "local_summaries": _table_checksum(summaries),
        "local_people_education": _table_checksum(education),
        "local_education": _table_checksum(schools),
        "local_profiles": _table_checksum(profiles, "person_id"),
    }


def actual_content_checksums(con: Any, present_tables: set[str] | None = None) -> dict[str, str | None]:
    present = present_tables if present_tables is not None else set(LOCAL_TABLES)
    out: dict[str, str | None] = {table: None for table in LOCAL_TABLES if table not in present}
    for table in sorted(present):
        columns = TABLE_COLUMNS[table]
        column_sql = ", ".join(columns)
        rows = [_row(columns, row) for row in con.execute(f"select {column_sql} from {table}").fetchall()]
        out[table] = _table_checksum(rows, "person_id" if table == "local_profiles" else "id")
    return out


def validate_parity(people_csv: str | Path, run_dir: str | Path, db_path: str | Path) -> dict[str, Any]:
    try:
        import duckdb  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError("duckdb is required for index parity validation") from exc

    people = Path(people_csv)
    rd = Path(run_dir)
    db = Path(db_path)
    con = duckdb.connect(str(db), read_only=True)
    actual_checksums: dict[str, str | None] = {}
    present_tables: set[str] = set()
    try:
        tables: dict[str, Any] = {}
        for table in LOCAL_TABLES:
            exists = con.execute(
                "select count(*) from information_schema.tables where table_schema in ('main', 'temp') and table_name = ?",
                [table],
            ).fetchone()[0]
            if exists:
                tables[table] = {
                    "rows": int(con.execute(f"select count(*) from {table}").fetchone()[0]),
                    "checksum": table_checksum(con, table),
                }
                present_tables.add(table)
            else:
                tables[table] = {"rows": 0, "checksum": None, "missing": True}
        actual_checksums = actual_content_checksums(con, present_tables)
    finally:
        con.close()

    records = {}
    for path in sorted((rd / "records").glob("*.jsonl")):
        records[path.name] = {"rows": sum(1 for _ in iter_jsonl(path)), "checksum": _jsonl_checksum(path)}

    profiles_path = rd / "profiles/hydrated_profiles.jsonl"
    profiles = {"rows": sum(1 for _ in iter_jsonl(profiles_path)) if profiles_path.exists() else 0, "checksum": _jsonl_checksum(profiles_path, "person_id")}
    vector_source = any("vector" in row or "embedding" in row for path in (rd / "records").glob("*.jsonl") for row in iter_jsonl(path))
    errors: list[str] = []
    expected_pairs = [
        ("people.records.jsonl", "local_people_positions"),
        ("summaries.records.jsonl", "local_summaries"),
        ("education.records.jsonl", "local_people_education"),
        ("schools.records.jsonl", "local_education"),
    ]
    for table_name, table_report in tables.items():
        if table_report.get("missing"):
            errors.append(f"missing DuckDB table: {table_name}")
    for record_name, table_name in expected_pairs:
        record_rows = int(records.get(record_name, {}).get("rows") or 0)
        table_rows = int(tables.get(table_name, {}).get("rows") or 0)
        if record_rows != table_rows:
            errors.append(f"row count mismatch: {record_name}={record_rows} {table_name}={table_rows}")
    if profiles["rows"] != int(tables.get("local_profiles", {}).get("rows") or 0):
        errors.append(f"row count mismatch: profiles={profiles['rows']} local_profiles={tables.get('local_profiles', {}).get('rows')}")
    expected_checksums = expected_content_checksums(rd)
    for table_name, expected_checksum in expected_checksums.items():
        actual_checksum = actual_checksums.get(table_name)
        if expected_checksum != actual_checksum:
            errors.append(f"content checksum mismatch: {table_name}")
    ok = not errors
    return {
        "status": "completed" if ok else "failed",
        "ok": ok,
        "errors": errors,
        "people_csv": {"path": str(people), "rows": _csv_rows(people), "sha256": sha256_file(people)},
        "profiles": profiles,
        "records": records,
        "tables": tables,
        "content_parity": {"expected": expected_checksums, "actual": actual_checksums},
        "vector_parity": {"status": "not_applicable" if not vector_source else "present", "source": "records" if vector_source else "people_csv_only"},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", nargs="?")
    parser.add_argument("--people-csv", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    report = validate_parity(args.people_csv, args.run_dir, args.db)
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
