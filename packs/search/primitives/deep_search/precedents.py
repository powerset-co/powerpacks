"""Retrieve prior payload edits and next-search decisions from fixed artifacts."""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[4]
SEED_PATH = ROOT / "packs/search/policies/search-harness-precedents.json"
DEFAULT_RESULTS_ROOTS = (
    ROOT / ".powerpacks/deep-search",
    ROOT.parent / "powerpacks-lab/data/search-v2",
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
            scored.append((score, output))
    return [card for _score, card in sorted(scored, key=lambda row: row[0], reverse=True)[:limit]]


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
            job = _job_text(result)
            card = {
                "source": str(path), "job": job, "query": iteration.get("query"),
                "pattern_default_edits": pattern_edits,
                "human_edit_delta": human_delta or None,
            }
            card["retrieval_text"] = _text(job, iteration.get("query"), pattern_edits, human_delta)
            cards.append(card)
    return _rank(cards, _text(title, brief, query), limit)


def _seed_move_cards() -> list[dict[str, Any]]:
    return list(_read(SEED_PATH).get("move_cards") or [])


def retrieve_next_moves(
    *, title: str, brief: Mapping[str, Any], query: str, diagnosis: str,
    roots: Sequence[Path] = DEFAULT_RESULTS_ROOTS, limit: int = 3,
) -> list[dict[str, Any]]:
    cards = []
    for seed in _seed_move_cards():
        card = dict(seed)
        card["retrieval_text"] = _text(seed.get("job"), seed.get("family"),
                                       seed.get("failure_mode"), seed.get("chain"), seed.get("reason"))
        cards.append(card)
    for path, result in _results(roots):
        iterations = list(result.get("iterations") or [])
        for index, iteration in enumerate(iterations):
            if not iteration.get("diagnosis") or not iteration.get("next_move"):
                continue
            proposal = iteration.get("next_move") or {}
            delta = iteration.get("proposal_delta") or {}
            actual = delta.get("actual") if isinstance(delta, Mapping) else None
            if not isinstance(actual, Mapping):
                next_query = (iterations[index + 1].get("query") if index + 1 < len(iterations)
                              else proposal.get("next_query"))
                actual = {"action": proposal.get("action"), "next_query": next_query}
            job = _job_text(result)
            card = {
                "source": str(path), "job": job, "failure_mode": iteration.get("diagnosis"),
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
    same_failure = [card for card in cards if card.get("failure_mode") == diagnosis]
    return _rank(same_failure or cards, _text(title, brief, query, diagnosis), limit)
