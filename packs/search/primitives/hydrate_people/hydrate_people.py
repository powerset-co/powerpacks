#!/usr/bin/env python3
"""Hydrate a Powerpacks frontier from the checked-in Postgres contract."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB_DIR))

from postgres_client import fetch_interaction_counts, fetch_person_rows  # noqa: E402
from powerpacks_contracts import normalize_hydrated_context  # noqa: E402


DEFAULT_ENV_FILE = Path(os.getenv("POWERPACKS_ENV_FILE", ".env"))


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def append_event(state_path: Path, event: dict[str, Any]) -> None:
    event_path = state_path.with_suffix(state_path.suffix + ".events.jsonl")
    event_path.parent.mkdir(parents=True, exist_ok=True)
    with event_path.open("a") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def step_output(state: dict[str, Any], step_id: str) -> dict[str, Any]:
    for step in reversed(state.get("steps", [])):
        if step.get("id") == step_id:
            return step.get("output", {}) or {}
    return {}


def frontier_ids(state: dict[str, Any]) -> list[str]:
    llm_filter = step_output(state, "llm_filter_candidates")
    ids = llm_filter.get("passed_candidate_ids") or []
    if ids:
        return list(dict.fromkeys(str(pid) for pid in ids if pid))

    merge = step_output(state, "merge_candidate_frontier")
    ids = merge.get("frontier_candidate_ids") or []
    if ids:
        return list(dict.fromkeys(str(pid) for pid in ids if pid))

    role_search = step_output(state, "execute_role_search")
    ids = role_search.get("candidate_ids") or []
    if ids:
        return list(dict.fromkeys(str(pid) for pid in ids if pid))

    slice_search = step_output(state, "execute_search_slice")
    ids = slice_search.get("candidate_ids") or []
    if ids:
        return list(dict.fromkeys(str(pid) for pid in ids if pid))

    direct = step_output(state, "direct_execute")
    ids = direct.get("person_ids") or direct.get("candidate_ids") or []
    if ids:
        return list(dict.fromkeys(str(pid) for pid in ids if pid))

    hydrate = step_output(state, "hydrate_people")
    return list(dict.fromkeys(str(p["person_id"]) for p in hydrate.get("profiles", []) or [] if p.get("person_id")))


def base_person_id(value: str) -> str:
    parts = str(value).split("-")
    if len(parts) == 6 and parts[5].isdigit():
        return "-".join(parts[:5])
    return str(value)


def artifact_dir(state_path: Path, state: dict[str, Any]) -> Path:
    existing = state.get("artifacts") or {}
    if existing.get("artifact_dir"):
        return Path(str(existing["artifact_dir"]))
    return state_path.parent / "artifacts" / str(state.get("task_id") or state_path.stem)


def current_profile_view(profile: dict[str, Any]) -> dict[str, Any]:
    """Compact/current-only view for inspecting what LLM filter/rerank should see."""
    positions = profile.get("positions") or []
    matched = set(profile.get("matched_position_indexes") or [])
    selected = []
    for idx, pos in enumerate(positions):
        if not isinstance(pos, dict):
            continue
        if pos.get("is_current") or idx in matched:
            selected.append(pos)
    if not selected and positions:
        selected = [positions[0]]
    return {
        "person_id": profile.get("person_id"),
        "name": profile.get("name"),
        "headline": profile.get("headline"),
        "location": profile.get("location"),
        "linkedin_url": profile.get("linkedin_url"),
        "positions": selected,
        "education": (profile.get("education") or [])[:3],
        "tech_skills": profile.get("tech_skills"),
        "total_interactions": profile.get("total_interactions"),
        "matched_position_indexes": profile.get("matched_position_indexes") or [],
    }


def record_step(state_path: Path, state: dict[str, Any], output: dict[str, Any], elapsed_ms: int) -> None:
    now = now_iso()
    state.setdefault("steps", []).append({
        "id": "hydrate_people",
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
        "step_id": "hydrate_people",
        "status": "completed",
        "timestamp": now,
        "elapsed_ms": elapsed_ms,
        "requested": output.get("requested"),
        "hydrated": output.get("hydrated"),
    })


def cmd_hydrate(args: argparse.Namespace) -> None:
    started = time.time()
    state_path = Path(args.state)
    state = read_json(state_path)
    requested = list(dict.fromkeys(base_person_id(pid) for pid in frontier_ids(state)))
    if args.limit:
        requested = requested[: args.limit]

    if args.dry_run:
        print(json.dumps({
            "state": str(state_path),
            "env_file": str(Path(args.env_file)) if args.env_file else None,
            "requested": len(requested),
            "sample_ids": requested[:10],
            "would_write_state": args.write_state,
        }, indent=2, sort_keys=True))
        return

    env_file = Path(args.env_file) if args.env_file else None
    rows = fetch_person_rows(requested, env_file=env_file)
    interaction_counts = fetch_interaction_counts(requested, env_file=env_file)
    profiles = []
    for row in rows:
        if interaction_counts.get(str(row.get("id"))):
            row["total_interactions"] = interaction_counts[str(row.get("id"))]
        profiles.append(normalize_hydrated_context(row))
    order = {pid: idx for idx, pid in enumerate(requested)}
    profiles.sort(key=lambda profile: order.get(str(profile.get("person_id")), len(order)))

    artifacts: dict[str, Any] = {}
    if state_path:
        out_dir = artifact_dir(state_path, state) / "hydrate_people"
        profiles_json = out_dir / "profiles.json"
        profiles_jsonl = out_dir / "profiles.jsonl"
        current_jsonl = out_dir / "profiles.current_or_matched.jsonl"
        write_json(profiles_json, {"profiles": profiles})
        write_jsonl(profiles_jsonl, profiles)
        write_jsonl(current_jsonl, [current_profile_view(profile) for profile in profiles])
        artifacts = {
            "profiles_json": str(profiles_json),
            "profiles_jsonl": str(profiles_jsonl),
            "current_or_matched_jsonl": str(current_jsonl),
        }

    output = {
        "requested": len(requested),
        "hydrated": len(profiles),
        "profile_ids": [profile.get("person_id") for profile in profiles if profile.get("person_id")],
        "profiles": profiles,
        "artifacts": artifacts,
        "source": {
            "type": "postgres_contract",
            "backend": "postgres_supabase",
            "env_file": str(env_file) if env_file else None,
        },
    }
    elapsed_ms = int((time.time() - started) * 1000)
    if args.write_state:
        record_step(state_path, state, output, elapsed_ms)
    print(json.dumps(output, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Hydrate Powerpacks candidate IDs through the Postgres/Supabase contract")
    parser.add_argument("--state", required=True)
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--write-state", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    cmd_hydrate(args)


if __name__ == "__main__":
    main()
