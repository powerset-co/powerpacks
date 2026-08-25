"""Aggregate pond evidence: pool distribution, edit deltas, and per-pond cost.

Score bands are display-only distribution evidence, never candidate-quality
labels. `_input_snapshot` freezes the editable part of a payload so the next
pond can be diffed against it, and `_pond_costs` sums the usage log by the
`pond_NN` stage tag written into every priced row.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # direct script execution
    from harness.annotate import _level, _rerank_score
except ImportError:  # pragma: no cover - module execution
    from .annotate import _level, _rerank_score

SCORE_BANDS = ("0.9+", "0.8-0.9", "0.7-0.8", "0.6-0.7", "below 0.6")
EDITABLE_FILTER_FIELDS = (
    "role_ids", "bm25_queries", "seniority_bands", "cities", "states", "countries",
    "metro_areas", "macro_regions", "is_current_role",
    "fields_of_study", "sector_types", "entity_types",
)


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
