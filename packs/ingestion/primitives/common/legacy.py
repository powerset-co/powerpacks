#!/usr/bin/env python3
"""Cope-with-old-installs scrubs — the ONE module allowed to know legacy shapes.

Each stage calls its scrub as the first line of `execute()`; everything after
that call may assume current shapes. No other module may read or write a legacy
artifact — that prohibition is what entitles the stage's boundary parsers to be
strict.

Every entry is dated and carries a removal condition: a legacy scrub is a
countdown, not a fixture. When the condition is met, delete the line. All
scrubs are idempotent and cheap — a no-op on a current install, safe to run
every time.

Changelog:
  2026-07-31: deep-context — `ensure_owner_phones`: owner.json predating the
    phones field gets the owner's own numbers harvested from chat.db account
    metadata, so the contact-identifier policy can drop them.
  2026-07-28 (created): collected the gmail import's inline legacy unlinks
    (`ledger.json`, `candidates.csv`) into the one quarantine module.
  2026-07-30: deep-context section — pre-2026-07-27 parent-slug artifact
    migration and the retired `message-linkedin:` identity aliases.
  2026-07-30: messages section — pre-interaction-counts people.csv probe and
    the retired `import/messages/` artifacts scrub.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from packs.shared.csv_io import CsvIO
import csv
import json
from typing import Any
from packs.ingestion.schemas.people_schema import (
    generate_person_id,
    legacy_message_linkedin_id,
)
from packs.ingestion.primitives.deep_context.build_owner import harvest_owner_phones


def scrub_gmail_import(import_dir: Path) -> None:
    """Upgrade an old install's gmail import dir in place.

    2026-07-23 ledger era — remove once no install predates powerpacks-v1.0.0.
    2026-07-25 candidates.csv fold-in (#339) — remove once no install predates
    powerpacks-v1.2.1; the candidate pool merges into people.csv now, so the
    file has no writer and a stale copy would shadow the folded rows.
    """
    (import_dir / "ledger.json").unlink(missing_ok=True)
    (import_dir / "candidates.csv").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

# Files and directories in `import/messages/` that older Powerpacks versions
# wrote and nothing reads today. Each entry carries the version that orphaned it
# and the condition for deleting the entry from this list.
#
#   people.input.csv  2026-07-23 — the review-era import input. Its producer went
#                     with the in-import research/review flow retired in #315.
#                     DELETE this entry once no supported install can predate
#                     #315 (i.e. once a fresh-install-only floor is declared).
#   enrichment/       2026-07-23 — the review-era per-run enrichment scratch dir,
#                     retired with the same flow. Same removal condition.
#   candidates.csv    2026-07-26 — the separate research-candidate pool. #339
#                     folded candidates into `people.csv`, leaving this file with
#                     a reader and no writer. DELETE this entry once no supported
#                     install can predate #339.
MESSAGES_RETIRED_IMPORT_ARTIFACTS = (
    "people.input.csv",
    "enrichment",
    "candidates.csv",
)


def scrub_messages_import_dir(import_dir: Path) -> None:
    """Delete the retired `import/messages/` artifacts listed above.

    Called from the messages import's MATERIALIZE path, not from its stage entry
    — the documented exception to this file's stage-entry rule. The no-op gate
    promises that a current run writes nothing; scrubbing at stage entry would
    make a "nothing to do" run mutate the import dir anyway. Materialize is the
    first point at which the stage is already rewriting this directory.
    """
    for name in MESSAGES_RETIRED_IMPORT_ARTIFACTS:
        target = import_dir / name
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)


def messages_people_csv_predates_interaction_counts(path: Path) -> bool:
    """True when an existing `import/messages/people.csv` was written before the
    interaction-count columns existed (2026-07-23).

    The messages import's fingerprint no-op cannot catch this: the CODE changed,
    not the input data, so the fingerprints still match and the stage would keep
    serving a people.csv missing `interaction_counts`. The import calls this
    first in `execute()` and self-invalidates instead of trusting its manifest.

    DELETE this entry once no supported install can carry a people.csv written
    before that column landed.
    """
    if not path.exists():
        return False
    with path.open(newline="", encoding="utf-8") as handle:
        header = next(CsvIO.reader(handle), [])
    return bool(header) and "interaction_counts" not in header


def parent_slug_migrations(
    old_parents: dict[str, dict[str, Any]],
    new_parents: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """Exact old-slug -> new-slug mapping for unchanged canonical parent IDs."""
    old_by_id = {
        str(parent.get("parent_id") or "").strip().lower(): slug
        for slug, parent in old_parents.items()
        if str(parent.get("parent_id") or "").strip()
    }
    new_by_id = {
        str(parent.get("parent_id") or "").strip().lower(): slug
        for slug, parent in new_parents.items()
        if str(parent.get("parent_id") or "").strip()
    }
    return {
        old_by_id[parent_id]: new_slug
        for parent_id, new_slug in new_by_id.items()
        if parent_id in old_by_id and old_by_id[parent_id] != new_slug
    }


def _rewrite_parent_slug_csv(
    path: Path,
    migrations: dict[str, str],
    fields: tuple[str, ...],
) -> int:
    if not path.exists() or not migrations:
        return 0
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if not fieldnames or not any(field in fieldnames for field in fields):
        return 0
    changed = 0
    for row in rows:
        row_changed = False
        for field in fields:
            old = str(row.get(field) or "").strip()
            if old in migrations:
                row[field] = migrations[old]
                row_changed = True
        changed += row_changed
    if not changed:
        return 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)
    return changed


def _rewrite_parent_slug_jsonl(
    path: Path,
    migrations: dict[str, str],
) -> int:
    if not path.exists() or not migrations:
        return 0
    records: list[dict[str, Any]] = []
    changed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        old = str(record.get("parent_slug") or "").strip()
        if old in migrations:
            record["parent_slug"] = migrations[old]
            changed += 1
        records.append(record)
    if not changed:
        return 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return changed


def migrate_parent_slug_artifacts(
    migrations: dict[str, str],
    *,
    deep_research_dir: Path,
    verdicts_jsonl: Path,
    verdicts_csv: Path,
    applied_csv: Path,
    synthetic_people_csv: Path,
) -> dict[str, int]:
    """Rewrite exact parent-slug references without touching paid result bodies.

    Every path is explicit: this module sits under the stages and never reads
    their constants. `build_parents` passes its own resolved locations.
    """
    directories_renamed = directory_conflicts = 0
    for old_slug, new_slug in sorted(migrations.items()):
        old_dir = deep_research_dir / old_slug
        new_dir = deep_research_dir / new_slug
        if not old_dir.exists():
            continue
        if new_dir.exists():
            directory_conflicts += 1
            continue
        old_dir.rename(new_dir)
        directories_renamed += 1

    csv_rows_rewritten = 0
    csv_rows_rewritten += _rewrite_parent_slug_csv(
        deep_research_dir / "research_queue.csv",
        migrations,
        ("handle", "source_parent_slug"),
    )
    csv_rows_rewritten += _rewrite_parent_slug_csv(
        verdicts_csv, migrations, ("parent_slug",)
    )
    csv_rows_rewritten += _rewrite_parent_slug_csv(
        applied_csv, migrations, ("parent_slug",)
    )
    csv_rows_rewritten += _rewrite_parent_slug_csv(
        synthetic_people_csv, migrations, ("source_parent_slug",)
    )
    return {
        "keys": len(migrations),
        "directories_renamed": directories_renamed,
        "directory_conflicts": directory_conflicts,
        "csv_rows_rewritten": csv_rows_rewritten,
        "jsonl_rows_rewritten": _rewrite_parent_slug_jsonl(
            verdicts_jsonl, migrations
        ),
    }


# -----------------------------------------------------------------------------
# Retired message-linkedin identity aliases
#
# Retired before 2026-07-19. The messages import used to mint
# `message-linkedin:<sha16(pub)>` for a LinkedIn-matched contact before its
# durable directory id existed, then a later run silently re-keyed the contact —
# stranding facts under the retired key as a floating twin of the real person.
# BOTH keys are pure functions of the pub (retired: sha16; durable: the
# directory UUIDv5), so any review row naming the pub yields the EXACT
# equivalence. This is a key migration, not a guess.
#
# `worth_view` calls this at load, so its grouping only ever sees one identity
# per human.
#
# REMOVAL CONDITION: delete once no `facts/*.jsonl` file remains under a
# `MESSAGE_LINKEDIN_PREFIX` person id — the live import can no longer mint the
# prefix, so the population only shrinks.
# -----------------------------------------------------------------------------

MESSAGE_LINKEDIN_PREFIX = "message-linkedin:"


def message_linkedin_aliases(rows: list[dict[str, str]]) -> dict[str, str]:
    """Retired message-linkedin pid (lower) -> the same human's durable person_id.

    Entries for pubs with no stranded facts are inert.
    """
    aliases: dict[str, str] = {}
    for row in rows:
        pub = str(row.get("public_identifier") or "").strip().lower()
        # real LinkedIn pubs only — review keys can also be person-id-shaped
        # (candidate:phone:..., synth-...) and those never minted a legacy id
        if not pub or ":" in pub or pub.startswith("synth-"):
            continue
        aliases[legacy_message_linkedin_id(pub)] = generate_person_id(pub)
    return aliases


# -----------------------------------------------------------------------------
# owner.json without a "phones" field
#
# Predates contact-info-identifiers-v2 (2026-07-31). Without the owner's own
# numbers, the contact-identifier policy cannot drop them, and group-chat
# channel metadata can attribute the owner's own iMessage number to a contact's
# Contact row. Harvest once from chat.db account metadata and stamp the key
# (possibly empty); build_owner writes it on any later rebuild.
#
# REMOVAL CONDITION: delete once no install predates powerpacks v1.6.0.
# -----------------------------------------------------------------------------


def ensure_owner_phones(owner_json: Path) -> bool:
    """Fill a missing OR empty "phones" key on owner.json from chat.db account
    metadata. An EMPTY key re-harvests (cheap, ~ms) so an install synced later
    still self-heals; a populated key is never touched. Returns True when the
    file was rewritten."""
    if not owner_json.exists():
        return False
    try:
        owner = json.loads(owner_json.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(owner, dict) or owner.get("phones"):
        return False
    phones = harvest_owner_phones()
    if not phones and "phones" in owner:
        return False  # nothing found and the shape is already current
    owner["phones"] = phones
    owner_json.write_text(json.dumps(owner, indent=2) + "\n", encoding="utf-8")
    return True
