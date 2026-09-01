"""Canonical tech-skill normalization and exact text extraction."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable


TAXONOMY_PATH = Path(__file__).parent / "data" / "tech_skills_taxonomy.json"
_QUERY_STOPWORDS = {
    "software", "backend", "frontend", "san", "data", "web", "cloud", "mobile", "design",
    "network", "system", "systems", "security", "testing", "management",
    "development", "engineering", "analysis", "architecture", "platform",
    "integration", "automation", "optimization", "modeling",
    "in", "at", "who", "with", "and", "or", "the", "a", "an",
    "for", "of", "to", "from", "by", "on", "is", "are", "was",
}
_SPECIAL_PATTERNS = {
    "c++": "c_plus_plus",
    "c#": "c_sharp",
    ".net": "dotnet",
    "node.js": "node_js",
    "react.js": "react",
    "vue.js": "vue",
    "next.js": "next_js",
    "three.js": "three_js",
}
_GO_SKILL_RE = re.compile(
    r"(?:\bGo\b(?!-)|\bgolang\b|\bgo(?!-)\s+(?:developer|engineer|experience|language|programming)\b|"
    r"\b(?:code|coding|experience|language|programming|using|with|written)\s+(?:in\s+)?go\b(?!-))"
)
_C_SKILL_RE = re.compile(
    r"(?i:\b(?:code|experience|knowledge\s+of|language|programming|using|with)\s+(?:in\s+)?c\b(?![+#])|"
    r"\bc\s+(?:developer|engineer|language|programming)\b)"
)
_R_SKILL_RE = re.compile(
    r"(?i:\b(?:code|experience|knowledge\s+of|language|programming|using|with)\s+(?:in\s+)?r\b(?!\s*&)|"
    r"\br\s+(?:developer|language|programming)\b)"
)
_RAILS_SKILL_RE = re.compile(r"(?:\bRails\b|(?i:\b(?:experience|using|with)\s+rails\b|\brails\s+experience\b))")
_LESS_SKILL_RE = re.compile(r"\bLESS\b")
_SKILL_INTENT_RE = re.compile(
    r"\b(?:built|code|coding|experience|experienced|expertise|know|knows|proficient|skills?|stack|using|uses|worked)\b",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def _taxonomy() -> tuple[dict[str, dict[str, str]], dict[str, str], int]:
    data = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    skills = data["skills"]
    lookup = {skill_id.lower(): skill_id for skill_id in skills}
    for skill_id, value in skills.items():
        lookup.setdefault(value["display"].lower(), skill_id)
    for alias, skill_id in data["aliases"].items():
        lookup.setdefault(alias.lower(), skill_id)
    longest = max(len(value.split()) for value in lookup)
    return skills, lookup, longest


def normalize(name: str) -> str | None:
    """Return one canonical skill ID, or ``None`` for a non-tech skill."""
    if not isinstance(name, str):
        return None
    _skills, lookup, _longest = _taxonomy()
    return lookup.get(name.lower().strip())


def normalize_many(values: Iterable[str | dict[str, Any]]) -> list[str]:
    """Normalize LinkedIn skill strings or ``{"name": ...}`` objects."""
    canonical: set[str] = set()
    for value in values:
        name = value.get("name") if isinstance(value, dict) else value
        skill_id = normalize(name) if isinstance(name, str) else None
        if skill_id:
            canonical.add(skill_id)
    return sorted(canonical)


def extract(text: str) -> list[str]:
    """Extract canonical skills by greedy exact display, ID, and alias matching."""
    if not isinstance(text, str) or not text.strip():
        return []

    skills, lookup, longest = _taxonomy()
    lowered = text.lower()
    words = [word.strip("()[]{}:!?\"'").rstrip(".") for word in re.sub(r"[,;|&/]", " ", lowered).split()]
    found: set[str] = set()
    index = 0
    while index < len(words):
        matched = False
        for width in range(min(longest, len(words) - index), 0, -1):
            phrase = " ".join(words[index : index + width])
            if width == 1 and phrase in _QUERY_STOPWORDS:
                break
            skill_id = lookup.get(phrase)
            if skill_id == "go" and not _GO_SKILL_RE.search(text):
                continue
            if skill_id == "c" and not _C_SKILL_RE.search(text):
                continue
            if skill_id == "r" and not _R_SKILL_RE.search(text):
                continue
            if skill_id == "rails" and not _RAILS_SKILL_RE.search(text):
                continue
            if skill_id == "less_css" and not _LESS_SKILL_RE.search(text):
                continue
            if skill_id:
                found.add(skill_id)
                index += width
                matched = True
                break
        if not matched:
            index += 1

    for pattern, skill_id in _SPECIAL_PATTERNS.items():
        if pattern in lowered and skill_id in skills:
            found.add(skill_id)
    if re.search(r"\bLESS\s+(?:CSS|stylesheet)", text):
        found.add("less_css")
    return sorted(found)


def extract_query(text: str) -> list[str]:
    """Extract a hard skill filter only when the query explicitly asks for skill evidence."""
    return extract(text) if isinstance(text, str) and _SKILL_INTENT_RE.search(text) else []
