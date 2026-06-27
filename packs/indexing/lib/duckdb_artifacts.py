"""Build and validate local DuckDB artifacts from indexing pipeline outputs."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

from packs.indexing.lib.fingerprints import sha256_json
from packs.indexing.lib.io import iter_jsonl, write_json
from packs.indexing.lib.manifest import duckdb_checksum_file

TOKEN_RE = re.compile(r"[a-z0-9]+")
LOCAL_TABLES = ["local_people_positions", "local_summaries", "local_people_education", "local_education", "local_profiles"]


def _tokens(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return TOKEN_RE.findall(value.lower())
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, dict)):
        out: list[str] = []
        for item in value:
            out.extend(_tokens(item))
        return list(dict.fromkeys(out))
    return TOKEN_RE.findall(str(value).lower())


def _list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else [value]
        except Exception:
            return [value]
    return [value]


def _str(value: Any) -> str:
    return "" if value is None else str(value)


def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _vector(row: dict[str, Any]) -> list[float]:
    for key in ("vector", "embedding"):
        if key in row and row[key] not in (None, ""):
            values: list[float] = []
            for item in _list(row[key]):
                try:
                    values.append(float(item))
                except (TypeError, ValueError):
                    return []
            return values
    return []


def _read_by_id(path: Path, *keys: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in iter_jsonl(path):
        for key in keys:
            value = row.get(key)
            if value:
                out[str(value)] = row
    return out


def _table_count(con: Any, table: str) -> int:
    return int(con.execute(f"select count(*) from {table}").fetchone()[0])


def build_local_duckdb(run_dir: str | Path, db_path: str | Path | None = None) -> dict[str, Any]:
    """Build local-search.duckdb from a completed search-index run directory."""

    try:
        import duckdb  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency availability
        raise RuntimeError("duckdb is required to build local search index artifacts") from exc

    rd = Path(run_dir)
    db = Path(db_path) if db_path else rd / "local-search.duckdb"
    tmp = db.with_name(f".{db.name}.tmp")
    if tmp.exists():
        tmp.unlink()
    db.parent.mkdir(parents=True, exist_ok=True)

    people_records = list(iter_jsonl(rd / "records/people.records.jsonl"))
    summaries_records = list(iter_jsonl(rd / "records/summaries.records.jsonl"))
    summary_text = _read_by_id(rd / "summaries/summary_records.jsonl", "person_id", "base_id", "id")
    education_records = list(iter_jsonl(rd / "records/education.records.jsonl"))
    schools_records = list(iter_jsonl(rd / "records/schools.records.jsonl"))
    profiles = list(iter_jsonl(rd / "profiles/hydrated_profiles.jsonl"))

    con = duckdb.connect(str(tmp))
    try:
        con.execute("create table local_people_positions (id varchar, position_id varchar, person_id varchar, base_id varchar, position_title varchar, company_id varchar, company_name varchar, city varchar, state varchar, country varchar, macro_region varchar, metro_areas varchar[], role_track varchar, seniority_band varchar, role_ids varchar[], is_current boolean, total_years_experience double, allowed_operator_ids varchar[], start_date_epoch bigint, end_date_epoch bigint, inferred_birth_year integer, x_twitter_followers integer, linkedin_followers integer, linkedin_connections integer, ig_followers integer, phrase_tokens varchar[], word_tokens varchar[], char_tokens varchar[], d2q_tokens varchar[], vector double[])")
        people_rows = [
            (
                    _str(row.get("id")), _str(row.get("position_id") or row.get("id")), _str(row.get("person_id") or row.get("base_id")), _str(row.get("base_id") or row.get("person_id")),
                    _str(row.get("position_title")), _str(row.get("company_id")), _str(row.get("company_name")), _str(row.get("city")), _str(row.get("state")), _str(row.get("country")), _str(row.get("macro_region")),
                    [str(v) for v in _list(row.get("metro_areas"))], _str(row.get("role_track")), _str(row.get("seniority_band")), [str(v) for v in _list(row.get("role_ids"))], bool(row.get("is_current")),
                    _float(row.get("total_years_experience")) or 0.0, [str(v) for v in _list(row.get("allowed_operator_ids"))], _int(row.get("start_date_epoch")) or 0, _int(row.get("end_date_epoch")) or 0,
                    _int(row.get("inferred_birth_year")) or 0, _int(row.get("x_twitter_followers")) or 0, _int(row.get("linkedin_followers")) or 0, _int(row.get("linkedin_connections")) or 0, _int(row.get("ig_followers")) or 0,
                    [str(v) for v in _list(row.get("phrase_tokens"))], [str(v) for v in _list(row.get("word_tokens"))], [str(v) for v in _list(row.get("char_tokens"))], [str(v) for v in _list(row.get("d2q_tokens"))], _vector(row),
            )
            for row in people_records
        ]
        if people_rows:
            con.executemany("insert into local_people_positions values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", people_rows)

        con.execute("create table local_summaries (id varchar, person_id varchar, base_id varchar, summary varchar, tech_skills varchar[], allowed_operator_ids varchar[], phrase_tokens varchar[], word_tokens varchar[], vector double[])")
        summary_rows = []
        for row in summaries_records:
            pid = _str(row.get("person_id") or row.get("base_id") or row.get("id"))
            text_row = summary_text.get(pid) or summary_text.get(_str(row.get("id"))) or {}
            text = _str(text_row.get("text") or row.get("summary") or "")
            summary_rows.append((_str(row.get("id") or pid), pid, pid, text, [str(v) for v in _list(row.get("tech_skills"))], [str(v) for v in _list(row.get("allowed_operator_ids"))], _tokens(text), _tokens(text) + [str(v).lower() for v in _list(row.get("tech_skills"))], _vector(row)))
        if summary_rows:
            con.executemany("insert into local_summaries values (?, ?, ?, ?, ?, ?, ?, ?, ?)", summary_rows)

        con.execute("create table local_people_education (id varchar, person_id varchar, base_id varchar, canonical_education_id varchar, school_name varchar, degree varchar, degree_normalized varchar, field_of_study varchar, start_year integer, end_year integer, graduation_year integer, allowed_operator_ids varchar[])")
        education_rows = [(_str(r.get("id")), _str(r.get("person_id") or r.get("base_id")), _str(r.get("base_id") or r.get("person_id")), _str(r.get("canonical_education_id")), _str(r.get("school_name")), _str(r.get("degree")), _str(r.get("degree_normalized")), _str(r.get("field_of_study")), _int(r.get("start_year")), _int(r.get("end_year")), _int(r.get("graduation_year")), [str(v) for v in _list(r.get("allowed_operator_ids"))]) for r in education_records]
        if education_rows:
            con.executemany("insert into local_people_education values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", education_rows)

        con.execute("create table local_education (id varchar, canonical_education_id varchar, school_name_tokens varchar[], school_name varchar, display_value varchar, person_count integer)")
        school_rows = [(_str(r.get("id") or r.get("canonical_education_id")), _str(r.get("canonical_education_id") or r.get("id")), _tokens(r.get("school_name")), _str(r.get("school_name")), _str(r.get("display_value") or r.get("school_name")), _int(r.get("person_count")) or 0) for r in schools_records]
        if school_rows:
            con.executemany("insert into local_education values (?, ?, ?, ?, ?, ?)", school_rows)

        con.execute("create table local_profiles (person_id varchar, base_id varchar, name varchar, profile_json varchar, total_interactions integer)")
        profile_rows = [(_str(p.get("person_id") or p.get("base_id") or p.get("id")), _str(p.get("base_id") or p.get("person_id") or p.get("id")), _str(p.get("name")), json.dumps(p, ensure_ascii=False, sort_keys=True), _int(p.get("total_interactions"))) for p in profiles]
        if profile_rows:
            con.executemany("insert into local_profiles values (?, ?, ?, ?, ?)", profile_rows)

        counts = {table: _table_count(con, table) for table in LOCAL_TABLES}
        checksums = {table: table_checksum(con, table) for table in LOCAL_TABLES}
        con.close()
        os.replace(tmp, db)
        checksum_path = duckdb_checksum_file(db)
    finally:
        try:
            con.close()
        except Exception:
            pass
        if tmp.exists():
            tmp.unlink()

    stats = {"duckdb": str(db), "checksum": str(checksum_path), "tables": counts, "content_checksums": checksums, "schema_version": "local-duckdb-v1"}
    write_json(rd / "stats/build_local_duckdb.json", stats)
    return stats


def table_checksum(con: Any, table: str) -> str:
    rows = con.execute(f"select * from {table}").fetchall()
    columns = [desc[0] for desc in con.description or []]
    normalized = []
    key = "id" if "id" in columns else "person_id"
    for row in rows:
        item = {col: _normalize(value) for col, value in zip(columns, row)}
        normalized.append(item)
    normalized.sort(key=lambda item: str(item.get(key) or item.get("person_id") or item))
    return sha256_json(normalized)


def _normalize(value: Any) -> Any:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, tuple):
        return [_normalize(v) for v in value]
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in sorted(value.items())}
    if isinstance(value, float):
        return round(value, 12)
    return value


def validate_local_search_index(db_path: str | Path) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[3]
    lib = root / "packs/search/primitives/lib"
    sys.path.insert(0, str(lib))
    try:
        from local_duckdb_store import LocalDuckDBSearchStore  # type: ignore
    finally:
        try:
            sys.path.remove(str(lib))
        except ValueError:
            pass

    store = LocalDuckDBSearchStore(str(db_path), read_only=True)
    probes: dict[str, Any] = {}
    try:
        operator_id = _first_operator_id(store)
        people_count = _table_count(store.conn, "local_people_positions")
        if people_count:
            people_tokens = _first_tokens(store, "local_people_positions", "word_tokens") or _first_tokens(store, "local_people_positions", "phrase_tokens")
            people_rank = ("word_tokens", "BM25", people_tokens) if people_tokens else None
            people = store.namespace("people").query(
                rank_by=people_rank,
                filters=("allowed_operator_ids", "ContainsAny", [operator_id]),
                top_k=5,
                include_attributes=["base_id", "position_title", "allowed_operator_ids"],
            )
            probes["people"] = {"ok": bool(people.rows), "rows": len(people.rows), "operator_id": operator_id, "base_ids": [getattr(row, "base_id", None) for row in people.rows], "mode": "seeded_bm25" if people_rank else "filter_only"}
        else:
            probes["people"] = {"ok": False, "rows": 0, "required_empty": True, "error": "core people namespace has no searchable position rows"}

        skill = _first_summary_skill(store)
        summary_count = _table_count(store.conn, "local_summaries")
        if not summary_count:
            probes["summaries"] = {"ok": True, "rows": 0, "skipped_empty": True}
        elif skill:
            summaries = store.namespace("summaries").query(
                filters=("And", [("tech_skills", "ContainsAny", [skill]), ("allowed_operator_ids", "ContainsAny", [operator_id])]),
                top_k=5,
                include_attributes=["base_id", "summary", "tech_skills", "allowed_operator_ids"],
            )
            summary_mode = "tech_skill_filter"
        else:
            summaries = store.namespace("summaries").query(
                rank_by=("word_tokens", "BM25", _first_summary_tokens(store)),
                filters=("allowed_operator_ids", "ContainsAny", [operator_id]),
                top_k=5,
                include_attributes=["base_id", "summary", "tech_skills", "allowed_operator_ids"],
            )
            summary_mode = "bm25_summary_text"
        if summary_count:
            probes["summaries"] = {"ok": bool(summaries.rows), "rows": len(summaries.rows), "skill": skill, "operator_id": operator_id, "mode": summary_mode, "base_ids": [getattr(row, "base_id", None) for row in summaries.rows]}

        education_count = _table_count(store.conn, "local_people_education")
        if education_count:
            education_seed = _first_education_seed(store)
            clauses: list[Any] = [("canonical_education_id", "In", [education_seed["canonical_education_id"]]), ("allowed_operator_ids", "ContainsAny", [operator_id])]
            if education_seed.get("degree_normalized"):
                clauses.insert(1, ("degree_normalized", "In", [education_seed["degree_normalized"]]))
            education = store.namespace("education").query(filters=("And", clauses), top_k=5, include_attributes=["base_id", "school_name", "degree_normalized", "canonical_education_id", "allowed_operator_ids"])
            probes["education"] = {"ok": bool(education.rows), "rows": len(education.rows), "seed": education_seed, "base_ids": [getattr(row, "base_id", None) for row in education.rows], "schools": [getattr(row, "school_name", None) for row in education.rows]}
        else:
            probes["education"] = {"ok": True, "rows": 0, "skipped_empty": True}

        school_count = _table_count(store.conn, "local_education")
        if school_count:
            school_seed = _first_school_seed(store)
            schools = store.namespace("schools").query(filters=("school_name", "ContainsAllTokens", school_seed["prefix"], {"last_as_prefix": True}), top_k=5, include_attributes=["school_name", "person_count"])
            probes["schools"] = {"ok": bool(schools.rows), "rows": len(schools.rows), "seed": school_seed, "school_names": [getattr(row, "school_name", None) for row in schools.rows]}
        else:
            probes["schools"] = {"ok": True, "rows": 0, "skipped_empty": True}
        ok = all(value.get("ok") for value in probes.values())
        return {"duckdb_opened": True, "namespace_probes_ok": bool(ok), "probes": probes}
    finally:
        store.conn.close()


def _first_operator_id(store: Any) -> str:
    row = store.conn.execute("select allowed_operator_ids from local_people_positions where array_length(allowed_operator_ids) > 0 limit 1").fetchone()
    values = row[0] if row else []
    return str(values[0]) if values else "local:user"


def _first_summary_skill(store: Any) -> str | None:
    row = store.conn.execute("select tech_skills from local_summaries where array_length(tech_skills) > 0 limit 1").fetchone()
    values = row[0] if row else []
    return str(values[0]) if values else None


def _first_summary_tokens(store: Any) -> list[str]:
    row = store.conn.execute("select word_tokens from local_summaries where array_length(word_tokens) > 0 limit 1").fetchone()
    values = row[0] if row else []
    return [str(value) for value in values[:3]] or ["founder"]


def _first_tokens(store: Any, table: str, column: str) -> list[str]:
    row = store.conn.execute(f"select {column} from {table} where array_length({column}) > 0 limit 1").fetchone()
    values = row[0] if row else []
    return [str(value) for value in values[:3]]


def _first_education_seed(store: Any) -> dict[str, str]:
    row = store.conn.execute("select canonical_education_id, degree_normalized from local_people_education where canonical_education_id != '' limit 1").fetchone()
    if not row:
        raise RuntimeError("local_people_education has no row with canonical_education_id for validation")
    return {"canonical_education_id": str(row[0]), "degree_normalized": str(row[1] or "")}


def _first_school_seed(store: Any) -> dict[str, str]:
    row = store.conn.execute("select school_name from local_education where school_name != '' limit 1").fetchone()
    if not row:
        raise RuntimeError("local_education has no school_name for validation")
    name = str(row[0])
    token = _tokens(name)[0] if _tokens(name) else name[:3]
    prefix = token[: max(3, min(len(token), 5))]
    return {"school_name": name, "prefix": prefix}
