#!/usr/bin/env python3
"""Resolve investor names to company/person URNs for company investor filters."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB_DIR))

from turbopuffer_client import STRONG_CONSISTENCY, comparison, load_env_file, namespace, namespace_name, role_payload_from_state, row_attrs  # noqa: E402


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def append_event(state_path: Path, event: dict[str, Any]) -> None:
    log_path = state_path.with_suffix(state_path.suffix + ".events.jsonl")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def investor_names(payload: dict[str, Any]) -> list[str]:
    values = payload.get("investor_names") or []
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


async def resolve_turbopuffer_investors(names: list[str], *, top_k: int) -> list[dict[str, Any]]:
    """Resolve investor names from the Powerpacks investor namespace.

    The namespace stores both firm and angel investor URNs with popularity
    counts. For exact-name collisions, choose highest `investment_count`, which
    matches the aleph recall behavior without importing aleph code.
    """
    if not names:
        return []
    ns = namespace("investors")
    rows: list[dict[str, Any]] = []
    for name in names:
        exact_filter = comparison("investor_name", "Eq", name)

        def exact_query() -> Any:
            return ns.query(
                filters=exact_filter,
                rank_by=["investment_count", "desc"],
                top_k=max(top_k, 10),
                include_attributes=["investor_name", "investor_type", "investment_count", "canonical_urn"],
                consistency=STRONG_CONSISTENCY,
            )

        response = await asyncio.to_thread(exact_query)
        matched_rows = list(response.rows or [])
        if not matched_rows:
            token_filter = comparison("investor_name_tokens", "ContainsAllTokens", name)

            def token_query() -> Any:
                return ns.query(
                    filters=token_filter,
                    rank_by=["investment_count", "desc"],
                    top_k=max(top_k, 10),
                    include_attributes=["investor_name", "investor_type", "investment_count", "canonical_urn"],
                    consistency=STRONG_CONSISTENCY,
                )

            response = await asyncio.to_thread(token_query)
            matched_rows = list(response.rows or [])

        ranked = [
            row_attrs(row, ["investor_name", "investor_type", "investment_count", "canonical_urn"])
            for row in matched_rows
        ]
        ranked.sort(key=lambda row: int(row.get("investment_count") or 0), reverse=True)
        for row in ranked[:top_k]:
            row["query_name"] = name
            row["urn"] = row.get("canonical_urn") or row.get("id")
            rows.append(row)
    return rows


async def run(args: argparse.Namespace) -> dict[str, Any]:
    env_file = Path(args.env_file) if args.env_file else None
    load_env_file(env_file)
    state_path = Path(args.state) if args.state else None
    state = read_json(state_path) if state_path else {}
    payload = json.loads(args.payload_json) if args.payload_json else role_payload_from_state(state)

    names = investor_names(payload)
    existing = [str(value) for value in payload.get("investors") or [] if value]
    if not names and existing:
        return {
            "namespaces": {"companies": namespace_name("companies"), "postgres": "persons"},
            "investor_names": [],
            "investor_urns": list(dict.fromkeys(existing)),
            "resolved_count": len(set(existing)),
            "unresolved_names": [],
            "sample_investors": [],
        }

    try:
        tp_rows = await resolve_turbopuffer_investors(names, top_k=args.investor_top_k)
    except Exception as exc:
        raise RuntimeError(
            f"failed to query TurboPuffer investor namespace {namespace_name('investors')!r}; "
            "build it with packs/search/primitives/build_investor_index/build_investor_index.py"
        ) from exc

    tp_urns = [str(row["urn"]) for row in tp_rows if row.get("urn")]
    investor_urns = list(dict.fromkeys([*existing, *tp_urns]))

    resolved_by_name: set[str] = set()
    for row in tp_rows:
        query_name = str(row.get("query_name") or "").strip().lower()
        if query_name:
            resolved_by_name.add(query_name)
    unresolved = [name for name in names if name.lower() not in resolved_by_name]

    return {
        "namespaces": {"investors": namespace_name("investors")},
        "investor_names": names,
        "investor_urns": investor_urns,
        "resolved_count": len(investor_urns),
        "unresolved_names": unresolved,
        "sample_investors": [
            *[
                {
                    "type": row.get("investor_type"),
                    "name": row.get("investor_name"),
                    "urn": row.get("urn"),
                    "investment_count": row.get("investment_count"),
                    "source": "turbopuffer_investors",
                }
                for row in tp_rows[:10]
            ],
        ],
    }


def record_step(state_path: Path, state: dict[str, Any], output: dict[str, Any], elapsed_ms: int) -> None:
    now = now_iso()
    state.setdefault("steps", []).append({
        "id": "resolve_investors",
        "status": "completed",
        "recorded_at": now,
        "elapsed_ms": elapsed_ms,
        "output": output,
    })
    state["updated_at"] = now
    write_json(state_path, state)
    append_event(state_path, {
        "event": "record_step",
        "task_id": state.get("task_id"),
        "state": str(state_path),
        "step_id": "resolve_investors",
        "status": "completed",
        "timestamp": now,
        "elapsed_ms": elapsed_ms,
        "resolved_count": output.get("resolved_count"),
        "unresolved_names": output.get("unresolved_names"),
    })


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve investor names to URNs")
    parser.add_argument("--state")
    parser.add_argument("--payload-json")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--write-state", action="store_true")
    parser.add_argument("--investor-top-k", type=int, default=1)
    args = parser.parse_args()

    started = time.time()
    output = asyncio.run(run(args))
    elapsed_ms = int((time.time() - started) * 1000)
    if args.write_state:
        if not args.state:
            raise SystemExit("--write-state requires --state")
        state_path = Path(args.state)
        record_step(state_path, read_json(state_path), output, elapsed_ms)
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
