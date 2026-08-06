"""Look up SQLite-projected dossiers by name, phone, or email."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.common import (
    CANONICAL_DB,
    DOSSIER_DIR,
    INDEX_JSON,
)
from packs.ingestion.primitives.deep_context.db.people_views import person_lookup
from packs.ingestion.primitives.deep_context.db.store import Db

# The whole exit-code policy: status -> (stderr message, exit code). "found"
# is absent because it renders instead and exits 0.
FAILURES: dict[str, tuple[str, int]] = {
    "no_index": ("No deep-context index at {index_json}. Build dossiers first.", 2),
    "no_query": ("Provide at least one of --name / --phone / --email.", 2),
    "no_match": ("No matching dossier found.", 1),
}


@dataclass(frozen=True)
class PersonMatch:
    """One matched dossier. `record` is the index entry merged with the slug and
    is emitted verbatim by --json, so its key order is observable output — never
    rebuild it, and note the merge order puts the real slug over any stale one."""

    slug: str
    record: dict[str, Any]
    dossier_body: str = ""

    @property
    def label(self) -> str:
        return self.record.get("name", self.slug)

    @property
    def headline(self) -> str:
        return self.record.get("headline", "")


@dataclass(frozen=True)
class LookupResult:
    """`status` is "found" or a key of FAILURES; matches are index order."""

    status: str
    matches: tuple[PersonMatch, ...] = ()


class PersonLookup:
    """Resolve one query against canonical SQLite."""

    def __init__(
        self,
        *,
        name: str = "",
        phone: str = "",
        email: str = "",
        db: Db | None = None,
        db_path: Path = CANONICAL_DB,
    ) -> None:
        self.name = name
        self.phone = phone
        self.email = email
        self.db = db
        self.db_path = db.db_path if db is not None else Path(db_path)

    def run(self) -> LookupResult:
        if self.db is None and not self.db_path.is_file():
            return LookupResult(status="no_index")
        db = self.db or Db(self.db_path)
        if not (self.name or self.phone or self.email):
            return LookupResult(status="no_query")

        records = person_lookup(
            db, name=self.name, phone=self.phone, email=self.email,
        )
        if not records:
            return LookupResult(status="no_match")
        matches = []
        for source in records:
            slug = str(source["slug"])
            record = (
                {"slug": slug}
                if source.get("children")
                else {
                    "person_id": source.get("person_id") or "",
                    "name": source.get("name") or "",
                    "path": source.get("path") or "",
                    "headline": source.get("headline") or "",
                    "full_name": source.get("full_name") or "",
                    "emails": list(source.get("emails") or []),
                    "phones": list(source.get("phones") or []),
                    "slug": slug,
                }
            )
            matches.append(PersonMatch(
                slug, record, str(source.get("dossier_body") or ""),
            ))
        return LookupResult(status="found", matches=tuple(matches))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Look up a person's deep-context dossier by name/phone/email.")
    p.add_argument("--name", default="")
    p.add_argument("--phone", default="")
    p.add_argument("--email", default="")
    p.add_argument("--index-json", default=str(INDEX_JSON))
    p.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    p.add_argument("--db", default=str(CANONICAL_DB))
    p.add_argument("--json", action="store_true", help="Emit match metadata as JSON instead of dossier text")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lookup = PersonLookup(
        name=args.name,
        phone=args.phone,
        email=args.email,
        db_path=Path(args.db),
    )
    result = lookup.run()
    if result.status in FAILURES:
        message, code = FAILURES[result.status]
        print(message.format(index_json=args.index_json), file=sys.stderr)
        return code

    if args.json:
        print(json.dumps({"matches": [m.record for m in result.matches]}, ensure_ascii=False, indent=2))
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
