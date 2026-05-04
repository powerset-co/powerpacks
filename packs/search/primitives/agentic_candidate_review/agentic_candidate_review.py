#!/usr/bin/env python3
"""Prepare and reduce sharded agentic candidate reviews.

This primitive does not call an LLM. It creates deterministic shard artifacts
that Codex, Claude Code, or another host harness can review in parallel, then
reduces their JSONL outputs into ranked artifacts.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REVIEW_FIELDS = [
    "rank",
    "person_id",
    "score",
    "decision",
    "reviewer_id",
    "name",
    "headline",
    "location",
    "evidence",
    "concerns",
    "source_shard",
]

DEFAULT_RUBRIC = """Score candidates for the user's recruiting search.

Use 0.0-1.0 relevance:
- 0.9-1.0: excellent fit with direct evidence
- 0.7-0.8: strong fit with minor uncertainty
- 0.5-0.6: plausible but mixed or incomplete evidence
- 0.3-0.4: weak fit; keep only if recall matters
- 0.0-0.2: not relevant

Prefer concise evidence grounded in the hydrated profile. Do not invent facts.
"""


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


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open() as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"{path}:{line_number}: invalid JSONL") from exc
            if not isinstance(row, dict):
                raise RuntimeError(f"{path}:{line_number}: review row must be an object")
            rows.append(row)
    return rows


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


def hydrated_profiles(state: dict[str, Any]) -> list[dict[str, Any]]:
    output = step_output(state, "hydrate_people")
    profiles = output.get("profiles") or []
    if not isinstance(profiles, list):
        raise RuntimeError("hydrate_people output profiles must be a list")
    return [profile for profile in profiles if isinstance(profile, dict) and profile.get("person_id")]


def frontier_ids(state: dict[str, Any]) -> list[str]:
    for step_id, keys in [
        ("llm_filter_candidates", ["passed_candidate_ids"]),
        ("merge_candidate_frontier", ["frontier_candidate_ids"]),
        ("execute_role_search", ["candidate_ids"]),
        ("execute_search_slice", ["candidate_ids"]),
        ("direct_execute", ["person_ids", "candidate_ids"]),
    ]:
        output = step_output(state, step_id)
        for key in keys:
            ids = output.get(key) or []
            if ids:
                return list(dict.fromkeys(str(pid) for pid in ids if pid))
    return [str(profile["person_id"]) for profile in hydrated_profiles(state)]


def ordered_profiles(state: dict[str, Any], limit: int | None) -> list[dict[str, Any]]:
    profiles_by_id = {str(profile["person_id"]): profile for profile in hydrated_profiles(state)}
    ids = frontier_ids(state)
    profiles = [profiles_by_id[pid] for pid in ids if pid in profiles_by_id]
    if limit:
        profiles = profiles[:limit]
    return profiles


def compact_profile(profile: dict[str, Any]) -> dict[str, Any]:
    positions = profile.get("positions") or []
    current_positions = profile.get("current_positions") or []
    return {
        "person_id": profile.get("person_id"),
        "name": profile.get("name"),
        "headline": profile.get("headline"),
        "location": profile.get("location"),
        "linkedin_url": profile.get("linkedin_url"),
        "years_of_experience": profile.get("years_of_experience"),
        "current_positions": current_positions[:5] if isinstance(current_positions, list) else [],
        "positions": positions[:20] if isinstance(positions, list) else [],
        "education": (profile.get("education") or [])[:8],
        "tech_skills": profile.get("tech_skills") or [],
        "trait_scores": profile.get("trait_scores") or {},
        "base_score": profile.get("base_score"),
    }


def chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def default_artifact_dir(state_path: Path, state: dict[str, Any]) -> Path:
    artifacts = state.get("artifacts") or {}
    if artifacts.get("artifact_dir"):
        return Path(str(artifacts["artifact_dir"])) / "agentic_candidate_review"
    return state_path.parent / "artifacts" / str(state.get("task_id") or state_path.stem) / "agentic_candidate_review"


def load_rubric(args: argparse.Namespace) -> str:
    if args.rubric_file:
        return Path(args.rubric_file).read_text()
    if args.rubric_text:
        return args.rubric_text
    return DEFAULT_RUBRIC


def write_reviewer_instructions(path: Path) -> None:
    path.write_text(
        """# Reviewer Instructions

Review exactly one shard file and write one JSONL row per candidate.

Required output fields:

- `person_id`
- `score`: number from 0.0 to 1.0
- `decision`: one of `strong_yes`, `yes`, `maybe`, `no`
- `evidence`: short grounded explanation
- `concerns`: short caveat or empty string

Do not reorder candidates inside the shard output. Do not omit candidates.
Do not invent profile facts. If evidence is missing, lower the score and say so.
""",
    )


def upsert_review_artifacts(state: dict[str, Any], review_artifacts: dict[str, Any]) -> None:
    artifacts = state.setdefault("artifacts", {})
    existing = artifacts.get("agentic_candidate_review")
    if isinstance(existing, dict):
        artifacts["agentic_candidate_review"] = {**existing, **review_artifacts}
    else:
        artifacts["agentic_candidate_review"] = review_artifacts


def record_prepare_step(state_path: Path, state: dict[str, Any], manifest: dict[str, Any], manifest_path: Path) -> None:
    now = now_iso()
    review_artifacts = {
        "status": "prepared",
        "manifest": str(manifest_path),
        "artifact_dir": manifest["artifact_dir"],
        "reviewer_instructions": manifest["reviewer_instructions"],
        "review_outputs_dir": manifest["review_outputs_dir"],
        "candidate_count": manifest["candidate_count"],
        "shard_count": manifest["shard_count"],
        "shard_size": manifest["shard_size"],
        "shards": manifest["shards"],
    }
    upsert_review_artifacts(state, review_artifacts)
    state.setdefault("steps", []).append({
        "id": "agentic_candidate_review_prepare",
        "status": "completed",
        "recorded_at": now,
        "output": {
            "mode": "agentic_candidate_review",
            **review_artifacts,
        },
    })
    state["updated_at"] = now
    write_json(state_path, state)
    append_event(state_path, {
        "event": "record_step",
        "task_id": state.get("task_id"),
        "state": str(state_path),
        "step_id": "agentic_candidate_review_prepare",
        "status": "completed",
        "timestamp": now,
        "manifest": str(manifest_path),
        "shard_count": manifest["shard_count"],
        "candidate_count": manifest["candidate_count"],
    })


def cmd_prepare(args: argparse.Namespace) -> None:
    state_path = Path(args.state)
    state = read_json(state_path)
    profiles = ordered_profiles(state, args.limit)
    out_dir = Path(args.out_dir) if args.out_dir else default_artifact_dir(state_path, state)
    shard_dir = out_dir / "review_shards"
    output_dir = out_dir / "review_outputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    rubric = load_rubric(args)
    shards = []
    for index, batch in enumerate(chunked(profiles, args.shard_size)):
        shard_id = f"{index:04d}"
        shard_path = shard_dir / f"{shard_id}.json"
        candidate_ids = [str(profile["person_id"]) for profile in batch]
        write_json(
            shard_path,
            {
                "schema_version": 1,
                "shard_id": shard_id,
                "task_id": state.get("task_id"),
                "state": str(state_path),
                "query": state.get("query"),
                "rubric": rubric,
                "candidate_count": len(batch),
                "candidate_ids": candidate_ids,
                "candidates": [compact_profile(profile) for profile in batch],
                "expected_output_jsonl": str(output_dir / f"{shard_id}.jsonl"),
            },
        )
        shards.append({
            "shard_id": shard_id,
            "path": str(shard_path),
            "output_jsonl": str(output_dir / f"{shard_id}.jsonl"),
            "candidate_count": len(batch),
            "candidate_ids": candidate_ids,
        })

    instructions_path = out_dir / "reviewer_instructions.md"
    write_reviewer_instructions(instructions_path)
    manifest = {
        "schema_version": 1,
        "created_at": now_iso(),
        "mode": "agentic_candidate_review",
        "task_id": state.get("task_id"),
        "query": state.get("query"),
        "state": str(state_path),
        "artifact_dir": str(out_dir),
        "reviewer_instructions": str(instructions_path),
        "review_outputs_dir": str(output_dir),
        "candidate_count": len(profiles),
        "shard_size": args.shard_size,
        "shard_count": len(shards),
        "shards": shards,
    }
    manifest_path = out_dir / "review_manifest.json"
    write_json(manifest_path, manifest)
    if args.write_state:
        record_prepare_step(state_path, state, manifest, manifest_path)
    print(json.dumps({**manifest, "manifest": str(manifest_path)}, indent=2, sort_keys=True))


def validate_review(row: dict[str, Any], *, path: Path) -> dict[str, Any]:
    person_id = str(row.get("person_id") or "").strip()
    if not person_id:
        raise RuntimeError(f"{path}: review row missing person_id")
    try:
        score = float(row.get("score"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{path}: review row has invalid score for {person_id}") from exc
    decision = str(row.get("decision") or "").strip()
    if decision not in {"strong_yes", "yes", "maybe", "no"}:
        raise RuntimeError(f"{path}: review row has invalid decision for {person_id}: {decision}")
    return {
        **row,
        "person_id": person_id,
        "score": max(0.0, min(1.0, score)),
        "decision": decision,
        "evidence": str(row.get("evidence") or ""),
        "concerns": str(row.get("concerns") or ""),
    }


def profile_lookup_from_manifest(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    for shard in manifest.get("shards") or []:
        shard_path = Path(str(shard["path"]))
        shard_data = read_json(shard_path)
        for profile in shard_data.get("candidates") or []:
            if isinstance(profile, dict) and profile.get("person_id"):
                profiles[str(profile["person_id"])] = profile
    return profiles


def reduce_reviews(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    profiles = profile_lookup_from_manifest(manifest)
    original_rank = {pid: rank for rank, pid in enumerate(profiles, start=1)}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for shard in manifest.get("shards") or []:
        output_path = Path(str(shard["output_jsonl"]))
        if not output_path.exists():
            raise RuntimeError(f"missing review output: {output_path}")
        for raw_row in read_jsonl(output_path):
            row = validate_review(raw_row, path=output_path)
            pid = row["person_id"]
            if pid in seen:
                raise RuntimeError(f"duplicate review for person_id {pid}")
            seen.add(pid)
            profile = profiles.get(pid) or {}
            rows.append({
                **row,
                "name": profile.get("name", ""),
                "headline": profile.get("headline", ""),
                "location": profile.get("location", ""),
                "source_shard": shard.get("shard_id"),
                "_original_rank": original_rank.get(pid, math.inf),
            })

    expected = {
        str(pid)
        for shard in manifest.get("shards") or []
        for pid in shard.get("candidate_ids") or []
    }
    missing = sorted(expected - seen)
    if missing:
        raise RuntimeError(f"missing reviews for {len(missing)} candidates: {missing[:10]}")

    rows.sort(key=lambda row: (-float(row["score"]), row["_original_rank"]))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
        row.pop("_original_rank", None)
    return rows


def write_review_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def record_step(state_path: Path, output: dict[str, Any]) -> None:
    state = read_json(state_path)
    now = now_iso()
    upsert_review_artifacts(state, {
        "status": "completed",
        "manifest": output.get("manifest"),
        **(output.get("artifacts") or {}),
        "reviewed_count": output.get("reviewed_count"),
        "strong_yes_count": output.get("strong_yes_count"),
        "yes_count": output.get("yes_count"),
        "maybe_count": output.get("maybe_count"),
        "no_count": output.get("no_count"),
    })
    state.setdefault("steps", []).append({
        "id": "agentic_candidate_review_reduce",
        "status": "completed",
        "recorded_at": now,
        "output": output,
    })
    state["updated_at"] = now
    write_json(state_path, state)
    append_event(state_path, {
        "event": "record_step",
        "task_id": state.get("task_id"),
        "state": str(state_path),
        "step_id": "agentic_candidate_review_reduce",
        "status": "completed",
        "timestamp": now,
        "reviewed_count": output.get("reviewed_count"),
        "ranked_jsonl": output.get("artifacts", {}).get("ranked_jsonl"),
    })


def cmd_reduce(args: argparse.Namespace) -> None:
    manifest_path = Path(args.manifest)
    manifest = read_json(manifest_path)
    rows = reduce_reviews(manifest)
    out_dir = Path(args.out_dir) if args.out_dir else Path(str(manifest["artifact_dir"]))
    ranked_jsonl = out_dir / "ranked_candidates.jsonl"
    ranked_csv = out_dir / "ranked_candidates.csv"
    summary_path = out_dir / "review_summary.json"
    write_jsonl(ranked_jsonl, rows)
    write_review_csv(ranked_csv, rows)

    output = {
        "mode": "agentic_candidate_review",
        "manifest": str(manifest_path),
        "reviewed_count": len(rows),
        "strong_yes_count": sum(1 for row in rows if row["decision"] == "strong_yes"),
        "yes_count": sum(1 for row in rows if row["decision"] == "yes"),
        "maybe_count": sum(1 for row in rows if row["decision"] == "maybe"),
        "no_count": sum(1 for row in rows if row["decision"] == "no"),
        "artifacts": {
            "ranked_jsonl": str(ranked_jsonl),
            "ranked_csv": str(ranked_csv),
            "summary": str(summary_path),
        },
    }
    write_json(summary_path, output)
    if args.write_state:
        record_step(Path(str(manifest["state"])), output)
    print(json.dumps(output, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare or reduce sharded candidate reviews")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--state", required=True)
    prepare.add_argument("--out-dir")
    prepare.add_argument("--shard-size", type=int, default=25)
    prepare.add_argument("--limit", type=int)
    prepare.add_argument("--rubric-file")
    prepare.add_argument("--rubric-text")
    prepare.add_argument("--write-state", action=argparse.BooleanOptionalAction, default=True)
    prepare.set_defaults(func=cmd_prepare)

    reduce = sub.add_parser("reduce")
    reduce.add_argument("--manifest", required=True)
    reduce.add_argument("--out-dir")
    reduce.add_argument("--write-state", action="store_true")
    reduce.set_defaults(func=cmd_reduce)

    args = parser.parse_args()
    if getattr(args, "shard_size", 1) < 1:
        raise SystemExit("--shard-size must be >= 1")
    args.func(args)


if __name__ == "__main__":
    main()
