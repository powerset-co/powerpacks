"""Research-candidate pool support for the deep-context pipeline.

Candidates are contacts the imports could NOT resolve to a LinkedIn identity;
each import writes them to its own ``import/<source>/candidates.csv``
(``packs/ingestion/schemas/candidates_schema.py``). They are absent from the
merged people.csv, so this module adapts them onto the same ``Person`` model the
pipeline already speaks — ``person_id = "candidate:<candidate_key>"`` — letting
collect/synthesize/compose/parents process them unchanged. The raw CSV row stays
retrievable by key so the mint stages (synthetic profiles, retargets) can carry
the candidate's contact identity onto the people row they produce.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterator

from packs.ingestion.primitives.deep_context.common import (
    FACTS_DIR,
    GMAIL_CHANNEL,
    IMESSAGE_CHANNEL,
    WHATSAPP_CHANNEL,
    Person,
    _collect_emails,
    _collect_phones,
)

# Import-owned candidate pools (fixed paths, same relative style as common.py).
GMAIL_CANDIDATES_CSV = Path(".powerpacks/network-import/import/gmail/candidates.csv")
MESSAGES_CANDIDATES_CSV = Path(".powerpacks/network-import/import/messages/candidates.csv")
CANDIDATE_CSVS = [GMAIL_CANDIDATES_CSV, MESSAGES_CANDIDATES_CSV]

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
    """Raw candidate rows across every existing pool, deduped by key (first file wins)."""
    seen: set[str] = set()
    for path in CANDIDATE_CSVS:
        if not path.exists():
            continue
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                key = str(row.get("candidate_key") or "").strip()
                if not key or key in seen:
                    continue
                seen.add(key)
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
            source_channels=candidate_channels(row),
        )
        if not person.emails and not person.phones:
            continue
        yield person
        yielded += 1
        if limit and yielded >= limit:
            return


def candidate_row(candidate_key: str) -> dict[str, str] | None:
    """The raw candidates.csv row for a key (None when unknown)."""
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
        "source_channels": ",".join(candidate_channels(row)),
    }


# --- Network-worth (yes | maybe | no) ----------------------------------------
# The synthesis LLM judges every profiled contact's `network_worth` from the
# actual message relationship (facts/<person_id>.jsonl). The user may overrule
# it per row via the sticky, user-owned `network_worth` column in
# overrides/review.csv. `no` gates a candidate out of paid reverse lookup and
# synthetic minting.

NETWORK_WORTH_VALUES = ("yes", "maybe", "no")
DEFAULT_NETWORK_WORTH = "maybe"


def llm_network_worth(person_id: str, facts_dir: Path = FACTS_DIR) -> dict[str, str]:
    """The synthesis LLM's {'decision','reason'} for a person ('' when absent).

    The incremental synthesizer refines ONE running profile, so the last record
    carries the final judgment."""
    path = facts_dir / f"{person_id}.jsonl"
    if not path.exists():
        return {"decision": "", "reason": ""}
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
    return worth


def effective_network_worth(
    person_id: str,
    override_rows: dict[str, dict[str, str]] | None = None,
    facts_dir: Path = FACTS_DIR,
) -> dict[str, str]:
    """Resolved worth for a person: the user's review.csv mark wins (an approved
    `exclude` action counts as a user `no` — one unified way of saying no), else the
    synthesis LLM's judgment from facts, else the machine-owned `llm_worth` column
    reconcile mirrors onto the review row (so the merge and the UI agree even when
    facts aren't on disk), else the default ('maybe' — needs a human look).
    Returns {'decision', 'reason', 'source': user|llm|default}."""
    row = (override_rows or {}).get(person_id.lower()) or {}
    user_mark = str(row.get("network_worth") or "").strip().lower()
    if user_mark in NETWORK_WORTH_VALUES:
        return {"decision": user_mark, "reason": "user decision", "source": "user"}
    if str(row.get("action") or "").strip().lower() == "exclude" and \
            str(row.get("approved") or "").strip().lower() in ("auto", "yes"):
        return {"decision": "no", "reason": "user excluded this person", "source": "user"}
    llm = llm_network_worth(person_id, facts_dir)
    if llm["decision"]:
        return {**llm, "source": "llm"}
    row_llm = str(row.get("llm_worth") or "").strip().lower()
    if row_llm in NETWORK_WORTH_VALUES:
        return {"decision": row_llm, "reason": str(row.get("llm_worth_reason") or ""), "source": "llm"}
    return {"decision": DEFAULT_NETWORK_WORTH, "reason": "not yet judged", "source": "default"}
