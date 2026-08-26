"""Retrieve prior payload edits and next-search decisions from fixed artifacts."""
from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # direct script execution
    from company_context import FIT_GROUPS
except ImportError:  # pragma: no cover - module execution
    from .company_context import FIT_GROUPS


ROOT = Path(__file__).resolve().parents[4]
SEED_PATH = ROOT / "packs/search/policies/search-harness-precedents.json"
DEFAULT_RESULTS_ROOTS = (
    ROOT / ".powerpacks/deep-search",
    Path(os.getenv(
        "POWERPACKS_SEARCH_HARNESS_LAB_ROOT",
        str(ROOT.parent / "powerpacks-lab/data/search-v2"),
    )).expanduser(),
)
STOP_WORDS = {
    "a", "an", "and", "at", "be", "by", "for", "from", "in", "is", "of", "on",
    "or", "the", "to", "with", "who",
}


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _tokens(value: Any) -> list[str]:
    return [word for word in re.findall(r"[a-z0-9]+", str(value or "").casefold())
            if len(word) > 1 and word not in STOP_WORDS]


def _text(*values: Any) -> str:
    return " ".join(str(value or "") for value in values)


def _rank(cards: Sequence[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
    if not cards:
        return []
    documents = [_tokens(card.get("retrieval_text")) for card in cards]
    document_frequency = Counter(word for words in documents for word in set(words))
    query_words = set(_tokens(query))
    scored = []
    for card, words in zip(cards, documents):
        counts = Counter(words)
        score = sum(
            math.log((len(cards) + 1) / (document_frequency[word] + .5))
            * (counts[word] * 2.2 / (counts[word] + 1.2))
            for word in query_words if counts[word]
        )
        if score:
            output = {key: value for key, value in card.items() if key != "retrieval_text"}
            output["retrieval_score"] = round(score, 4)
            scored.append((int(card.get("quality_tier") or 0), score, output))
    return [card for _tier, _score, card in
            sorted(scored, key=lambda row: (row[0], row[1]), reverse=True)[:limit]]


def _results(roots: Sequence[Path]) -> list[tuple[Path, dict[str, Any]]]:
    rows = []
    for root in roots:
        if root.is_dir():
            rows.extend((path, value) for path in root.glob("*/results.json")
                        if (value := _read(path)).get("iterations"))
    return rows


def _job_text(result: Mapping[str, Any]) -> str:
    brief = result.get("brief") or {}
    return _text(result.get("title"), brief.get("occupation"),
                 brief.get("defining_capability"), brief.get("geography"))


def retrieve_payload_edits(
    *, title: str, brief: Mapping[str, Any], query: str,
    roots: Sequence[Path] = DEFAULT_RESULTS_ROOTS, limit: int = 3,
) -> list[dict[str, Any]]:
    cards = []
    for path, result in _results(roots):
        for iteration in result.get("iterations") or []:
            pattern_edits = iteration.get("pattern_default_edits") or []
            human_delta = iteration.get("human_edit_delta") or {}
            if not pattern_edits and not human_delta:
                continue
            human_reviewed = bool(human_delta) or iteration.get("payload_reviewed") is True
            filter_delta = human_delta.get("filters") or {}
            judged_edits = []
            for edit in pattern_edits:
                field = str(edit.get("field") or "")
                changed = (any(key in filter_delta for key in
                               ("cities", "states", "countries", "metro_areas", "macro_regions"))
                           if field == "location" else field in filter_delta)
                judged_edits.append({**edit, "verdict": "reverted" if changed else "accepted"})
            job = _job_text(result)
            card = {
                "source": str(path), "job": job, "query": iteration.get("query"),
                "quality": "human_confirmed" if human_reviewed else "agent_history",
                "quality_tier": 2 if human_reviewed else 0,
                "pattern_default_edits": judged_edits,
                "human_edit_delta": human_delta or None,
            }
            card["retrieval_text"] = _text(job, iteration.get("query"), judged_edits, human_delta)
            if human_reviewed:
                cards.append(card)
    return _rank(cards, _text(title, brief, query), limit)


def _seed_fit_cards() -> list[dict[str, Any]]:
    return list(_read(SEED_PATH).get("fit_cards") or [])


def retrieve_fit_precedents(
    *, title: str, brief: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]],
    roots: Sequence[Path] = DEFAULT_RESULTS_ROOTS, limit: int = 6,
) -> list[dict[str, Any]]:
    """Retrieve generalized seed judgments and human-reviewed candidate fit."""
    cards = []
    for seed in _seed_fit_cards():
        card = dict(seed)
        card.update({"source": "seed", "quality": "jake_seed", "quality_tier": 2})
        card["retrieval_text"] = _text(seed.get("family"), seed.get("signal"),
                                       seed.get("expected_group"), seed.get("reason"))
        cards.append(card)
    for path, result in _results(roots):
        job = _job_text(result)
        role_family = (result.get("brief") or {}).get("occupation")
        for iteration in result.get("iterations") or []:
            for row in iteration.get("shortlist_grades") or []:
                override = row.get("fit_override")
                if (not isinstance(override, Mapping) or override.get("reviewed") is not True or
                        override.get("group") not in FIT_GROUPS):
                    continue
                why = str(override.get("why") or "").strip()
                if not why:
                    continue
                card = {
                    "source": "human_review", "role_family": role_family,
                    "candidate_title": row.get("title"),
                    "employer_context": {
                        "headcount": row.get("current_company_headcount"),
                        "stage": row.get("current_company_stage"),
                        "funding": row.get("current_company_funding"),
                    },
                    "group": override["group"], "reason": why,
                    "quality": "human_confirmed", "quality_tier": 2,
                }
                card["retrieval_text"] = _text(
                    job, role_family, row.get("company"), row.get("title"),
                    row.get("trait_scores"), card["employer_context"], why)
                cards.append(card)
    candidate_context = [
        _text(row.get("company"), row.get("title"), row.get("trait_scores"),
              row.get("recent_roles"))
        for row in candidates
    ]
    return _rank(cards, _text(title, brief, candidate_context), limit)


def _seed_move_cards() -> list[dict[str, Any]]:
    return list(_read(SEED_PATH).get("move_cards") or [])


def retrieve_next_moves(
    *, title: str, brief: Mapping[str, Any], query: str, diagnosis: str,
    roots: Sequence[Path] = DEFAULT_RESULTS_ROOTS, limit: int = 3,
) -> list[dict[str, Any]]:
    cards = []
    for seed in _seed_move_cards():
        card = dict(seed)
        card.update({"source": "seed", "quality": "jake_seed", "quality_tier": 2})
        card["retrieval_text"] = _text(seed.get("job"), seed.get("family"),
                                       seed.get("failure_mode"), seed.get("chain"), seed.get("reason"))
        cards.append(card)
    for path, result in _results(roots):
        iterations = list(result.get("iterations") or [])
        for index, iteration in enumerate(iterations):
            if not iteration.get("diagnosis") or not iteration.get("next_move"):
                continue
            delta = iteration.get("proposal_delta") or {}
            if not isinstance(delta, Mapping) or delta.get("reviewed") is not True:
                continue
            proposal = iteration.get("next_move") or {}
            actual = delta.get("actual") if isinstance(delta, Mapping) else None
            if not isinstance(actual, Mapping):
                next_query = (iterations[index + 1].get("query") if index + 1 < len(iterations)
                              else proposal.get("next_query"))
                actual = {"action": proposal.get("action"), "next_query": next_query}
            job = _job_text(result)
            card = {
                "source": str(path), "job": job, "failure_mode": iteration.get("diagnosis"),
                "quality": "human_confirmed", "quality_tier": 2,
                "query": iteration.get("query"), "human_note": (
                    (iteration.get("human_override") or {}).get("note")
                    if isinstance(iteration.get("human_override"), Mapping) else None),
                "proposal": {"action": proposal.get("action"),
                             "next_query": proposal.get("next_query")},
                "human_actual": dict(actual),
            }
            card["retrieval_text"] = _text(job, iteration.get("query"), iteration.get("diagnosis"),
                                           card["human_note"], actual)
            cards.append(card)
    return _rank(cards, _text(title, brief, query, diagnosis), limit)
