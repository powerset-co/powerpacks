"""Look up SQLite-projected dossiers by name, phone, or email."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

from packs.ingestion.primitives.deep_context.shared.common import (
    CANONICAL_DB,
)
from packs.ingestion.primitives.deep_context.db.people_views import (
    person_lookup,
)
from packs.ingestion.primitives.deep_context.db.view_models import ParentLookupRow
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db

# The whole exit-code policy: status -> (stderr message, exit code). "found"
# is absent because it renders instead and exits 0.
FAILURES: dict[str, tuple[str, int]] = {
    "no_query": ("Provide at least one of --name / --phone / --email.", 2),
    "no_match": ("No matching dossier found.", 1),
}


@dataclass(frozen=True)
class PersonMatch:
    slug: str
    person_id: str
    name: str
    path: str
    headline: str
    full_name: str
    emails: tuple[str, ...]
    phones: tuple[str, ...]
    dossier_body: str = ""

    @property
    def label(self) -> str:
        return self.name or self.slug

    def as_dict(self) -> dict[str, object]:
        """Serialize in the pinned historical CLI key order."""
        return {
            "person_id": self.person_id,
            "name": self.name,
            "path": self.path,
            "headline": self.headline,
            "full_name": self.full_name,
            "emails": list(self.emails),
            "phones": list(self.phones),
            "slug": self.slug,
        }


@dataclass(frozen=True)
class ParentMatch:
    slug: str
    dossier_body: str = ""

    @property
    def label(self) -> str:
        return self.slug

    @property
    def headline(self) -> str:
        return ""

    def as_dict(self) -> dict[str, str]:
        return {"slug": self.slug}


@dataclass(frozen=True)
class LookupResult:
    """`status` is "found" or a key of FAILURES; matches are index order."""

    status: str
    matches: tuple[PersonMatch | ParentMatch, ...] = ()


class PersonLookup:
    """Resolve one query against canonical SQLite."""

    def __init__(
        self,
        *,
        name: str | None = None,
        phone: str | None = None,
        email: str | None = None,
        db: Db,
    ) -> None:
        self.name = name
        self.phone = phone
        self.email = email
        self.db = db

    def run(self) -> LookupResult:
        if not (self.name or self.phone or self.email):
            return LookupResult(status="no_query")

        records = person_lookup(
            self.db,
            name=self.name,
            phone=self.phone,
            email=self.email,
        )
        if not records:
            return LookupResult(status="no_match")
        matches: list[PersonMatch | ParentMatch] = []
        for source in records:
            # person_lookup emits a ParentLookupRow when the match resolved at the
            # parent level with no single owning child row (e.g. a name hit on the
            # merged identity itself); PersonLookupRow otherwise.
            if isinstance(source, ParentLookupRow):
                matches.append(ParentMatch(source.slug, source.dossier_body))
                continue
            matches.append(
                PersonMatch(
                    slug=source.slug,
                    person_id=source.person_id,
                    name=source.name,
                    path=source.path,
                    headline=source.headline,
                    full_name=source.full_name,
                    emails=source.emails,
                    phones=source.phones,
                    dossier_body=source.dossier_body,
                )
            )
        return LookupResult(status="found", matches=tuple(matches))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Look up a person's deep-context dossier by name/phone/email.")
    p.add_argument("--name")
    p.add_argument("--phone")
    p.add_argument("--email")
    p.add_argument("--db", default=str(CANONICAL_DB))
    p.add_argument("--json", action="store_true", help="Emit match metadata as JSON instead of dossier text")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = open_existing_db(args.db)
    lookup = PersonLookup(
        name=args.name,
        phone=args.phone,
        email=args.email,
        db=db,
    )
    result = lookup.run()
    if result.status in FAILURES:
        message, code = FAILURES[result.status]
        print(message, file=sys.stderr)
        return code

    if args.json:
        print(json.dumps({"matches": [m.as_dict() for m in result.matches]}, ensure_ascii=False, indent=2))
        return 0

    if len(result.matches) > 1:
        print(f"{len(result.matches)} matching dossiers:\n", file=sys.stderr)
        for m in result.matches:
            print(f"- {m.label} — {m.headline}  [{m.slug}]", file=sys.stderr)
        print("", file=sys.stderr)

    for i, m in enumerate(result.matches):
        if not m.dossier_body:
            continue
        if i:
            print("\n" + "=" * 80 + "\n")
        print(m.dossier_body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
