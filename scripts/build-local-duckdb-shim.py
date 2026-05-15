#!/usr/bin/env python3
"""Build local-search golden/candidate artifacts and load a DuckDB search DB.

Sidecar helper for pipe-cleaning the local DuckDB search path while the durable
processing pipeline is still settling. It wraps the checked-in indexing pipeline,
adds stable flavor-suffixed artifact aliases, and materializes the JSONL records
into the table names expected by ``packs/search/primitives/lib/local_duckdb_store.py``.

Example:

  uv run --project . python scripts/build-local-duckdb-shim.py \
    --source people_harmonic_all.csv \
    --operator-id e33a648a-ae5f-432e-83ce-b90d75546ada \
    --operator-email thearthurchen@gmail.com \
    --flavor golden \
    --force

Then test local search with:

  POWERPACKS_LOCAL_SEARCH_DB=.powerpacks/search-index/operator-e33a648a/golden/local-search.golden.duckdb \
    uv run --project . python packs/search/primitives/search_network_pipeline/search_network_pipeline.py ...
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_limit = sys.maxsize
while True:
    try:
        csv.field_size_limit(_limit)
        break
    except OverflowError:
        _limit //= 10

DEFAULT_OPERATOR_ID = "e33a648a-ae5f-432e-83ce-b90d75546ada"
DEFAULT_OPERATOR_EMAIL = "thearthurchen@gmail.com"
DEFAULT_SOURCE = Path("people_harmonic_all.csv")
DEFAULT_OUTPUT_DIR = Path(".powerpacks/search-index/operator-e33a648a")

ARTIFACT_ALIASES = {
    "unified/flattened_people.jsonl": "unified/flattened_people.{flavor}.jsonl",
    "unified/unified_person.csv": "unified/unified_person.{flavor}.csv",
    "profiles/hydrated_profiles.jsonl": "profiles/hydrated_profiles.{flavor}.jsonl",
    "records/people.records.jsonl": "records/people.records.{flavor}.jsonl",
    "records/companies.records.jsonl": "records/companies.records.{flavor}.jsonl",
    "records/summaries.records.jsonl": "records/summaries.records.{flavor}.jsonl",
    "records/education.records.jsonl": "records/education.records.{flavor}.jsonl",
    "records/schools.records.jsonl": "records/schools.records.{flavor}.jsonl",
}

LOCAL_TABLES = {
    "local_people_positions": "records/people.records.jsonl",
    "local_summaries": "records/summaries.records.jsonl",
    "local_people_education": "records/education.records.jsonl",
    "local_education": "records/schools.records.jsonl",
    "local_companies": "records/companies.records.jsonl",
}

# Local DuckDB contract for the five search namespaces.  These columns mirror
# the Aleph TurboPuffer upload contracts copied under .powerpacks/aleph-seed:
# people/summaries/companies carry embeddings; education/schools are lookup and
# prefilter tables and intentionally do not require vectors in Aleph.
LOCAL_TABLE_CONTRACT: dict[str, dict[str, str]] = {
    "local_people_positions": {
        "id": "VARCHAR",
        "position_id": "VARCHAR",
        "person_id": "VARCHAR",
        "base_id": "VARCHAR",
        "vector": "DOUBLE[]",
        "word_tokens": "VARCHAR[]",
        "char_tokens": "VARCHAR[]",
        "d2q_tokens": "VARCHAR[]",
        "phrase_tokens": "VARCHAR[]",
        "position_title": "VARCHAR",
        "seniority_band": "VARCHAR",
        "company_id": "VARCHAR",
        "company_name": "VARCHAR",
        "city": "VARCHAR",
        "state": "VARCHAR",
        "country": "VARCHAR",
        "macro_region": "VARCHAR",
        "is_current": "BOOLEAN",
        "total_years_experience": "DOUBLE",
        "start_date_epoch": "BIGINT",
        "end_date_epoch": "BIGINT",
        "tenure_years": "DOUBLE",
        "role_track": "VARCHAR",
        "metro_areas": "VARCHAR[]",
        "allowed_operator_ids": "VARCHAR[]",
        "role_ids": "VARCHAR[]",
        "inferred_birth_year": "BIGINT",
        "x_twitter_followers": "BIGINT",
        "linkedin_followers": "BIGINT",
        "linkedin_connections": "BIGINT",
        "ig_followers": "BIGINT",
    },
    "local_summaries": {
        "id": "VARCHAR",
        "person_id": "VARCHAR",
        "base_id": "VARCHAR",
        "summary": "VARCHAR",
        "summary_tokens": "VARCHAR[]",
        "tech_skills": "VARCHAR[]",
        "allowed_operator_ids": "VARCHAR[]",
        "phrase_tokens": "VARCHAR[]",
        "word_tokens": "VARCHAR[]",
        "vector": "DOUBLE[]",
    },
    "local_companies": {
        "id": "VARCHAR",
        "company_urn": "VARCHAR",
        "vector": "DOUBLE[]",
        "company_name": "VARCHAR",
        "aliases": "VARCHAR",
        "name_aliases_text": "VARCHAR",
        "semantic_text": "VARCHAR",
        "doc2query": "VARCHAR",
        "d2q_text": "VARCHAR",
        "doc2query_text": "VARCHAR",
        "entity_sector_text": "VARCHAR",
        "word_text": "VARCHAR",
        "website_domain": "VARCHAR",
        "linkedin_url": "VARCHAR",
        "logo_url": "VARCHAR",
        "description": "VARCHAR",
        "headcount": "BIGINT",
        "funding_stage": "BIGINT",
        "funding_total": "DOUBLE",
        "city": "VARCHAR",
        "state": "VARCHAR",
        "country": "VARCHAR",
        "metro_area": "VARCHAR",
        "macro_region": "VARCHAR",
        "entity_types": "VARCHAR[]",
        "sector_types": "VARCHAR[]",
        "technology_types": "VARCHAR[]",
        "customer_type": "VARCHAR[]",
        "investor_urns": "VARCHAR[]",
        "accelerators": "VARCHAR[]",
        "yc_batches": "VARCHAR[]",
        "stage": "VARCHAR",
        "founded_year": "BIGINT",
        "last_funding_at": "BIGINT",
        "valuation": "DOUBLE",
        "allowed_operator_ids": "VARCHAR[]",
    },
    "local_people_education": {
        "id": "VARCHAR",
        "person_id": "VARCHAR",
        "base_id": "VARCHAR",
        "education_id": "VARCHAR",
        "canonical_education_id": "VARCHAR",
        "school_name": "VARCHAR",
        "degree": "VARCHAR",
        "degree_normalized": "VARCHAR",
        "field_of_study": "VARCHAR",
        "start_year": "BIGINT",
        "end_year": "BIGINT",
        "graduation_year": "BIGINT",
        "allowed_operator_ids": "VARCHAR[]",
    },
    "local_education": {
        "id": "VARCHAR",
        "canonical_education_id": "VARCHAR",
        "school_name": "VARCHAR",
        "display_value": "VARCHAR",
        "school_name_tokens": "VARCHAR[]",
        "person_count": "BIGINT",
    },
}

VECTOR_TABLES = ["local_people_positions", "local_summaries", "local_companies"]
EXTRA_COLUMNS = LOCAL_TABLE_CONTRACT


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def run(cmd: list[str]) -> None:
    completed = subprocess.run(cmd, cwd=ROOT, text=True)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def read_jsonl(path: Path, limit: int | None = None):
    if not path.exists():
        return
    count = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            yield json.loads(line)
            count += 1
            if limit is not None and count >= limit:
                return


def write_jsonl(path: Path, rows) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            count += 1
    return count


def has_records(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    with path.open("rb") as handle:
        return bool(handle.read(4096).strip())


def link_or_copy(src: Path, dst: Path, *, force: bool = False) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        if src.resolve() == dst.resolve():
            return
    except OSError:
        pass
    if dst.exists() or dst.is_symlink():
        if not force:
            return
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def create_aliases(run_dir: Path, flavor: str, *, force: bool = False, golden_typo_alias: bool = False) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for source_rel, alias_rel_template in ARTIFACT_ALIASES.items():
        src = run_dir / source_rel
        alias_rel = alias_rel_template.format(flavor=flavor)
        dst = run_dir / alias_rel
        link_or_copy(src, dst, force=force)
        if dst.exists():
            aliases[source_rel] = str(dst)
        if golden_typo_alias and flavor == "golden" and alias_rel.endswith(".golden.jsonl"):
            typo_dst = run_dir / alias_rel.replace(".golden.jsonl", ".golen.jsonl")
            link_or_copy(src, typo_dst, force=force)
            if typo_dst.exists():
                aliases[source_rel + "#golen_typo"] = str(typo_dst)
    return aliases


def record_source_path(records_dir: Path, rel: str, flavor: str) -> Path | None:
    """Resolve a records artifact from either a run root or a records/ dir."""
    candidates = [
        records_dir / rel,
        records_dir / Path(rel).name,
    ]
    alias_template = ARTIFACT_ALIASES.get(rel)
    if alias_template:
        alias_rel = alias_template.format(flavor=flavor)
        candidates.extend([
            records_dir / alias_rel,
            records_dir / Path(alias_rel).name,
        ])
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def materialize_records_dir(records_dir: Path, run_dir: Path, flavor: str, *, force: bool = False) -> dict[str, str]:
    """Copy/link normal pipeline records artifacts into a shim run directory.

    ``records_dir`` may be either the run root containing ``records/`` or the
    ``records/`` directory itself.  The shim then loads from ``run_dir/records``
    exactly like people.csv pipeline mode, so both modes share one DuckDB loader.
    """
    if not records_dir.exists():
        raise SystemExit(f"missing records directory: {records_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    copied: dict[str, str] = {}
    for _table, rel in LOCAL_TABLES.items():
        src = record_source_path(records_dir, rel, flavor)
        if not src:
            continue
        dst = run_dir / rel
        link_or_copy(src, dst, force=force)
        if dst.exists():
            copied[rel] = str(dst)
    if not copied:
        raise SystemExit(f"no records artifacts found under {records_dir}")
    return copied


FUNDING_STAGE_MAP = {
    "PRE_SEED": 1,
    "SEED": 2,
    "SERIES_A": 3,
    "SERIES_B": 4,
    "SERIES_C": 5,
    "SERIES_D": 6,
    "SERIES_E": 7,
    "SERIES_F": 8,
    "SERIES_G": 9,
    "SERIES_H": 10,
    "SERIES_I": 11,
    "LATE_STAGE": 50,
    "IPO": 90,
    "PUBLIC": 91,
    "EXITED": 99,
    "VENTURE_UNKNOWN": 0,
}


def _as_list(value: Any) -> list[Any]:
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


def _date_to_int(value: Any) -> int | None:
    text = str(value or "").strip()
    if len(text) >= 10 and text[0:4].isdigit() and text[5:7].isdigit() and text[8:10].isdigit():
        return int(text[0:4] + text[5:7] + text[8:10])
    return None


def _customer_type(value: Any) -> list[str]:
    text = str(value or "")
    return [token for token in ("B2B", "B2C", "B2G") if token in text]


def _word_tokens(text: str) -> list[str]:
    import re

    words = re.findall(r"[a-z0-9]+", str(text).lower())
    return words + [f"{words[i]} {words[i + 1]}" for i in range(len(words) - 1)]


def _load_csv_by_id(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    csv.field_size_limit(sys.maxsize)
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return {row.get("id", ""): row for row in reader if row.get("id")}


def materialize_aleph_output_dir(aleph_dir: Path, run_dir: Path, operator_id: str, *, limit: int | None = None, force: bool = False) -> dict[str, str]:
    """Convert copied Aleph pipeline_output artifacts to local records JSONL.

    This follows the checked-in Aleph upload scripts' artifact names and field
    shapes: companies_corpus_v3 + company_embeddings_v3, summary_embeddings +
    person_tech_skills + unified_person.csv, people_education, schools_corpus.
    No network calls, uploads, or paid providers are used.
    """
    if not aleph_dir.exists():
        raise SystemExit(f"missing Aleph output directory: {aleph_dir}")
    if force and run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    records: dict[str, str] = {}

    # Companies: data_pipeline_v2/pipelines/company/upload_companies_to_turbopuffer.py
    # For limited canaries, pick the first embedding rows and then join corpus
    # metadata to avoid scanning the huge 1536-dim embeddings file looking for
    # arbitrary early corpus IDs.
    company_embeddings: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(aleph_dir / "company/company_embeddings_v3.jsonl", limit):
        urn = str(row.get("company_urn") or "")
        if urn and row.get("embedding"):
            company_embeddings[urn] = row
    wanted_company_urns = set(company_embeddings)
    company_candidates_by_urn: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(aleph_dir / "company/companies_corpus_v3.jsonl"):
        urn = str(row.get("company_urn") or "")
        if urn in wanted_company_urns:
            company_candidates_by_urn[urn] = row
            if len(company_candidates_by_urn) >= len(wanted_company_urns):
                break
    company_candidates = [company_candidates_by_urn.get(urn, {"company_urn": urn, **emb}) for urn, emb in company_embeddings.items()]

    def company_rows():
        emitted = 0
        for row in company_candidates:
            urn = str(row.get("company_urn") or "")
            emb = company_embeddings.get(urn)
            if not urn or not emb:
                continue
            company_name = row.get("company_name") or emb.get("company_name") or ""
            aliases = row.get("name_aliases") or []
            yield {
                "id": urn,
                "company_urn": urn,
                "vector": emb.get("embedding") or [],
                "entity_sector_text": row.get("word_text") or "",
                "word_text": row.get("word_text") or "",
                "name_aliases_text": " ".join([company_name] + [str(v) for v in aliases]),
                "doc2query_text": row.get("d2q_text") or "",
                "d2q_text": row.get("d2q_text") or "",
                "semantic_text": row.get("semantic_text") or emb.get("semantic_text") or "",
                "company_name": company_name,
                "city": row.get("city") or None,
                "state": row.get("state") or None,
                "country": row.get("country") or None,
                "metro_area": row.get("metro_area") or None,
                "macro_region": row.get("macro_region") or None,
                "funding_stage": FUNDING_STAGE_MAP.get(str(row.get("funding_stage") or "").upper(), 0),
                "funding_total": row.get("funding_total") or 0,
                "headcount": row.get("headcount") or 0,
                "entity_types": row.get("entity_types") or [],
                "sector_types": row.get("sector_types") or [],
                "technology_types": _as_list(row.get("technology_types")),
                "customer_type": _customer_type(row.get("customer_type")),
                "investor_urns": row.get("investor_urns") or [],
                "accelerators": _as_list(row.get("accelerators")),
                "yc_batches": _as_list(row.get("yc_batches")),
                "stage": row.get("stage") or "",
                "founded_year": int(row.get("founded_year") or 0),
                "last_funding_at": _date_to_int(row.get("last_funding_at")) or 0,
                "valuation": float(row.get("valuation") or 0),
                "description": row.get("description") or "",
                "linkedin_url": row.get("linkedin_url") or "",
                "logo_url": row.get("logo_url") or "",
                "website_domain": row.get("website_domain") or "",
                "allowed_operator_ids": [operator_id],
            }
            emitted += 1
            if limit is not None and emitted >= limit:
                return

    records["records/companies.records.jsonl"] = str(run_dir / "records/companies.records.jsonl")
    write_jsonl(run_dir / "records/companies.records.jsonl", company_rows())

    # Summaries: data_pipeline_v2/pipelines/people/indexing/upload_summaries_turbopuffer.py
    unified = _load_csv_by_id(aleph_dir / "unified/unified_person.csv")
    skills = {str(row.get("person_id")): row.get("tech_skills") or [] for row in read_jsonl(aleph_dir / "unified/person_tech_skills.jsonl") if row.get("person_id")}

    def summary_rows():
        for row in read_jsonl(aleph_dir / "unified/summary_embeddings.jsonl", limit):
            pid = str(row.get("person_id") or "")
            if not pid:
                continue
            summary = (unified.get(pid) or {}).get("summary") or ""
            yield {
                "id": pid,
                "person_id": pid,
                "base_id": pid,
                "summary": summary,
                "summary_tokens": _word_tokens(summary),
                "tech_skills": skills.get(pid, []),
                "allowed_operator_ids": [operator_id],
                "phrase_tokens": [],
                "word_tokens": _word_tokens(summary),
                "vector": row.get("embedding") or [],
            }

    records["records/summaries.records.jsonl"] = str(run_dir / "records/summaries.records.jsonl")
    write_jsonl(run_dir / "records/summaries.records.jsonl", summary_rows())

    # Education: data_pipeline_v2/pipelines/education upload scripts.
    schools_by_id = {str(row.get("entity_urn")): row for row in read_jsonl(aleph_dir / "education/schools_corpus.jsonl") if row.get("entity_urn")}

    def education_rows():
        import uuid

        namespace = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
        for row in read_jsonl(aleph_dir / "education/people_education.jsonl", limit):
            person_id = str(row.get("person_id") or "")
            education_id = str(row.get("education_id") or "")
            school = schools_by_id.get(education_id) or {}
            yield {
                "id": str(uuid.uuid5(namespace, f"pe:{person_id}:{education_id}")),
                "person_id": person_id,
                "base_id": person_id,
                "education_id": education_id,
                "canonical_education_id": education_id,
                "school_name": school.get("school_name") or "",
                "degree": row.get("degree") or "",
                "degree_normalized": row.get("degree_normalized") or "",
                "field_of_study": row.get("field_of_study") or "",
                "start_year": row.get("start_year") or 0,
                "end_year": row.get("end_year") or 0,
                "graduation_year": row.get("graduation_year") or 0,
                "allowed_operator_ids": [operator_id],
            }

    records["records/education.records.jsonl"] = str(run_dir / "records/education.records.jsonl")
    write_jsonl(run_dir / "records/education.records.jsonl", education_rows())

    def school_rows():
        for row in read_jsonl(aleph_dir / "education/schools_corpus.jsonl", limit):
            urn = str(row.get("entity_urn") or "")
            school_name = row.get("school_name") or ""
            yield {
                "id": urn,
                "canonical_education_id": urn,
                "school_name": school_name,
                "display_value": school_name,
                "school_name_tokens": _word_tokens(school_name),
                "person_count": int(row.get("person_count") or 0),
            }

    records["records/schools.records.jsonl"] = str(run_dir / "records/schools.records.jsonl")
    write_jsonl(run_dir / "records/schools.records.jsonl", school_rows())

    # People vectors require roles_with_embeddings + flattened_people.  The copied
    # seed bundle used by local tests does not include roles_with_embeddings, so
    # emit an empty Aleph-shaped people records file rather than inventing shape.
    records["records/people.records.jsonl"] = str(run_dir / "records/people.records.jsonl")
    write_jsonl(run_dir / "records/people.records.jsonl", [])
    return records


def qident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def table_columns(con: Any, table: str) -> set[str]:
    rows = con.execute(f"PRAGMA table_info({qident(table)})").fetchall()
    return {str(row[1]) for row in rows}


def add_missing_columns(con: Any, table: str, columns: dict[str, str]) -> None:
    existing = table_columns(con, table)
    for name, type_name in columns.items():
        if name not in existing:
            con.execute(f"ALTER TABLE {qident(table)} ADD COLUMN {qident(name)} {type_name}")


def load_jsonl_table(con: Any, table: str, path: Path) -> int:
    con.execute(f"DROP TABLE IF EXISTS {qident(table)}")
    if has_records(path):
        con.execute(
            f"CREATE TABLE {qident(table)} AS SELECT * FROM read_json_auto(?, format='newline_delimited', union_by_name=true, maximum_object_size=134217728)",
            [str(path)],
        )
    else:
        con.execute(f"CREATE TABLE {qident(table)} (id VARCHAR)")
    return int(con.execute(f"SELECT count(*) FROM {qident(table)}").fetchone()[0])


def postprocess_table(con: Any, table: str, operator_id: str) -> None:
    add_missing_columns(con, table, LOCAL_TABLE_CONTRACT.get(table, {}))
    cols = table_columns(con, table)

    if table == "local_people_positions":
        if {"person_id", "base_id"} <= cols:
            con.execute(f"UPDATE {qident(table)} SET person_id = COALESCE(NULLIF(CAST(person_id AS VARCHAR), ''), CAST(base_id AS VARCHAR))")
        if {"person_id", "id"} <= cols:
            con.execute(f"UPDATE {qident(table)} SET person_id = COALESCE(NULLIF(CAST(person_id AS VARCHAR), ''), CAST(id AS VARCHAR))")
        if {"position_id", "id"} <= cols:
            con.execute(f"UPDATE {qident(table)} SET position_id = COALESCE(NULLIF(CAST(position_id AS VARCHAR), ''), CAST(id AS VARCHAR))")

    if table == "local_summaries":
        if {"person_id", "id"} <= cols:
            con.execute(f"UPDATE {qident(table)} SET person_id = COALESCE(NULLIF(CAST(person_id AS VARCHAR), ''), CAST(id AS VARCHAR))")
        if {"base_id", "person_id"} <= cols:
            con.execute(f"UPDATE {qident(table)} SET base_id = COALESCE(NULLIF(CAST(base_id AS VARCHAR), ''), CAST(person_id AS VARCHAR))")

    if table == "local_companies":
        if {"company_urn", "id"} <= cols:
            con.execute(f"UPDATE {qident(table)} SET company_urn = COALESCE(NULLIF(CAST(company_urn AS VARCHAR), ''), CAST(id AS VARCHAR))")
        if {"id", "company_urn"} <= cols:
            con.execute(f"UPDATE {qident(table)} SET id = COALESCE(NULLIF(CAST(id AS VARCHAR), ''), CAST(company_urn AS VARCHAR))")
        if {"doc2query_text", "doc2query", "d2q_text"} <= cols:
            con.execute(
                f"UPDATE {qident(table)} SET doc2query_text = COALESCE("
                f"NULLIF(CAST(doc2query_text AS VARCHAR), ''), "
                f"NULLIF(CAST(doc2query AS VARCHAR), ''), "
                f"NULLIF(CAST(d2q_text AS VARCHAR), ''))"
            )
        if {"entity_sector_text", "word_text"} <= cols:
            con.execute(
                f"UPDATE {qident(table)} SET entity_sector_text = COALESCE("
                f"NULLIF(CAST(entity_sector_text AS VARCHAR), ''), "
                f"NULLIF(CAST(word_text AS VARCHAR), ''))"
            )
        if {"name_aliases_text", "aliases", "company_name"} <= cols:
            con.execute(
                f"UPDATE {qident(table)} SET name_aliases_text = COALESCE("
                f"NULLIF(CAST(name_aliases_text AS VARCHAR), ''), "
                f"NULLIF(CAST(aliases AS VARCHAR), ''), "
                f"NULLIF(CAST(company_name AS VARCHAR), ''))"
            )

    if table == "local_people_education" and {"base_id", "person_id"} <= cols:
        con.execute(f"UPDATE {qident(table)} SET base_id = COALESCE(NULLIF(CAST(base_id AS VARCHAR), ''), CAST(person_id AS VARCHAR))")

    if table == "local_education" and {"school_name_tokens", "school_name"} <= cols:
        con.execute(
            f"UPDATE {qident(table)} SET school_name_tokens = regexp_extract_all(lower(COALESCE(school_name, '')), '[a-z0-9]+')"
        )

    cols = table_columns(con, table)
    if "allowed_operator_ids" in cols:
        # Fill null/empty operator lists so local filters can scope to the operator.
        try:
            con.execute(
                f"UPDATE {qident(table)} SET allowed_operator_ids = list_value(?) "
                f"WHERE allowed_operator_ids IS NULL OR len(allowed_operator_ids) = 0",
                [operator_id],
            )
        except Exception:
            # Some auto-detected JSON columns may not expose len(); local reader can
            # still parse JSON strings/lists, so only null fill is essential.
            con.execute(
                f"UPDATE {qident(table)} SET allowed_operator_ids = list_value(?) WHERE allowed_operator_ids IS NULL",
                [operator_id],
            )


def resolve_artifact_path(run_dir: Path, rel: str, flavor: str) -> Path:
    standard = run_dir / rel
    if standard.exists():
        return standard
    alias_template = ARTIFACT_ALIASES.get(rel)
    if alias_template:
        alias = run_dir / alias_template.format(flavor=flavor)
        if alias.exists():
            return alias
    return standard


def load_duckdb(run_dir: Path, flavor: str, operator_id: str, *, force: bool = False) -> tuple[Path, dict[str, int]]:
    try:
        import duckdb  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit("duckdb is required; run through `uv run --project . python ...`") from exc

    db_path = run_dir / f"local-search.{flavor}.duckdb"
    if db_path.exists():
        if force:
            db_path.unlink()
        else:
            raise SystemExit(f"DuckDB already exists: {db_path}. Use --force to replace it.")

    con = duckdb.connect(str(db_path))
    counts: dict[str, int] = {}
    try:
        for table, rel in LOCAL_TABLES.items():
            path = resolve_artifact_path(run_dir, rel, flavor)
            counts[table] = load_jsonl_table(con, table, path)
            postprocess_table(con, table, operator_id)
        con.execute("CREATE OR REPLACE VIEW local_people AS SELECT * FROM local_people_positions")
        con.execute("CHECKPOINT")
    finally:
        con.close()
    return db_path, counts


def build_pipeline(args: argparse.Namespace, run_dir: Path) -> None:
    if args.skip_pipeline:
        return
    cmd = [
        sys.executable,
        "packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py",
        "run",
        "--input",
        str(Path(args.source)),
        "--output-dir",
        str(Path(args.output_dir)),
        "--run-id",
        args.flavor,
        "--default-operator-id",
        args.operator_id,
    ]
    if args.limit is not None:
        cmd.extend(["--limit", str(args.limit)])
    if args.force:
        cmd.append("--force")
    run(cmd)


def write_manifest(run_dir: Path, flavor: str, args: argparse.Namespace, aliases: dict[str, str], db_path: Path, table_counts: dict[str, int]) -> Path:
    source_value = str(Path(args.aleph_output_dir)) if args.aleph_output_dir else str(Path(args.records_dir)) if args.records_dir else str(Path(args.source))
    manifest = {
        "status": "ok",
        "flavor": flavor,
        "operator_id": args.operator_id,
        "operator_email": args.operator_email,
        "source": source_value,
        "records_dir": str(Path(args.records_dir)) if args.records_dir else None,
        "aleph_output_dir": str(Path(args.aleph_output_dir)) if args.aleph_output_dir else None,
        "run_dir": str(run_dir),
        "duckdb": str(db_path),
        "powerpacks_local_search_db": str(db_path),
        "aliases": aliases,
        "tables": table_counts,
        "artifact_contract": {
            "golden_flattened": ".powerpacks/search-index/operator-e33a648a/golden/unified/flattened_people.golden.jsonl",
            "candidate_flattened": ".powerpacks/search-index/operator-e33a648a/candidate/unified/flattened_people.candidate.jsonl",
        },
        "local_table_contract": LOCAL_TABLE_CONTRACT,
        "vector_tables": VECTOR_TABLES,
    }
    path = run_dir / f"manifest.{flavor}.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="Input people CSV; defaults to people_harmonic_all.csv")
    parser.add_argument("--records-dir", help="Existing normal pipeline run root or records/ directory containing *.records.jsonl; skips people.csv pipeline build")
    parser.add_argument("--aleph-output-dir", help="Copied Aleph pipeline_output directory; converts Aleph upload artifacts to local records without API calls")
    parser.add_argument("--operator-id", default=DEFAULT_OPERATOR_ID)
    parser.add_argument("--operator-email", default=DEFAULT_OPERATOR_EMAIL)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Parent output dir; run is created under <output-dir>/<flavor>/")
    parser.add_argument("--flavor", choices=["golden", "candidate"], default="golden")
    parser.add_argument("--limit", type=int, help="Optional row limit for smoke tests")
    parser.add_argument("--force", action="store_true", help="Replace existing run dir / DuckDB / aliases")
    parser.add_argument("--skip-pipeline", action="store_true", help="Only load DuckDB from existing records in the run dir")
    parser.add_argument("--golden-typo-alias", action="store_true", help="Also create .golen.jsonl hardlink aliases for the golden typo")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    run_dir = Path(args.output_dir) / args.flavor
    if args.aleph_output_dir:
        aleph_dir = ROOT / args.aleph_output_dir if not Path(args.aleph_output_dir).is_absolute() else Path(args.aleph_output_dir)
        args.aleph_output_dir = str(aleph_dir)
        materialize_aleph_output_dir(aleph_dir, run_dir, args.operator_id, limit=args.limit, force=args.force)
    elif args.records_dir:
        records_dir = ROOT / args.records_dir if not Path(args.records_dir).is_absolute() else Path(args.records_dir)
        args.records_dir = str(records_dir)
        materialize_records_dir(records_dir, run_dir, args.flavor, force=args.force)
    else:
        source = ROOT / args.source if not Path(args.source).is_absolute() else Path(args.source)
        if not source.exists() and not args.skip_pipeline:
            raise SystemExit(f"missing source CSV: {source}")
        args.source = str(source)
        build_pipeline(args, run_dir)
    if not run_dir.exists():
        raise SystemExit(f"missing run dir after pipeline/artifact materialization: {run_dir}")
    aliases = create_aliases(run_dir, args.flavor, force=args.force, golden_typo_alias=args.golden_typo_alias)
    db_path, table_counts = load_duckdb(run_dir, args.flavor, args.operator_id, force=args.force)
    manifest_path = write_manifest(run_dir, args.flavor, args, aliases, db_path, table_counts)
    emit({
        "status": "ok",
        "run_dir": str(run_dir),
        "manifest": str(manifest_path),
        "duckdb": str(db_path),
        "tables": table_counts,
        "env": f"POWERPACKS_LOCAL_SEARCH_DB={db_path}",
    })


if __name__ == "__main__":
    main()
