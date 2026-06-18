#!/usr/bin/env python3
"""Export and inspect Powerpacks search result artifacts."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packs.shared.csv_io import CsvIO  # noqa: E402


CSV_FIELDS = [
    "rank",
    "person_id",
    "result_index",
    "final_score",
    "trait_scores",
    "overall_reasoning",
    "matched_position_indexes",
    "pre_rerank_score",
    "tags",
    "vertical_sources",
    "name",
    "headline",
    "location",
    "current_titles",
    "current_companies",
    "source_operator",
    "source_channel",
    "linkedin_url",
    "hydrated",
    "source_run",
    "source_query",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"{path}:{line_number}: invalid JSONL") from exc
            if not isinstance(row, dict):
                raise RuntimeError(f"{path}:{line_number}: expected object")
            rows.append(row)
    return rows


def append_event(state_path: Path, event: dict[str, Any]) -> None:
    log_path = state_path.with_suffix(state_path.suffix + ".events.jsonl")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def step_output(state: dict[str, Any], step_id: str) -> dict[str, Any]:
    for step in reversed(state.get("steps", [])):
        if step.get("id") == step_id:
            return step.get("output", {}) or {}
    return {}


def hydrated_profiles(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    output = step_output(state, "hydrate_people")
    rows = read_jsonl(Path(str(output["profiles_path"]))) if output.get("profiles_path") else output.get("profiles", []) or []
    profiles = {}
    for profile in rows:
        person_id = profile.get("person_id")
        if person_id:
            profiles[person_id] = profile
    return profiles


def rerank_rows(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    output = step_output(state, "llm_rerank_candidates")
    artifacts = output.get("artifacts", {}) or {}
    path = artifacts.get("query_results_csv") or output.get("query_results_csv")
    if not path:
        return {}

    rows: dict[str, dict[str, Any]] = {}
    with Path(str(path)).open(newline="") as handle:
        for row in CsvIO.dict_reader(handle):
            person_id = row.get("person_id")
            if person_id:
                rows[person_id] = row
    return rows


def frontier_ids(state: dict[str, Any]) -> list[str]:
    llm_rerank = step_output(state, "llm_rerank_candidates")
    ids = llm_rerank.get("ranked_candidate_ids") or []
    if ids:
        return list(dict.fromkeys(ids))

    llm_filter = step_output(state, "llm_filter_candidates")
    ids = llm_filter.get("passed_candidate_ids") or []
    if ids:
        return list(dict.fromkeys(ids))

    merge = step_output(state, "merge_candidate_frontier")
    ids = merge.get("frontier_candidate_ids") or []
    if ids:
        return list(dict.fromkeys(ids))

    role_search = step_output(state, "execute_role_search")
    ids = role_search.get("candidate_ids") or []
    if ids:
        return list(dict.fromkeys(ids))

    slice_search = step_output(state, "execute_search_slice")
    ids = slice_search.get("candidate_ids") or []
    if ids:
        return list(dict.fromkeys(ids))

    direct = step_output(state, "direct_execute")
    ids = direct.get("person_ids") or direct.get("candidate_ids") or []
    if ids:
        return list(dict.fromkeys(ids))

    hydrate = step_output(state, "hydrate_people")
    ids = hydrate.get("profile_ids") or []
    if ids:
        return list(dict.fromkeys(ids))
    return [p["person_id"] for p in hydrate.get("profiles", []) or [] if p.get("person_id")]


def _position_sort_key(position: dict[str, Any]) -> str:
    """Recency key (higher sorts as more recent):
    - ended role: rank by end_date
    - ongoing role (start present, no end): boost above ended roles
    - no dates at all: rank last (no recency signal), below dated roles
    """
    end = position.get("end_date")
    start = position.get("start_date")
    if end:
        return f"5-{end}"
    if start:
        # Ongoing/unknown end but we know it started: rank above ended roles.
        return f"9-{start}"
    # No dates: lowest priority.
    return "0-"


def _has_dates(position: dict[str, Any]) -> bool:
    return bool(position.get("start_date") or position.get("end_date"))


def compact_positions(profile: dict[str, Any]) -> tuple[str, str]:
    all_positions = [p for p in (profile.get("positions", []) or []) if isinstance(p, dict)]
    positions = profile.get("current_positions") or []
    if not positions:
        # 1. Explicit is_current flag (when the source set it reliably).
        positions = [p for p in all_positions if p.get("is_current")]
    if not positions:
        # 2. Ongoing role: has a start date but no end date. This is the most
        #    reliable "current" signal in practice — the is_current boolean is
        #    frequently missing/false even for present roles.
        positions = [p for p in all_positions if p.get("start_date") and not p.get("end_date")]
    if not positions and all_positions:
        # 3. Fallback: most recent position(s) by date so display/merge/shortlist
        #    show a role instead of going blank. Ignore positions with no dates.
        dated = [p for p in all_positions if _has_dates(p)] or all_positions
        ranked = sorted(dated, key=_position_sort_key, reverse=True)
        top_key = _position_sort_key(ranked[0])
        positions = [p for p in ranked if _position_sort_key(p) == top_key]
    titles = []
    companies = []
    for position in positions:
        if not isinstance(position, dict):
            continue
        title = position.get("title") or position.get("position_title")
        company = position.get("company") or position.get("company_name")
        if title:
            titles.append(str(title))
        if company:
            companies.append(str(company))
    return "; ".join(titles), "; ".join(companies)


def result_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    profiles = hydrated_profiles(state)
    rerank_by_id = rerank_rows(state)
    ids = frontier_ids(state)
    if not ids:
        ids = list(profiles)

    rows = []
    for rank, person_id in enumerate(ids, start=1):
        profile = profiles.get(person_id, {})
        rerank = rerank_by_id.get(person_id, {})
        titles, companies = compact_positions(profile)
        rows.append({
            "rank": rank,
            "person_id": person_id,
            "result_index": rerank.get("result_index", ""),
            "final_score": rerank.get("final_score", ""),
            "trait_scores": rerank.get("trait_scores", ""),
            "overall_reasoning": rerank.get("overall_reasoning", ""),
            "matched_position_indexes": rerank.get("matched_position_indexes", ""),
            "pre_rerank_score": rerank.get("pre_rerank_score", ""),
            "tags": rerank.get("tags", ""),
            "vertical_sources": rerank.get("vertical_sources") or "; ".join(profile.get("vertical_sources") or []),
            "name": profile.get("name", ""),
            "headline": profile.get("headline", ""),
            "location": profile.get("location", ""),
            "current_titles": titles,
            "current_companies": companies,
            "source_operator": "; ".join(profile.get("source_operators", []) or []),
            "source_channel": "; ".join(profile.get("source_channels", []) or []),
            "linkedin_url": profile.get("linkedin_url", ""),
            "hydrated": bool(profile),
            "source_run": state.get("task_id", ""),
            "source_query": state.get("query", ""),
        })
    return rows


def default_artifact_dir(state_path: Path, state: dict[str, Any]) -> Path:
    return state_path.parent / "artifacts" / state.get("task_id", state_path.stem)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def update_state_artifacts(state_path: Path, state: dict[str, Any], artifacts: dict[str, Any]) -> None:
    state["artifacts"] = artifacts
    state["updated_at"] = now_iso()
    write_json(state_path, state)
    append_event(state_path, {
        "event": "persist_results",
        "task_id": state.get("task_id"),
        "state": str(state_path),
        "timestamp": state["updated_at"],
        "artifact_dir": artifacts.get("artifact_dir"),
        "csv": artifacts.get("csv"),
        "jsonl": artifacts.get("jsonl"),
        "manifest": artifacts.get("manifest"),
        "row_count": artifacts.get("row_count"),
    })


def cmd_export(args: argparse.Namespace) -> None:
    state_path = Path(args.state)
    state = read_json(state_path)
    rows = result_rows(state)
    out_dir = Path(args.out_dir) if args.out_dir else default_artifact_dir(state_path, state)
    stem = args.name or state.get("task_id") or state_path.stem

    csv_path = out_dir / f"{stem}.csv"
    jsonl_path = out_dir / f"{stem}.jsonl"
    manifest_path = out_dir / f"{stem}.manifest.json"

    write_csv(csv_path, rows)
    write_jsonl(jsonl_path, rows)

    manifest = {
        "task_id": state.get("task_id"),
        "query": state.get("query"),
        "created_at": now_iso(),
        "state": str(state_path),
        "artifact_dir": str(out_dir),
        "csv": str(csv_path),
        "jsonl": str(jsonl_path),
        "row_count": len(rows),
        "hydrated_count": sum(1 for row in rows if row["hydrated"]),
        "frontier_count": len(rows),
    }
    write_json(manifest_path, manifest)
    manifest["manifest"] = str(manifest_path)
    update_state_artifacts(state_path, state, manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))


def truncate(value: Any, width: int) -> str:
    text = str(value or "")
    return text if len(text) <= width else text[: max(0, width - 1)] + "~"


def cmd_view(args: argparse.Namespace) -> None:
    state = read_json(Path(args.state))
    rows = result_rows(state)[: args.limit]
    columns = [
        ("rank", 4),
        ("name", 24),
        ("headline", 32),
        ("current_companies", 24),
        ("location", 28),
        ("hydrated", 8),
    ]
    header = "  ".join(name.ljust(width) for name, width in columns)
    print(header)
    print("-" * len(header))
    for row in rows:
        print("  ".join(truncate(row.get(name), width).ljust(width) for name, width in columns))


def main() -> None:
    parser = argparse.ArgumentParser(description="Export or inspect Powerpacks search results")
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export")
    export.add_argument("--state", required=True)
    export.add_argument("--out-dir")
    export.add_argument("--name")
    export.set_defaults(func=cmd_export)

    view = sub.add_parser("view")
    view.add_argument("--state", required=True)
    view.add_argument("--limit", type=int, default=25)
    view.set_defaults(func=cmd_view)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
