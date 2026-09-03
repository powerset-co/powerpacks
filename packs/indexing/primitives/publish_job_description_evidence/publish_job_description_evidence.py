#!/usr/bin/env python3
"""Publish built job-description evidence to TurboPuffer and Postgres."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
SEARCH_PRIMITIVES = ROOT / "packs/search/primitives"
for path in [SEARCH_PRIMITIVES / "lib", SEARCH_PRIMITIVES / "shared", SEARCH_PRIMITIVES / "turbopuffer"]:
    sys.path.insert(0, str(path))
sys.path.insert(0, str(ROOT))

from postgres_client import database_url, ensure_psycopg2  # noqa: E402
from turbopuffer_search_backend import load_env_file, namespace, namespace_name  # noqa: E402

from packs.indexing.lib.artifact_io import iter_artifact_rows  # noqa: E402


JOB_RECORD = "records/job_descriptions.records.parquet"
MATCH_RECORD = "records/job_description_positions.records.parquet"
JOB_SCHEMA = {
    "company_domain": {"type": "string"},
    "title": {"type": "string"},
    "description": {"type": "string"},
    "retrieval_text": {"type": "string"},
    "word_tokens": {"type": "[]string", "full_text_search": {"tokenizer": "pre_tokenized_array"}},
    "tech_skills": {"type": "[]string"},
    "posted_date": {"type": "string"},
    "url": {"type": "string"},
    "ats_provider": {"type": "string"},
    "is_open": {"type": "bool"},
    "allowed_operator_ids": {"type": "[]string"},
}


def publish_mappings(rows: list[dict[str, Any]], job_ids: list[str]) -> None:
    psycopg2 = ensure_psycopg2()
    with psycopg2.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS job_description_positions (
                    id text PRIMARY KEY,
                    job_description_id text NOT NULL,
                    position_id text NOT NULL,
                    person_id text NOT NULL,
                    company_domain text NOT NULL,
                    match_score double precision NOT NULL,
                    match_type text NOT NULL,
                    posting_position_gap_days integer NOT NULL
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS job_description_positions_job_id_idx ON job_description_positions (job_description_id)")
            if job_ids:
                cursor.execute("DELETE FROM job_description_positions WHERE job_description_id = ANY(%s::text[])", (job_ids,))
            if rows:
                psycopg2.extras.execute_values(
                    cursor,
                    """INSERT INTO job_description_positions
                       (id, job_description_id, position_id, person_id, company_domain, match_score, match_type, posting_position_gap_days)
                       VALUES %s""",
                    [(
                        row["id"], row["job_description_id"], row["position_id"], row["person_id"],
                        row["company_domain"], row["match_score"], row["match_type"], row["posting_position_gap_days"],
                    ) for row in rows],
                )


def run(records_dir: Path, *, batch_size: int = 500, env_file: Path | None = None, dry_run: bool = False) -> dict[str, Any]:
    load_env_file(env_file)
    jobs = list(iter_artifact_rows(records_dir / JOB_RECORD))
    matches = list(iter_artifact_rows(records_dir / MATCH_RECORD))
    if any({"local", "local:user"} & set(row.get("allowed_operator_ids") or []) for row in jobs):
        raise ValueError("remote publishing requires records built with an actual operator id")
    result = {
        "status": "dry-run" if dry_run else "completed",
        "namespace": namespace_name("job_descriptions"),
        "job_descriptions": len(jobs),
        "job_descriptions_with_vectors": sum(1 for row in jobs if row.get("vector")),
        "position_matches": len(matches),
    }
    if dry_run:
        return result

    publish_mappings(matches, [str(row["id"]) for row in jobs])
    target = namespace("job_descriptions")
    for start in range(0, len(jobs), batch_size):
        target.write(
            upsert_rows=jobs[start:start + batch_size],
            schema=JOB_SCHEMA,
            distance_metric="cosine_distance",
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records-dir", required=True)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(
        Path(args.records_dir),
        batch_size=args.batch_size,
        env_file=Path(args.env_file) if args.env_file else None,
        dry_run=args.dry_run,
    ), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
