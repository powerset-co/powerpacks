#!/usr/bin/env python3
"""Enrich sales-nav mutual connections with operator attribution.

Phase 1 (--mode attribution): Fast batch lookup via person-attribution API.
  Takes existing mutuals with resolved person_ids and enriches with operator
  names, source channels, and interaction counts. No Sales Nav API calls.

Phase 2 (--mode discover): Full mutual discovery via discover API.
  For each lead, calls /v2/network/mutuals/discover to get ALL mutual
  connections (not just the 2-3 preview from search results). Rate-limited
  with configurable stagger. Then enriches with attribution.

Usage:
  # Phase 1: enrich existing mutuals with attribution
  uv run --env-file .env --project . python packs/sales-nav/primitives/enrich_mutual_attribution/enrich_mutual_attribution.py \
    --state .powerpacks/runs/sales-nav-run.json --mode attribution

  # Phase 2: discover full mutuals + attribution
  uv run --env-file .env --project . python packs/sales-nav/primitives/enrich_mutual_attribution/enrich_mutual_attribution.py \
    --state .powerpacks/runs/sales-nav-run.json --mode discover --stagger 2.0 --max-leads 25
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


API_BASE_ENV_KEYS = ("POWERSET_API_BASE", "POWERPACKS_API_URL", "POWERSET_API_URL", "POWERPACKS_SEARCH_API_URL")


def missing_api_base_message() -> str:
    keys = ", ".join(API_BASE_ENV_KEYS)
    return (
        f"missing required Powerset API config: set one of {keys}. "
        "Copy packs/powerset/templates/env.powerset.example to .env for Powerset-hosted use."
    )


def resolve_api_base(value: str | None = None) -> str:
    if value:
        return value.rstrip("/")
    for key in API_BASE_ENV_KEYS:
        candidate = (os.environ.get(key) or "").strip()
        if candidate:
            return candidate.rstrip("/")
    raise SystemExit(missing_api_base_message())


def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_env(env_file: str) -> None:
    path = Path(env_file)
    if not path.exists():
        return
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k not in os.environ and v.strip():
            os.environ[k] = v.strip().strip('"').strip("'")


def get_auth_token() -> str:
    """Get bearer token from credentials file."""
    creds_path = Path.home() / ".powerpacks" / "credentials.json"
    if creds_path.exists():
        creds = json.loads(creds_path.read_text())
        token = creds.get("access_token")
        if token:
            return token
    raise RuntimeError("No auth token found. Run $powerset login first.")


def api_call(method: str, path: str, body: dict | None = None, *, base_url: str, token: str, timeout: int = 30) -> dict:
    """Make an authenticated API call."""
    url = f"{base_url.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")


def load_state(path: Path) -> dict:
    return json.loads(path.read_text())


def state_paths(state: dict) -> dict[str, str]:
    return state.get("paths") or state.get("files") or {}


# ---------------------------------------------------------------------------
# Phase 1: Attribution enrichment for existing mutuals
# ---------------------------------------------------------------------------

def enrich_attribution(
    state_path: Path,
    *,
    set_id: str,
    base_url: str,
    token: str,
    batch_size: int = 50,
) -> dict[str, Any]:
    """Enrich existing mutuals with operator attribution via person-attribution API."""
    state = load_state(state_path)
    paths = state_paths(state)
    mutuals = read_jsonl(Path(paths["mutuals_jsonl"]))

    # Collect unique person_ids that have been resolved
    person_ids = list({
        str(m["mutual_person_id"])
        for m in mutuals
        if m.get("mutual_person_id") and str(m["mutual_person_id"]) != "None"
    })

    if not person_ids:
        return {"status": "completed", "enriched": 0, "reason": "no resolved mutual person_ids"}

    # Batch call person-attribution API
    all_attributions: dict[str, dict] = {}
    for i in range(0, len(person_ids), batch_size):
        batch = person_ids[i:i + batch_size]
        try:
            results = api_call(
                "POST",
                f"/v2/sets/{set_id}/person-attribution",
                {"person_ids": batch},
                base_url=base_url,
                token=token,
            )
            for attr in results:
                pid = attr.get("person_id")
                if pid:
                    all_attributions[pid] = attr
        except Exception as e:
            print(f"warning: attribution batch {i//batch_size} failed: {e}", file=sys.stderr)

    # Merge attribution into mutuals
    enriched_count = 0
    for m in mutuals:
        pid = str(m.get("mutual_person_id") or "")
        attr = all_attributions.get(pid)
        if not attr:
            continue
        enriched_count += 1
        m["total_interactions"] = max(
            int(m.get("total_interactions") or 0),
            int(attr.get("total_interactions") or 0),
        )
        # Merge operators from attribution
        existing_ops = {op.get("operator_id") for op in (m.get("operators") or []) if isinstance(op, dict)}
        for op in attr.get("operators") or []:
            if op.get("operator_id") not in existing_ops:
                m.setdefault("operators", []).append({
                    "operator_id": op["operator_id"],
                    "operator_name": op.get("operator_name", ""),
                    "source_channels": op.get("channels", []),
                })
        # Add source badges
        m["source_badges"] = attr.get("sources", [])

    # Write back
    write_jsonl(Path(paths["mutuals_jsonl"]), mutuals)

    return {
        "status": "completed",
        "mode": "attribution",
        "total_mutuals": len(mutuals),
        "resolved_person_ids": len(person_ids),
        "attribution_found": len(all_attributions),
        "enriched": enriched_count,
    }


# ---------------------------------------------------------------------------
# Phase 2: Full mutual discovery + attribution
# ---------------------------------------------------------------------------

def discover_mutuals(
    state_path: Path,
    *,
    set_id: str,
    base_url: str,
    token: str,
    max_leads: int = 25,
    stagger: float = 2.0,
    min_mutual_count: int = 2,
) -> dict[str, Any]:
    """Discover full mutual connections for top leads, then enrich with attribution."""
    state = load_state(state_path)
    paths = state_paths(state)
    leads = read_jsonl(Path(paths["leads_jsonl"]))
    mutuals = read_jsonl(Path(paths["mutuals_jsonl"]))

    # Select leads worth discovering: have profile_id and mutual_count above threshold
    candidates = [
        lead for lead in leads
        if lead.get("profile_id")
        and int(lead.get("total_mutual_count") or lead.get("mutual_count") or 0) >= min_mutual_count
    ]
    # Sort by mutual count descending, take top N
    candidates.sort(key=lambda l: int(l.get("total_mutual_count") or l.get("mutual_count") or 0), reverse=True)
    candidates = candidates[:max_leads]

    if not candidates:
        return {"status": "completed", "discovered": 0, "reason": "no leads with enough mutuals"}

    # Batch discover (API takes up to 10 profile_ids)
    all_discovered: dict[str, list[dict]] = {}
    profile_ids = [lead["profile_id"] for lead in candidates]
    batch_size = 10
    api_calls = 0

    for i in range(0, len(profile_ids), batch_size):
        batch = profile_ids[i:i + batch_size]

        # Stagger between batches
        if api_calls > 0:
            delay = stagger + random.uniform(0, 1)
            time.sleep(delay)

        try:
            result = api_call(
                "POST",
                "/v2/network/mutuals/discover",
                {"profile_ids": batch},
                base_url=base_url,
                token=token,
                timeout=120,  # Discovery is slow
            )
            api_calls += 1

            # Parse results
            for pid, lead_result in (result.get("results") or result).items():
                connections = lead_result.get("shared_connections") or []
                all_discovered[pid] = connections
        except Exception as e:
            print(f"warning: discover batch {i//batch_size} failed: {e}", file=sys.stderr)

    # Build new mutual rows from discovered connections
    new_mutual_count = 0
    existing_edges = {
        (str(m.get("lead_member_id")), str(m.get("mutual_member_id")))
        for m in mutuals
    }

    for lead in candidates:
        pid = lead["profile_id"]
        connections = all_discovered.get(pid, [])
        lead_mid = str(lead.get("member_id") or "")

        for conn in connections:
            mutual_mid = str(conn.get("member_id") or "")
            if not mutual_mid or (lead_mid, mutual_mid) in existing_edges:
                continue

            operators = []
            for op in conn.get("operators") or []:
                operators.append({
                    "operator_id": op.get("operator_id", ""),
                    "operator_name": op.get("operator_name", ""),
                    "source_channels": op.get("source_channels") or op.get("channels") or [],
                })

            mutuals.append({
                "conversation_id": lead.get("conversation_id", ""),
                "set_id": lead.get("set_id", ""),
                "artifact_id": lead.get("artifact_id", ""),
                "lead_member_id": lead_mid,
                "lead_name": lead.get("name"),
                "lead_linkedin_url": lead.get("linkedin_url"),
                "mutual_member_id": mutual_mid,
                "mutual_name": conn.get("name") or conn.get("first_name"),
                "mutual_person_id": conn.get("person_id"),
                "mutual_linkedin_url": conn.get("linkedin_url"),
                "total_interactions": int(conn.get("total_interactions") or 0),
                "source_account_ids": conn.get("source_account_ids") or [],
                "operators": operators,
                "first_seen_at": now_iso(),
                "last_seen_at": now_iso(),
                "times_seen": 1,
            })
            new_mutual_count += 1
            existing_edges.add((lead_mid, mutual_mid))

    # Write back
    write_jsonl(Path(paths["mutuals_jsonl"]), mutuals)

    # Update lead mutual counts
    mutual_counts: dict[str, int] = {}
    for m in mutuals:
        lmid = str(m.get("lead_member_id") or "")
        mutual_counts[lmid] = mutual_counts.get(lmid, 0) + 1

    for lead in leads:
        lmid = str(lead.get("member_id") or "")
        if lmid in mutual_counts:
            lead["mutual_count"] = max(int(lead.get("mutual_count") or 0), mutual_counts[lmid])

    write_jsonl(Path(paths["leads_jsonl"]), leads)

    # Now run Phase 1 attribution on the enriched mutuals
    attr_result = enrich_attribution(
        state_path,
        set_id=set_id,
        base_url=base_url,
        token=token,
    )

    return {
        "status": "completed",
        "mode": "discover",
        "leads_processed": len(candidates),
        "api_calls": api_calls,
        "new_mutuals_discovered": new_mutual_count,
        "total_mutuals": len(mutuals),
        "attribution": attr_result,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich sales-nav mutuals with operator attribution")
    parser.add_argument("--state", required=True, help="Sales-nav run state JSON")
    parser.add_argument("--mode", choices=["attribution", "discover"], default="attribution",
                        help="attribution = enrich existing mutuals; discover = full mutual discovery + attribution")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--set-id", default=None, help="Powerset set UUID (reads from state if not provided)")
    parser.add_argument("--api-base", default=None, help="API base URL (default from env)")
    # Phase 2 options
    parser.add_argument("--max-leads", type=int, default=25, help="Max leads to discover mutuals for")
    parser.add_argument("--stagger", type=float, default=2.0, help="Seconds between discover API batches")
    parser.add_argument("--min-mutual-count", type=int, default=2, help="Min mutual count to trigger discovery")
    args = parser.parse_args()

    load_env(args.env_file)

    state_path = Path(args.state)
    state = load_state(state_path)
    set_id = args.set_id or state.get("set_id") or os.environ.get("POWERPACKS_DEFAULT_SET_ID")
    if not set_id:
        print(json.dumps({"status": "failed", "error": "No set_id provided or found in state/env"}))
        raise SystemExit(1)

    try:
        base_url = resolve_api_base(args.api_base)
    except SystemExit as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        raise SystemExit(2) from exc
    token = get_auth_token()

    if args.mode == "attribution":
        result = enrich_attribution(state_path, set_id=set_id, base_url=base_url, token=token)
    else:
        result = discover_mutuals(
            state_path,
            set_id=set_id,
            base_url=base_url,
            token=token,
            max_leads=args.max_leads,
            stagger=args.stagger,
            min_mutual_count=args.min_mutual_count,
        )

    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
