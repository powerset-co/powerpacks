"""Resolve a local contact to its canonical parent dossier."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.readiness import CANONICAL_DB
from packs.ingestion.primitives.deep_context.db.people_views import person_lookup
from packs.ingestion.primitives.deep_context.db.view_models import ParentLookupRow


@dataclass(frozen=True)
class LookupResult:
    status: str
    matches: tuple[ParentLookupRow, ...] = ()
    message: str = ""


class PersonLookup:
    """Read only SQLite; missing dossiers never hide matching contacts."""

    def __init__(self, *, db: Path, name: str | None = None,
                 phone: str | None = None, email: str | None = None,
                 parent_id: str | None = None) -> None:
        self.db = db
        self.name = name
        self.phone = phone
        self.email = email
        self.parent_id = parent_id

    def run(self) -> LookupResult:
        if not (self.name or self.phone or self.email or self.parent_id):
            return LookupResult("no_query", message="Provide --name, --phone, --email, or --parent-id.")
        if not self.db.is_file():
            return LookupResult("missing_database", message=(
                f"No local dossiers database at {self.db}. Use available contact/profile lookup."
            ))
        try:
            matches = tuple(person_lookup(self.db, name=self.name, phone=self.phone,
                                          email=self.email, parent_id=self.parent_id))
        except ValueError as exc:
            return LookupResult("unreadable_database", message=str(exc))
        if not matches:
            return LookupResult("no_match", message="No matching person found.")
        return LookupResult("ambiguous" if len(matches) > 1 else "found", matches)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Look up a person's canonical parent dossier.")
    p.add_argument("--name")
    p.add_argument("--phone")
    p.add_argument("--email")
    p.add_argument("--parent-id", help="Choose a parent after an ambiguous lookup")
    p.add_argument("--db", type=Path, default=CANONICAL_DB)
    p.add_argument("--json", action="store_true", help="Emit parent metadata and, for one match, its dossier body")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = PersonLookup(db=args.db, name=args.name, phone=args.phone,
                          email=args.email, parent_id=args.parent_id).run()
    if args.json:
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    elif result.message:
        print(result.message, file=sys.stderr)
    elif result.status == "ambiguous":
        print(f"{len(result.matches)} matching people. Select one with --parent-id:\n")
        for match in result.matches:
            details = " | ".join(filter(None, (match.headline, *match.emails,
                                               *match.phones, *match.linkedin_urls)))
            print(f"- {match.name or match.slug or match.parent_id} [{match.parent_id}] {details}")
    else:
        match = result.matches[0]
        print(match.dossier_body or (
            f"{match.name or match.slug or match.parent_id} [{match.parent_id}]: No parent dossier is available."
        ))
    return 0 if result.matches else (2 if result.status == "no_query" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
