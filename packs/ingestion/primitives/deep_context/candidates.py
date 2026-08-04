"""Candidate views over canonical merged people rows.

An unresolved person is a normal merged row with a ``candidate:<contact-key>``
id and no public LinkedIn identifier. This module keeps the candidate-oriented
review and research callers working without a second import CSV contract.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterator

from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    FACTS_DIR,
    GMAIL_CHANNEL,
    IMESSAGE_CHANNEL,
    INDEX_JSON,
    WHATSAPP_CHANNEL,
    Person,
    _collect_emails,
    _collect_phones,
)

# The canonical merged network file, from its one home in common.py (which in
# turn derives the network-import root from primitives/common/paths.py).
MERGED_PEOPLE_CSV = DEFAULT_PEOPLE_CSV

PERSON_ID_PREFIX = "candidate:"

# candidates.csv `source` -> people.csv channel label (the vocabulary
# collect_person_context and the dossier layer already use).
SOURCE_TO_CHANNEL = {
    "gmail": GMAIL_CHANNEL,
    "imessage": IMESSAGE_CHANNEL,
    "whatsapp": WHATSAPP_CHANNEL,
}


def candidate_person_id(candidate_key: str) -> str:
    return f"{PERSON_ID_PREFIX}{candidate_key}"


def is_candidate_id(person_id: str) -> bool:
    return (person_id or "").startswith(PERSON_ID_PREFIX)


def candidate_key_of(person_id: str) -> str:
    """The candidate_key inside a candidate person_id ('' for any other id)."""
    pid = person_id or ""
    return pid[len(PERSON_ID_PREFIX):] if pid.startswith(PERSON_ID_PREFIX) else ""


def candidate_channels(row: dict[str, str]) -> list[str]:
    """People-style channel labels for a candidate row.

    Maps the row's ``source`` and — for messages rows — every source listed in
    ``evidence.channels`` (a contact can be on both iMessage and WhatsApp)."""
    try:
        evidence = json.loads(row.get("evidence") or "{}")
    except (json.JSONDecodeError, TypeError):
        evidence = {}
    listed = evidence.get("channels") if isinstance(evidence, dict) else None
    channels: list[str] = []
    for source in [row.get("source", ""), *(listed if isinstance(listed, list) else [])]:
        channel = SOURCE_TO_CHANNEL.get(str(source or "").strip().lower())
        if channel and channel not in channels:
            channels.append(channel)
    return channels


def iter_candidate_rows() -> Iterator[dict[str, str]]:
    """Merged people rows whose stable id is a candidate identity."""
    if not MERGED_PEOPLE_CSV.exists():
        return
    with MERGED_PEOPLE_CSV.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            person_id = str(row.get("id") or "").strip()
            if not is_candidate_id(person_id):
                continue
            row["candidate_key"] = candidate_key_of(person_id)
            row["source"] = next(iter(str(row.get("source_channels") or "").split(",")), "")
            yield row


def load_candidates(*, limit: int = 0, candidate_key: str = "") -> Iterator[Person]:
    """Yield candidates as ``Person`` rows the collect stage can process as-is.

    person_id is filename-safe by construction: keys are normalized emails/E.164
    phones (``:``/``@``/``+``/``.`` are POSIX-legal), and the rare path-hostile
    key is skipped because every stage names files ``<person_id>.json(l)``.
    """
    yielded = 0
    for row in iter_candidate_rows():
        key = str(row.get("candidate_key") or "").strip()
        if candidate_key and key != candidate_key:
            continue
        if "/" in key or "\\" in key:
            continue
        person = Person(
            person_id=candidate_person_id(key),
            full_name=str(row.get("full_name") or "").strip(),
            emails=_collect_emails(row),
            phones=_collect_phones(row),
            source_channels=(
                [channel for channel in str(row.get("source_channels") or "").split(",") if channel]
                or candidate_channels(row)
            ),
        )
        if not person.emails and not person.phones:
            continue
        yield person
        yielded += 1
        if limit and yielded >= limit:
            return


def candidate_row(candidate_key: str) -> dict[str, str] | None:
    """The canonical merged row for a candidate key (None when unknown)."""
    key = (candidate_key or "").strip()
    if not key:
        return None
    for row in iter_candidate_rows():
        if str(row.get("candidate_key") or "").strip() == key:
            return row
    return None


def candidate_carry(row: dict[str, str]) -> dict[str, Any]:
    """People-schema contact columns (apply_retargets CARRY_COLUMNS shape) sourced
    from a candidate row, for minting people rows from candidate research results."""
    return {
        "primary_email": row.get("primary_email", ""),
        "all_emails": row.get("all_emails", ""),
        "primary_phone": row.get("primary_phone", ""),
        "all_phones": row.get("all_phones", ""),
        "interaction_counts": row.get("interaction_counts", ""),
        "last_interaction": row.get("last_interaction", ""),
        "source_channels": row.get("source_channels", "") or ",".join(candidate_channels(row)),
    }


def current_parent_by_person_id(index_json: Path = INDEX_JSON) -> dict[str, str]:
    """Map every child person_id -> the CURRENT parent slug that owns it.

    This is the durable child->parent membership that ``parents/*.md`` + index.json
    already encode: ``parents[slug].children`` lists child dossier slugs and
    ``slugs[child].person_id`` names the person. A later cluster_merge can fold two
    former parents' children under one new parent; parent-scoped artifacts keyed on
    the OLD (now-dead) parent slug are re-keyed to the current parent by looking up
    their person_ids here. Keys are lowercased for case-insensitive lookup.
    """
    try:
        index = json.loads(index_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    slugs = index.get("slugs") or {}
    mapping: dict[str, str] = {}
    for parent_slug, parent in (index.get("parents") or {}).items():
        for child_slug in parent.get("children") or []:
            person_id = str((slugs.get(child_slug) or {}).get("person_id") or "").strip()
            if person_id:
                mapping.setdefault(person_id.lower(), parent_slug)
    return mapping


def candidate_identifier_key(person_id: str) -> str:
    """Normalize a ``candidate:email:X`` / ``candidate:phone:Y`` id for lookup.

    Emails lowercase; phones reduce to digits and keep the last 10 (national
    number) so ``+14125892976`` matches index.json's ``4125892976``. Shorter
    numbers keep all their digits. Non-candidate ids pass through lowercased."""
    key = str(person_id or "").strip().lower()
    if key.startswith("candidate:phone:"):
        digits = "".join(ch for ch in key if ch.isdigit())
        return f"candidate:phone:{digits[-10:] if len(digits) >= 10 else digits}"
    return key


def parent_by_candidate_identifier(index_json: Path = INDEX_JSON) -> dict[str, str]:
    """Map ``candidate:email:<addr>`` / ``candidate:phone:<digits>`` ids -> the
    current parent slug whose merged identity already owns that identifier.

    A synthetic profile minted for an import candidate is keyed to the candidate
    person, which is never clustered against real people — but the REAL parent's
    identifier union (index.json's by_email/by_phone) already knows who owns the
    candidate's email/phone. This join is what lets the synthetic ride as an
    option on the real person's review card instead of minting a duplicate."""
    try:
        index = json.loads(index_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    parents = index.get("parents") or {}
    child_parent = {
        child_slug: parent_slug
        for parent_slug, parent in parents.items()
        for child_slug in parent.get("children") or []
    }

    def owner(slug_list: Any) -> str:
        for slug in slug_list or []:
            if slug in parents:
                return str(slug)
            if slug in child_parent:
                return child_parent[slug]
        return ""

    mapping: dict[str, str] = {}
    for email, slug_list in (index.get("by_email") or {}).items():
        target = owner(slug_list)
        if target:
            mapping[candidate_identifier_key(f"candidate:email:{email}")] = target
    for phone, slug_list in (index.get("by_phone") or {}).items():
        target = owner(slug_list)
        if target:
            mapping[candidate_identifier_key(f"candidate:phone:{phone}")] = target
    return mapping


def resolve_current_parent(person_ids: list[str], stale_slug: str = "",
                           index_json: Path = INDEX_JSON) -> str:
    """The current parent slug that owns any of ``person_ids`` (via index membership).

    Falls back to ``stale_slug`` when no person_id is currently indexed under a
    parent (unchanged behavior for artifacts whose parent is still live). Empty only
    when neither a mapping nor a fallback slug is available.
    """
    mapping = current_parent_by_person_id(index_json)
    for person_id in person_ids:
        slug = mapping.get(str(person_id or "").strip().lower())
        if slug:
            return slug
    return stale_slug


def candidates_resolved_by_existing(index_json: Path = INDEX_JSON) -> set[str]:
    """Candidate person ids already folded into a canonical parent that also has
    a real people.csv child.

    Duplicate resolution has already identified these contacts, so they must not
    reappear as standalone people-review or paid-lookup subjects. Reconcile carries
    their contact fields onto the kept LinkedIn through ``consolidate-people.csv``.
    """
    try:
        index = json.loads(index_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    slugs = index.get("slugs") or {}
    resolved: set[str] = set()
    for parent in (index.get("parents") or {}).values():
        person_ids = [str((slugs.get(slug) or {}).get("person_id") or "")
                      for slug in parent.get("children") or []]
        person_ids = [person_id for person_id in person_ids if person_id]
        if any(not is_candidate_id(person_id) for person_id in person_ids):
            resolved.update(person_id.lower() for person_id in person_ids
                            if is_candidate_id(person_id))
    return resolved


# --- Network-worth (yes | maybe | no) ----------------------------------------
# The synthesis LLM judges every profiled contact's `network_worth` from the
# actual message relationship (facts/<person_id>.jsonl), then mirrors it into
# overrides/review.csv. Runtime consumers read that single review surface. The
# user may overrule it via the sticky, user-owned `network_worth` column.

NETWORK_WORTH_VALUES = ("yes", "maybe", "no")
DEFAULT_NETWORK_WORTH = "maybe"


# (facts_dir, person_id) -> (mtime_ns, worth). effective_network_worth reads
# facts per person on every model build; the stat-keyed cache makes repeat
# reads one os.stat instead of a file parse.
_WORTH_CACHE: dict[tuple[str, str], tuple[int, dict[str, str]]] = {}


def llm_network_worth(person_id: str, facts_dir: Path = FACTS_DIR) -> dict[str, str]:
    """The synthesis LLM's {'decision','reason'} for a person ('' when absent).

    The incremental synthesizer refines ONE running profile, so the last record
    carries the final judgment."""
    path = facts_dir / f"{person_id}.jsonl"
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {"decision": "", "reason": ""}
    cache_key = (str(facts_dir), person_id)
    cached = _WORTH_CACHE.get(cache_key)
    if cached is not None and cached[0] == mtime:
        return dict(cached[1])
    worth: dict[str, str] = {"decision": "", "reason": ""}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return worth
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        # The synthesizer nests the extracted profile under "facts" (see
        # synthesize_person_context.on_result); tolerate a bare facts record too.
        value = (record.get("facts") or {}).get("network_worth") or record.get("network_worth")
        if isinstance(value, dict) and str(value.get("decision") or "").lower() in NETWORK_WORTH_VALUES:
            worth = {
                "decision": str(value.get("decision")).lower(),
                "reason": str(value.get("reason") or ""),
            }
    _WORTH_CACHE[cache_key] = (mtime, dict(worth))
    return worth


def effective_network_worth(
    person_id: str,
    override_rows: dict[str, dict[str, str]] | None = None,
    facts_dir: Path = FACTS_DIR,
) -> dict[str, str]:
    """Resolved worth for a person: the user's review.csv mark wins (an approved
    `exclude` action counts as a user `no` — one unified way of saying no), else
    the machine verdict read DIRECTLY from facts/<pid>.jsonl (the source of
    truth — owner decision 2026-07-19: the worth view shows the facts verdict
    regardless of any mirror/membership state), else the review.csv `llm_worth`
    mirror for people whose facts file is absent, else the default ('maybe' —
    needs a human look). Returns {'decision', 'reason', 'source': user|llm|default}."""
    row = (override_rows or {}).get(person_id.lower()) or {}
    user_mark = str(row.get("network_worth") or "").strip().lower()
    if user_mark in NETWORK_WORTH_VALUES:
        return {"decision": user_mark, "reason": "user decision", "source": "user"}
    if str(row.get("action") or "").strip().lower() == "exclude" and \
            str(row.get("approved") or "").strip().lower() == "yes":
        return {"decision": "no", "reason": "user excluded this person", "source": "user"}
    facts = llm_network_worth(person_id, facts_dir)
    if facts.get("decision") in NETWORK_WORTH_VALUES:
        return {"decision": facts["decision"], "reason": facts.get("reason") or "",
                "source": "llm"}
    row_llm = str(row.get("llm_worth") or "").strip().lower()
    if row_llm in NETWORK_WORTH_VALUES:
        return {"decision": row_llm, "reason": str(row.get("llm_worth_reason") or ""), "source": "llm"}
    return {"decision": DEFAULT_NETWORK_WORTH, "reason": "not yet judged", "source": "default"}
