"""Input parsing + shared layout for the $logbook raw-archive pipeline.

Two CSV shapes are accepted and auto-detected:

  * "founder" CSV — columns ``Founder, Cell, Emails, WhatsApp Groups``.
      ``Cell`` is comma-separated phones; ``Emails`` is semicolon-separated; the
      ``WhatsApp Groups`` cell names ONE group to archive as its own entry.
  * merged ``people.csv`` — the canonical network-import schema (``id``,
      ``full_name``, ``primary_email``/``all_emails``, ``primary_phone``/...).

Identity normalization is reused verbatim from ``deep_context.common`` so the
same phone/email keys resolve to the same messages both pipelines see.

Slugs: a top-level entry is a PERSON (``slugify(name, id)`` — name + short id
suffix, collision-proof) or a GROUP (``group_slug(name)`` — clean name slug). A
group is its own entry written once, which also kills cross-person duplication.
"""
from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from packs.ingestion.primitives.deep_context.common import (
    Person,
    load_people,
    normalize_email,
    normalize_name,
    normalize_phone,
    slugify,
)

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
            # Delegate to the canonical reader; no group column in this shape.
            for person in load_people(csv_path, limit=limit, require_channels=False):
                if slug and person.slug != slug:
                    continue
                people.append(person)
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
