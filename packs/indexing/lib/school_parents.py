"""The school parent mapping: which school rolls up to which.

``data/education/school_parents.jsonl`` holds one row per known school:

    {"id", "key", "school_name", "parent_id", "source"}

``id`` is ``school_uuid(key)``, the same id in ``aleph_education_v3`` and in
``canonical_education_id``. ``parent_id`` is null when the school is its own
parent. ``source`` says who decided: ``seed`` (the pre-RapidAPI mapping),
``manual``, ``name`` (same normalized name) or ``llm``. A null ``source`` means
nobody has reviewed the row yet; ``update_school_parents`` only touches those.

``school_name`` is written to the doc verbatim and is what released apps match a
query against, so never trim an alias like "(MIT)" off a parent's name: an app
that has not updated loses that search.

``data/education/school_parent_reviews.jsonl`` records why a reviewed rollup
was kept or split: the flag, each independent check's reason, and the decision.
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

MAPPING_PATH = Path(__file__).resolve().parents[1] / "data/education/school_parents.jsonl"


def read_rows(path: Path = MAPPING_PATH) -> dict[str, dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return {row["id"]: row for row in rows}


def write_rows(rows: dict[str, dict[str, Any]], path: Path = MAPPING_PATH) -> None:
    path = Path(path)
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        for school_id in sorted(rows):
            handle.write(json.dumps(rows[school_id], ensure_ascii=False, sort_keys=True) + "\n")
    temp.replace(path)


@lru_cache(maxsize=1)
def school_rows() -> dict[str, dict[str, Any]]:
    return read_rows()


def parent_row(school_id: str) -> dict[str, Any] | None:
    """The row a school rolls up to, or None when it is its own parent."""
    row = school_rows().get(school_id)
    return school_rows().get(row["parent_id"]) if row and row["parent_id"] else None


def normalize_name(name: str) -> str:
    """Case, accents, punctuation, a leading "The" and a trailing parenthetical."""
    text = re.sub(r"\s*\([^)]*\)\s*$", "", name or "")
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    return re.sub(r"^the ", "", text)


def add_schools(rows: dict[str, dict[str, Any]], schools: Iterable[dict[str, Any]]) -> int:
    """Add schools the mapping has not seen, unreviewed. Returns how many."""
    added = 0
    for school in schools:
        if school["id"] not in rows:
            rows[school["id"]] = {
                "id": school["id"],
                "key": school.get("canonical_key"),
                "school_name": school["school_name"],
                "parent_id": None,
                "source": None,
            }
            added += 1
    return added


def apply_same_names(rows: dict[str, dict[str, Any]], counts: dict[str, int]) -> int:
    """Roll unreviewed name-keyed schools into a school with the same name.

    A provider id is a real entity, so a single provider-keyed school wins and
    two provider-keyed schools stay apart even when their names match. Without
    a provider-keyed school the largest name-keyed one wins.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows.values():
        groups.setdefault(normalize_name(row["school_name"]), []).append(row)
    matched = 0
    for name, group in groups.items():
        if not name or len(group) < 2:
            continue
        providers = [row for row in group if (row["key"] or "").startswith("provider:")]
        if len(providers) > 1:
            continue
        target = providers[0] if providers else max(group, key=lambda row: (counts.get(row["id"], 0), row["id"]))
        for row in group:
            if row is not target and row["source"] is None and not (row["key"] or "").startswith("provider:"):
                row["parent_id"] = target["id"]
                row["source"] = "name"
                matched += 1
    return matched


def apply_canonical_names(
    rows: dict[str, dict[str, Any]],
    reviewed: Iterable[str],
    canonical_names: dict[str, str],
    counts: dict[str, int],
) -> None:
    """Record one LLM batch: ``reviewed`` ids, ``canonical_names`` by school name."""
    by_name: dict[str, str] = {}
    for row in rows.values():
        name = normalize_name(row["school_name"])
        if name and (name not in by_name or counts.get(row["id"], 0) > counts.get(by_name[name], 0)):
            by_name[name] = row["id"]
    for school_id in reviewed:
        row = rows[school_id]
        target = by_name.get(normalize_name(canonical_names.get(row["school_name"], "")))
        row["source"] = "llm"
        if target and target != school_id:
            row["parent_id"] = target


def flatten(rows: dict[str, dict[str, Any]]) -> None:
    """Point every school at its top parent; a loop keeps its smallest id on top."""

    def top(school_id: str) -> str:
        path = [school_id]
        while (parent := rows[path[-1]]["parent_id"]) and parent in rows:
            if parent in path:
                return min(path[path.index(parent):])
            path.append(parent)
        return path[-1]

    tops = {school_id: top(school_id) for school_id in rows}
    for school_id, row in rows.items():
        row["parent_id"] = tops[school_id] if tops[school_id] != school_id else None
