#!/usr/bin/env python3
"""In-sandbox orchestrator: seed -> pipeline -> duckdb -> persist -> status.

Runs as the Modal sandbox entrypoint so the whole processing run completes
server-side even if the dispatching laptop disconnects. Progress and the final
outcome land on the volume as <run-vol>/status.json; artifacts the local
machine consumes (local-search.duckdb, manifest.json) are persisted alongside.

Multi-operator volume layout: inputs and runs are per-operator
(operators/<operator-id>/...), while the enrichment caches under /data/cache
are shared by every operator. Cache keys are content-derived (title_hash,
company name, person_id from the linkedin slug), so rows are operator-agnostic
and overlap across networks means free cache hits. After a successful run the
caches are refreshed by KEY-UNION merge - never overwrite - because a run's
output only contains rows for that operator's network and a plain copy would
drop other operators' cached rows.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/repo")
PIPELINE = REPO / "packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py"
DUCKDB_SHIM = REPO / "scripts/build-local-duckdb-shim.py"
BENCH = REPO / "packs/indexing/modal/bench_wrapper.py"

# cache-relative path -> key fields (first non-empty wins) used for union merge
CACHE_KEYS = {
    "artifacts/roles_with_dense_text.jsonl": ("title_hash",),
    "artifacts/roles_with_embeddings.jsonl": ("title_hash",),
    "artifacts/companies_corpus_v3.jsonl": ("company_urn", "company_name"),
    "artifacts/company_embeddings_v3.jsonl": ("company_urn", "company_name"),
    "artifacts/summary_embeddings.jsonl": ("person_id",),
    "artifacts/person_tech_skills.jsonl": ("person_id",),
    "seeds/founder_enrichment.jsonl": ("position_id",),
    "seeds/inferred_ages.jsonl": ("person_id",),
}
# work-output path that feeds each cache file after a run
WORK_TO_CACHE = {
    "roles/roles_with_dense_text.jsonl": "artifacts/roles_with_dense_text.jsonl",
    "roles/roles_with_embeddings.jsonl": "artifacts/roles_with_embeddings.jsonl",
    "company/companies_corpus_v3.jsonl": "artifacts/companies_corpus_v3.jsonl",
    "company/company_embeddings_v3.jsonl": "artifacts/company_embeddings_v3.jsonl",
    "unified/summary_embeddings.jsonl": "artifacts/summary_embeddings.jsonl",
    "unified/person_tech_skills.jsonl": "artifacts/person_tech_skills.jsonl",
    "unified/roles/founder_enrichment.jsonl": "seeds/founder_enrichment.jsonl",
    "unified/inferred_ages.jsonl": "seeds/inferred_ages.jsonl",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_status(run_vol: Path, payload: dict) -> None:
    run_vol.mkdir(parents=True, exist_ok=True)
    tmp = run_vol / "status.json.tmp"
    tmp.write_text(json.dumps(payload | {"updated_at": now_iso()}, indent=2))
    tmp.replace(run_vol / "status.json")


def row_key(row: dict, key_fields: tuple[str, ...]) -> str:
    for field in key_fields:
        value = str(row.get(field) or "").strip()
        if value:
            return f"{field}={value}"
    return ""


def merge_cache_file(new_rows_path: Path, cache_path: Path, key_fields: tuple[str, ...]) -> tuple[int, int]:
    """Union-merge: new rows win for shared keys, existing cache rows for keys
    the run did not touch are preserved. Streaming with a seen-key set; atomic
    tmp+rename so concurrent runs cannot corrupt the file (a lost race only
    delays a row until the next run re-adds it)."""
    seen: set[str] = set()
    tmp = cache_path.parent / (cache_path.name + f".tmp-{new_rows_path.stat().st_ino}")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    new_count = 0
    kept_count = 0
    with tmp.open("w", encoding="utf-8") as out:
        with new_rows_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                key = row_key(json.loads(line), key_fields)
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                out.write(line + "\n")
                new_count += 1
        if cache_path.exists():
            with cache_path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    key = row_key(json.loads(line), key_fields)
                    if key and key in seen:
                        continue
                    if key:
                        seen.add(key)
                    out.write(line + "\n")
                    kept_count += 1
    tmp.replace(cache_path)
    return new_count, kept_count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--people-csv", required=True)
    ap.add_argument("--cache-root", required=True)
    ap.add_argument("--run-vol", required=True)
    ap.add_argument("--operator-id", required=True)
    ap.add_argument("--persist-artifacts", action="store_true")
    ap.add_argument("--no-refresh-cache", action="store_true")
    args = ap.parse_args()

    cache_root = Path(args.cache_root)
    run_vol = Path(args.run_vol)
    work = Path("/tmp/run/search-index")
    status = {"status": "running", "phase": "seed", "started_at": now_iso()}
    write_status(run_vol, status)

    (work / "unified/roles").mkdir(parents=True, exist_ok=True)
    seed_map = {
        "seeds/founder_enrichment.jsonl": "unified/roles/founder_enrichment.jsonl",
        "seeds/inferred_ages.jsonl": "unified/inferred_ages.jsonl",
    }
    for rel_src, rel_dest in seed_map.items():
        src = cache_root / rel_src
        if src.exists():
            shutil.copyfile(src, work / rel_dest)

    artifacts = cache_root / "artifacts"
    pipeline_cmd = [
        sys.executable, str(BENCH), str(run_vol / "bench-pipeline.json"),
        sys.executable, str(PIPELINE), "run",
        "--input", args.people_csv,
        "--output-dir", str(work),
        "--default-operator-id", args.operator_id,
        "--role-input-classifications", str(artifacts / "roles_with_dense_text.jsonl"),
        "--role-input-embeddings", str(artifacts / "roles_with_embeddings.jsonl"),
        "--company-input-classifications", str(artifacts / "companies_corpus_v3.jsonl"),
        "--company-input-embeddings", str(artifacts / "company_embeddings_v3.jsonl"),
        "--summary-input-embeddings", str(artifacts / "summary_embeddings.jsonl"),
        "--person-tech-skills-input", str(artifacts / "person_tech_skills.jsonl"),
    ]
    write_status(run_vol, status | {"phase": "pipeline"})
    pipeline_code = subprocess.run(pipeline_cmd).returncode
    if pipeline_code != 0:
        write_status(run_vol, status | {"status": "failed", "phase": "pipeline", "exit_code": pipeline_code, "finished_at": now_iso()})
        return pipeline_code

    write_status(run_vol, status | {"phase": "duckdb"})
    duckdb_code = subprocess.run([
        sys.executable, str(BENCH), str(run_vol / "bench-duckdb.json"),
        sys.executable, str(DUCKDB_SHIM),
        "--records-dir", str(work),
        "--output-dir", str(work),
        "--operator-id", args.operator_id,
        "--force",
    ]).returncode
    if duckdb_code != 0:
        write_status(run_vol, status | {"status": "failed", "phase": "duckdb", "exit_code": duckdb_code, "finished_at": now_iso()})
        return duckdb_code

    write_status(run_vol, status | {"phase": "persist"})
    keep = ["ledger.json", "manifest.json", "stats", "local-search.duckdb"]
    for name in keep:
        src = work / name
        if not src.exists():
            continue
        dest = run_vol / name
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
        else:
            shutil.copyfile(src, dest)
    if args.persist_artifacts and (work / "records").exists():
        shutil.copytree(work / "records", run_vol / "records", dirs_exist_ok=True)

    if not args.no_refresh_cache:
        write_status(run_vol, status | {"phase": "refresh-cache"})
        for rel_src, rel_cache in WORK_TO_CACHE.items():
            src = work / rel_src
            if not src.exists() or src.stat().st_size == 0:
                continue
            new_count, kept_count = merge_cache_file(src, cache_root / rel_cache, CACHE_KEYS[rel_cache])
            print(f"[run-in-sandbox] cache {rel_cache}: {new_count} from run + {kept_count} preserved", flush=True)

    write_status(run_vol, status | {"status": "completed", "phase": "done", "finished_at": now_iso()})
    print("[run-in-sandbox] completed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
