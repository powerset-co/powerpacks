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
     (never multiple cards for the same human). Child machine verdicts aggregate
     as Yes > Maybe > No, so a real relationship on any channel wins. An identity keyed by the
     RETIRED message-linkedin recipe keeps its current indexed parent when
     present, otherwise it folds into its durable sibling (the recipe is a pure
     function of the review row's pub — an exact key migration, see
     _legacy_aliases).
  3. The human decision is the canonical parent row's review.csv
     `network_worth`. Legacy child decisions are read only as a migration
     fallback; `parents` moves them into the parent row.
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

from packs.ingestion.primitives.deep_context.build_parents import parent_id_for
from packs.ingestion.primitives.deep_context.review_store import (
    HUMAN_WORTH_VALUES,
    OVERRIDE_COLUMNS,
    PARENT_WORTH_PREFIX,
    is_parent_worth_row,
    load_override_rows,
    parent_id_from_worth_key,
    parent_worth_key,
    parse_worth_person_ids,
    write_override_rows,
)
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.schemas.people_schema import generate_person_id, legacy_message_linkedin_id

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


def _identity_groups(index_json: Path) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Return child membership and canonical parent metadata from index.json."""
    try:
        index = json.loads(index_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, {}
    slugs = index.get("slugs") or {}
    mapping: dict[str, str] = {}
    parents: dict[str, dict[str, Any]] = {}
    for parent_key, parent in (index.get("parents") or {}).items():
        person_ids: list[str] = []
        for child in parent.get("children") or []:
            pid = str((slugs.get(child) or {}).get("person_id") or "").strip().lower()
            if pid:
                mapping[pid] = parent_key
                person_ids.append(pid)
        canonical_id = str(parent.get("parent_id") or "").strip().lower()
        if not canonical_id and person_ids:
            canonical_id = parent_id_for(person_ids)
        parents[parent_key] = {
            "parent_id": canonical_id,
            "name": str(parent.get("name") or "").strip(),
            "person_ids": person_ids,
        }
    return mapping, parents


def _row_signal(row: dict[str, str]) -> tuple[str, str] | None:
    mark = str(row.get("network_worth") or "").strip().lower()
    if mark not in WORTH_VALUES:
        if (str(row.get("action") or "").strip().lower() == "exclude"
                and str(row.get("approved") or "").strip().lower() == "yes"):
            mark = "no"
        else:
            return None
    return mark, str(row.get("updated_at") or "")


def _signals_from_rows(
    rows: list[dict[str, str]],
) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    """Return human signals keyed by legacy identity and canonical parent id."""
    identities: dict[str, tuple[str, str]] = {}
    parents: dict[str, tuple[str, str]] = {}
    for row in rows:
        signal = _row_signal(row)
        if signal is None:
            continue
        parent_id = parent_id_from_worth_key(
            str(row.get("public_identifier") or "")
        )
        if parent_id and (parent_id not in parents or signal[1] > parents[parent_id][1]):
            parents[parent_id] = signal
        for key in (str(row.get("person_id") or "").strip().lower(),
                    str(row.get("public_identifier") or "").strip().lower()):
            if (key and not key.startswith(PARENT_WORTH_PREFIX)
                    and (key not in identities or signal[1] > identities[key][1])):
                identities[key] = signal
    return identities, parents


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
    identity_humans, parent_humans = _signals_from_rows(rows)
    return _build(facts_dir, identity_humans, parent_humans, index_json,
                  aliases=_legacy_aliases(rows))


def load(facts_dir: Path = FACTS_DIR, review_csv: Path = REVIEW_CSV,
         index_json: Path = INDEX_JSON) -> list[dict[str, Any]]:
    """All worth rows: one per PERSON (identities grouped by index parent)."""
    if review_csv.exists():
        with review_csv.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    else:
        rows = []
    identity_humans, parent_humans = _signals_from_rows(rows)
    return _build(facts_dir, identity_humans, parent_humans, index_json,
                  aliases=_legacy_aliases(rows))


_MACHINE_PRIORITY = {"no": 0, "maybe": 1, "yes": 2}


def _build(
    facts_dir: Path,
    identity_humans: dict[str, tuple[str, str]],
    parent_humans: dict[str, tuple[str, str]],
    index_json: Path,
    aliases: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    groups, indexed_parents = _identity_groups(index_json)
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
        # Current parent membership is authoritative.  A retired
        # message-linkedin identity may still be a real child of the current
        # parent; only fall back to its generated durable-id alias when that
        # retired key is absent from the index.
        key = groups.get(pid.lower()) or groups.get(canon.lower(), canon)
        indexed = indexed_parents.get(key) or {}
        canonical_id = str(indexed.get("parent_id") or "").strip().lower()
        if not canonical_id:
            canonical_id = parent_id_for([canon])
        person = people.setdefault(key, {
            "key": parent_worth_key(canonical_id),
            "parent_id": canonical_id,
            "parent_slug": key,
            "person_ids": [],
            "_machine_candidates": [],
            "name": str(indexed.get("name") or ""),
            "_owner": False,
        })
        person["person_ids"].append(pid)
        person["_owner"] = person["_owner"] or bool(verdict.get("is_owner"))
        decision = str(verdict.get("decision") or "").strip().lower()
        person["_machine_candidates"].append({
            "decision": decision if decision in WORTH_VALUES else "maybe",
            "reason": str(verdict.get("reason") or ""),
            "person_id": pid.lower(),
            "source": "llm" if decision in WORTH_VALUES else "default",
        })
        person["name"] = person["name"] or verdict.get("name") or ""

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
        candidates = sorted(
            person.pop("_machine_candidates"),
            key=lambda item: (-_MACHINE_PRIORITY[item["decision"]], item["person_id"]),
        )
        person.pop("_owner", None)
        machine = candidates[0] if candidates else {
            "decision": "maybe", "reason": "", "source": "default",
        }
        # Parent rows are authoritative. Legacy child-level human decisions are
        # read only as a migration fallback so existing reviews are not lost.
        human = parent_humans.get(person["parent_id"])
        if human is None:
            legacy_marks = [
                identity_humans[pid.lower()]
                for pid in person["person_ids"]
                if pid.lower() in identity_humans
            ]
            human = max(legacy_marks, key=lambda item: item[1]) if legacy_marks else None
        effective = human[0] if human else machine["decision"]
        rows.append({
            **person,
            "name": person["name"] or person["person_ids"][0],
            "machine": machine,
            "human": {"decision": human[0], "updated_at": human[1]} if human else None,
            "effective": effective,
            "source": "user" if human else machine["source"],
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


def sync_parent_worth_rows(
    review_csv: Path,
    facts_dir: Path,
    index_json: Path = INDEX_JSON,
) -> dict[str, int]:
    """Materialize one machine/human worth row per current facts-backed parent.

    Child facts remain the machine source of truth. Existing child-level human
    marks are migrated once, then cleared so later decisions have exactly one
    durable owner: ``parent-worth:<parent_id>``. The row also stores its child
    membership so that decision follows the people when clustering changes.
    """
    override_rows = load_override_rows(review_csv)
    view_rows = rows_from(facts_dir, override_rows, index_json)
    legacy_keys_by_pid: dict[str, set[str]] = {}
    for key, row in override_rows.items():
        if is_parent_worth_row(row, key):
            continue
        for pid in {
            key,
            str(row.get("person_id") or "").strip().lower(),
        } - {""}:
            legacy_keys_by_pid.setdefault(pid, set()).add(key)

    prior_parent_rows = {
        key: row
        for key, row in override_rows.items()
        if is_parent_worth_row(row, key)
    }
    current_keys = {str(row["key"]) for row in view_rows}
    current_person_ids = {
        str(person_id or "").strip().lower()
        for row in view_rows
        for person_id in row.get("person_ids") or []
        if str(person_id or "").strip()
    }
    residual_parent_rows: dict[str, dict[str, str]] = {}
    residual_ids_on_current_key: dict[str, set[str]] = {}
    for prior_key, prior in prior_parent_rows.items():
        prior_person_ids = set(parse_worth_person_ids(prior))
        if not prior_person_ids.intersection(current_person_ids):
            continue
        residual_ids = prior_person_ids - current_person_ids
        if not residual_ids:
            continue
        if prior_key in current_keys:
            residual_ids_on_current_key[prior_key] = residual_ids
        else:
            residual = dict(prior)
            residual["worth_person_ids"] = "|".join(sorted(residual_ids))
            residual_parent_rows[prior_key] = residual

    legacy_marks_cleared = 0
    human_migrated = 0
    consumed_prior_keys: set[str] = set()
    next_parent_rows: dict[str, dict[str, str]] = {}
    for worth in view_rows:
        key = str(worth["key"])
        person_ids = sorted({
            str(person_id or "").strip().lower()
            for person_id in worth.get("person_ids") or []
            if str(person_id or "").strip()
        } | residual_ids_on_current_key.get(key, set()))
        person_id_set = set(person_ids)
        row = dict(prior_parent_rows.get(key) or {
            column: "" for column in OVERRIDE_COLUMNS
        })
        human_candidates: list[tuple[str, str, str]] = []
        human = worth.get("human") or {}
        human_decision = str(human.get("decision") or "").strip().lower()
        # Only attribute the view's human signal to this key when this parent
        # row already exists. On the first migration, the same signal came
        # from a legacy child and is collected below with its real source key.
        if human_decision in HUMAN_WORTH_VALUES and key in prior_parent_rows:
            human_candidates.append((
                human_decision,
                str(human.get("updated_at") or ""),
                key,
            ))
        for person_id in person_ids:
            for legacy_key in legacy_keys_by_pid.get(person_id, set()):
                legacy_signal = _row_signal(override_rows[legacy_key])
                if legacy_signal is not None:
                    human_candidates.append((
                        legacy_signal[0],
                        legacy_signal[1],
                        legacy_key,
                    ))
        for prior_key, prior in prior_parent_rows.items():
            overlaps = bool(
                person_id_set.intersection(parse_worth_person_ids(prior))
            )
            if overlaps:
                consumed_prior_keys.add(prior_key)
            prior_decision = str(prior.get("network_worth") or "").strip().lower()
            if (
                prior_decision in HUMAN_WORTH_VALUES
                and overlaps
            ):
                human_candidates.append((
                    prior_decision,
                    str(prior.get("updated_at") or ""),
                    prior_key,
                ))
        winner = (
            max(
                human_candidates,
                key=lambda item: (
                    item[1],
                    item[2] == key,
                    item[2] in prior_parent_rows,
                    item[2],
                ),
            )
            if human_candidates
            else None
        )
        row.update({
            "public_identifier": key,
            "worth_person_ids": "|".join(person_ids),
            "llm_worth": str((worth.get("machine") or {}).get("decision") or "maybe"),
            "llm_worth_reason": str((worth.get("machine") or {}).get("reason") or ""),
            "source": row.get("source") or "deep-context-parent-worth",
        })
        if winner is not None:
            row["network_worth"] = winner[0]
            row["updated_at"] = winner[1] or now_iso()
            human_migrated += winner[2] != key
        else:
            row["network_worth"] = ""
        if not row.get("updated_at"):
            row["updated_at"] = now_iso()
        next_parent_rows[key] = row

        for pid in worth.get("person_ids") or []:
            for legacy_key in legacy_keys_by_pid.get(str(pid).lower(), set()):
                legacy_row = override_rows[legacy_key]
                if (
                    str(legacy_row.get("network_worth") or "").strip().lower()
                    in HUMAN_WORTH_VALUES
                ):
                    legacy_row["network_worth"] = ""
                    legacy_marks_cleared += 1
                if (
                    str(legacy_row.get("action") or "").strip().lower() == "exclude"
                    and str(legacy_row.get("approved") or "").strip().lower() == "yes"
                ):
                    legacy_row["action"] = ""
                    legacy_row["approved"] = ""

    for key in list(override_rows):
        if (
            is_parent_worth_row(override_rows[key], key)
            and (key in current_keys or key in consumed_prior_keys)
        ):
            override_rows.pop(key)
    override_rows.update(next_parent_rows)
    override_rows.update(residual_parent_rows)
    write_override_rows(review_csv, override_rows)
    return {
        "parent_rows": len(view_rows),
        "human_migrated": human_migrated,
        "legacy_marks_cleared": legacy_marks_cleared,
        "stale_parent_rows_removed": len(
            consumed_prior_keys - current_keys - set(residual_parent_rows)
        ),
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
