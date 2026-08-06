"""Deterministic reduction of synthesized fact chunks into one person profile."""
from __future__ import annotations

from collections import Counter
from typing import Any

MAX_TOPICS = 25
NETWORK_WORTH_VALUES = ("yes", "maybe", "no")


def _unique(facts: list[dict[str, Any]], field: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for fact in facts:
        for value in fact.get(field) or []:
            text = str(value).strip()
            if text and text.lower() not in seen:
                values.append(text)
                seen.add(text.lower())
    return values


def merge_facts(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    facts = [chunk.get("facts") or {} for chunk in chunks if chunk.get("facts")]
    if not facts:
        return {}

    def best_scalar(field: str) -> str:
        candidates = [
            (
                fact.get("confidence") or 0.0,
                len(str(fact.get(field) or "")),
                str(fact.get(field) or "").strip(),
            )
            for fact in facts if str(fact.get(field) or "").strip()
        ]
        return max(candidates)[2] if candidates else ""

    names = [
        str(fact.get("canonical_name") or "").strip()
        for fact in facts if fact.get("canonical_name")
    ]
    canonical = Counter(names).most_common(1)[0][0] if names else ""
    employers: dict[str, dict[str, str]] = {}
    status_rank = {"current": 2, "past": 1, "unknown": 0}
    for fact in facts:
        for employer in fact.get("employers") or []:
            name = str(employer.get("name") or "").strip()
            if not name:
                continue
            key = name.lower()
            candidate = {
                "name": name,
                "role": str(employer.get("role") or "").strip(),
                "status": str(employer.get("status") or "unknown"),
            }
            incumbent = employers.setdefault(key, candidate)
            if incumbent is candidate:
                continue
            if status_rank.get(candidate["status"], 0) > status_rank.get(incumbent["status"], 0):
                incumbent["status"] = candidate["status"]
            if not incumbent["role"] and candidate["role"]:
                incumbent["role"] = candidate["role"]

    aliases: list[str] = []
    owned_identifiers = {"emails": [], "phones": [], "urls": []}
    owned_seen = {kind: set() for kind in owned_identifiers}
    for fact in facts:
        for value in fact.get("aliases") or []:
            text = str(value).strip()
            if text and text != canonical and text not in aliases:
                aliases.append(text)
        for kind in owned_identifiers:
            for value in (fact.get("owned_identifiers") or {}).get(kind) or []:
                text = str(value).strip()
                if text and text.lower() not in owned_seen[kind]:
                    owned_identifiers[kind].append(text)
                    owned_seen[kind].add(text.lower())

    events: dict[tuple[str, str], dict[str, str]] = {}
    for fact in facts:
        for event in fact.get("notable_events") or []:
            summary = str(event.get("summary") or "").strip()
            if summary:
                date = str(event.get("date") or "").strip()
                events[(date, summary.lower())] = {"date": date, "summary": summary}
    relationship = max((
        str(fact.get("relationship_to_owner") or "").strip()
        for fact in facts if str(fact.get("relationship_to_owner") or "").strip()
    ), key=len, default="")

    worth: dict[str, str] = {}
    for fact in facts:
        value = fact.get("network_worth")
        if isinstance(value, dict) and str(value.get("decision") or "").lower() in NETWORK_WORTH_VALUES:
            worth = {
                "decision": str(value.get("decision")).lower(),
                "reason": str(value.get("reason") or "").strip(),
            }
    shared: dict[str, dict[str, str]] = {}
    for fact in facts:
        for context in fact.get("shared_context") or []:
            detail = str(context.get("detail") or "").strip()
            if detail:
                shared[detail.lower()] = {
                    "overlap": str(context.get("overlap") or "other"),
                    "detail": detail,
                    "evidence": str(context.get("evidence") or "").strip(),
                }

    return {
        "canonical_name": canonical,
        "aliases": aliases,
        "employers": list(employers.values()),
        "title": best_scalar("title"),
        "school": best_scalar("school"),
        "field_of_study": best_scalar("field_of_study"),
        "location": best_scalar("location"),
        "relationship_to_owner": relationship,
        "topics": _unique(facts, "topics")[:MAX_TOPICS],
        "notable_events": sorted(events.values(), key=lambda event: event["date"] or "9999"),
        "identifiers": _unique(facts, "identifiers"),
        "owned_identifiers": owned_identifiers,
        "shared_context": list(shared.values()),
        "network_worth": worth,
        "confidence": max((fact.get("confidence") or 0.0 for fact in facts), default=0.0),
    }


def headline(merged: dict[str, Any]) -> str:
    title = merged.get("title") or ""
    employers = merged.get("employers") or []
    current = next(
        (employer for employer in employers if employer.get("status") == "current"),
        employers[0] if employers else None,
    )
    company = current.get("name") if current else ""
    if title and company:
        return f"{title} at {company}"
    if title or company:
        return title or company
    relationship = (merged.get("relationship_to_owner") or "").strip()
    if len(relationship) <= 80:
        return relationship
    prefix = relationship[:80].rsplit(" ", 1)[0].rstrip(",;:")
    return f"{prefix}…"
