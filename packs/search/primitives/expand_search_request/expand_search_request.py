#!/usr/bin/env python3
"""Expand a natural-language search query into role_search_filters via parallel extractors.

Runs 7 domain-specific extractors in parallel (role, company, location, education,
temporal, seniority, social) using the OpenAI SDK, then merges results into the
powerpacks role_search_filters schema.

Prompts are ported from network-search-api's battle-tested extractors.

Usage:
  uv run --env-file .env --project . python packs/search/primitives/expand_search_request/expand_search_request.py \
    --query "founders backed by sequoia" \
    --env-file .env

  # Or with state:
  ... --query "..." --state .powerpacks/runs/task.json --write-state
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_MODEL = os.environ.get("EXPAND_SEARCH_MODEL", "gpt-4o-mini")
DEFAULT_API_BASE = os.environ.get("OPENAI_API_BASE", "https://api.openai.com")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate_output(value: dict[str, Any]) -> list[str]:
    """Return list of validation warnings (non-fatal)."""
    warnings: list[str] = []
    filters = value.get("role_search_filters")
    if not isinstance(filters, dict):
        warnings.append("missing role_search_filters")
        return warnings

    # Strip role_tracks if present (we don't want it)
    if "role_tracks" in filters:
        del filters["role_tracks"]
        warnings.append("stripped role_tracks from output")

    # Ensure bm25_queries present when semantic_query exists
    sq = filters.get("semantic_query")
    bm25 = filters.get("bm25_queries")
    if sq and (not bm25 or len(bm25) == 0):
        warnings.append("semantic_query present but bm25_queries empty")

    # Validate entity_types
    valid_entity = {"venture_backed_startup", "public_company", "private_company", "non_profit",
                    "vc_firm", "pe_firm", "bank", "family_office", "government_public_sector",
                    "insurance_carrier", "nonprofit"}
    for et in filters.get("entity_types") or []:
        if et not in valid_entity:
            warnings.append(f"invalid entity_type: {et}")

    # Validate seniority_bands
    valid_seniority = {"entry", "junior", "mid", "senior", "staff", "principal",
                       "manager", "director", "vice_president", "c_suite", "partner", "owner"}
    for sb in filters.get("seniority_bands") or []:
        if sb not in valid_seniority:
            warnings.append(f"invalid seniority_band: {sb}")

    return warnings


def clean_output(value: dict[str, Any]) -> dict[str, Any]:
    """Remove null/empty fields and strip role_tracks."""
    filters = value.get("role_search_filters")
    if isinstance(filters, dict):
        # Always strip role_tracks
        filters.pop("role_tracks", None)
        # Fix common entity_type mistakes
        entity_types = filters.get("entity_types")
        if entity_types:
            fixed = []
            for et in entity_types:
                if et == "startup":
                    fixed.append("venture_backed_startup")
                else:
                    fixed.append(et)
            filters["entity_types"] = fixed
        # Coerce graduation years to int
        for key in ("graduation_year_min", "graduation_year_max"):
            val = filters.get(key)
            if isinstance(val, str) and val.strip().isdigit():
                filters[key] = int(val)
        # Remove empty/null fields
        value["role_search_filters"] = {k: v for k, v in filters.items() if v is not None and v != [] and v != ""}
    return value


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def record_step(state_path: Path, state: dict[str, Any], output: dict[str, Any], elapsed_ms: int) -> None:
    now = now_iso()
    state.setdefault("steps", []).append({
        "id": "expand_search_request",
        "status": "completed",
        "recorded_at": now,
        "elapsed_ms": elapsed_ms,
        "output": output,
    })
    state["updated_at"] = now
    write_json(state_path, state)


def main() -> None:
    parser = argparse.ArgumentParser(description="Expand a search query into role_search_filters via parallel extractors")
    parser.add_argument("--query", required=True, help="Natural-language search query")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Override model for all extractors")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--api-key", default=None, help="OpenAI API key (default: $OPENAI_API_KEY)")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--state", help="Task state file to record expansion into")
    parser.add_argument("--write-state", action="store_true")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    # Load env file for API key
    env_path = Path(args.env_file)
    if env_path.exists():
        for line in env_path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k not in os.environ and v.strip():
                os.environ[k] = v.strip().strip('"').strip("'")

    api_key = args.api_key or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print(json.dumps({"primitive": "expand_search_request", "status": "failed", "error": "OPENAI_API_KEY not set"}))
        raise SystemExit(1)

    started = time.time()
    try:
        from parallel_extractors import expand_query_parallel
        result = asyncio.run(expand_query_parallel(
            args.query,
            api_key=api_key,
            api_base=args.api_base,
            model_override=args.model if args.model != DEFAULT_MODEL else None,
        ))

        result = clean_output(result)
        warnings = validate_output(result)
        elapsed_ms = result.get("total_ms") or int((time.time() - started) * 1000)

        output = {
            "primitive": "expand_search_request",
            "status": "completed",
            "model": args.model,
            "query": args.query,
            "elapsed_ms": elapsed_ms,
            "warnings": warnings,
            **{k: v for k, v in result.items() if k != "total_ms"},
        }

        if args.write_state and args.state:
            state_path = Path(args.state)
            state = read_json(state_path) if state_path.exists() else {}
            record_step(state_path, state, result, elapsed_ms)

        print(json.dumps(output, indent=2, sort_keys=True))

    except Exception as e:
        elapsed_ms = int((time.time() - started) * 1000)
        print(json.dumps({
            "primitive": "expand_search_request",
            "status": "failed",
            "error": str(e),
            "elapsed_ms": elapsed_ms,
        }))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
