#!/usr/bin/env python3
"""LinkedIn import sandbox runner: connections.csv -> enriched people.csv.

Hosts the LinkedIn import pipeline
(imports/linkedin/network_import.py) inside a Modal sandbox:

  parse + convert -> RapidAPI enrichment -> merged people.csv

RapidAPI spend is ALWAYS approved on this path (team decision): the single
`run` is invoked with the explicit --approve-spend gate satisfied, so it fetches
cache misses without a needs_approval stop (no approve/continue loop). The shared
profile cache lives on the volume (cache/profile_cache_v2), so the primitive's
cache hits/misses are team-wide: every profile any operator fetched before is
free and instant, and every new fetch lands in the shared cache for the next
operator.

Output people.csv is written to the operator's volume input path, ready for
run_indexing.py.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

REPO = Path("/repo")
sys.path.insert(0, str(REPO))

from packs.indexing.modal.sandbox_common import now_iso, write_status  # noqa: E402
from packs.ingestion.primitives.common.paths import resolve_discover_source_dir  # noqa: E402
from packs.ingestion.primitives.enrich.rapidapi_client import (  # noqa: E402
    DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS,
    DEFAULT_RAPIDAPI_MAX_RPM,
    DEFAULT_RAPIDAPI_MAX_WORKERS,
)
from packs.ingestion.primitives.imports.linkedin import network_import as linkedin_import  # noqa: E402
from packs.shared.csv_io import CsvIO  # noqa: E402

WORK = Path("/tmp/linkedin-import")


def import_namespace(args: argparse.Namespace, cache_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        csv=args.connections_csv,
        source_user=args.source_user,
        operator_id=args.operator_id,
        limit=None,
        output_dir=str(WORK),
        # RapidAPI spend is always approved on the Modal path (team decision):
        # satisfy the explicit gate so the single `run` proceeds through cache
        # misses without a needs_approval stop.
        approve_spend=True,
        refresh_cache=False,
        profile_cache_dir=str(cache_dir),
        company_corpus_jsonl=[],
        sleep_seconds=0.0,
        force_enrich=False,
        convert_only=False,
        max_workers=DEFAULT_RAPIDAPI_MAX_WORKERS,
        max_rpm=DEFAULT_RAPIDAPI_MAX_RPM,
        failure_retry_hours=DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS,
    )


def enrichment_stats(manifest: dict) -> dict:
    counts = manifest.get("counts") or {}
    return {
        "queue_count": counts.get("queue_count"),
        "cache_hit_count": counts.get("cache_hit_count"),
        "paid_call_count": counts.get("paid_call_count"),
        "recent_failure_count": counts.get("recent_failure_count"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--connections-csv", required=True, help="volume path to the uploaded Connections.csv")
    ap.add_argument("--people-out", required=True, help="volume path for the merged people.csv")
    ap.add_argument("--cache-root", required=True)
    ap.add_argument("--run-vol", required=True)
    ap.add_argument("--operator-id", required=True)
    ap.add_argument("--source-user", default="linkedin")
    args = ap.parse_args()

    run_vol = Path(args.run_vol)
    cache_dir = Path(args.cache_root) / "profile_cache_v2"
    cache_dir.mkdir(parents=True, exist_ok=True)
    status = {"status": "running", "phase": "parse", "started_at": now_iso()}
    write_status(run_vol, status)

    if not os.environ.get("RAPIDAPI_LINKEDIN_KEY") and not os.environ.get("RAPIDAPI_KEY"):
        write_status(run_vol, status | {"status": "failed", "phase": "parse", "error": "RAPIDAPI_LINKEDIN_KEY missing in sandbox (powerset-rapidapi secret not mounted?)", "finished_at": now_iso()})
        return 2
    if not Path(args.connections_csv).exists():
        write_status(run_vol, status | {"status": "failed", "phase": "parse", "error": f"missing connections csv: {args.connections_csv}", "finished_at": now_iso()})
        return 2

    WORK.mkdir(parents=True, exist_ok=True)
    ns = import_namespace(args, cache_dir)
    # The import stage overwrites one manifest.json in its fixed discover dir;
    # that manifest (not a ledger) is the source of truth for status + artifacts.
    manifest_path = resolve_discover_source_dir(Path(ns.output_dir), "linkedin") / "manifest.json"

    write_status(run_vol, status | {"phase": "enrich"})
    # RapidAPI is always approved on this path (approve_spend=True), so the single
    # `run` completes without a needs_approval stop — no approve/continue loop.
    code = linkedin_import.LinkedInImport.command_run(ns)

    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    if code != 0 or manifest.get("status") != "completed":
        write_status(run_vol, status | {"status": "failed", "phase": "enrich", "exit_code": code, "run_status": manifest.get("status"), "error": manifest.get("error"), "finished_at": now_iso()})
        return code or 1

    write_status(run_vol, status | {"phase": "persist"})
    people_csv = str((manifest.get("artifacts") or {}).get("people_csv") or "")
    if not people_csv or not Path(people_csv).exists():
        write_status(run_vol, status | {"status": "failed", "phase": "persist", "error": f"import completed but people.csv missing: {people_csv}", "finished_at": now_iso()})
        return 1
    people_out = Path(args.people_out)
    people_out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(people_csv, people_out)
    with Path(people_csv).open(newline="", encoding="utf-8-sig") as handle:
        people_count = sum(1 for _ in CsvIO.dict_reader(handle))

    stats = enrichment_stats(manifest) | {"people": people_count}
    (run_vol / "import-stats.json").write_text(json.dumps(stats, indent=2))
    print(f"[run-linkedin] people={people_count} stats={json.dumps(stats)}", flush=True)
    write_status(run_vol, status | {"status": "completed", "phase": "done", "stats": stats, "finished_at": now_iso()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
