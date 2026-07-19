#!/usr/bin/env python3
"""THE worth (People review) model — the whole stage in one file.

Fixed inputs — this file reads these three paths and NOTHING else:

  FACTS_DIR    .powerpacks/deep-context/facts/                   machine verdicts (source of truth)
  REVIEW_CSV   .powerpacks/network-import/overrides/review.csv   human decisions
  INDEX_JSON   .powerpacks/deep-context/index.json               identity -> parent grouping

The whole logic:

  1. Every facts/<person_id>.jsonl is one identity. A file without a
     network_worth verdict is an UNJUDGED identity — still in view, because
     rule 4 defaults it to "maybe" (nobody enters the network unreviewed).
  2. Identities under the same index.json parent are ONE person -> ONE row
     (never multiple cards for the same human). The newest facts file supplies
     the machine verdict; ties break by person_id sort.
  3. The human decision is review.csv `network_worth` (an approved `exclude`
     action is also a human no) on ANY of the person's identities; the newest
     `updated_at` wins.
  4. effective = human > machine > "maybe". effective == "maybe" is the review
     queue; "yes" is Added; "no" is Rejected.

Nothing else may filter or extend this view — no candidate pools, no
people.csv, no membership inference, no mirrors. If a judged person is missing
from the worth section, the bug is in one of the four rules above.

Created: 2026-07-19
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

FACTS_DIR = Path(".powerpacks/deep-context/facts")
REVIEW_CSV = Path(".powerpacks/network-import/overrides/review.csv")
INDEX_JSON = Path(".powerpacks/deep-context/index.json")

WORTH_VALUES = ("yes", "maybe", "no")

# facts parse cache: path -> (mtime_ns, {"name","decision","reason"})
_FACTS_CACHE: dict[str, tuple[int, dict[str, str]]] = {}


def _read_facts(path: Path) -> dict[str, str] | None:
    """Name + last network_worth verdict of one facts file (mtime-cached).
    A file without a verdict still returns (decision='') — an unjudged
    identity. Only an unreadable file returns None."""
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    cached = _FACTS_CACHE.get(str(path))
    if cached is not None and cached[0] == mtime:
        return dict(cached[1])
    out = {"decision": "", "reason": "", "name": ""}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        facts = record.get("facts") if isinstance(record.get("facts"), dict) else record
        out["name"] = str(facts.get("canonical_name") or "").strip() or out["name"]
        verdict = facts.get("network_worth")
        if isinstance(verdict, dict) and str(verdict.get("decision") or "").lower() in WORTH_VALUES:
            out["decision"] = str(verdict["decision"]).lower()
            out["reason"] = str(verdict.get("reason") or "")
    _FACTS_CACHE[str(path)] = (mtime, dict(out))
    return out


def _identity_groups(index_json: Path) -> dict[str, str]:
    """person_id (lower) -> parent key, from index.json's parent->children map."""
    try:
        index = json.loads(index_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    slugs = index.get("slugs") or {}
    mapping: dict[str, str] = {}
    for parent_key, parent in (index.get("parents") or {}).items():
        for child in parent.get("children") or []:
            pid = str((slugs.get(child) or {}).get("person_id") or "").strip().lower()
            if pid:
                mapping[pid] = parent_key
    return mapping


def _row_signal(row: dict[str, str]) -> tuple[str, str] | None:
    mark = str(row.get("network_worth") or "").strip().lower()
    if mark not in WORTH_VALUES:
        if (str(row.get("action") or "").strip().lower() == "exclude"
                and str(row.get("approved") or "").strip().lower() == "yes"):
            mark = "no"
        else:
            return None
    return mark, str(row.get("updated_at") or "")


def _signals_from_rows(rows: list[dict[str, str]]) -> dict[str, tuple[str, str]]:
    """identity key (lower person_id AND public_identifier) -> (decision, updated_at)."""
    signals: dict[str, tuple[str, str]] = {}
    for row in rows:
        signal = _row_signal(row)
        if signal is None:
            continue
        for key in (str(row.get("person_id") or "").strip().lower(),
                    str(row.get("public_identifier") or "").strip().lower()):
            if key and (key not in signals or signal[1] > signals[key][1]):
                signals[key] = signal
    return signals


def _human_signals(review_csv: Path) -> dict[str, tuple[str, str]]:
    if not review_csv.exists():
        return {}
    with review_csv.open(newline="", encoding="utf-8") as fh:
        return _signals_from_rows(list(csv.DictReader(fh)))


def rows_from(facts_dir: Path, override_rows: dict[str, dict[str, str]],
              index_json: Path = INDEX_JSON) -> list[dict[str, Any]]:
    """load() for callers that already hold review.csv rows in memory."""
    return _build(facts_dir, _signals_from_rows(list(override_rows.values())),
                  index_json)


def load(facts_dir: Path = FACTS_DIR, review_csv: Path = REVIEW_CSV,
         index_json: Path = INDEX_JSON) -> list[dict[str, Any]]:
    """All worth rows: one per PERSON (identities grouped by index parent)."""
    return _build(facts_dir, _human_signals(review_csv), index_json)


def _build(facts_dir: Path, humans: dict[str, tuple[str, str]],
           index_json: Path) -> list[dict[str, Any]]:
    groups = _identity_groups(index_json)

    people: dict[str, dict[str, Any]] = {}
    for path in sorted(facts_dir.glob("*.jsonl")):
        verdict = _read_facts(path)
        if verdict is None:
            continue
        pid = path.stem
        key = groups.get(pid.lower(), pid)
        person = people.setdefault(key, {"key": key, "person_ids": [], "machine": None,
                                         "_machine_mtime": -1, "name": ""})
        person["person_ids"].append(pid)
        mtime = path.stat().st_mtime_ns
        if mtime > person["_machine_mtime"] or (
                mtime == person["_machine_mtime"]
                and pid < (person["person_ids"] or [""])[0]):
            person["_machine_mtime"] = mtime
            person["machine"] = {"decision": verdict["decision"], "reason": verdict["reason"]}
            person["name"] = verdict.get("name") or person["name"]

    rows: list[dict[str, Any]] = []
    for person in people.values():
        marks = [humans[pid.lower()] for pid in person["person_ids"]
                 if pid.lower() in humans]
        human = max(marks, key=lambda item: item[1]) if marks else None
        machine = person["machine"] or {"decision": "", "reason": ""}
        effective = (human[0] if human else machine["decision"]) or "maybe"
        rows.append({
            "key": person["key"],
            "name": person["name"] or person["person_ids"][0],
            "person_ids": person["person_ids"],
            "machine": machine,
            "human": {"decision": human[0], "updated_at": human[1]} if human else None,
            "effective": effective,
            "source": "user" if human else ("llm" if machine["decision"] else "default"),
        })
    rows.sort(key=lambda row: (row["name"].lower(), row["key"]))
    return rows


def counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(rows),
        "pending": sum(1 for row in rows if row["effective"] == "maybe"),
        "yes": sum(1 for row in rows if row["effective"] == "yes"),
        "no": sum(1 for row in rows if row["effective"] == "no"),
    }


def rows_by_person_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Fast lookup: every identity person_id (lower) -> its person row."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        for pid in row["person_ids"]:
            out[pid.lower()] = row
    return out


def main() -> int:
    rows = load()
    summary = counts(rows)
    print(json.dumps({
        "primitive": "worth_view",
        "counts": summary,
        "pending": [{"name": row["name"], "key": row["key"]}
                    for row in rows if row["effective"] == "maybe"],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
