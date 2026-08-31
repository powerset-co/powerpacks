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
    from fit_contract import (
        FIT_GROUPS, FitCard, FitDimension, FitGroup, parse_fit_card, parse_fit_dimension,
    )
except ImportError:  # pragma: no cover - module execution
    from .fit_contract import (
        FIT_GROUPS, FitCard, FitDimension, FitGroup, parse_fit_card, parse_fit_dimension,
    )


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
FIT_JD_FLOOR = 0.25
FIT_CANDIDATE_FLOOR = 0.05
FIT_SCORE_FLOOR = 0.08
FIT_EXCLUSION_FLOOR = 0.28


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


def _context_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return " ".join(_context_text(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return " ".join(_context_text(item) for item in value)
    return str(value or "")


def _terms(value: Any) -> list[str]:
    roots = []
    for word in _tokens(value):
        for suffix in ("ments", "ment", "ings", "ing", "ed", "s"):
            if word.endswith(suffix) and len(word) > len(suffix) + 3:
                word = word[:-len(suffix)]
                break
        roots.append(word)
    return roots


def _tfidf_scores(query: Any, documents: Sequence[Any]) -> list[float]:
    tokenized = [_terms(document) for document in documents]
    if not tokenized:
        return []
    query_terms = _terms(query)
    document_frequency = Counter(word for words in tokenized for word in set(words))
    count = len(tokenized)
    idf = {word: math.log((1 + count) / (1 + seen)) + 1
           for word, seen in document_frequency.items()}
    for word in set(query_terms):
        idf.setdefault(word, math.log(1 + count) + 1)

    def vector(words: Sequence[str]) -> dict[str, float]:
        counts = Counter(words)
        total = len(words) or 1
        return {word: amount / total * idf[word]
                for word, amount in counts.items() if word in idf}

    query_vector = vector(query_terms)
    query_norm = math.sqrt(sum(weight * weight for weight in query_vector.values()))
    scores = []
    for words in tokenized:
        document_vector = vector(words)
        document_norm = math.sqrt(sum(weight * weight for weight in document_vector.values()))
        numerator = sum(query_vector[word] * document_vector[word]
                        for word in query_vector.keys() & document_vector.keys())
        scores.append(numerator / (query_norm * document_norm)
                      if query_norm and document_norm else 0.0)
    return scores


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


def _seed_fit_cards() -> list[FitCard]:
    return [parse_fit_card(card) for card in _read(SEED_PATH).get("fit_cards") or []]


def _fit_candidate_parts(candidate: Mapping[str, Any]) -> list[str]:
    try:
        months = int(candidate.get("months_in_seat"))
    except (TypeError, ValueError):
        tenure = ""
    else:
        tenure = ("recent move under eighteen months" if months < 18 else
                  "established role longer than eighteen months")
    values = [
        _text(candidate.get("title"), candidate.get("company")),
        _context_text({
            "role_ids": candidate.get("current_role_ids"),
            "company_description": candidate.get("current_company_description"),
            "company_sector_types": candidate.get("current_company_sector_types"),
            "company_entity_types": candidate.get("current_company_entity_types"),
            "stage": candidate.get("current_company_stage"),
            "headcount": candidate.get("current_company_headcount"),
        }),
        tenure,
        *[_context_text(role) for role in candidate.get("recent_roles") or []],
        _context_text(candidate.get("education")),
        _context_text(candidate.get("trait_scores")),
    ]
    return [value for value in values if value.strip()]


def _fit_candidate_context(candidate: Mapping[str, Any]) -> str:
    return " ".join(_fit_candidate_parts(candidate))


def _rank_fit_cards(
    cards: Sequence[dict[str, Any]], *, title: str, brief: Mapping[str, Any],
    target_level: Any, candidate: Mapping[str, Any], limit: int,
) -> list[dict[str, Any]]:
    has_candidate_evidence = any(candidate.get(key) for key in (
        "company", "current_role_ids", "current_company_description",
        "current_company_sector_types", "current_company_entity_types",
        "current_company_stage",
        "current_company_headcount", "months_in_seat", "recent_roles", "education",
        "trait_scores",
    ))
    if not cards or not has_candidate_evidence:
        return []
    query_terms = _terms(brief.get("occupation") or title)
    query_head = query_terms[-1] if query_terms else ""
    eligible = []
    for card in cards:
        context = card.get("jd_context") or {}
        heads = context.get("role_heads") if isinstance(context, Mapping) else None
        if not heads and isinstance(context, Mapping) and context.get("occupation"):
            terms = _terms(context["occupation"])
            heads = terms[-1:] if terms else []
        normalized_heads = set(_terms(heads))
        if normalized_heads and query_head not in normalized_heads:
            continue
        eligible.append(card)
    cards = eligible
    if not cards:
        return []
    jd_query = _context_text({
        "title": title,
        "occupation": brief.get("occupation"),
        "defining_capability": brief.get("defining_capability"),
        "target_level": target_level,
    })
    candidate_query = _fit_candidate_context(candidate)
    jd_scores = _tfidf_scores(
        jd_query, [_context_text(card.get("jd_context")) for card in cards])
    candidate_scores = _tfidf_scores(
        candidate_query, [_context_text(card.get("candidate_context")) for card in cards])
    jd_exclusion_scores = _tfidf_scores(
        jd_query, [_context_text((card.get("excludes") or {}).get("jd_context"))
                   if isinstance(card.get("excludes"), Mapping) else "" for card in cards])
    candidate_exclusions = [
        _context_text((card.get("excludes") or {}).get("candidate_context"))
        if isinstance(card.get("excludes"), Mapping) else card.get("excludes")
        for card in cards]
    candidate_exclusion_scores = [0.0] * len(cards)
    for part in _fit_candidate_parts(candidate):
        candidate_exclusion_scores = [max(current, score) for current, score in zip(
            candidate_exclusion_scores, _tfidf_scores(part, candidate_exclusions))]
    ranked = []
    for card, jd_score, candidate_score, jd_exclusion, candidate_exclusion in zip(
            cards, jd_scores, candidate_scores, jd_exclusion_scores,
            candidate_exclusion_scores):
        score = math.sqrt(jd_score * candidate_score)
        if (jd_score < FIT_JD_FLOOR or candidate_score < FIT_CANDIDATE_FLOOR or
                score < FIT_SCORE_FLOOR or jd_exclusion >= FIT_EXCLUSION_FLOOR or
                candidate_exclusion >= FIT_EXCLUSION_FLOOR):
            continue
        output = dict(card)
        output["retrieval_score"] = round(score, 4)
        output["retrieval_evidence"] = {
            "jd": round(jd_score, 4),
            "candidate": round(candidate_score, 4),
            "jd_exclusion": round(jd_exclusion, 4),
            "candidate_exclusion": round(candidate_exclusion, 4),
        }
        ranked.append((score, int(card.get("quality_tier") or 0), output))
    return [card for _score, _tier, card in sorted(
        ranked, key=lambda row: (row[0], row[1]), reverse=True)[:limit]]


def load_fit_precedents(
    roots: Sequence[Path] = DEFAULT_RESULTS_ROOTS,
) -> list[FitCard]:
    """Load seed and human-reviewed fit judgments once per panel run."""
    cards = []
    for seed in _seed_fit_cards():
        card = seed.copy()
        card.update({"source": "seed", "quality": "jake_seed", "quality_tier": 2})
        cards.append(card)
    for path, result in _results(roots):
        result_jd = str(result.get("jd_id") or path.parent.name)
        source_brief = result.get("brief") or {}
        for iteration in result.get("iterations") or []:
            for row in iteration.get("shortlist_grades") or []:
                override = row.get("fit_override")
                if (not isinstance(override, Mapping) or override.get("reviewed") is not True or
                        override.get("group") not in FIT_GROUPS):
                    continue
                why = str(override.get("why") or "").strip()
                if not why:
                    continue
                source_person = str(row.get("person") or row.get("person_id") or "")
                try:
                    override_dimension = parse_fit_dimension(
                        override.get("dimension") or FitDimension.FINAL_DECISION)
                except ValueError:
                    continue
                judgment = (
                    {"group": FitGroup(str(override["group"]))}
                    if override_dimension is FitDimension.FINAL_DECISION
                    else {"label": override.get("label")}
                )
                raw_card = {
                    "id": f"human:{result_jd}:{source_person or row.get('name') or 'candidate'}",
                    "dimension": override_dimension,
                    "jd_context": {
                        "title": result.get("title"),
                        "occupation": source_brief.get("occupation"),
                        "defining_capability": source_brief.get("defining_capability"),
                    },
                    "candidate_context": {
                        "title": row.get("title"), "company": row.get("company"),
                        "role_ids": row.get("current_role_ids"),
                        "company_description": row.get("current_company_description"),
                        "company_sector_types": row.get("current_company_sector_types"),
                        "company_entity_types": row.get("current_company_entity_types"),
                        "company_stage": row.get("current_company_stage"),
                        "company_headcount": row.get("current_company_headcount"),
                        "months_in_seat": row.get("months_in_seat"),
                        "recent_roles": row.get("recent_roles"),
                        "education": row.get("education"),
                        "trait_scores": row.get("trait_scores"),
                    },
                    "judgment": judgment, "excludes": {}, "reason": why,
                    "source": str(path), "source_jd": result_jd,
                    "source_person": source_person,
                    "quality": "human_confirmed", "quality_tier": 2,
                }
                try:
                    cards.append(parse_fit_card(raw_card))
                except ValueError:
                    continue
    return list({str(card["id"]): card for card in cards}.values())


def retrieve_fit_precedents(
    *, title: str, brief: Mapping[str, Any], target_level: Any,
    candidate: Mapping[str, Any], dimension: FitDimension, source_jd: str = "",
    roots: Sequence[Path] = DEFAULT_RESULTS_ROOTS, limit: int = 2,
    cards: Sequence[object] | None = None,
) -> list[dict[str, Any]]:
    """Retrieve reviewed fit judgments for one candidate and one judge."""
    dimension = parse_fit_dimension(dimension)
    person = str(candidate.get("person") or "")
    loaded = [parse_fit_card(card) for card in (
        cards if cards is not None else load_fit_precedents(roots))]
    available = [card for card in loaded
                 if card.get("dimension") is dimension and
                 not (source_jd and card.get("source_jd") == source_jd) and
                 not (person and card.get("source_person") == person)]
    return _rank_fit_cards(
        available, title=title, brief=brief, target_level=target_level,
        candidate=candidate, limit=limit)


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
