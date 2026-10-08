"""The `lookup` command: a person by name, email or phone, with everything the store knows about them.

Matches candidates by written name (every word of the name given appears in the written name or the
dossier's canonical name) or by a normalized identifier, groups them into families, and prints each
family's facts: who they are, how the owner knows them, their employers, topics and events, their
identifiers and the one LinkedIn the store confirmed. `--json` prints the same as JSON. Nothing is
written; nothing is spent.

Created: 2026-10-07
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.contact_fields import normalize_email, normalize_phone
from packs.ingestion.primitives.deep_context_v2.db import queries_worth
from packs.ingestion.primitives.deep_context_v2.db.queries_worth import MemberFacts
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.worth.evidence import family_facts


@dataclass(frozen=True)
class Match:
    parent_id: str
    name: str
    relationship_to_owner: str
    emails: tuple[str, ...]
    phones: tuple[str, ...]
    linkedin_url: str
    facts: dict[str, Any]


def _words(text: str) -> set[str]:
    return {word for word in text.lower().replace(",", " ").split() if word}


def matches(conn: sqlite3.Connection, *, name: str = "", email: str = "", phone: str = "") -> list[Match]:
    """Every family one of the given keys lands on."""
    parents: set[str] = set()
    if email:
        for row in conn.execute(
            "SELECT p.parent_id FROM candidate_identifiers i JOIN current_parent p USING (candidate_id) "
            "WHERE i.kind = 'email' AND i.normalized_value = ?", (normalize_email(email),)):
            parents.add(row["parent_id"])
    if phone:
        for row in conn.execute(
            "SELECT p.parent_id FROM candidate_identifiers i JOIN current_parent p USING (candidate_id) "
            "WHERE i.kind = 'phone' AND i.normalized_value = ?", (normalize_phone(phone),)):
            parents.add(row["parent_id"])
    members: dict[str, list[MemberFacts]] = {}
    for member in queries_worth.members_with_facts(conn):
        members.setdefault(member.family_key, []).append(member)
    if name:
        wanted: set[str] = _words(name)
        names: dict[str, str] = {}
        for row in conn.execute("SELECT candidate_id, display_name FROM candidates"):
            names[row["candidate_id"]] = row["display_name"]
        for parent_id, family in members.items():
            known: list[str] = []
            for member in family:
                known.append(names.get(member.candidate_id, ""))
                known.append(json.loads(member.facts_json).get("canonical_name") or "")
            if wanted and any(wanted <= _words(name) for name in known):
                parents.add(parent_id)
    found: list[Match] = []
    for parent_id in sorted(parents):
        family_members = members.get(parent_id)
        if family_members is None:
            continue  # a family without facts has nothing to read
        facts = family_facts(family_members).to_payload()
        emails: list[str] = []
        phones: list[str] = []
        for row in conn.execute(
            "SELECT i.kind, i.display_value FROM candidate_identifiers i JOIN current_parent p USING (candidate_id) "
            "WHERE p.parent_id = ? ORDER BY i.kind, i.display_value", (parent_id,)):
            (emails if row["kind"] == "email" else phones).append(row["display_value"])
        profile = conn.execute("SELECT profile_key FROM current_profile WHERE parent_id = ?", (parent_id,)).fetchone()
        url: str = profile["profile_key"] if profile and profile["profile_key"] and not profile["profile_key"].startswith("synthetic:") else ""
        found.append(Match(parent_id, facts["canonical_name"], facts["relationship_to_owner"], tuple(emails), tuple(phones), url, facts))
    return found


def render(match: Match) -> str:
    facts = match.facts
    lines: list[str] = [f"# {match.name}  [{match.parent_id}]"]
    if match.relationship_to_owner:
        lines.append(f"Relationship: {match.relationship_to_owner}")
    if match.linkedin_url:
        lines.append(f"LinkedIn: {match.linkedin_url}")
    if match.emails or match.phones:
        lines.append("Contact: " + " · ".join((*match.emails, *match.phones)))
    if facts.get("title") or facts.get("employers"):
        employers = ", ".join(f"{e.get('role') or '?'} @ {e.get('name') or '?'}" for e in facts.get("employers") or [])
        lines.append(f"Work: {facts.get('title') or ''} {employers}".strip())
    for key in ("school", "location"):
        if facts.get(key):
            lines.append(f"{key.title()}: {facts[key]}")
    if facts.get("topics"):
        lines.append("Topics: " + "; ".join(facts["topics"]))
    for event in facts.get("notable_events") or []:
        lines.append(f"- {event.get('date', '')}: {event.get('summary', '')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Look a person up by name, email or phone.")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    parser.add_argument("--name", default="")
    parser.add_argument("--email", default="")
    parser.add_argument("--phone", default="")
    parser.add_argument("--json", action="store_true", help="print the matches as JSON, facts included")
    args = parser.parse_args(argv)
    if not (args.name or args.email or args.phone):
        parser.error("give --name, --email or --phone")
    found = matches(open_store(store_path(args.data_root)), name=args.name, email=args.email, phone=args.phone)
    if args.json:
        print(json.dumps([asdict(match) for match in found], ensure_ascii=False, indent=2))
    elif not found:
        print("no one matches")
    else:
        print("\n\n".join(render(match) for match in found))
    return 0 if found else 1


if __name__ == "__main__":
    raise SystemExit(main())
