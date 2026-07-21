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
     TWO exclusions: a person whose every identity is a retired
     message-linkedin:* key is a GHOST — present in no population file, so no
     decision on them can act — and a person any of whose identities carries
     synthesis's is_owner flag is the MAILBOX OWNER, not a contact. Neither
     is shown (see the _build comments).
  2. Identities under the same index.json parent are ONE person -> ONE row
     (never multiple cards for the same human). An identity keyed by the
     RETIRED message-linkedin recipe folds into its durable sibling (the
     recipe is a pure function of the review row's pub — an exact key
     migration, see _legacy_aliases). The newest facts file supplies
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

from packs.ingestion.schemas.people_schema import (
    generate_person_id,
    legacy_message_linkedin_id,
)

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
    out = {"decision": "", "reason": "", "name": "", "is_owner": False}
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
        out["is_owner"] = bool(facts.get("is_owner")) or out["is_owner"]
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


def _legacy_aliases(rows: list[dict[str, str]]) -> dict[str, str]:
    """Retired message-linkedin pid (lower) -> the same human's durable person_id.

    The messages import used to mint `message-linkedin:<sha16(pub)>` for a
    LinkedIn-matched contact before its durable directory id existed, then a
    later run silently re-keyed the contact — stranding facts under the retired
    key as a floating twin of the real person. BOTH keys are pure functions of
    the pub (retired: sha16; durable: the directory UUIDv5), so any review row
    that names the pub yields the exact equivalence — a key migration, not a
    guess. Entries for pubs with no stranded facts are inert."""
    aliases: dict[str, str] = {}
    for row in rows:
        pub = str(row.get("public_identifier") or "").strip().lower()
        # real LinkedIn pubs only — review keys can also be person-id-shaped
        # (candidate:phone:..., synth-...) and those never minted a legacy id
        if not pub or ":" in pub or pub.startswith("synth-"):
            continue
        aliases[legacy_message_linkedin_id(pub)] = generate_person_id(pub)
    return aliases


def rows_from(facts_dir: Path, override_rows: dict[str, dict[str, str]],
              index_json: Path = INDEX_JSON) -> list[dict[str, Any]]:
    """load() for callers that already hold review.csv rows in memory."""
    rows = list(override_rows.values())
    return _build(facts_dir, _signals_from_rows(rows), index_json,
                  aliases=_legacy_aliases(rows))


def load(facts_dir: Path = FACTS_DIR, review_csv: Path = REVIEW_CSV,
         index_json: Path = INDEX_JSON) -> list[dict[str, Any]]:
    """All worth rows: one per PERSON (identities grouped by index parent)."""
    if review_csv.exists():
        with review_csv.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    else:
        rows = []
    return _build(facts_dir, _signals_from_rows(rows), index_json,
                  aliases=_legacy_aliases(rows))


def _build(facts_dir: Path, humans: dict[str, tuple[str, str]],
           index_json: Path, aliases: dict[str, str] | None = None) -> list[dict[str, Any]]:
    groups = _identity_groups(index_json)
    aliases = aliases or {}

    people: dict[str, dict[str, Any]] = {}
    for path in sorted(facts_dir.glob("*.jsonl")):
        verdict = _read_facts(path)
        if verdict is None:
            continue
        pid = path.stem
        # A retired-key identity groups AS its durable sibling (rule 2: one
        # person, one row) — via the sibling's index parent when it has one.
        canon = aliases.get(pid.lower(), pid)
        key = groups.get(canon.lower(), canon)
        person = people.setdefault(key, {"key": key, "person_ids": [], "machine": None,
                                         "_machine_mtime": -1, "name": "", "_owner": False})
        person["person_ids"].append(pid)
        person["_owner"] = person["_owner"] or bool(verdict.get("is_owner"))
        mtime = path.stat().st_mtime_ns
        if mtime > person["_machine_mtime"] or (
                mtime == person["_machine_mtime"]
                and pid < (person["person_ids"] or [""])[0]):
            person["_machine_mtime"] = mtime
            person["machine"] = {"decision": verdict["decision"], "reason": verdict["reason"]}
            person["name"] = verdict.get("name") or person["name"]

    rows: list[dict[str, Any]] = []
    for person in people.values():
        # GHOSTS are not reviewable: a person whose EVERY identity is a
        # retired message-linkedin:* key exists in no population file — a Yes
        # cannot add them to the network and a No rejects nothing that could
        # have entered, so the card is pure decision-theater. The live import
        # can no longer mint this prefix, folding (aliases above) has already
        # claimed any ghost with a durable sibling, and a real identity
        # re-appears here the moment the contact matches again.
        if all(pid.startswith("message-linkedin:") for pid in person["person_ids"]):
            continue
        # The OWNER is not a network-membership decision: synthesis flags the
        # mailbox owner's own identities (is_owner), build_parents already
        # refuses to make them a parent — the review honors the same flag.
        if person["_owner"]:
            continue
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
