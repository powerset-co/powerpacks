"""Reviewed-candidate summary, manifest, and the save that persists both.

`_save` is the single write door for `results.json`: it stamps `updated_at`,
rebuilds the summary, exports the shortlist CSVs once the run is completed, and
writes the stage manifest beside it.

  results -> _enrich_summary_sources (source operator/channel from the run JSONL)
          -> build_search_summary (dedupe same-JD runs into four review groups)
          -> export_search_summary (shortlist.csv, relationship.csv)
          -> _manifest
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # direct script execution
    from company_context import FIT_GROUPS
    from harness.artifacts import (
        _now, _read_json, _usage_cost, _write_json, resolve_artifact_path,
    )
except ImportError:  # pragma: no cover - module execution
    from ..company_context import FIT_GROUPS
    from .artifacts import (
        _now, _read_json, _usage_cost, _write_json, resolve_artifact_path,
    )
from packs.search.primitives.export_candidate_shortlist.export_candidate_shortlist import (
    write_shortlist_csv,
)


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
