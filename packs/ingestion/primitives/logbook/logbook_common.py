"""Input parsing + shared layout for the $logbook raw-archive pipeline.

Two CSV shapes are accepted and auto-detected:

  * "founder" CSV — columns ``Founder, Cell, Emails, WhatsApp Groups``.
      ``Cell`` is comma-separated phones; ``Emails`` is semicolon-separated; the
      ``WhatsApp Groups`` cell names ONE group to archive as its own entry.
  * merged ``people.csv`` — the canonical network-import schema (``id``,
      ``full_name``, ``primary_email``/``all_emails``, ``primary_phone``/...).

Identity normalization uses the shared contact/message helpers so the same
phone/email keys resolve to the same messages across ingestion pipelines.

Slugs: a top-level entry is a PERSON (``slugify(name, id)`` — name + short id
suffix, collision-proof) or a GROUP (``group_slug(name)`` — clean name slug). A
group is its own entry written once, which also kills cross-person duplication.

Changelog:
  2026-08-06: parse merged people.csv rows at the logbook input boundary;
  Deep Context no longer owns a general file reader.
  2026-07-23 (audit dedup): normalize_email imports from common.contact_fields instead of deep_context.common (deduped there); no behavior change.
"""
from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from packs.ingestion.primitives.common.contact_fields import (
    normalize_email,
    normalize_name_key as normalize_name,
)
from packs.ingestion.primitives.deep_context.common import Person
from packs.ingestion.primitives.discover.messages.wacli.util import (
    canonicalize_phone as normalize_phone,
)
from packs.ingestion.schemas.people_schema import parse_jsonish

# --- Fixed output layout (one dir, append-only sync; no ledgers, no run ids) ---
LOGBOOK_ROOT = Path(".powerpacks/logbook")
INDEX_MD = LOGBOOK_ROOT / "index.md"
MANIFEST_JSON = LOGBOOK_ROOT / "manifest.json"

# Store defaults (expanded at the CLI layer via Path(...).expanduser()).
DEFAULT_MSGVAULT_DB = "~/.msgvault/msgvault.db"
DEFAULT_CHAT_DB = "~/Library/Messages/chat.db"
DEFAULT_WACLI_DB = ".powerpacks/messages/wacli/wacli.db"

# A logbook person is tried against EVERY requested channel (we want to find them
# wherever they are), so founder rows are tagged with all three message channels.
ALL_MESSAGE_CHANNELS = ["gmail_msgvault", "imessage", "whatsapp"]


@dataclass
class GroupTarget:
    """A named group chat to archive as its own top-level entry."""

    name: str            # display name from the CSV, e.g. "George S - Powerset"
    member_name: str     # the person row it came from (for cross-linking)
    channel: str         # "whatsapp" | "imessage"

    @property
    def slug(self) -> str:
        return group_slug(self.name)


def group_slug(name: str) -> str:
    """Clean, readable slug for a group name (no id suffix — names are the key)."""
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return base or "group"


def _founder_person_id(name: str, emails: list[str], phones: list[str]) -> str:
    """Deterministic stable id for a founder row (no id column in that CSV)."""
    key = "|".join([normalize_name(name), *sorted(emails), *sorted(phones)])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _split(value: str, sep: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(sep) if part.strip()]


def _detect_schema(fieldnames: list[str]) -> str:
    lower = {f.strip().lower() for f in (fieldnames or [])}
    if "founder" in lower:
        return "founder"
    if "id" in lower and "full_name" in lower:
        return "merged"
    # Be forgiving: a name+email/phone CSV is treated as founder-shaped.
    if {"name", "emails"} & lower or {"name", "cell"} & lower:
        return "founder"
    return "merged"


def _founder_row_to_person(row: dict[str, str]) -> tuple[Person, GroupTarget | None]:
    name = (row.get("Founder") or row.get("Name") or row.get("name") or "").strip()
    raw_phones = row.get("Cell") or row.get("Phone") or row.get("phones") or ""
    raw_emails = row.get("Emails") or row.get("Email") or row.get("emails") or ""
    phones: list[str] = []
    for value in _split(raw_phones, ","):
        norm = normalize_phone(value)
        if norm and norm not in phones:
            phones.append(norm)
    emails: list[str] = []
    for value in _split(raw_emails, ";"):
        norm = normalize_email(value)
        if norm and "@" in norm and norm not in emails:
            emails.append(norm)
    person = Person(
        person_id=_founder_person_id(name, emails, phones),
        full_name=name,
        emails=emails,
        phones=phones,
        source_channels=list(ALL_MESSAGE_CHANNELS),
    )
    group_name = (row.get("WhatsApp Groups") or row.get("WhatsApp Group") or "").strip()
    group = GroupTarget(name=group_name, member_name=name, channel="whatsapp") if group_name else None
    return person, group


def _list_values(value: str) -> list[str]:
    parsed = parse_jsonish(value, None)
    items = parsed if isinstance(parsed, list) else [parsed or value]
    return list(dict.fromkeys(
        text for item in items if (text := str(item or "").strip())
    ))


def _merged_row_to_person(row: dict[str, str]) -> Person | None:
    person_id = str(row.get("id") or "").strip()
    if not person_id:
        return None
    emails = []
    for value in [row.get("primary_email", ""), *_list_values(row.get("all_emails", ""))]:
        normalized = normalize_email(value)
        if normalized and "@" in normalized and normalized not in emails:
            emails.append(normalized)
    phones = []
    for value in [row.get("primary_phone", ""), *_list_values(row.get("all_phones", ""))]:
        normalized = normalize_phone(value)
        if normalized and normalized not in phones:
            phones.append(normalized)
    return Person(
        person_id=person_id,
        full_name=str(row.get("full_name") or "").strip(),
        emails=emails,
        phones=phones,
        source_channels=[
            channel.strip()
            for channel in str(row.get("source_channels") or "").split(",")
            if channel.strip()
        ],
    )


def load_people_from_csv(
    csv_path: Path,
    *,
    limit: int = 0,
    slug: str = "",
) -> tuple[list[Person], list[GroupTarget]]:
    """Yield ``(people, group_targets)`` from either accepted CSV shape.

    ``slug`` restricts to a single person- or group-slug (for ``--slug`` runs).
    """
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        schema = _detect_schema(reader.fieldnames or [])
        people: list[Person] = []
        groups: list[GroupTarget] = []
        if schema == "merged":
            # Merged people.csv is a Logbook input boundary; it has no groups.
            loaded = 0
            for row in reader:
                person = _merged_row_to_person(row)
                if person is None:
                    continue
                loaded += 1
                if slug and person.slug != slug:
                    pass
                else:
                    people.append(person)
                if limit and loaded >= limit:
                    break
            return people, groups
        seen_groups: set[str] = set()
        for row in reader:
            person, group = _founder_row_to_person(row)
            if not person.full_name and not person.emails and not person.phones:
                continue
            if group and group.slug not in seen_groups:
                seen_groups.add(group.slug)
                groups.append(group)
            if slug:
                if person.slug == slug:
                    people.append(person)
                continue
            people.append(person)
            if limit and len(people) >= limit:
                break
    if slug:
        groups = [g for g in groups if g.slug == slug]
    return people, groups


def iter_people_from_csv(csv_path: Path, *, limit: int = 0, slug: str = "") -> Iterator[Person]:
    """Convenience: just the people (used where group targets are irrelevant)."""
    people, _ = load_people_from_csv(csv_path, limit=limit, slug=slug)
    yield from people
