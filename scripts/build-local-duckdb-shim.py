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
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

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
        "yc_batches": "VARCHAR[]",
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
    source_value = str(Path(args.records_dir)) if args.records_dir else str(Path(args.source))
    manifest = {
        "status": "ok",
        "flavor": flavor,
        "operator_id": args.operator_id,
        "operator_email": args.operator_email,
        "source": source_value,
        "records_dir": str(Path(args.records_dir)) if args.records_dir else None,
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
    if args.records_dir:
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
