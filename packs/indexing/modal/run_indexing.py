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
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path("/repo")
sys.path.insert(0, str(REPO))

from packs.indexing.modal.sandbox_common import merge_cache_file, now_iso, write_status  # noqa: E402
PIPELINE = REPO / "packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py"
DUCKDB_SHIM = REPO / "scripts/build-local-duckdb-shim.py"
BENCH = REPO / "packs/indexing/modal/bench_wrapper.py"

# cache-relative path -> key fields (first non-empty wins) used for union merge
CACHE_KEYS = {
    "artifacts/roles_with_dense_text.jsonl": ("title_hash",),
    "artifacts/roles_with_embeddings.parquet": ("title_hash",),
    "artifacts/companies_corpus_v3.jsonl": ("company_urn", "company_name"),
    "artifacts/company_embeddings_v3.parquet": ("company_urn", "company_name"),
    "artifacts/summary_embeddings.parquet": ("person_id",),
    "artifacts/person_tech_skills.jsonl": ("person_id",),
    "seeds/founder_enrichment.jsonl": ("position_id",),
    "seeds/inferred_ages.jsonl": ("person_id",),
}
# work-output path that feeds each cache file after a run
WORK_TO_CACHE = {
    "roles/roles_with_dense_text.jsonl": "artifacts/roles_with_dense_text.jsonl",
    "roles/roles_with_embeddings.jsonl": "artifacts/roles_with_embeddings.parquet",
    "company/companies_corpus_v3.jsonl": "artifacts/companies_corpus_v3.jsonl",
    "company/company_embeddings_v3.jsonl": "artifacts/company_embeddings_v3.parquet",
    "unified/summary_embeddings.jsonl": "artifacts/summary_embeddings.parquet",
    "unified/person_tech_skills.jsonl": "artifacts/person_tech_skills.jsonl",
    "unified/roles/founder_enrichment.jsonl": "seeds/founder_enrichment.jsonl",
    "unified/inferred_ages.jsonl": "seeds/inferred_ages.jsonl",
}
CACHE_VECTOR_FIELDS = {
    "artifacts/roles_with_embeddings.parquet": "dense_embedding",
    "artifacts/company_embeddings_v3.parquet": "embedding",
    "artifacts/summary_embeddings.parquet": "embedding",
}


def embedding_cache_path(artifacts: Path, stem: str) -> Path:
    parquet = artifacts / f"{stem}.parquet"
    return parquet if parquet.exists() else artifacts / f"{stem}.jsonl"






def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--people-csv", required=True)
    ap.add_argument("--cache-root", required=True)
    ap.add_argument("--run-vol", required=True)
    ap.add_argument("--operator-id", required=True)
    ap.add_argument("--compute-threads", type=int, default=16)
    ap.add_argument("--persist-artifacts", action="store_true")
    ap.add_argument("--no-refresh-cache", action="store_true")
    ap.add_argument("--enrich", action="store_true",
                    help="allow paid OpenAI calls for cache misses (requires OPENAI_API_KEY in the sandbox)")
    ap.add_argument("--no-skip-unresolved-companies", action="store_true",
                    help="opt out of the default: by default we skip LLM enrichment for companies with no LinkedIn slug that also miss the corpus (freetext employer strings, no handle to enrich)")
    ap.add_argument("--max-usd", type=float, default=25.0,
                    help="abort before the paid run if the dry-run estimate exceeds this")
    args = ap.parse_args()

    cache_root = Path(args.cache_root)
    run_vol = Path(args.run_vol)
    work = Path("/tmp/run/search-index")
    # Shared RapidAPI company-details cache: read by company enrichment as LLM
    # context for new companies (teammates seed it with
    # `modal volume put <vol> .powerpacks/rapidapi-company-cache cache/rapidapi-company-cache`).
    os.environ.setdefault("POWERPACKS_RAPIDAPI_COMPANY_CACHE", str(cache_root / "rapidapi-company-cache"))
    # Measured (jake-150 run): flex-tier gpt-5.1/5.2 calls are queued
    # server-side for minutes regardless of client concurrency — 487 role
    # calls took 20 min at 256-way. Onboarding is interactive, so pay
    # standard tier (~2x tokens, seconds per call); flex stays the library
    # default for non-urgent bulk jobs. The 300s timeout covers the standard
    # tail; a 256-way sweep showed the sandbox handles that concurrency fine.
    os.environ.setdefault("POWERPACKS_OPENAI_SERVICE_TIER", "default")
    os.environ.setdefault("POWERPACKS_OPENAI_CONCURRENCY", "256")
    os.environ.setdefault("POWERPACKS_OPENAI_TIMEOUT_SECONDS", "300")
    # The duckdb shim defaults to a 2GB memory_limit; DuckDB also misdetects the
    # container's RAM, so a full-network build OOMs at ~1.8GB. The indexing
    # sandbox has 16GB, so lift the limit (leaving headroom for python + OS).
    os.environ.setdefault("POWERPACKS_DUCKDB_MEMORY_LIMIT", "12GB")
    os.environ.setdefault("POWERPACKS_DUCKDB_THREADS", str(max(1, args.compute_threads)))
    os.environ.setdefault("POWERPACKS_CACHE_THREADS", "8")
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
        "--role-input-embeddings", str(embedding_cache_path(artifacts, "roles_with_embeddings")),
        "--company-input-classifications", str(artifacts / "companies_corpus_v3.jsonl"),
        "--company-input-embeddings", str(embedding_cache_path(artifacts, "company_embeddings_v3")),
        "--summary-input-embeddings", str(embedding_cache_path(artifacts, "summary_embeddings")),
        "--person-tech-skills-input", str(artifacts / "person_tech_skills.jsonl"),
    ]
    if not args.no_skip_unresolved_companies:
        # Default: skip freetext companies (no LinkedIn slug, miss the corpus) —
        # there is no handle to enrich them. Applies to both the dry-run estimate
        # (pipeline_cmd[3:] + --dry-run) and the paid run, so the estimate matches
        # what the build does.
        pipeline_cmd.append("--skip-unresolved-companies")
    if args.enrich and args.max_usd <= 0:
        # Uncapped internal mode: skip the dry-run estimate pass entirely (it
        # costs a full extra read of the cache artifacts). The paid run still
        # only pays for cache misses; covered rows replay free.
        pipeline_cmd += [
            "--allow-paid-role-provider",
            "--allow-paid-embeddings",
            "--allow-paid-company-provider",
        ]
    elif args.enrich:
        # Estimate gate: dry-run the same command, persist the estimate, and
        # refuse to spend past --max-usd. The paid run itself still only pays
        # for cache misses; covered rows replay free.
        write_status(run_vol, status | {"phase": "estimate"})
        dry = subprocess.run(pipeline_cmd[3:] + ["--dry-run"], capture_output=True, text=True)
        estimated_usd = None
        try:
            estimate = json.loads((dry.stdout or "").strip().splitlines()[-1])
            estimated_usd = float(estimate.get("estimated_cost_usd") or 0.0)
            (run_vol / "estimate.json").write_text(json.dumps(estimate, indent=2))
        except (json.JSONDecodeError, IndexError, ValueError):
            pass
        if estimated_usd is None:
            write_status(run_vol, status | {"status": "failed", "phase": "estimate", "error": "could not parse dry-run estimate", "finished_at": now_iso()})
            print(dry.stdout[-2000:] if dry.stdout else dry.stderr[-2000:], flush=True)
            return 2
        print(f"[run-in-sandbox] enrich estimate: ${estimated_usd:.2f} (cap ${args.max_usd:.2f})", flush=True)
        if estimated_usd > args.max_usd:
            write_status(run_vol, status | {"status": "failed", "phase": "estimate", "estimated_usd": estimated_usd, "max_usd": args.max_usd, "error": "estimate exceeds --max-usd cap", "finished_at": now_iso()})
            return 2
        pipeline_cmd += [
            "--allow-paid-role-provider",
            "--allow-paid-embeddings",
            "--allow-paid-company-provider",
        ]

    write_status(run_vol, status | {"phase": "pipeline"})
    pipeline_code = subprocess.run(pipeline_cmd).returncode
    if pipeline_code != 0:
        write_status(run_vol, status | {"status": "failed", "phase": "pipeline", "exit_code": pipeline_code, "finished_at": now_iso()})
        return pipeline_code

    # Refresh the shared caches BEFORE the duckdb build so the expensive
    # enrichment (company corpus, roles, summary embeddings) persists to the
    # volume even if the heavy duckdb build fails — we never want to re-pay the
    # LLM enrichment because of a downstream packaging failure.
    if not args.no_refresh_cache:
        write_status(run_vol, status | {"phase": "refresh-cache"})
        for rel_src, rel_cache in WORK_TO_CACHE.items():
            src = work / rel_src
            if not src.exists() or src.stat().st_size == 0:
                continue
            new_count, kept_count = merge_cache_file(
                src,
                cache_root / rel_cache,
                CACHE_KEYS[rel_cache],
                vector_field=CACHE_VECTOR_FIELDS.get(rel_cache),
            )
            print(f"[run-in-sandbox] cache {rel_cache}: {new_count} from run + {kept_count} preserved", flush=True)

    write_status(run_vol, status | {"phase": "duckdb"})
    # Feed the same people.csv that fed the processing pipeline as the
    # person-profiles source so the shim also builds local_person_profiles
    # (the denormalized contacts table the /contacts UI reads). It is loaded
    # through the same flatten_people path as --input, so no reshaping is
    # needed; LinkedIn-only rows simply carry sparse email/phone.
    duckdb_code = subprocess.run([
        sys.executable, str(BENCH), str(run_vol / "bench-duckdb.json"),
        sys.executable, str(DUCKDB_SHIM),
        "--records-dir", str(work),
        "--output-dir", str(work),
        "--operator-id", args.operator_id,
        "--person-profiles-csv", args.people_csv,
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

    write_status(run_vol, status | {"status": "completed", "phase": "done", "finished_at": now_iso()})
    print("[run-in-sandbox] completed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
