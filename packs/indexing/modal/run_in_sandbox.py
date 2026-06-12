#!/usr/bin/env python3
"""In-sandbox orchestrator: seed -> pipeline -> duckdb -> persist -> status.

Runs as the Modal sandbox entrypoint so the whole processing run completes
server-side even if the dispatching laptop disconnects. Progress and the final
outcome land on the volume as runs/<label>/status.json; artifacts the local
machine consumes (local-search.duckdb, manifest.json) are persisted alongside.
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


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_status(run_vol: Path, payload: dict) -> None:
    run_vol.mkdir(parents=True, exist_ok=True)
    tmp = run_vol / "status.json.tmp"
    tmp.write_text(json.dumps(payload | {"updated_at": now_iso()}, indent=2))
    tmp.replace(run_vol / "status.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-root", required=True)
    ap.add_argument("--run-vol", required=True)
    ap.add_argument("--operator-id", required=True)
    ap.add_argument("--persist-artifacts", action="store_true")
    args = ap.parse_args()

    input_root = Path(args.input_root)
    run_vol = Path(args.run_vol)
    work = Path("/tmp/run/search-index")
    status = {"status": "running", "phase": "seed", "started_at": now_iso()}
    write_status(run_vol, status)

    (work / "unified/roles").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(input_root / "seeds/founder_enrichment.jsonl", work / "unified/roles/founder_enrichment.jsonl")
    shutil.copyfile(input_root / "seeds/inferred_ages.jsonl", work / "unified/inferred_ages.jsonl")

    artifacts = input_root / "artifacts"
    pipeline_cmd = [
        sys.executable, str(BENCH), str(run_vol / "bench-pipeline.json"),
        sys.executable, str(PIPELINE), "run",
        "--input", str(input_root / "people.csv"),
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

    # Refresh the enrichment caches on the volume in place so the next run
    # (including one with new people) replays everything already paid for.
    # Outputs are merged full files, so this is the union of old cache + any
    # newly enriched rows.
    cache_map = {
        "roles/roles_with_dense_text.jsonl": "artifacts/roles_with_dense_text.jsonl",
        "roles/roles_with_embeddings.jsonl": "artifacts/roles_with_embeddings.jsonl",
        "company/companies_corpus_v3.jsonl": "artifacts/companies_corpus_v3.jsonl",
        "company/company_embeddings_v3.jsonl": "artifacts/company_embeddings_v3.jsonl",
        "unified/summary_embeddings.jsonl": "artifacts/summary_embeddings.jsonl",
        "unified/person_tech_skills.jsonl": "artifacts/person_tech_skills.jsonl",
        "unified/roles/founder_enrichment.jsonl": "seeds/founder_enrichment.jsonl",
        "unified/inferred_ages.jsonl": "seeds/inferred_ages.jsonl",
    }
    refreshed = 0
    for rel_src, rel_dest in cache_map.items():
        src = work / rel_src
        if src.exists() and src.stat().st_size > 0:
            dest = input_root / rel_dest
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            refreshed += 1
    print(f"[run-in-sandbox] refreshed {refreshed} cache artifacts on volume", flush=True)

    write_status(run_vol, status | {"status": "completed", "phase": "done", "finished_at": now_iso()})
    print("[run-in-sandbox] completed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
