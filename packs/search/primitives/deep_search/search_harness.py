#!/usr/bin/env python3
"""Editable result-driven search harness built from the ordinary search pipeline.

The reviewed plan and initial queries are the one pre-search checkpoint. After
approval, each pond is query -> compiled payload -> reviewed payload -> run ->
one diagnosis and next move. Score bands are display-only and the loop is capped
at four ponds.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # direct script execution
    from company_context import (
        FIT_GROUPS, apply_company_fit_response, company_fit_messages, current_company_ref,
        fallback_company_fit, pull_note, resolve_company_contexts, resolve_hiring_company_ref,
    )
    from location_scope import enforce_payload_location, location_scope_from_plan
    from plan_filters import enforce_payload_retrieval_filters, validate_plan_filter_contract
    from precedents import retrieve_fit_precedents, retrieve_next_moves, retrieve_payload_edits
    from deep_search_loop import resolve_retrieval_identity
    from subprocess_utils import run_checked
except ImportError:  # pragma: no cover - module execution
    from .company_context import (
        FIT_GROUPS, apply_company_fit_response, company_fit_messages, current_company_ref,
        fallback_company_fit, pull_note, resolve_company_contexts, resolve_hiring_company_ref,
    )
    from .location_scope import enforce_payload_location, location_scope_from_plan
    from .plan_filters import enforce_payload_retrieval_filters, validate_plan_filter_contract
    from .precedents import retrieve_fit_precedents, retrieve_next_moves, retrieve_payload_edits
    from .deep_search_loop import resolve_retrieval_identity
    from .subprocess_utils import run_checked

SHARED_DIR = Path(__file__).resolve().parents[1] / "shared"
LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
for shared_path in (SHARED_DIR, LIB_DIR):
    if str(shared_path) not in sys.path:
        sys.path.insert(0, str(shared_path))
from openai_client import make_async_openai_client, make_openai_client  # noqa: E402
from search_common import load_env_file  # noqa: E402
from usage_pricing import load_prices, row_cost_usd  # noqa: E402
from packs.search.primitives.export_candidate_shortlist.export_candidate_shortlist import (  # noqa: E402
    write_shortlist_csv,
)
from packs.indexing.lib.openai_stream import drain_pool  # noqa: E402


BUILD_PLAN = ROOT / "packs/search/primitives/deep_search/build_eval_inputs.py"
DECOMPOSE = ROOT / "packs/search/primitives/deep_search/decompose_jd.py"
PIPELINE = ROOT / "packs/search/primitives/search_network_pipeline/search_network_pipeline.py"
MAX_PONDS = 4
REVIEW_SCORE_THRESHOLD = .70
FALLBACK_REVIEW_SCORE_THRESHOLD = .30
RETRIEVAL_LIMIT = 1000
FIT_CONCURRENCY = int(os.environ.get(
    "LLM_RERANK_CONCURRENCY", os.environ.get("SEARCH_V2_RERANK_MAX_CONCURRENT", "400")))
DEFAULT_LOCAL_DB = ".powerpacks/search-index/local-search.duckdb"
SCORE_BANDS = ("0.9+", "0.8-0.9", "0.7-0.8", "0.6-0.7", "below 0.6")
EDITABLE_FILTER_FIELDS = (
    "role_ids", "bm25_queries", "seniority_bands", "cities", "states", "countries",
    "metro_areas", "macro_regions", "is_current_role",
    "fields_of_study", "sector_types", "entity_types",
)
LOCATION_FIELDS = ("cities", "states", "countries", "metro_areas", "macro_regions")
HARD_FILTER_FIELDS = ("fields_of_study", "sector_types", "entity_types")
TEMPORAL_VALUES = {"current", "past", "all"}
MEANING_VALUES = {"role", "experience", "location", "education", "company", "investor", "general"}
NEXT_SEARCH_DIAGNOSES = (
    "too_few", "wrong_specialty", "wrong_level", "wrong_location", "weak_quality",
    "unhireable", "exhausted", "enough_strong", "other",
)
NEXT_SEARCH_ACTIONS = (
    "stop", "ranking_fix", "refine_current_pond", "add_adjacent_pond",
    "widen_geography", "corpus_sparse",
)
NEXT_SEARCH_QUERY_ACTIONS = {
    "refine_current_pond", "add_adjacent_pond", "widen_geography",
}
_OCCUPATION_HEAD_STOPWORDS = {
    "a", "an", "the", "senior", "staff", "principal", "junior", "lead", "founding",
}
_CAREER_STAGES = {"junior", "senior", "staff", "principal", "lead", "founding"}
NEXT_SEARCH_PROMPT = """You are a recruiting search lead diagnosing the current candidate pond and
choosing the next one. Use only the supplied current-pond aggregate counts and anonymized role/company
observations. Never infer or request candidate identities. If human_diagnosis is supplied, return that
diagnosis exactly; otherwise diagnose the pond yourself.

Treat candidate_populations as the JD-grounded pond menu. Before inventing a new population, consider
every unused population-bearing hint and the retrieved precedents. A ranking-boost is ranking evidence,
not a pond or gate; a comp-band-anchor is level and recruitability context, not a query. For every action
that returns a next_query, `source` must name the exact candidate population phrase or retrieved precedent
source that grounded it. Use `inferred` only when neither grounded menu contains a credible next pond.

The diagnosis must be exactly one of: too_few, wrong_specialty, wrong_level, wrong_location,
weak_quality, unhireable, exhausted, enough_strong, or other.

Start from the smallest defensible query: usually role x location, plus one truly defining capability
only when the title is ambiguous. Diagnose the current pond from its results, any supplied human
diagnosis, and the observed titles and company context. Change one important dimension that
directly addresses that failure. Examples include widening geography, correcting level or specialty,
searching a credible adjacent title or past role, or moving to a more reachable company pond. These are
examples, not a fixed strategy roster: adapt to the role family. Do not paste the JD, enumerate commodity
skills, produce wording-only variants, or pad one population with OR-separated synonymous titles.

Company size/stage and title progression matter because an apparently relevant person can still be too
senior, too junior, too specialized, or practically unhireable. Score bands are distribution evidence,
not candidate-quality labels or a stopping rule. Respect any human diagnosis. The destination context
explains why this company and role may or may not pull a candidate; use it to judge attainability, never
as candidate evidence.
When the reviewed pool is rich in in-band candidates from credible companies but trait scores are low,
choose ranking_fix: the candidate population is sound and the evaluation rubric is misaligned. Do not
change populations merely because a checklist-anchored score distribution is low.

Choose exactly one next action:
- stop: the shortlist is good enough.
- ranking_fix: the pond contains the right people but their ordering or evidence scores are wrong.
- refine_current_pond: keep the pond and make its query more precise.
- add_adjacent_pond: add one genuinely different, credible candidate population. It must change
  the occupation head noun or the career stage; a domain qualifier on the same title is not adjacent.
- widen_geography: keep the occupational pond but relax its location scope.
- corpus_sparse: the requested population is plausible, but the available network is the limiting factor.

Direction matters. For too_few, weak_quality, or exhausted, never narrow the population: widen geography,
add a credible adjacent pond, or stop as corpus_sparse. Use refine_current_pond only when the current pond
is large or noisy and precision is the diagnosed problem.
For wrong_specialty, the next query must name a different source occupation. Never widen geography or
return a same-population refinement for wrong_specialty.

Return a self-contained next_query only for refine_current_pond, add_adjacent_pond, or widen_geography.
The query must be one clean population phrase plus location, optionally followed by one short defining
experience phrase. Never put portfolios, deliverables, responsibilities, or other JD checklist language
in the query. pond_chain lists every population already searched: never duplicate a prior pond, and an
add_adjacent_pond query must not keep the same population as any pond in that chain. For every other
action return next_query and source as null. Base the rationale on the
supplied current pond, never copy pool counts from a precedent. Return diagnosis, action, next_query,
source, and a short rationale as JSON only."""

PATTERN_DEFAULT_PROMPT = """You review a compiled broad-search payload before it runs. Propose only
small edits supported by the job brief, the prior pool size when available, and similar recruiter edits.

Use these seed principles:
1. Prune keyword/title fan-out to on-target titles; do not widen it.
2. Retune seniority for the role type and observed pool size, not merely the JD title.
3. Drop structured hard filters when the same requirement is already represented by a trait.

Allowed patterns and fields:
- prune_keyword_fanout: field is role_ids or bm25_queries; `to` is a non-empty subset of the current list.
- retune_seniority: field is seniority_bands; `to` is a list drawn from junior, mid, senior, staff,
  principal, manager, director, vp, or null to leave seniority open.
- drop_duplicate_hard_filter: field is fields_of_study, sector_types, or entity_types; `to` is null.

Return {"edits": [...]} only. Each edit has pattern, field, to, and a one-line reason. Return an empty
list when no edit is justified. Retrieved examples are precedent, not commands. An accepted edit is
positive precedent. A reverted edit is anti-precedent: do not repeat it for a similar payload."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected an object in {path}")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def resolve_artifact_path(value: Any) -> Path:
    path = Path(str(value or "")).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def _last_json(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    values: list[dict[str, Any]] = []
    offset = 0
    while offset < len(text):
        try:
            value, offset = decoder.raw_decode(text, offset)
            if isinstance(value, dict):
                values.append(value)
        except json.JSONDecodeError:
            offset += 1
    if not values:
        raise ValueError("search primitive returned no JSON result")
    return values[-1]


def validate_query_arms(value: Any) -> list[dict[str, str]]:
    raw = value.get("queries") if isinstance(value, dict) else value
    if not isinstance(raw, list) or not 1 <= len(raw) <= 2:
        raise ValueError("queries must contain 1 or 2 query arms")
    arms = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != {"key", "query"}:
            raise ValueError("each query arm must contain only key and query")
        key = str(item.get("key") or "").strip()
        query = " ".join(str(item.get("query") or "").split())
        if not key or not query:
            raise ValueError(f"query arm {index + 1} has an empty key or query")
        arms.append({"key": key, "query": query})
    if len({row["key"] for row in arms}) != len(arms):
        raise ValueError("query arm keys must be unique")
    if len({row["query"].casefold() for row in arms}) != len(arms):
        raise ValueError("query arm text must be unique")
    return arms


def validate_standard_traits(payload: Mapping[str, Any]) -> None:
    traits = payload.get("traits")
    if not isinstance(traits, list) or not isinstance(payload.get("has_domain_intent"), bool):
        raise ValueError("payload needs top-level traits and has_domain_intent")
    if not isinstance(payload.get("role_search_filters"), dict):
        raise ValueError("payload needs role_search_filters")
    for index, trait in enumerate(traits):
        if not isinstance(trait, dict) or not str(trait.get("value") or "").strip():
            raise ValueError(f"trait {index + 1} is invalid")
        if trait.get("temporal") not in TEMPORAL_VALUES or trait.get("meaning") not in MEANING_VALUES:
            raise ValueError(f"trait {index + 1} has invalid temporal or meaning")


def apply_shared_plan_scope(payload: dict[str, Any], plan: Mapping[str, Any], *,
                            backend: str, set_id: str | None) -> dict[str, Any]:
    _location, location_filters = location_scope_from_plan(dict(plan))
    enforce_payload_location(payload, location_filters)
    enforce_payload_retrieval_filters(payload, validate_plan_filter_contract(dict(plan)))
    filters = payload.setdefault("role_search_filters", {})
    filters.pop("age_min", None)
    filters.pop("age_max", None)
    if backend == "powerset" and set_id:
        filters["set_id"] = set_id
    return payload


def _plan_generation_command(args: Any, epoch0: Path, plan_path: Path) -> list[object]:
    command: list[object] = [
        sys.executable, BUILD_PLAN, "--run-dir", epoch0, "--jd-file", args.jd_file,
        "--created-at", args.created_at, "--plan-only", "--model", args.plan_model,
        "--reasoning-effort", args.plan_reasoning_effort,
    ]
    if args.jd_url:
        command += ["--source-url", args.jd_url]
    source_json = epoch0.parent / "source.json"
    if source_json.is_file():
        command += ["--source-json", source_json]
    if args.set_id:
        command += ["--set-id", args.set_id]
    if args.preferences:
        command += ["--preferences", args.preferences]
    return command


def _query_generation_command(args: Any, plan_path: Path, queries_path: Path) -> list[object]:
    return [
        sys.executable, DECOMPOSE, "--jd-file", args.jd_file, "--plan", plan_path,
        "--model", args.query_model, "--reasoning-effort", args.query_reasoning_effort,
        "--query-only", "--dynamic-simple", "--out", queries_path,
    ]


def prepare_review(args: Any, run_dir: Path, plan_path: Path, queries_path: Path) -> dict[str, Any]:
    epoch0 = run_dir / "epoch0"
    epoch0.mkdir(parents=True, exist_ok=True)
    if not plan_path.exists():
        run_checked(_plan_generation_command(args, epoch0, plan_path),
                    expected_paths=[plan_path], description="build deep-search plan")
    if not queries_path.exists():
        run_checked(_query_generation_command(args, plan_path, queries_path),
                    expected_paths=[queries_path], description="generate initial search queries")
    arms = validate_query_arms(json.loads(queries_path.read_text(encoding="utf-8")))
    return {
        "primitive": "deep_search_loop", "status": "awaiting_plan_approval", "mode": "simple",
        "plan": str(plan_path), "queries": str(queries_path), "query_arms": arms,
        "source_started": False,
        "review": "Edit the plan and one or two queries, then rerun with --plan-approved.",
        "next": "rerun with --plan-approved",
    }


def _usage_cost(path: Path) -> float:
    if not path.is_file():
        return 0.0
    return round(sum(float(json.loads(line).get("cost_usd") or 0)
                     for line in path.read_text(encoding="utf-8").splitlines() if line.strip()), 6)


def _manifest(results: Mapping[str, Any], run_dir: Path) -> dict[str, Any]:
    iterations = list(results.get("iterations") or [])
    summary = results.get("summary") or {}
    return {
        "schema_version": "search-harness.manifest.v1", "status": results["status"],
        "jd_id": results["jd_id"],
        "ponds_run": max((int(row.get("pond_n") or 0) for row in iterations), default=0),
        "gt_recall": None, "cost_usd": _usage_cost(run_dir / "usage.jsonl"),
        "rapidapi": deepcopy(results.get("rapidapi") or {}),
        "results": str(run_dir / "results.json"),
        "shortlist_csv": summary.get("shortlist_csv"),
        "relationship_csv": summary.get("relationship_csv"),
    }


def _candidate_key(candidate: Mapping[str, Any]) -> str:
    name = re.sub(r"[^a-z0-9]+", "", str(candidate.get("name") or "").casefold())
    company = re.sub(r"[^a-z0-9]+", "", str(candidate.get("company") or "").split(";", 1)[0].casefold())
    if name and company:
        return f"{name}|{company}"
    key = str(candidate.get("linkedin_url") or "").strip()
    person = str(candidate.get("person") or "").strip()
    key = key or person
    return key or "|".join(str(candidate.get(field) or "").casefold()
                             for field in ("name", "title", "company"))


def _enrich_summary_sources(results: Mapping[str, Any]) -> dict[str, Any]:
    enriched = deepcopy(dict(results))
    for iteration in enriched.get("iterations") or []:
        artifacts = (iteration.get("arm") or {}).get("artifacts") or {}
        path = resolve_artifact_path(artifacts.get("jsonl"))
        if not path.is_file():
            continue
        source_by_person = {
            str(row.get("person_id") or ""): row
            for row in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                        if line.strip())
        }
        for candidate in iteration.get("shortlist_grades") or []:
            source = source_by_person.get(str(candidate.get("person") or "")) or {}
            candidate.setdefault("source_operator", source.get("source_operator"))
            candidate.setdefault("source_channel", source.get("source_channel"))
    return enriched


def _run_identity(run_dir: Path, results: Mapping[str, Any]) -> tuple[str, str, str]:
    plan_path = run_dir / "epoch0" / "plan.json"
    plan = _read_json(plan_path) if plan_path.is_file() else {}
    source_url = str(plan.get("source_url") or "").split("#", 1)[0].split("?", 1)[0]
    return (source_url.rstrip("/").casefold(), str(results.get("company") or "").casefold(),
            str(results.get("title") or "").casefold())


def _same_jd(left: tuple[str, str, str], right: tuple[str, str, str]) -> bool:
    if left[0] and right[0]:
        return left[0] == right[0]
    return bool(left[1] and left[2] and left[1:] == right[1:])


def _related_run_frames(run_dir: Path, results: Mapping[str, Any]) -> list[dict[str, Any]]:
    identity = _run_identity(run_dir, results)
    frames = []
    for path in sorted(run_dir.parent.glob("*/results.json")):
        if path.parent == run_dir:
            continue
        candidate = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(candidate, Mapping) or "iterations" not in candidate:
            continue
        if _same_jd(identity, _run_identity(path.parent, candidate)):
            frames.append({"run": path.parent.name,
                           "results": _enrich_summary_sources(candidate),
                           "cost_usd": _usage_cost(path.parent / "usage.jsonl")})
    return frames


def build_search_summary(results: Mapping[str, Any], total_cost_usd: float, *,
                         run_name: str = "current",
                         related_runs: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Deduplicate reviewed candidates across same-JD runs into four review groups."""
    frames = [{"run": run_name, "results": results, "cost_usd": total_cost_usd},
              *related_runs]
    occurrences: dict[str, list[dict[str, Any]]] = {}
    found_by: dict[str, list[dict[str, Any]]] = {}
    chain = []
    for frame in frames:
        frame_name = str(frame.get("run") or "current")
        frame_results = frame.get("results") or {}
        for iteration in frame_results.get("iterations") or []:
            pond_n = int(iteration.get("pond_n") or 0)
            query = str(iteration.get("query") or "")
            chain.append({
                "run": frame_name, "pond_n": pond_n, "query": query,
                "diagnosis": iteration.get("diagnosis"),
                "move": (iteration.get("next_move") or {}).get("action"),
                "below_threshold": bool(iteration.get("below_threshold")),
                "result_count": iteration.get("result_count"), "cost_usd": iteration.get("cost_usd"),
            })
            for raw in iteration.get("shortlist_grades") or []:
                candidate = dict(raw)
                key = _candidate_key(candidate)
                occurrences.setdefault(key, []).append(candidate)
                marker = {"run": frame_name, "pond": pond_n, "query": query}
                if marker not in found_by.setdefault(key, []):
                    found_by[key].append(marker)

    groups = {name: [] for name in (
        "send_worthy", "chat_worthy", "wrong_timing_relationship", "passed")}
    for key, candidates in occurrences.items():
        primary = max(candidates, key=lambda row: (
            str(row.get("fit_annotation_source") or "") == "human",
            float(row.get("score") or 0),
        ))
        group = str(primary.get("group") or "")
        if group not in FIT_GROUPS:
            continue
        move = str(primary.get("move_plausibility") or "unknown")
        pedigree = str(primary.get("pedigree_prior") or "neutral")
        score = float(primary.get("score") or 0)
        months = primary.get("months_in_seat")
        timing = ("destination pull" if move == "flag-relationship" else
                  "wrong-timing" if move == "wrong-timing" else
                  f"{months} months in seat" if months is not None else
                  str(primary.get("company_timing") or "unknown"))
        markers = found_by[key]
        groups[group].append({
            "person": str(primary.get("person") or ""), "name": primary.get("name"),
            "title": primary.get("title"), "company": primary.get("company"),
            "linkedin_url": primary.get("linkedin_url"),
            "rerank_score": round(score, 4),
            "level": primary.get("level_read") or "Level unclear",
            "timing": timing, "move_plausibility": move,
            "pedigree_prior": pedigree,
            "why": " ".join(str(primary.get("why") or "No fit reason recorded.").split()),
            "source_operator": primary.get("source_operator"),
            "source_channel": primary.get("source_channel"),
            "runs": sorted({row["run"] for row in markers}),
            "ponds": sorted({int(row["pond"]) for row in markers}),
            "found_by": markers,
        })
    for rows in groups.values():
        rows.sort(key=lambda row: float(row["rerank_score"]), reverse=True)
    return {
        "deduped_candidate_count": sum(len(rows) for rows in groups.values()),
        "counts": {name: len(rows) for name, rows in groups.items()},
        "groups": groups, "pond_chain": chain,
        "total_cost_usd": round(sum(float(frame.get("cost_usd") or 0) for frame in frames), 6),
    }


def build_saved_search_summary(results: Mapping[str, Any], run_dir: Path) -> dict[str, Any]:
    current = _enrich_summary_sources(results)
    related = (_related_run_frames(run_dir, results)
               if results.get("status") == "completed" else [])
    return build_search_summary(
        current, _usage_cost(run_dir / "usage.jsonl"), run_name=run_dir.name,
        related_runs=related)


def export_search_summary(summary: Mapping[str, Any], run_dir: Path) -> dict[str, str]:
    def rows(groups: Sequence[str]) -> list[dict[str, Any]]:
        output = []
        for group in groups:
            for candidate in (summary.get("groups") or {}).get(group) or []:
                output.append({
                    "Rank": len(output) + 1, "Name": candidate.get("name") or "",
                    "LinkedIn URL": candidate.get("linkedin_url") or "",
                    "Current Role": candidate.get("title") or "",
                    "Current Company": candidate.get("company") or "",
                    "Source": candidate.get("source_operator") or "",
                    "Channel": candidate.get("source_channel") or "",
                    "Rationale": candidate.get("why") or "",
                })
        return output

    shortlist = run_dir / "shortlist.csv"
    relationship = run_dir / "relationship.csv"
    write_shortlist_csv(shortlist, rows(("send_worthy", "chat_worthy")))
    write_shortlist_csv(relationship, rows(("wrong_timing_relationship",)))
    return {"shortlist_csv": str(shortlist), "relationship_csv": str(relationship)}


def _save(results: dict[str, Any], run_dir: Path) -> None:
    results["updated_at"] = _now()
    results["summary"] = build_saved_search_summary(results, run_dir)
    if results.get("status") == "completed":
        results["summary"].update(export_search_summary(results["summary"], run_dir))
    _write_json(run_dir / "results.json", results)
    _write_json(run_dir / "manifest.json", _manifest(results, run_dir))


def _occupation_heads(queries: Sequence[Any]) -> set[str]:
    heads: set[str] = set()
    for raw in queries:
        head = re.split(r"\s+with\s+|\s+who\s+|\s+in\s+|,|—|\||/",
                        str(raw or "").lower(), maxsplit=1)[0]
        tokens = [token for token in re.findall(r"[a-z][a-z-]+", head)
                  if token not in _OCCUPATION_HEAD_STOPWORDS]
        if tokens:
            heads.add(" ".join(tokens[-2:]) if len(tokens) >= 2 else tokens[0])
            heads.add(tokens[-1])
    return heads


def _occupation_heads_overlap(left: set[str], right: set[str]) -> bool:
    return bool(left & right) or any(
        a.rstrip("s") == b.rstrip("s")
        for a in left for b in right if " " not in a and " " not in b
    )


def _career_stages(query: Any) -> set[str]:
    return set(re.findall(r"[a-z][a-z-]+", str(query or "").casefold())) & _CAREER_STAGES


def _adjacent_population_changed(current_query: Any, next_query: Any) -> bool:
    return (not _occupation_heads_overlap(
        _occupation_heads([current_query]), _occupation_heads([next_query])) or
        _career_stages(current_query) != _career_stages(next_query))


def _source_occupation(query: Any) -> str:
    heads = _occupation_heads([query])
    return max(heads, key=lambda value: (len(value.split()), len(value)), default="")


def initialize_run(*, run_dir: Path, jd_path: Path, plan_path: Path, queries_path: Path) -> Path:
    results_path = run_dir / "results.json"
    if results_path.exists():
        return results_path
    bound_jd = run_dir / "jd.txt"
    if jd_path.resolve() != bound_jd.resolve():
        shutil.copyfile(jd_path, bound_jd)
    plan = _read_json(plan_path)
    queries = validate_query_arms(json.loads(queries_path.read_text(encoding="utf-8")))
    must = ((plan.get("traits") or {}).get("must_have") or [])
    defining = next((str(row.get("trait") or "").strip() for row in must
                     if row.get("tier") == "core" and str(row.get("trait") or "").strip()), None)
    scope = plan.get("search_scope") or {}
    hiring_company = dict(plan.get("hiring_company") or {})
    results = {
        "schema_version": "search-harness.v1", "created_at": _now(),
        "jd_id": str(plan.get("job_id") or run_dir.name),
        "company": str(hiring_company.get("name") or ""),
        "hiring_company": hiring_company,
        "candidate_populations": deepcopy(plan.get("candidate_populations") or []),
        "comp_band": deepcopy(plan.get("comp_band")),
        "title": str(plan.get("job_title") or plan.get("source_title") or ""),
        "url": str(plan.get("source_url") or ""),
        "brief": {
            "occupation": (_source_occupation(queries[0]["query"]) or
                           str(plan.get("normalized_archetype") or plan.get("job_title") or "")),
            "defining_capability": defining, "geography": str(scope.get("location") or ""),
        },
        "frozen_initial_queries": queries, "pending_query": queries[0],
        "pending_payload": None, "status": "ready_to_compile", "iterations": [],
        "raw_model_responses": [], "hiring_company_context": None,
        "rapidapi": {"cache_hits": 0, "cache_misses": 0, "live_lookups": 0,
                     "unresolved": 0, "cost_usd": 0.0, "unit_cost_usd": 0.0,
                     "billing_basis": "unit_price_not_configured"},
    }
    _save(results, run_dir)
    return results_path


def run_search_harness(args: Any, run_dir: Path, decision_path: Path | None, *,
                    validate_plan: Callable[..., dict[str, Any]],
                    resolve_identity: Callable[..., tuple[dict[str, Any], str | None, str]],
                    bind_plan: Callable[..., tuple[Path, str]]) -> dict[str, Any]:
    plan_path = Path(args.approved_plan).resolve() if args.approved_plan else run_dir / "epoch0" / "plan.json"
    queries_path = Path(args.queries_file).resolve() if args.queries_file else run_dir / "queries.json"
    if args.plan_approved and args.approved_plan:
        raise ValueError("use only one of --plan-approved or --approved-plan")
    approved = bool(args.plan_approved or args.approved_plan)
    if not approved:
        return prepare_review(args, run_dir, plan_path, queries_path)
    if not plan_path.is_file() or not queries_path.is_file():
        raise ValueError("reviewed plan and queries must exist before --plan-approved")
    plan = validate_plan(plan_path, expected_source_url=args.jd_url)
    retrieval, args.set_id, args.db = resolve_identity(args.backend, plan, args.set_id, args.db)
    plan_path, _digest = bind_plan(run_dir, plan_path, retrieval, Path(args.jd_file),
                                   reviewed_queries_path=queries_path)
    results_path = initialize_run(run_dir=run_dir, jd_path=Path(args.jd_file),
                                  plan_path=plan_path, queries_path=queries_path)
    return {
        "primitive": "deep_search_loop", "status": "ready_to_compile", "mode": "simple",
        "results": str(results_path), "manifest": str(run_dir / "manifest.json"),
        "decision": str(decision_path) if decision_path else None,
        "next": f"run {Path(__file__).name} compile-pond --run-dir {run_dir}",
    }


def update_pending_query(*, run_dir: Path, query: str) -> Path:
    query = " ".join(str(query or "").split())
    if not query:
        raise ValueError("query cannot be empty")
    results = _read_json(run_dir / "results.json")
    if results.get("status") not in {"ready_to_compile", "awaiting_payload_review", "ready_to_run"}:
        raise ValueError("the current query is not editable")
    pond_n = max((int(row.get("pond_n") or 0) for row in results.get("iterations") or []), default=0) + 1
    if results.get("iterations"):
        prior = results["iterations"][-1]
        delta = prior.get("proposal_delta")
        if isinstance(delta, dict) and isinstance(delta.get("proposal"), Mapping):
            actual = dict(delta.get("actual") or {})
            actual["next_query"] = query
            delta["actual"] = actual
            proposal = delta["proposal"]
            delta["changed"] = (
                proposal.get("action") != actual.get("action") or
                proposal.get("next_query") != actual.get("next_query")
            )
    results["pending_query"] = {"key": f"pond_{pond_n:02d}", "query": query}
    results["pending_payload"] = None
    results["status"] = "ready_to_compile"
    _save(results, run_dir)
    return run_dir / "results.json"


def _pattern_defaults(payload: Mapping[str, Any], plan: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    edited = deepcopy(payload)
    filters = edited["role_search_filters"]
    changes = []
    for field in HARD_FILTER_FIELDS:
        if filters.get(field):
            before = deepcopy(filters.pop(field))
            changes.append({"pattern": "drop_duplicate_hard_filter", "field": field,
                            "from": before, "to": None})
    role_trait = next((str(row.get("value") or "").casefold() for row in edited.get("traits") or []
                       if row.get("meaning") == "role"), "")
    bm25 = list(filters.get("bm25_queries") or [])
    if role_trait and len(filters.get("role_ids") or []) <= 1 and len(bm25) > 1:
        words = {word for word in re.findall(r"[a-z0-9]+", role_trait) if len(word) > 2}
        kept = [value for value in bm25
                if words and words <= set(re.findall(r"[a-z0-9]+", str(value).casefold()))]
        if kept and kept != bm25:
            filters["bm25_queries"] = kept
            changes.append({"pattern": "prune_keyword_fanout", "field": "bm25_queries",
                            "from": bm25, "to": kept})
    occupation = " ".join((str(plan.get("normalized_archetype") or ""), role_trait)).casefold()
    bands = list(filters.get("seniority_bands") or [])
    departments = {str(value).casefold() for value in filters.get("role_departments") or []}
    if ({"design", "engineering"} <= departments or
            any(word in occupation for word in ("assistant", "consultant", "banker"))):
        target = []
    elif any(word in occupation for word in ("recruit", "talent")):
        target = ["mid", "senior", "staff", "principal", "manager", "director", "vp"]
    elif any(word in occupation for word in ("engineer", "developer", "research")):
        target = ["mid", "senior", "staff", "principal"]
    else:
        target = bands
    if target != bands:
        if target:
            filters["seniority_bands"] = target
        else:
            filters.pop("seniority_bands", None)
        changes.append({"pattern": "retune_seniority", "field": "seniority_bands",
                        "from": bands or None, "to": target or None})
    return edited, changes


def _merge_rapidapi_stats(results: dict[str, Any], stats: Mapping[str, Any]) -> None:
    total = dict(results.get("rapidapi") or {})
    for field in ("cache_hits", "cache_misses", "live_lookups", "unresolved"):
        total[field] = int(total.get(field) or 0) + int(stats.get(field) or 0)
    incoming_cost = stats.get("cost_usd")
    prior_unknown = "cost_usd" in total and total["cost_usd"] is None
    total["cost_usd"] = (None if prior_unknown or incoming_cost is None else
                         round(float(total.get("cost_usd") or 0) + float(incoming_cost), 6))
    total["unit_cost_usd"] = float(stats.get("unit_cost_usd") or
                                   total.get("unit_cost_usd") or 0)
    total["billing_basis"] = stats.get("billing_basis") or total.get("billing_basis")
    results["rapidapi"] = total


def _ensure_hiring_company_context(results: dict[str, Any], plan: Mapping[str, Any]) -> None:
    if results.get("hiring_company_context") is not None:
        return
    hiring_company = dict(plan.get("hiring_company") or results.get("hiring_company") or {})
    results["hiring_company"] = hiring_company
    results["company"] = str(hiring_company.get("name") or results.get("company") or "")
    contexts, stats = resolve_company_contexts([
        resolve_hiring_company_ref(hiring_company, plan.get("source_url"))
    ])
    context = contexts[0]
    if context:
        context["pull_note"] = pull_note(context)
        if not results.get("company"):
            results["company"] = context.get("name") or ""
    results["hiring_company_context"] = context
    _merge_rapidapi_stats(results, stats)


def _apply_pattern_proposal(payload: Mapping[str, Any], proposal: Mapping[str, Any]
                            ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    edited = deepcopy(payload)
    filters = edited["role_search_filters"]
    changes = []
    valid_bands = {"junior", "mid", "senior", "staff", "principal", "manager", "director", "vp"}
    for item in proposal.get("edits") or []:
        if not isinstance(item, Mapping):
            raise ValueError("pattern edit must be an object")
        pattern, field = str(item.get("pattern") or ""), str(item.get("field") or "")
        reason = " ".join(str(item.get("reason") or "").split())
        if not reason:
            raise ValueError("pattern edit needs a reason")
        before, target = deepcopy(filters.get(field)), item.get("to")
        if pattern == "drop_duplicate_hard_filter" and field in HARD_FILTER_FIELDS and target is None:
            filters.pop(field, None)
        elif pattern == "prune_keyword_fanout" and field in {"role_ids", "bm25_queries"}:
            if not isinstance(target, list) or not target or not set(target) <= set(before or []):
                raise ValueError("keyword pruning must keep a non-empty subset")
            filters[field] = target
        elif pattern == "retune_seniority" and field == "seniority_bands":
            if target is not None and (not isinstance(target, list) or not set(target) <= valid_bands):
                raise ValueError("invalid seniority proposal")
            if target:
                filters[field] = target
            else:
                filters.pop(field, None)
        else:
            raise ValueError("unsupported pattern edit")
        after = deepcopy(filters.get(field))
        if before != after:
            changes.append({"pattern": pattern, "field": field, "from": before,
                            "to": after, "reason": reason, "source": "llm_precedent"})
    return edited, changes


def _llm_pattern_defaults(
    *, payload: Mapping[str, Any], plan: Mapping[str, Any], results: dict[str, Any],
    run_dir: Path, pond_n: int, query: str, client: Any | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    checkpoint = run_dir / "ponds" / f"pond-{pond_n:02d}" / "pattern-defaults.raw.json"
    try:
        precedents = retrieve_payload_edits(
            title=str(results.get("title") or ""), brief=results.get("brief") or {}, query=query)
        context = {
            "job": {"title": results.get("title"), "brief": results.get("brief"),
                    "target_level": plan.get("target_level")},
            "query": query, "compiled_payload": payload,
            "prior_pool": ((results.get("iterations") or [{}])[-1].get("pool_stats")
                           if results.get("iterations") else None),
            "retrieved_precedents": precedents,
        }
        input_sha = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        if checkpoint.is_file() and _read_json(checkpoint).get("input_sha") == input_sha:
            record = _read_json(checkpoint)
        else:
            os.environ["POWERPACKS_USAGE_LOG"] = str(run_dir / "usage.jsonl")
            os.environ["POWERPACKS_USAGE_STAGE"] = f"search_harness.pond_{pond_n:02d}.pattern_defaults"
            os.environ["OPENAI_SERVICE_TIER"] = "flex"
            response = (client or make_openai_client(os.environ.get("OPENAI_API_KEY"))).chat.completions.create(
                model="gpt-5.6-terra", reasoning_effort="medium", service_tier="flex",
                messages=[{"role": "system", "content": PATTERN_DEFAULT_PROMPT},
                          {"role": "user", "content": json.dumps(context, indent=2)}],
                response_format={"type": "json_object"},
            )
            record = {"input_sha": input_sha, "raw": response.choices[0].message.content or "{}",
                      "usage": _response_usage(response), "precedents": precedents}
            _write_json(checkpoint, record)
        raw_record = {"kind": "pattern_defaults", "pond_n": pond_n, **record}
        replaced = False
        for index, row in enumerate(results.get("raw_model_responses") or []):
            if row.get("kind") == "pattern_defaults" and row.get("pond_n") == pond_n:
                results["raw_model_responses"][index] = raw_record
                replaced = True
                break
        if not replaced:
            results["raw_model_responses"].append(raw_record)
        _save(results, run_dir)
        return _apply_pattern_proposal(payload, json.loads(str(record["raw"])))
    except Exception as exc:
        edited, changes = _pattern_defaults(payload, plan)
        for change in changes:
            change.update({"reason": "LLM proposal failed; applied the prior default.",
                           "source": "deterministic_fallback"})
        results["raw_model_responses"].append({
            "kind": "pattern_defaults_fallback", "pond_n": pond_n,
            "error": f"{type(exc).__name__}: {exc}",
        })
        return edited, changes


def _decision_backend(run_dir: Path, backend: str | None) -> str:
    recorded = _read_json(run_dir / "decision.json")
    value = str(recorded.get("backend") or "powerset")
    if backend and backend != value:
        raise ValueError(f"backend {backend!r} conflicts with decision.json backend {value!r}")
    return value


def _backend_args(backend: str, db: str) -> list[str]:
    return ["--backend", "local", "--db", db] if backend == "local" else []


def _approved_retrieval(run_dir: Path, plan: Mapping[str, Any], backend: str,
                        db: str) -> tuple[str | None, str]:
    approved = _read_json(run_dir / "plan_binding.json")["retrieval"]
    if approved.get("backend") != backend:
        raise ValueError("decision backend differs from the approved retrieval corpus")
    requested_db = str(approved.get("db_path") or db)
    identity, set_id, resolved_db = resolve_retrieval_identity(
        backend, dict(plan), approved.get("set_id"), requested_db)
    if identity != approved:
        raise ValueError("retrieval corpus differs from the corpus bound to this run")
    return set_id, resolved_db


def _price_usage_log(path: Path) -> None:
    if not path.is_file():
        return
    prices = load_prices()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        cost = row_cost_usd(row, prices)
        if cost is not None:
            row["cost_usd"] = cost
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _run_command(command: list[str], *, run_dir: Path, log: Path,
                 stage: str, timeout: int = 7200) -> dict[str, Any]:
    env = os.environ.copy()
    env["POWERPACKS_USAGE_LOG"] = str(run_dir / "usage.jsonl")
    env["POWERPACKS_USAGE_STAGE"] = stage
    env["OPENAI_SERVICE_TIER"] = "flex"
    completed = subprocess.run(command, cwd=ROOT, env=env, text=True,
                               capture_output=True, timeout=timeout)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text((completed.stdout or "") + (completed.stderr or ""), encoding="utf-8")
    _price_usage_log(run_dir / "usage.jsonl")
    if completed.returncode:
        raise RuntimeError(f"search pipeline failed ({completed.returncode}): "
                           f"{(completed.stderr or completed.stdout)[-1600:]}")
    return _last_json(completed.stdout)


def compile_pond(*, run_dir: Path, env_file: str, backend: str | None = None,
                 db: str = DEFAULT_LOCAL_DB,
                 client: Any | None = None) -> Path:
    results = _read_json(run_dir / "results.json")
    if results.get("status") != "ready_to_compile" or not results.get("pending_query"):
        raise ValueError("search has no query ready to compile")
    pond_n = max((int(row.get("pond_n") or 0) for row in results.get("iterations") or []), default=0) + 1
    if pond_n > MAX_PONDS:
        raise ValueError("search already reached the four-pond cap")
    query = str(results["pending_query"]["query"])
    pond_dir = run_dir / "ponds" / f"pond-{pond_n:02d}"
    prepare_dir = pond_dir / "prepare"
    backend = _decision_backend(run_dir, backend)
    plan_path = run_dir / "epoch0" / "plan.json"
    plan = _read_json(plan_path)
    set_id, db = _approved_retrieval(run_dir, plan, backend, db)
    result = _run_command([
        sys.executable, str(PIPELINE), "prepare", "--query", query,
        "--env-file", env_file, "--output-dir", str(prepare_dir),
        "--expand-model", "gpt-5.6-luna", "--expand-reasoning-effort", "medium",
        "--limit", str(RETRIEVAL_LIMIT),
        *_backend_args(backend, db),
    ], run_dir=run_dir, log=pond_dir / "compile.log",
       stage=f"search_harness.pond_{pond_n:02d}.compile", timeout=300)
    payload = _read_json(resolve_artifact_path(result["payload_json"]))
    validate_standard_traits(payload)
    load_env_file(Path(env_file))
    compiled_locations = {field: deepcopy(payload["role_search_filters"].get(field))
                          for field in LOCATION_FIELDS if payload["role_search_filters"].get(field)}
    apply_shared_plan_scope(payload, plan, backend=backend, set_id=set_id)
    _ensure_hiring_company_context(results, plan)
    payload, pattern_edits = _llm_pattern_defaults(
        payload=payload, plan=plan, results=results, run_dir=run_dir,
        pond_n=pond_n, query=query, client=client)
    _price_usage_log(run_dir / "usage.jsonl")
    if compiled_locations or re.search(r"\b(worldwide|global|anywhere)\b", query, re.I):
        filters = payload["role_search_filters"]
        before = {field: deepcopy(filters.get(field)) for field in LOCATION_FIELDS if filters.get(field)}
        for field in LOCATION_FIELDS:
            filters.pop(field, None)
        filters.update(compiled_locations)
        if before != compiled_locations:
            pattern_edits.append({"pattern": "query_location_scope", "field": "location",
                                  "from": before or None, "to": compiled_locations or None})
    validate_standard_traits(payload)
    payload_path = pond_dir / "payload.json"
    _write_json(payload_path, payload)
    results["pending_payload"] = {
        "pond_n": pond_n, "query": query, "payload_json": str(payload_path),
        "ledger": str(prepare_dir / "pipeline.ledger.json"), "payload": payload,
        "rerank_exclusions": [], "rerank_only": False,
        "pattern_default_edits": pattern_edits, "proposed_payload": deepcopy(payload),
    }
    results["status"] = "awaiting_payload_review"
    _save(results, run_dir)
    return run_dir / "results.json"


def review_payload(*, run_dir: Path, payload_path: Path | None = None,
                   rerank_exclusions: Sequence[str] = (), human_reviewed: bool = False) -> Path:
    results = _read_json(run_dir / "results.json")
    if results.get("status") != "awaiting_payload_review" or not results.get("pending_payload"):
        raise ValueError("search has no compiled payload awaiting review")
    pending = dict(results["pending_payload"])
    target = Path(str(pending["payload_json"]))
    reviewed = _read_json(payload_path or target)
    validate_standard_traits(reviewed)
    exclusions = list(dict.fromkeys(" ".join(str(value).split()) for value in rerank_exclusions
                                    if str(value).strip()))
    _write_json(target, reviewed)
    proposed = pending.get("proposed_payload") or pending.get("payload") or {}
    human_delta = _edit_delta(
        _input_snapshot(str(pending["query"]), proposed, []),
        _input_snapshot(str(pending["query"]), reviewed, exclusions),
    )
    pending["human_edit_delta"] = human_delta if any((
        human_delta.get("query"), human_delta.get("traits_added"), human_delta.get("traits_removed"),
        human_delta.get("filters"), human_delta.get("rerank_exclusions"),
    )) else None
    pending["payload"] = reviewed
    pending["rerank_exclusions"] = exclusions
    pending["human_reviewed"] = human_reviewed
    results["pending_payload"] = pending
    results["status"] = "ready_to_rerank" if pending.get("rerank_only") else "ready_to_run"
    _save(results, run_dir)
    return run_dir / "results.json"


def _evaluation_text(text: str, exclusions: Sequence[str]) -> str:
    if exclusions:
        text += "\n\nRecruiter rerank exclusions: candidates primarily specializing in "
        text += "; ".join(exclusions) + " are not a fit for this search."
    return text


def _profiles(path_text: Any) -> dict[str, dict[str, Any]]:
    path = resolve_artifact_path(path_text)
    if not path.is_file():
        return {}
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return {str(row["person_id"]): row for row in (json.loads(line) for line in handle if line.strip())
                if row.get("person_id")}


def _level(title: Any) -> str:
    text = " ".join(str(title or "").lower().split())
    rules = (
        (r"\b(founder|owner|partner|chief|cto|ceo|cfo|coo)\b", "Founder / C-suite"),
        (r"\b(vp|vice president)\b", "VP"), (r"\b(director|head of)\b", "Director / Head"),
        (r"\bmanager\b", "Manager"), (r"\b(staff|principal)\b", "Staff / Principal"),
        (r"\bsenior\b", "Senior"), (r"\b(junior|associate|analyst|intern)\b", "Early career"),
    )
    return next((label for pattern, label in rules if re.search(pattern, text)), "Unspecified")


def _recent_roles(profile: Mapping[str, Any]) -> list[dict[str, str]]:
    return [{
        "title": str(row.get("title") or row.get("position_title") or "").strip(),
        "company": str(row.get("company_name") or row.get("company") or "").strip(),
    } for row in (profile.get("positions") or [])[:3] if isinstance(row, Mapping)]


def _trait_scores(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _rerank_score(row: Mapping[str, Any]) -> float:
    value = row.get("final_score")
    return float(value if value is not None else row.get("score") or 0)


def _review_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    primary = [row for row in rows if _rerank_score(row) >= REVIEW_SCORE_THRESHOLD]
    return primary or [row for row in rows
                       if _rerank_score(row) >= FALLBACK_REVIEW_SCORE_THRESHOLD]


def _review_candidates(rows: Sequence[Mapping[str, Any]],
                       profiles: Mapping[str, Mapping[str, Any]],
                       company_contexts: Sequence[Mapping[str, Any]] = (),
                       company_refs: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    candidates = []
    for index, row in enumerate(_review_rows(rows)):
        person = str(row.get("person_id") or "")
        profile = profiles.get(person) or {}
        title = row.get("current_titles") or profile.get("current_title")
        context = company_contexts[index] if index < len(company_contexts) else {}
        candidates.append({
            "person": person, "name": row.get("name") or profile.get("name"),
            "title": title,
            "company": row.get("current_companies") or profile.get("current_company"),
            "location": row.get("location") or profile.get("location") or profile.get("city"),
            "linkedin_url": row.get("linkedin_url") or profile.get("linkedin_url"),
            "score": round(float(row.get("final_score") or 0), 4),
            "source_operator": row.get("source_operator"),
            "source_channel": row.get("source_channel"),
            "current_company_headcount": context.get("headcount"),
            "current_company_stage": context.get("stage"),
            "current_company_funding": context.get("funding"),
            "current_company_funding_basis": context.get("funding_basis"),
            "company_timing": ((company_refs[index].get("company_timing")
                                if index < len(company_refs) else None) or "current"),
            "current_position_start_date": (company_refs[index].get("current_position_start_date")
                                            if index < len(company_refs) else None),
            "months_in_seat": (company_refs[index].get("months_in_seat")
                               if index < len(company_refs) else None),
            "recent_roles": _recent_roles(profile),
            "company_card_id": None,
            "trait_scores": _trait_scores(row.get("trait_scores")),
            "reason": " ".join(str(row.get("overall_reasoning") or "").split())[:900],
        })
    return candidates


def _annotate_company_fit(*, candidates: Sequence[Mapping[str, Any]], results: dict[str, Any],
                          run_dir: Path, pond_n: int, plan: Mapping[str, Any],
                          client: Any | None = None) -> list[dict[str, Any]]:
    if not candidates:
        return []
    jd = (run_dir / "jd.txt").read_text(encoding="utf-8")
    hiring_company = results.get("hiring_company_context") or results.get("hiring_company") or {}
    brief = results.get("brief") or {}
    precedents = retrieve_fit_precedents(
        title=str(results.get("title") or ""), brief=results.get("brief") or {},
        candidates=candidates)
    checkpoint_dir = run_dir / "ponds" / f"pond-{pond_n:02d}" / "company-fit"
    os.environ["POWERPACKS_USAGE_LOG"] = str(run_dir / "usage.jsonl")
    os.environ["POWERPACKS_USAGE_STAGE"] = f"search_harness.pond_{pond_n:02d}.company_fit"
    os.environ["OPENAI_SERVICE_TIER"] = "flex"

    async def annotate_all() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        semaphore = asyncio.Semaphore(max(1, min(FIT_CONCURRENCY, len(candidates))))
        api_client = client or make_async_openai_client(os.environ.get("OPENAI_API_KEY"))

        async def annotate_one(index: int, candidate: Mapping[str, Any]
                               ) -> tuple[dict[str, Any], dict[str, Any]]:
            messages = company_fit_messages(
                jd=jd, target_level=plan.get("target_level"), comp_band=plan.get("comp_band"),
                hiring_company=hiring_company, candidate=candidate, brief=brief,
                fit_precedents=precedents)
            input_sha = hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()
            checkpoint = checkpoint_dir / f"{index:03d}.json"
            record = _read_json(checkpoint) if checkpoint.is_file() else {}
            if record.get("input_sha") == input_sha and record.get("raw"):
                try:
                    return apply_company_fit_response(candidate, str(record["raw"])), {
                        "candidate_index": index, "input_sha": input_sha,
                        "checkpoint": str(checkpoint), "cached": True}
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
            async with semaphore:
                response = await api_client.chat.completions.create(
                    model="gpt-5.6-luna", reasoning_effort="medium", service_tier="flex",
                    messages=messages, response_format={"type": "json_object"})
            record = {"input_sha": input_sha, "raw": response.choices[0].message.content or "{}",
                      "usage": _response_usage(response)}
            _write_json(checkpoint, record)
            annotated = apply_company_fit_response(candidate, str(record["raw"]))
            return annotated, {"candidate_index": index, "input_sha": input_sha,
                               "checkpoint": str(checkpoint), "cached": False}

        async def guarded(index: int, candidate: Mapping[str, Any]
                          ) -> tuple[int, dict[str, Any], dict[str, Any]]:
            try:
                annotated, record = await annotate_one(index, candidate)
            except Exception as exc:
                annotated = {**dict(candidate),
                             **fallback_company_fit(candidate, plan.get("target_level"))}
                record = {"candidate_index": index, "error": f"{type(exc).__name__}: {exc}"}
            return index, annotated, record

        output: list[dict[str, Any] | None] = [None] * len(candidates)
        records: list[dict[str, Any] | None] = [None] * len(candidates)

        def handle(value: tuple[int, dict[str, Any], dict[str, Any]]) -> None:
            index, annotated, record = value
            output[index], records[index] = annotated, record

        try:
            await drain_pool([
                guarded(index, candidate) for index, candidate in enumerate(candidates)], handle)
        finally:
            if client is None:
                await api_client.close()
        return ([row for row in output if row is not None],
                [row for row in records if row is not None])

    annotated, checkpoints = asyncio.run(annotate_all())
    raw_record = {"kind": "company_fit", "pond_n": pond_n, "checkpoints": checkpoints}
    raw_responses = results.setdefault("raw_model_responses", [])
    prior = next((index for index, row in enumerate(raw_responses)
                  if row.get("kind") == "company_fit" and row.get("pond_n") == pond_n), None)
    if prior is None:
        raw_responses.append(raw_record)
    else:
        raw_responses[prior] = raw_record
    _price_usage_log(run_dir / "usage.jsonl")
    _save(results, run_dir)
    return annotated


def _top_counts(values: Sequence[str], limit: int = 10) -> dict[str, int]:
    return dict(Counter(value for value in values if value).most_common(limit))


def _score_histogram(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    histogram: Counter[str] = Counter()
    for row in rows:
        score = _rerank_score(row)
        band = ("0.9+" if score >= .9 else "0.8-0.9" if score >= .8 else
                "0.7-0.8" if score >= .7 else "0.6-0.7" if score >= .6 else "below 0.6")
        histogram[band] += 1
    return {band: histogram[band] for band in SCORE_BANDS}


def _pool_stats(rows: Sequence[Mapping[str, Any]], reviewed_count: int) -> dict[str, Any]:
    companies = [part.strip() for row in rows
                 for part in str(row.get("current_companies") or row.get("company") or "").split(";")
                 if part.strip()]
    histogram = _score_histogram(rows)
    return {
        "reviewed_count": reviewed_count, "result_count": len(rows),
        "score_histogram": histogram,
        "level_mix": _top_counts([_level(row.get("current_titles") or row.get("title"))
                                  for row in rows]),
        "geo_mix": _top_counts([str(row.get("location") or "Unknown") for row in rows]),
        "top_companies": _top_counts(companies),
        "diagnosis_note": f"Retrieved {len(rows)}; reviewed {reviewed_count}. Score bands: {histogram}.",
    }


def _input_snapshot(query: str, payload: Mapping[str, Any], exclusions: Sequence[str]) -> dict[str, Any]:
    filters = payload.get("role_search_filters") or {}
    return {
        "query": query, "traits": deepcopy(payload.get("traits") or []),
        "filters": {key: deepcopy(filters.get(key)) for key in EDITABLE_FILTER_FIELDS if key in filters},
        "rerank_exclusions": list(exclusions),
    }


def _edit_delta(previous: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    prior_traits = {(str(row.get("value") or ""), str(row.get("temporal") or ""),
                     str(row.get("meaning") or "")) for row in previous.get("traits") or []}
    current_traits = {(str(row.get("value") or ""), str(row.get("temporal") or ""),
                       str(row.get("meaning") or "")) for row in current.get("traits") or []}
    old_filters, new_filters = previous.get("filters") or {}, current.get("filters") or {}
    return {
        "query": ({"from": previous.get("query"), "to": current.get("query")}
                  if previous.get("query") != current.get("query") else None),
        "traits_added": [list(row) for row in sorted(current_traits - prior_traits)],
        "traits_removed": [list(row) for row in sorted(prior_traits - current_traits)],
        "filters": {key: {"from": old_filters.get(key), "to": new_filters.get(key)}
                    for key in EDITABLE_FILTER_FIELDS if old_filters.get(key) != new_filters.get(key)},
        "rerank_exclusions": ({"from": previous.get("rerank_exclusions") or [],
                               "to": current.get("rerank_exclusions") or []}
                              if (previous.get("rerank_exclusions") or []) !=
                                 (current.get("rerank_exclusions") or []) else None),
    }


def _result_delta(previous: Mapping[str, Any] | None, current: Mapping[str, Any]) -> dict[str, Any]:
    old = ((previous or {}).get("pool_stats") or {}).get("score_histogram") or {}
    new = (current.get("pool_stats") or {}).get("score_histogram") or {}
    return {"score_histogram": {band: int(new.get(band) or 0) - int(old.get(band) or 0)
                                for band in SCORE_BANDS}, "gt_reviewed": None}


def _pond_costs(run_dir: Path) -> dict[int, float]:
    path = run_dir / "usage.jsonl"
    if not path.is_file():
        return {}
    costs: Counter[int] = Counter()
    for row in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()):
        match = re.search(r"pond_(\d+)", str(row.get("stage") or ""))
        if match:
            costs[int(match.group(1))] += float(row.get("cost_usd") or 0)
    return {pond: round(cost, 6) for pond, cost in costs.items()}


def run_pond(*, run_dir: Path, env_file: str, backend: str | None = None,
             db: str = DEFAULT_LOCAL_DB,
             client: Any | None = None) -> Path:
    results = _read_json(run_dir / "results.json")
    if results.get("status") not in {"ready_to_run", "ready_to_rerank"} or not results.get("pending_payload"):
        raise ValueError("search has no reviewed payload ready to run")
    pending = dict(results["pending_payload"])
    load_env_file(Path(env_file))
    pond_n = int(pending["pond_n"])
    pond_dir = run_dir / "ponds" / f"pond-{pond_n:02d}"
    backend = _decision_backend(run_dir, backend)
    plan = _read_json(run_dir / "epoch0" / "plan.json")
    set_id, db = _approved_retrieval(run_dir, plan, backend, db)
    payload = _read_json(Path(str(pending["payload_json"])))
    apply_shared_plan_scope(payload, plan, backend=backend, set_id=set_id)
    validate_standard_traits(payload)
    _write_json(Path(str(pending["payload_json"])), payload)
    command = [
        sys.executable, str(PIPELINE), "run", "--ledger", str(pending["ledger"]),
        "--env-file", env_file, "--execute-approved",
        "--filter-model", "gpt-5.6-luna", "--filter-reasoning-effort", "none",
        "--model", "gpt-5.6-luna", "--reasoning-effort", "medium",
        "--limit", str(RETRIEVAL_LIMIT), *_backend_args(backend, db),
    ]
    if pending.get("rerank_exclusions"):
        command += ["--evaluation-query", _evaluation_text(
            str(pending["query"]), pending["rerank_exclusions"])]
    if pending.get("rerank_only"):
        command.append("--force-llm")
    else:
        command += ["--query", str(pending["query"]), "--payload-json", str(pending["payload_json"])]
    result = _run_command(command, run_dir=run_dir, log=pond_dir / "run.log",
                          stage=f"search_harness.pond_{pond_n:02d}.run")
    artifacts = result.get("artifacts") or {}
    rows_path = resolve_artifact_path(artifacts.get("jsonl"))
    if not rows_path.is_file():
        raise ValueError(f"search result JSONL is missing: {rows_path}")
    rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows.sort(key=lambda row: float(row.get("final_score") or 0), reverse=True)
    arm = {
        "key": f"pond_{pond_n:02d}", "query": str(pending["query"]),
        "payload_json": str(pending["payload_json"]), "ledger": str(pending["ledger"]),
        "traits": payload["traits"], "has_domain_intent": payload["has_domain_intent"],
        "result_count": len(rows), "artifacts": artifacts,
    }
    profiles = _profiles(artifacts.get("profiles_path"))
    review_rows = _review_rows(rows)
    below_threshold = bool(
        review_rows and _rerank_score(review_rows[0]) < REVIEW_SCORE_THRESHOLD)
    _ensure_hiring_company_context(results, plan)
    refs = [current_company_ref(
        profiles.get(str(row.get("person_id") or "")) or {}, row.get("current_companies"))
        for row in review_rows]
    company_contexts, rapidapi_stats = resolve_company_contexts(refs)
    _merge_rapidapi_stats(results, rapidapi_stats)
    candidates = _review_candidates(rows, profiles, company_contexts, refs)
    candidates = _annotate_company_fit(
        candidates=candidates, results=results, run_dir=run_dir, pond_n=pond_n,
        plan=plan, client=client)
    snapshot = _input_snapshot(str(pending["query"]), payload, pending.get("rerank_exclusions") or [])
    prior = results["iterations"][-1] if results.get("iterations") else None
    prior_input = (prior or {}).get("input") or {
        "query": str(results["frozen_initial_queries"][0]["query"]),
        "traits": [], "filters": {}, "rerank_exclusions": [],
    }
    iteration = {
        "jd_id": results["jd_id"], "epoch_n": len(results["iterations"]) + 1,
        "pond_n": pond_n, "query": str(pending["query"]),
        "payload_sha": hashlib.sha256(Path(str(pending["payload_json"])).read_bytes()).hexdigest(),
        "input": snapshot, "edit_delta": _edit_delta(prior_input, snapshot),
        "pattern_default_edits": deepcopy(pending.get("pattern_default_edits") or []),
        "human_edit_delta": deepcopy(pending.get("human_edit_delta")),
        "payload_reviewed": bool(pending.get("human_reviewed")),
        "pool_stats": _pool_stats(rows, len(candidates)), "diagnosis": None,
        "below_threshold": below_threshold,
        "human_override": None, "next_move": None, "shortlist_grades": candidates,
        "reviewed_count": len(candidates), "result_count": len(rows), "arm": arm,
        "cost_usd": _pond_costs(run_dir).get(pond_n, 0.0), "gt_recall": None,
    }
    iteration["result_delta"] = _result_delta(prior, iteration)
    results["iterations"].append(iteration)
    results["pending_query"] = None
    results["pending_payload"] = None
    if pond_n == MAX_PONDS and not pending.get("rerank_only"):
        iteration["next_move"] = {"action": "stop", "next_query": None,
                                  "rationale": "Four-pond cap reached; candidate quality is unreviewed."}
        results["status"] = "completed"
    else:
        results["status"] = "awaiting_diagnosis"
    _save(results, run_dir)
    return run_dir / "results.json"


def reannotate_saved(*, run_dir: Path, env_file: str, pond: int | None = None,
                     client: Any | None = None) -> Path:
    """Refresh company context and fit labels from saved rerank rows; never searches."""
    load_env_file(Path(env_file))
    results = _read_json(run_dir / "results.json")
    plan = _read_json(run_dir / "epoch0" / "plan.json")
    results["hiring_company_context"] = None
    results["rapidapi"] = {"cache_hits": 0, "cache_misses": 0, "live_lookups": 0,
                           "unresolved": 0, "cost_usd": 0.0, "unit_cost_usd": 0.0,
                           "billing_basis": "unit_price_not_configured"}
    _ensure_hiring_company_context(results, plan)
    iterations = list(results.get("iterations") or [])
    if pond is not None:
        iterations = [row for row in iterations if int(row.get("pond_n") or 0) == pond][-1:]
    for iteration in iterations:
        pond_n = int(iteration["pond_n"])
        artifacts = (iteration.get("arm") or {}).get("artifacts") or {}
        rows_path = resolve_artifact_path(artifacts.get("jsonl"))
        rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        rows.sort(key=lambda row: float(row.get("final_score") or 0), reverse=True)
        profiles = _profiles(artifacts.get("profiles_path"))
        review_rows = _review_rows(rows)
        refs = [current_company_ref(
            profiles.get(str(row.get("person_id") or "")) or {}, row.get("current_companies"))
            for row in review_rows]
        contexts, stats = resolve_company_contexts(refs)
        _merge_rapidapi_stats(results, stats)
        candidates = _review_candidates(rows, profiles, contexts, refs)
        saved = {str(row.get("person") or ""): row
                 for row in iteration.get("shortlist_grades") or []}
        for candidate in candidates:
            prior = saved.get(str(candidate.get("person") or "")) or {}
            if prior.get("fit_override"):
                candidate["fit_override"] = deepcopy(prior["fit_override"])
        iteration["shortlist_grades"] = _annotate_company_fit(
            candidates=candidates, results=results, run_dir=run_dir, pond_n=pond_n,
            plan=plan, client=client)
        iteration["pool_stats"] = _pool_stats(rows, len(iteration["shortlist_grades"]))
        iteration["reviewed_count"] = len(iteration["shortlist_grades"])
        iteration["below_threshold"] = bool(
            review_rows and _rerank_score(review_rows[0]) < REVIEW_SCORE_THRESHOLD)
    _price_usage_log(run_dir / "usage.jsonl")
    costs = _pond_costs(run_dir)
    for iteration in results.get("iterations") or []:
        iteration["cost_usd"] = costs.get(int(iteration["pond_n"]), 0.0)
    _save(results, run_dir)
    return run_dir / "results.json"


def _next_move_context(results: Mapping[str, Any], iteration: Mapping[str, Any],
                       diagnosis: str | None, note: str) -> dict[str, Any]:
    stats = iteration["pool_stats"]
    iterations = results.get("iterations") or []
    used = {str(row["query"]).casefold() for row in iterations}
    remaining = [row for row in results.get("frozen_initial_queries") or []
                 if str(row.get("query") or "").casefold() not in used]
    return {
        "job": {"title": results["title"], "hiring_company": results["company"] or "unknown",
                "destination_context": None},
        "current_query": iteration["query"], "frozen_brief": results["brief"],
        "pond_chain": [
            {
                "pond_n": int(row.get("pond_n") or 0),
                "query": str(row.get("query") or ""),
                "diagnosis": (diagnosis if row is iteration and diagnosis else row.get("diagnosis")),
                "action": (row.get("next_move") or {}).get("action"),
            }
            for row in iterations
        ],
        "candidate_populations": results.get("candidate_populations") or [],
        "comp_band": results.get("comp_band"),
        "frozen_initial_queries_remaining": remaining,
        "relaxation_order": [
            "widen geography before relaxing the occupation",
            "then broaden to someone who could feasibly do the work or a feeder career",
            "never relax the defining capability",
            "use corpus_sparse when the available network is the limit",
        ],
        "human_diagnosis": ({"category": diagnosis, "note": note or None}
                            if diagnosis else None),
        "retrieved_precedents": retrieve_next_moves(
            title=str(results.get("title") or ""), brief=results.get("brief") or {},
            query=str(iteration.get("query") or ""), diagnosis=diagnosis or ""),
        "pool": {key: stats[key] for key in
                 ("result_count", "reviewed_count", "score_histogram", "level_mix", "geo_mix", "top_companies")},
        "anonymized_observations": [
            {"title": row.get("title") or "unknown", "company": row.get("company") or "unknown"}
            for row in iteration.get("shortlist_grades") or []
        ][:20],
    }


def _response_usage(response: Any) -> dict[str, Any]:
    usage = response.usage
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    return {
        "model": str(getattr(response, "model", "")),
        "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "cached_tokens": int(getattr(prompt_details, "cached_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "reasoning_tokens": int(getattr(completion_details, "reasoning_tokens", 0) or 0),
        "service_tier": str(getattr(response, "service_tier", "") or ""),
    }


def _shared_requirement_ngram(query: str, plan: Mapping[str, Any], size: int = 3) -> str | None:
    traits = plan.get("traits") or {}
    requirements = " ".join(
        str(row.get("trait") or "")
        for field in ("must_have", "nice_to_have")
        for row in (traits.get(field) or []) if isinstance(row, Mapping)
    )
    requirement_tokens = re.findall(r"[a-z0-9]+", requirements.casefold())
    requirement_ngrams = {
        tuple(requirement_tokens[index:index + size])
        for index in range(len(requirement_tokens) - size + 1)
    }
    query_tokens = re.findall(r"[a-z0-9]+", query.casefold())
    for index in range(len(query_tokens) - size + 1):
        ngram = tuple(query_tokens[index:index + size])
        if ngram in requirement_ngrams:
            return " ".join(ngram)
    return None


def _parse_next_move(raw: str) -> dict[str, Any]:
    proposal = json.loads(raw)
    if set(proposal) != {"diagnosis", "action", "next_query", "source", "rationale"}:
        raise ValueError("next move must contain diagnosis, action, next_query, source, and rationale")
    if str(proposal["diagnosis"]) not in NEXT_SEARCH_DIAGNOSES:
        raise ValueError("next move diagnosis is invalid")
    action = str(proposal["action"])
    if action not in NEXT_SEARCH_ACTIONS:
        raise ValueError("next move action is invalid")
    if action in NEXT_SEARCH_QUERY_ACTIONS:
        query = " ".join(str(proposal.get("next_query") or "").split())
        source = " ".join(str(proposal.get("source") or "").split())
        if len(query) < 10 or not source:
            raise ValueError("next search action needs a self-contained query and grounded source")
        proposal["next_query"] = query
        proposal["source"] = source
    elif proposal.get("next_query") is not None or proposal.get("source") is not None:
        raise ValueError("non-search next move must not contain a query or source")
    return proposal


def decide(*, run_dir: Path, choice: int | None = None, diagnosis: str | None = None,
           note: str = "", autonomous: bool = False, model: str = "gpt-5.6-luna",
           reasoning_effort: str = "medium", client: Any | None = None) -> Path:
    results = _read_json(run_dir / "results.json")
    status = results.get("status")
    if status != "awaiting_diagnosis" and not (status == "awaiting_payload_review" and choice == 3):
        raise ValueError("search must await diagnosis")
    if autonomous == (choice is not None):
        raise ValueError("use either --autonomous or an interactive choice")
    if choice not in {None, 2, 3}:
        raise ValueError("interactive choice must be 2 or 3")
    iteration = results["iterations"][-1]
    if choice == 3:
        selected = str(diagnosis or "other")
        if selected not in NEXT_SEARCH_DIAGNOSES:
            raise ValueError("unknown diagnosis")
        iteration["diagnosis"] = selected
        iteration["human_override"] = {"choice": 3, "diagnosis": selected, "note": note}
        iteration["next_move"] = {"action": "stop", "next_query": None,
                                  "source": None, "rationale": note or "Human stopped the search."}
        iteration["proposal_delta"] = {
            "proposal": None,
            "actual": {"diagnosis": selected, "action": "stop", "next_query": None},
            "changed": True,
        }
        results["status"] = "completed"
        _save(results, run_dir)
        return run_dir / "results.json"
    selected = None if autonomous else str(diagnosis or "")
    if selected is not None and selected not in NEXT_SEARCH_DIAGNOSES:
        raise ValueError("unknown diagnosis")
    if not autonomous:
        iteration["diagnosis"] = selected
        iteration["human_override"] = {"choice": 2, "diagnosis": selected, "note": note}
        _save(results, run_dir)
    os.environ["POWERPACKS_USAGE_LOG"] = str(run_dir / "usage.jsonl")
    os.environ["POWERPACKS_USAGE_STAGE"] = f"search_harness.pond_{int(iteration['pond_n']):02d}.next_move"
    os.environ["OPENAI_SERVICE_TIER"] = "flex"
    client = client or make_openai_client(os.environ.get("OPENAI_API_KEY"))
    next_context = _next_move_context(results, iteration, selected, note)
    messages = [{"role": "system", "content": NEXT_SEARCH_PROMPT},
                {"role": "user", "content": json.dumps(next_context, indent=2)}]
    plan = _read_json(run_dir / "epoch0" / "plan.json")
    for attempt in range(2):
        response = client.chat.completions.create(
            model=model, reasoning_effort=reasoning_effort, service_tier="flex",
            messages=messages, response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
        results["raw_model_responses"].append({
            "kind": "next_move", "pond_n": iteration["pond_n"], "attempt": attempt + 1,
            "raw": raw, "usage": _response_usage(response),
        })
        iteration["next_move_precedents"] = next_context["retrieved_precedents"]
        _save(results, run_dir)
        proposal = _parse_next_move(raw)
        overlap = (_shared_requirement_ngram(str(proposal.get("next_query") or ""), plan)
                   if proposal["action"] in NEXT_SEARCH_QUERY_ACTIONS else None)
        same_population = (
            proposal["action"] == "add_adjacent_pond" and
            any(not _adjacent_population_changed(row["query"], proposal.get("next_query"))
                for row in next_context["pond_chain"])
        )
        source_options = {"inferred"}
        source_options.update(
            str(row.get("population") or "").strip().casefold()
            for row in next_context.get("candidate_populations") or []
            if (isinstance(row, Mapping) and
                row.get("hint_kind") not in {"ranking-boost", "comp-band-anchor"})
        )
        source_options.update(
            str(row.get("source") or "").strip().casefold()
            for row in next_context.get("retrieved_precedents") or [] if isinstance(row, Mapping)
        )
        invalid_source = (
            proposal["action"] in NEXT_SEARCH_QUERY_ACTIONS and
            str(proposal.get("source") or "").casefold() not in source_options
        )
        conflicting_diagnosis = selected is not None and proposal["diagnosis"] != selected
        if not overlap and not same_population and not invalid_source and not conflicting_diagnosis:
            break
        if attempt == 0:
            rejection = (
                f"Reject that query because it copies the JD requirement phrase '{overlap}'. "
                "Return a different clean candidate population without any three-word phrase "
                "from the requirements."
                if overlap else
                "Reject that adjacent pond because it keeps the same occupation head noun and "
                "career stage as a pond already in pond_chain. Return a genuinely unused population "
                "with a different occupation head noun or career stage. A domain qualifier on the "
                "same title does not count."
                if same_population else
                "Reject that source citation because it does not name an exact candidate-population "
                "phrase or retrieved precedent source. Return a grounded source, or inferred only when "
                "neither menu contains a credible pond."
                if invalid_source else
                f"Reject that move because the human selected diagnosis '{selected}'. Return that "
                "diagnosis exactly and choose an action that addresses it."
            )
            messages.extend([
                {"role": "assistant", "content": raw},
                {"role": "user", "content": rejection},
            ])
            continue
        if same_population:
            filters = (iteration.get("input") or {}).get("filters") or {}
            bounded = any(filters.get(field) for field in LOCATION_FIELDS)
            matches = list(re.finditer(r"\s+in\s+", str(iteration["query"]), flags=re.I))
            widened = str(iteration["query"])[:matches[-1].start()].strip() if bounded and matches else ""
            proposal = {
                "diagnosis": selected or proposal["diagnosis"],
                "action": "widen_geography" if widened else "stop",
                "next_query": widened or None,
                "source": _source_occupation(iteration["query"]) if widened else None,
                "rationale": ("Both adjacent proposals repeated a searched population; widened the "
                              "current pond's geography instead."
                              if widened else
                              "Both adjacent proposals repeated a searched population and geography "
                              "was already unbounded."),
            }
        else:
            proposal = {
                "diagnosis": selected or proposal["diagnosis"], "action": "stop", "next_query": None,
                "source": None,
                "rationale": ("Stopped for human review after two queries copied JD requirement language."
                              if overlap else
                              "Stopped for human review after two proposals used an ungrounded source."
                              if invalid_source else
                              "Stopped for human review after two proposals conflicted with the selected diagnosis."),
            }
    proposed_diagnosis = str(proposal["diagnosis"])
    selected = selected or proposed_diagnosis
    action = str(proposal["action"])
    if action in NEXT_SEARCH_QUERY_ACTIONS:
        query = str(proposal["next_query"])
        pond_n = max((int(row.get("pond_n") or 0) for row in results["iterations"]), default=0) + 1
        results["pending_query"] = {"key": f"pond_{pond_n:02d}", "query": query}
        results["status"] = "ready_to_compile"
    elif action == "ranking_fix":
        prior_payload = _read_json(Path(iteration["arm"]["payload_json"]))
        results["pending_payload"] = {
            "pond_n": int(iteration["pond_n"]), "query": iteration["query"],
            "payload_json": iteration["arm"]["payload_json"], "ledger": iteration["arm"]["ledger"],
            "payload": prior_payload,
            "rerank_exclusions": list((iteration.get("input") or {}).get("rerank_exclusions") or []),
            "rerank_only": True, "pattern_default_edits": [],
        }
        results["status"] = "awaiting_payload_review"
    else:
        results["status"] = "completed"
    move = {key: proposal[key] for key in ("action", "next_query", "source", "rationale")}
    iteration["diagnosis"] = selected
    iteration["next_move"] = move
    iteration["proposal_delta"] = {
        "proposal": {"diagnosis": proposed_diagnosis, "action": proposal["action"],
                     "next_query": proposal.get("next_query"), "source": proposal.get("source")},
        "actual": {"diagnosis": selected, "action": proposal["action"],
                   "next_query": proposal.get("next_query"), "source": proposal.get("source")},
        "changed": proposed_diagnosis != selected,
    }
    _price_usage_log(run_dir / "usage.jsonl")
    iteration["cost_usd"] = _pond_costs(run_dir).get(int(iteration["pond_n"]), 0.0)
    _save(results, run_dir)
    return run_dir / "results.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("set-query", "compile-pond", "review-payload", "run-pond", "decide",
                 "reannotate-saved"):
        command = sub.add_parser(name)
        command.add_argument("--run-dir", required=True)
        if name in {"compile-pond", "run-pond", "reannotate-saved"}:
            command.add_argument("--env-file", default=str(ROOT / ".env"))
            if name in {"compile-pond", "run-pond"}:
                command.add_argument("--backend", choices=("powerset", "local"))
                command.add_argument("--db", default=str(ROOT / DEFAULT_LOCAL_DB))
            elif name == "reannotate-saved":
                command.add_argument("--pond", type=int)
        elif name == "set-query":
            command.add_argument("--query", required=True)
        elif name == "review-payload":
            command.add_argument("--payload-json")
            command.add_argument("--rerank-exclusion", action="append", default=[])
            command.add_argument("--human-reviewed", action="store_true")
        else:
            command.add_argument("--autonomous", action="store_true")
            command.add_argument("--choice", type=int, choices=(2, 3))
            command.add_argument("--diagnosis", choices=NEXT_SEARCH_DIAGNOSES)
            command.add_argument("--note", default="")
            command.add_argument("--model", default="gpt-5.6-luna")
            command.add_argument("--reasoning-effort", default="medium")
    args = parser.parse_args()
    run_dir = Path(args.run_dir).resolve()
    if args.command == "set-query":
        path = update_pending_query(run_dir=run_dir, query=args.query)
    elif args.command == "compile-pond":
        path = compile_pond(run_dir=run_dir, env_file=args.env_file,
                            backend=args.backend, db=args.db)
    elif args.command == "review-payload":
        path = review_payload(run_dir=run_dir,
                              payload_path=Path(args.payload_json) if args.payload_json else None,
                              rerank_exclusions=args.rerank_exclusion,
                              human_reviewed=args.human_reviewed)
    elif args.command == "run-pond":
        path = run_pond(run_dir=run_dir, env_file=args.env_file,
                        backend=args.backend, db=args.db)
    elif args.command == "reannotate-saved":
        path = reannotate_saved(run_dir=run_dir, env_file=args.env_file, pond=args.pond)
    else:
        path = decide(run_dir=run_dir, choice=args.choice, diagnosis=args.diagnosis,
                      note=args.note, autonomous=args.autonomous, model=args.model,
                      reasoning_effort=args.reasoning_effort)
    print(json.dumps({"status": "completed", "results": str(path)}, indent=2))


if __name__ == "__main__":
    main()
