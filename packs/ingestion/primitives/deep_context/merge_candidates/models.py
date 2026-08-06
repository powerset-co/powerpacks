"""Typed person loading and identity values for merge-candidate clustering."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.common import (
    normalize_name,
    phone_digits,
    read_jsonl,
)
from packs.ingestion.primitives.deep_context.dossier.facts import merge_facts

SAMPLE_PER_DIRECTION = 6
SAMPLE_CHARS = 200


@dataclass(frozen=True)
class MergePerson:
    slug: str
    person_id: str
    name: str
    name_key: str
    emails: tuple[str, ...] = ()
    extra_emails: tuple[str, ...] = ()
    phone_digits: tuple[str, ...] = ()
    extra_phones: tuple[str, ...] = ()
    profile: dict[str, Any] | None = None
    from_me: tuple[str, ...] = ()
    from_them: tuple[str, ...] = ()


def parse_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    metadata: dict[str, Any] = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        try:
            metadata[key.strip()] = json.loads(raw.strip())
        except json.JSONDecodeError:
            metadata[key.strip()] = raw.strip().strip('"')
    return metadata


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _sample(messages: list[dict[str, Any]], direction: str) -> tuple[str, ...]:
    samples: list[str] = []
    for message in sorted(messages, key=lambda item: item.get("at") or "", reverse=True):
        if message.get("direction") != direction:
            continue
        text = (message.get("text") or "").strip()
        if text:
            samples.append(text[:SAMPLE_CHARS])
        if len(samples) >= SAMPLE_PER_DIRECTION:
            break
    return tuple(samples)


def _profile(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    records = list(read_jsonl(path))
    facts = merge_facts(records) if records else {}
    if not facts:
        return {}
    return {
        "relationship": str(facts.get("relationship_to_owner") or ""),
        "title": str(facts.get("title") or ""),
        "employers": [item.get("name", "") for item in facts.get("employers") or []
                      if item.get("name")],
        "school": str(facts.get("school") or ""),
        "location": str(facts.get("location") or ""),
        "topics": list(facts.get("topics") or [])[:8],
        "identifiers": [str(value) for value in facts.get("identifiers") or []],
        "owned_identifiers": {kind: [str(value) for value in
                                      (facts.get("owned_identifiers") or {}).get(kind) or []]
                              for kind in ("emails", "phones", "urls")},
    }


def identifier_emails(identifiers: list[str]) -> set[str]:
    values = (str(identifier).strip() for identifier in identifiers or [])
    return {
        value.lower()
        for value in values
        if "@" in value and "." in value.rsplit("@", 1)[-1]
    }


def identifier_phones(identifiers: list[str]) -> set[str]:
    phones: set[str] = set()
    for raw in identifiers or []:
        value = str(raw).strip()
        if not value or "@" in value or re.search(r"[a-z]{2,}\.[a-z]{2,}", value.lower()):
            continue
        digits = phone_digits(value)
        if 7 <= len(digits) <= 15:
            phones.add(digits)
    return phones


def owner_identifiers(base: Path) -> tuple[set[str], set[str]]:
    owner = read_json(base / "owner.json")
    emails = {value.strip().lower() for value in owner.get("emails") or [] if value.strip()}
    phones = {phone_digits(value) for value in owner.get("phones") or [] if phone_digits(value)}
    return emails, phones


def load_people(
    index: dict[str, Any], dossier_dir: Path, raw_dir: Path, facts_dir: Path,
) -> list[MergePerson]:
    by_phone = index.get("by_phone", {})
    owner_emails, owner_phones = owner_identifiers(dossier_dir.parent)
    people: list[MergePerson] = []
    for slug, info in index.get("slugs", {}).items():
        dossier = dossier_dir / f"{slug}.md"
        if not dossier.exists():
            continue
        metadata = parse_frontmatter(dossier.read_text(encoding="utf-8"))
        person_id = info.get("person_id", "")
        messages = read_json(raw_dir / f"{person_id}.json").get("messages") or []
        profile = _profile(facts_dir / f"{person_id}.jsonl")
        emails = tuple(email.lower() for email in metadata.get("emails") or [])
        owned = profile.get("owned_identifiers") or {}
        extra_emails = tuple(sorted(
            identifier_emails(owned.get("emails") or []) - set(emails) - owner_emails
        ))
        record_phones = tuple(
            digits for digits, slugs in by_phone.items() if slug in slugs
        )
        extra_phones = tuple(sorted(
            identifier_phones(owned.get("phones") or []) - set(record_phones) - owner_phones
        ))
        name = metadata.get("name") or info.get("name") or ""
        people.append(MergePerson(
            slug=slug,
            person_id=person_id,
            name=name,
            name_key=normalize_name(name),
            emails=emails,
            extra_emails=extra_emails,
            phone_digits=record_phones,
            extra_phones=extra_phones,
            profile=profile,
            from_me=_sample(messages, "from_me"),
            from_them=_sample(messages, "from_them"),
        ))
    return people


def all_emails(person: MergePerson) -> set[str]:
    return set(person.emails) | set(person.extra_emails)


def all_phones(person: MergePerson) -> set[str]:
    return set(person.phone_digits) | set(person.extra_phones)


def fmt_phone(digits: str) -> str:
    if len(digits) == 10:
        return f"+1 ({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return f"+{digits}"
