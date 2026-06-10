#!/usr/bin/env python3
"""Dispatch import/enrich for one or all contact sources."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from packs.ingestion.primitives.discover_contacts_pipeline.common import emit, now_iso, read_accounts, read_json, write_json
    from packs.ingestion.primitives.import_contacts_pipeline import gmail, linkedin, messages
    from packs.ingestion.primitives.import_contacts_pipeline.common import DEFAULT_ACCOUNTS, DEFAULT_IMPORT_DIR
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.primitives.discover_contacts_pipeline.common import emit, now_iso, read_accounts, read_json, write_json
    from packs.ingestion.primitives.import_contacts_pipeline import gmail, linkedin, messages
    from packs.ingestion.primitives.import_contacts_pipeline.common import DEFAULT_ACCOUNTS, DEFAULT_IMPORT_DIR


def stable_aggregate_signature(payload: dict) -> dict:
    signature = dict(payload)
    signature.pop("updated_at", None)
    return signature


def write_aggregate_manifest(payload: dict) -> dict:
    manifest = DEFAULT_IMPORT_DIR / "manifest.json"
    existing = read_json(manifest, {}) or {}
    if isinstance(existing, dict) and stable_aggregate_signature(existing) == stable_aggregate_signature(payload):
        return existing
    write_json(manifest, payload)
    return payload


def run_sources(args: argparse.Namespace) -> dict:
    read_accounts(args.accounts)
    sources = ["gmail", "linkedin", "messages"] if args.source == "all" else [args.source]
    results = {}
    status = "completed"
    for source in sources:
        if source == "gmail":
            result = gmail.run(args)
        elif source in {"linkedin", "linkedin_csv"}:
            result = linkedin.run(args)
        elif source == "messages":
            result = messages.run(args)
        else:
            raise ValueError(f"unsupported source: {source}")
        results[source] = result
        if result.get("status") not in {"completed", "skipped"}:
            status = str(result.get("status") or "failed")
            break
    payload = {"status": status, "sources": results, "updated_at": now_iso()}
    if args.source == "all":
        write_aggregate_manifest(payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import/enrich discovered contacts into local people artifacts")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--source", choices=["gmail", "linkedin", "linkedin_csv", "messages", "all"], default="all")
    run.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    run.add_argument("--operator-id", default="local")
    run.add_argument("--approve-parallel-spend", action="store_true")
    run.add_argument("--confirm-import", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = run_sources(args)
    emit(payload)
    return 20 if payload.get("status") == "blocked_approval" else 1 if payload.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
