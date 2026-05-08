#!/usr/bin/env python3
"""Conservative LLM filter for hydrated Powerpacks search candidates."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any


LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB_DIR))

from powerpacks_contracts import validate_hydrated_profile  # noqa: E402


RESULT_FILTER_BATCH_SYSTEM_PROMPT = """You are a fast pre-screener filtering search results.

Given a search query, expected traits, and multiple candidate profiles, determine which should be reviewed further.

=== FILTERING CONSEQUENCES ===

Your filtering decisions directly impact candidate quality. Mistakes waste time and money:

FALSE NEGATIVES (filtering out good candidates, score <0.3 when they match):
- Qualified people miss opportunities they deserve
- We lose placements to competitors who found them
- Revenue loss, client disappointment
- Filters producing >10% false negatives are replaced

FALSE POSITIVES are less costly at this stage (reranking will catch them), but excessive false positives (>40%) waste downstream compute and will trigger replacement.

When uncertain, INCLUDE the candidate (score 0.4-0.5). It's better to pass a borderline case to reranking than to incorrectly filter someone out.

=== SCORING GUIDE ===

- 0.9-1.0: Clearly matches query/traits - definitely review
- 0.6-0.8: Likely matches, worth reviewing
- 0.4-0.5: Uncertain, borderline - INCLUDE these
- 0.1-0.3: Probably doesn't match - needs clear evidence of mismatch
- 0.0: Clearly doesn't match query/traits at all

Be FAST and DECISIVE. When in doubt, lean towards including (higher score).

For each candidate, output their ID, score (0.0-1.0), and brief reasoning (max 20 words)."""


RESULT_FILTER_BATCH_HUMAN_PROMPT = """Query: {query}

Expected Traits:
{traits_list}

Candidates to filter:
{candidates_profiles}

Score each candidate for relevance."""


DEFAULT_MODEL = os.getenv("POWERPACKS_LLM_FILTER_MODEL", "gpt-4.1-mini")
DEFAULT_THRESHOLD = 0.3
DEFAULT_BATCH_SIZE = 5


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
    merge = step_output(state, "merge_candidate_frontier")
    ids = merge.get("frontier_candidate_ids") or []
    if ids:
        return list(dict.fromkeys(str(pid) for pid in ids if pid))

    direct = step_output(state, "direct_execute")
    ids = direct.get("person_ids") or direct.get("candidate_ids") or []
    if ids:
        return list(dict.fromkeys(str(pid) for pid in ids if pid))

    hydrate = step_output(state, "hydrate_people")
    ids = hydrate.get("profile_ids") or []
    if ids:
        return list(dict.fromkeys(str(pid) for pid in ids if pid))
    return list(dict.fromkeys(str(p["person_id"]) for p in hydrate.get("profiles", []) or [] if p.get("person_id")))


def hydrated_profiles(state: dict[str, Any], *, llm_handoff: bool) -> dict[str, dict[str, Any]]:
    hydrate = step_output(state, "hydrate_people")
    path_key = "llm_profiles_path" if llm_handoff else "profiles_path"
    profiles_path = hydrate.get(path_key) or hydrate.get("profiles_path")
    rows = read_jsonl(Path(str(profiles_path))) if profiles_path else hydrate.get("profiles", []) or []
    profiles: dict[str, dict[str, Any]] = {}
    validation_errors: list[str] = []
    for profile in rows:
        if not isinstance(profile, dict):
            validation_errors.append("hydrated profile must be an object")
            continue
        errors = validate_hydrated_profile(profile)
        if errors:
            validation_errors.extend(f"{profile.get('person_id') or '<unknown>'}: {error}" for error in errors)
        person_id = profile.get("person_id")
        if person_id:
            profiles[str(person_id)] = profile
    if validation_errors:
        sample = "; ".join(validation_errors[:10])
        raise RuntimeError(f"hydrate_people output does not match hydrated profile contract: {sample}")
    return profiles


def artifact_dir(state_path: Path, state: dict[str, Any]) -> Path:
    existing = state.get("artifacts") or {}
    if existing.get("artifact_dir"):
        return Path(str(existing["artifact_dir"]))
    return state_path.parent / "artifacts" / str(state.get("task_id") or state_path.stem)


def batches(items: list[str], batch_size: int) -> list[list[str]]:
    return [items[i:i + batch_size] for i in range(0, len(items), batch_size)]


def role_filters_from_state(state: dict[str, Any]) -> dict[str, Any]:
    expand_request = step_output(state, "expand_search_request")
    if isinstance(expand_request.get("role_search_filters"), dict):
        return expand_request["role_search_filters"]
    expand = step_output(state, "expand")
    return expand if isinstance(expand, dict) else {}


def use_compact_profiles(args: argparse.Namespace, state: dict[str, Any]) -> bool:
    if args.profile_scope == "current":
        return True
    if args.profile_scope == "all":
        return False
    if args.current_and_matched_only:
        return True
    role_filters = role_filters_from_state(state)
    return role_filters.get("is_current_role") is True


def trait_lines(state: dict[str, Any]) -> str:
    expand = role_filters_from_state(state)
    lines: list[str] = []
    role_query = expand.get("role_semantic_query")
    if role_query:
        lines.append(f"- Role search: {role_query}")
    for key in [
        "role_tracks",
        "seniority_bands",
        "cities",
        "metro_areas",
        "company_names",
        "company_semantic_queries",
        "sector_types",
        "entity_types",
        "years_experience_min",
        "years_experience_max",
        "age_min",
        "age_max",
    ]:
        value = expand.get(key)
        if value not in (None, [], ""):
            lines.append(f"- {key}: {value}")
    return "\n".join(lines) if lines else "Based on the query"


def tag(name: str, value: Any, indent: str = "  ") -> str | None:
    if value in (None, "", [], {}):
        return None
    return f"{indent}<{name}>{escape(str(value))}</{name}>"


def profile_to_xml(profile: dict[str, Any], *, current_and_matched_only: bool = False) -> str:
    person_id = profile.get("person_id") or profile.get("id") or ""
    parts = [f"<person id='{escape(str(person_id))}'>"]
    for key in ["name", "headline", "location", "summary", "inferred_age", "years_of_experience"]:
        line = tag(key, profile.get(key))
        if line:
            parts.append(line)

    positions = profile.get("positions") or []
    if not positions and profile.get("current_positions"):
        positions = [
            {
                "position_title": p.get("title"),
                "company_name": p.get("company"),
                "is_current": True,
            }
            for p in profile.get("current_positions", [])
            if isinstance(p, dict)
        ]

    matched = set(profile.get("matched_position_indexes") or [])
    if current_and_matched_only:
        selected_positions = [
            (idx, pos) for idx, pos in enumerate(positions)
            if isinstance(pos, dict) and (pos.get("is_current") or idx in matched)
        ]
        if not selected_positions and positions:
            selected_positions = [(0, positions[0])]
    else:
        selected_positions = list(enumerate(positions[:20]))
    for idx, pos in selected_positions:
        if not isinstance(pos, dict):
            continue
        status = "current" if pos.get("is_current") else "past"
        if idx in matched:
            status += ", matched"
        parts.append("  <work>")
        title = pos.get("position_title") or pos.get("title")
        if title:
            parts.append(f"    <title>{escape(str(title))} ({status})</title>")
        for key in [
            "seniority_band",
            "role_track",
            "start_date",
            "end_date",
            "dense_text",
            "description",
            "company_name",
            "company_domain",
            "company_description",
            "company_sector_types",
            "company_entity_types",
            "company_stage",
            "company_headcount",
            "company_funding_total",
            "investor_names",
        ]:
            line = tag(key, pos.get(key), indent="    ")
            if line:
                parts.append(line)
        parts.append("  </work>")

    education = profile.get("education") or []
    if education:
        parts.append("  <education>")
        for edu in education[:5]:
            if not isinstance(edu, dict):
                continue
            edu_text = " | ".join(
                str(edu.get(key))
                for key in ["school_name", "degree", "field_of_study", "end_year"]
                if edu.get(key)
            )
            if edu_text:
                parts.append(f"    <school>{escape(edu_text)}</school>")
        parts.append("  </education>")

    skills = profile.get("tech_skills")
    line = tag("tech_skills", ", ".join(skills) if isinstance(skills, list) else skills)
    if line:
        parts.append(line)
    parts.append("</person>")
    return "\n".join(parts)


def response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["candidates"],
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "score", "reason"],
                    "properties": {
                        "id": {"type": "string"},
                        "score": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                },
            }
        },
    }


def call_openai(model: str, system_prompt: str, human_prompt: str) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required unless --dry-run is used")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": human_prompt},
        ],
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "candidate_filter_scores",
                "strict": True,
                "schema": response_schema(),
            },
        },
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI request failed: HTTP {exc.code}: {body}") from exc
    content = data["choices"][0]["message"]["content"]
    return json.loads(content)


def record_step(state_path: Path, state: dict[str, Any], output: dict[str, Any], elapsed_ms: int) -> None:
    now = now_iso()
    state.setdefault("steps", []).append({
        "id": "llm_filter_candidates",
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
        "step_id": "llm_filter_candidates",
        "status": "completed",
        "timestamp": now,
        "elapsed_ms": elapsed_ms,
        "passed_count": output.get("passed_count"),
        "filtered_count": output.get("filtered_count"),
    })


def cmd_filter(args: argparse.Namespace) -> None:
    started = time.time()
    state_path = Path(args.state)
    state = read_json(state_path)
    ids = frontier_ids(state)
    compact_profiles = use_compact_profiles(args, state)
    profiles = hydrated_profiles(state, llm_handoff=compact_profiles)
    missing = [pid for pid in ids if pid not in profiles]
    if missing and not args.allow_partial_hydration:
        raise SystemExit(
            f"hydrate_people must cover the full frontier before LLM filtering: "
            f"{len(missing)}/{len(ids)} missing. Re-run hydrate_people for all frontier IDs "
            f"or pass --allow-partial-hydration."
        )

    filter_ids = [pid for pid in ids if pid in profiles]
    if args.max_candidates:
        filter_ids = filter_ids[: args.max_candidates]
    batch_ids = batches(filter_ids, args.batch_size)
    query = state.get("query") or ""
    traits = args.traits or trait_lines(state)

    out_dir = artifact_dir(state_path, state) / "llm_filter_candidates"

    if args.dry_run:
        print(json.dumps({
            "state": str(state_path),
            "candidate_count": len(filter_ids),
            "missing_hydration_count": len(missing),
            "batch_count": len(batch_ids),
            "batch_size": args.batch_size,
            "model": args.model,
            "threshold": args.threshold,
            "profile_scope": "current" if compact_profiles else "all",
            "would_write_state": args.write_state,
        }, indent=2, sort_keys=True))
        return

    prompt_rows: list[dict[str, Any]] = []

    scores: dict[str, dict[str, Any]] = {}
    for batch_idx, batch in enumerate(batch_ids):
        candidates_profiles = "\n\n".join(
            profile_to_xml(profiles[pid], current_and_matched_only=compact_profiles)
            for pid in batch
        )
        human_prompt = RESULT_FILTER_BATCH_HUMAN_PROMPT.format(
            query=query,
            traits_list=traits,
            candidates_profiles=candidates_profiles,
        )
        prompt_rows.append({
            "batch_index": batch_idx,
            "candidate_ids": batch,
            "prompt": human_prompt,
        })
        try:
            parsed = call_openai(args.model, RESULT_FILTER_BATCH_SYSTEM_PROMPT, human_prompt)
        except Exception:
            if args.on_error == "fail":
                raise
            parsed = {
                "candidates": [
                    {"id": pid, "score": 1.0, "reason": "Error during filtering"}
                    for pid in batch
                ]
            }
        for item in parsed.get("candidates", []) or []:
            pid = str(item.get("id") or "")
            if not pid:
                continue
            try:
                score = float(item.get("score"))
            except (TypeError, ValueError):
                score = 1.0
            scores[pid] = {
                "person_id": pid,
                "score": max(0.0, min(1.0, score)),
                "reason": str(item.get("reason") or "")[:240],
            }

    # Missing model outputs pass through. The filter must be conservative.
    for pid in filter_ids:
        scores.setdefault(pid, {"person_id": pid, "score": 1.0, "reason": "Missing model output; passed conservatively"})

    passed = [pid for pid in ids if pid not in scores or scores[pid]["score"] >= args.threshold]
    filtered = [pid for pid in ids if pid in scores and scores[pid]["score"] < args.threshold]
    score_rows = [scores[pid] for pid in filter_ids if pid in scores]
    filtered_rows = [scores[pid] for pid in filtered if pid in scores]

    artifacts: dict[str, Any] = {}
    if args.dump_debug:
        all_scores_path = out_dir / "scores.jsonl"
        filtered_path = out_dir / "filtered.jsonl"
        prompts_path = out_dir / "batch_prompts.jsonl"
        write_jsonl(all_scores_path, score_rows)
        write_jsonl(filtered_path, filtered_rows)
        write_jsonl(prompts_path, prompt_rows)
        artifacts = {
            "scores_jsonl": str(all_scores_path),
            "filtered_jsonl": str(filtered_path),
            "batch_prompts_jsonl": str(prompts_path),
        }

    output = {
        "model": args.model,
        "threshold": args.threshold,
        "batch_size": args.batch_size,
        "profile_scope": "current" if compact_profiles else "all",
        "candidate_count": len(ids),
        "hydrated_count": len(profiles),
        "scored_count": len(filter_ids),
        "missing_hydration_count": len(missing),
        "passed_count": len(passed),
        "filtered_count": len(filtered),
        "passed_candidate_ids": passed,
        "filtered_candidate_ids": filtered,
        "filtered_results": {pid: scores[pid] for pid in filtered if pid in scores},
        "artifacts": artifacts,
    }

    elapsed_ms = int((time.time() - started) * 1000)
    if args.write_state:
        record_step(state_path, state, output, elapsed_ms)
    print(json.dumps(output, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Filter hydrated candidates with a conservative LLM pre-screen")
    parser.add_argument("--state", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-candidates", type=int)
    parser.add_argument("--traits")
    parser.add_argument("--write-state", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-partial-hydration", action="store_true")
    parser.add_argument("--profile-scope", choices=["auto", "current", "all"], default="auto", help="Profile handoff for filtering: auto uses compact current-role profiles only when role filters are current-scoped")
    parser.add_argument("--current-and-matched-only", action="store_true", help="Backward-compatible alias for --profile-scope current")
    parser.add_argument("--dump-debug", action="store_true", help="Write filter scores/prompts artifacts for debugging")
    parser.add_argument("--on-error", choices=["pass_all", "fail"], default="pass_all")
    args = parser.parse_args()
    cmd_filter(args)


if __name__ == "__main__":
    main()
