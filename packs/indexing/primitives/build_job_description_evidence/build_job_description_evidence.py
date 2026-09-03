#!/usr/bin/env python3
"""Build local job-description records and their position matches."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402

from packs.indexing.lib.artifact_io import iter_artifact_rows, write_parquet_rows  # noqa: E402
from packs.indexing.lib.contracts import contract_duckdb_columns, load_search_contract, normalize_record_for_contract, validate_record  # noqa: E402
from packs.indexing.lib.io import write_json, write_jsonl  # noqa: E402
from packs.indexing.lib.job_descriptions import job_description_record, match_job_descriptions_to_positions  # noqa: E402


JOB_RECORD = "records/job_descriptions.records.parquet"
MATCH_RECORD = "records/job_description_positions.records.parquet"
EMBEDDING_INPUT = "job-descriptions/job_descriptions.jsonl"
STATS = "stats/build_job_description_evidence.json"


def _rows(db_path: Path, query: str) -> list[dict[str, Any]]:
    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = connection.execute(query).fetchall()
        columns = [column[0] for column in connection.description or []]
        return [dict(zip(columns, row)) for row in rows]
    finally:
        connection.close()


def _embeddings(path: Path | None) -> dict[str, list[float]]:
    if path is None:
        return {}
    if not path.is_file() or path.suffix.lower() != ".parquet":
        raise ValueError(f"job-description embeddings must be an existing Parquet file: {path}")
    return {
        str(row["id"]): [float(value) for value in row.get("embedding") or row.get("vector") or []]
        for row in iter_artifact_rows(path)
        if row.get("id") and (row.get("embedding") or row.get("vector"))
    }


def _job_rows(jobs_db: Path | None, jobs_jsonl: list[Path], limit: int | None) -> list[dict[str, Any]]:
    limit_sql = f" limit {int(limit)}" if limit is not None else ""
    if jobs_db is not None:
        return _rows(
            jobs_db,
            """
            select listing_id, company, title, description, posted_date, url, apply_url,
                   ats_provider, is_open
            from listings
            where length(trim(coalesce(description, ''))) >= 200
            order by listing_id
            """ + limit_sql,
        )

    missing = [path for path in jobs_jsonl if not path.is_file()]
    if missing:
        raise ValueError(f"jobs JSONL not found: {missing[0]}")
    connection = duckdb.connect()
    try:
        rows = connection.execute(
            """
            select * exclude(row_number)
            from (
              select
                md5(lower(coalesce(company, '')) || '|' ||
                    trim(regexp_replace(lower(coalesce(title, '')), '[^a-z0-9]+', ' ', 'g')) || '|' ||
                    trim(regexp_replace(lower(coalesce(string_split_regex(location, '[,;/]')[1], '')), '[^a-z0-9]+', ' ', 'g')) || '|' ||
                    lower(coalesce(url, ''))) as listing_id,
                company, title, description, postedDate as posted_date, url,
                null::varchar as apply_url, atsProvider as ats_provider, true as is_open,
                row_number() over (
                  partition by listing_id
                  order by length(coalesce(description, '')) desc
                ) as row_number
              from read_json_auto(?, format='newline_delimited', union_by_name=true)
              where length(trim(coalesce(description, ''))) >= 200
            )
            where row_number = 1
            order by listing_id
            """ + limit_sql,
            [[str(path) for path in jobs_jsonl]],
        ).fetchall()
        columns = [column[0] for column in connection.description or []]
        return [dict(zip(columns, row)) for row in rows]
    finally:
        connection.close()


def run(
    jobs_db: Path | None,
    positions_path: Path,
    output_dir: Path,
    *,
    jobs_jsonl: list[Path] | None = None,
    operator_id: str = "local:user",
    embeddings: Path | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    if jobs_db is not None and not jobs_db.is_file():
        raise ValueError(f"jobs DuckDB not found: {jobs_db}")
    if jobs_db is None and not jobs_jsonl:
        raise ValueError("one jobs DuckDB or at least one jobs JSONL is required")
    if not positions_path.is_file():
        raise ValueError(f"people position records not found: {positions_path}")

    raw_jobs = _job_rows(jobs_db, jobs_jsonl or [], limit)
    positions = [
        row for row in iter_artifact_rows(positions_path)
        if str(row.get("company_domain") or "").strip()
    ]

    jobs = [record for row in raw_jobs if (record := job_description_record(row, operator_id)) is not None]
    vectors = _embeddings(embeddings)
    for job in jobs:
        if job["id"] in vectors:
            job["vector"] = vectors[job["id"]]
    matches = match_job_descriptions_to_positions(jobs, positions)

    job_contract = load_search_contract("turbopuffer/job_descriptions.namespace.json")
    match_contract = load_search_contract("postgres/job_description_positions.table.json")
    job_records = [normalize_record_for_contract(row, job_contract) for row in jobs]
    match_records = [normalize_record_for_contract(row, match_contract) for row in matches]
    errors = [
        *(validate_record(row, job_contract) for row in job_records if not validate_record(row, job_contract)["ok"]),
        *(validate_record(row, match_contract) for row in match_records if not validate_record(row, match_contract)["ok"]),
    ]
    if errors:
        raise ValueError(json.dumps({"contract_errors": errors[:10]}))

    job_path = output_dir / JOB_RECORD
    match_path = output_dir / MATCH_RECORD
    input_path = output_dir / EMBEDDING_INPUT
    write_parquet_rows(
        job_path,
        job_records,
        float_array_fields=("vector",),
        schema=contract_duckdb_columns(job_contract),
    )
    write_parquet_rows(match_path, match_records, schema=contract_duckdb_columns(match_contract))
    write_jsonl(input_path, [
        {"id": row["id"], "retrieval_text": row["retrieval_text"]}
        for row in job_records
    ])

    matched_job_ids = {row["job_description_id"] for row in match_records}
    matched_position_ids = {row["position_id"] for row in match_records}
    stats = {
        "job_descriptions": len(job_records),
        "job_descriptions_with_vectors": sum(1 for row in job_records if row.get("vector")),
        "job_descriptions_with_tech_skills": sum(1 for row in job_records if row.get("tech_skills")),
        "job_description_domains": len({row["company_domain"] for row in job_records}),
        "positions": len(positions),
        "position_domains": len({str(row.get("company_domain") or "").lower() for row in positions}),
        "matches": len(match_records),
        "matches_during_tenure": sum(1 for row in match_records if not row["posting_position_gap_days"]),
        "matches_near_tenure": sum(1 for row in match_records if row["posting_position_gap_days"]),
        "matched_job_descriptions": len(matched_job_ids),
        "matched_positions": len(matched_position_ids),
        "outputs": {
            "job_descriptions": str(job_path),
            "job_description_positions": str(match_path),
            "embedding_input": str(input_path),
        },
    }
    write_json(output_dir / STATS, stats)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--jobs-db")
    source.add_argument("--jobs-jsonl", action="append", default=[])
    parser.add_argument("--positions", required=True, help="people.records.parquet from the search indexing pipeline")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--operator-id", default="local:user")
    parser.add_argument("--embeddings")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    result = run(
        Path(args.jobs_db) if args.jobs_db else None,
        Path(args.positions),
        Path(args.output_dir),
        jobs_jsonl=[Path(path) for path in args.jobs_jsonl],
        operator_id=args.operator_id,
        embeddings=Path(args.embeddings) if args.embeddings else None,
        limit=args.limit,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
