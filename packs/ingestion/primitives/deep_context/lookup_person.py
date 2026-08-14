"""Retrieve a person's dossier by name and/or phone (or email).

The only user-facing query surface. Pure local file read against ``index.json`` —
no DB, no embeddings, no network. Phone matches on normalized digits (US country
code dropped), email is exact-lowercased, name is exact-normalized then falls
back to an all-tokens-contained fuzzy match.

Flow: `PersonLookup` resolves the query against index.json into typed
`PersonMatch` records (or one of the `FAILURES` statuses); `main()` renders —
the match banner on STDERR, dossier markdown (or --json) on STDOUT — and maps
status to the exit code (0 found, 1 no match, 2 bad args / missing index).

Usage:
  lookup_person.py --phone "+1 415 555 1234"
  lookup_person.py --name "Jane Doe"
  lookup_person.py --email jane@acme.com --json

Changelog:
  2026-07-30 (house style): `run(args)` became the construct-and-run
    `PersonLookup` class returning a typed `LookupResult`, with the exit-code
    policy in the one `FAILURES` table and rendering left in `main()`.
    `find_slugs` is unchanged and still module-level. Same exit codes, same
    stdout/stderr split, same --json bytes; no behavior change.
  2026-07-23 (audit dedup): normalize_email imports from common.contact_fields instead of deep_context.common (deduped there); no behavior change.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.common import (
    DOSSIER_DIR,
    INDEX_JSON,
    normalize_name,
    phone_digits,
)
from packs.ingestion.primitives.common.contact_fields import normalize_email

# The whole exit-code policy: status -> (stderr message, exit code). "found"
# is absent because it renders instead and exits 0.
FAILURES: dict[str, tuple[str, int]] = {
    "no_index": ("No deep-context index at {index_json}. Build dossiers first.", 2),
    "no_query": ("Provide at least one of --name / --phone / --email.", 2),
    "no_match": ("No matching dossier found.", 1),
}


def _dedup(slugs: list[str]) -> list[str]:
    out: list[str] = []
    for s in slugs:
        if s not in out:
            out.append(s)
    return out


def find_slugs(index: dict[str, Any], *, name: str, phone: str, email: str) -> list[str]:
    hits: list[str] = []
    if phone:
        digits = phone_digits(phone)
        if digits:
            hits += index.get("by_phone", {}).get(digits, [])
    if email:
        hits += index.get("by_email", {}).get(normalize_email(email), [])
    if name:
        key = normalize_name(name)
        by_name = index.get("by_name", {})
        if key in by_name:
            hits += by_name[key]
        else:
            tokens = set(key.split())
            for cand_key, slugs in by_name.items():
                if tokens and tokens <= set(cand_key.split()):
                    hits += slugs
    return _dedup(hits)


@dataclass(frozen=True)
class PersonMatch:
    """One matched dossier. `record` is the index entry merged with the slug and
    is emitted verbatim by --json, so its key order is observable output — never
    rebuild it, and note the merge order puts the real slug over any stale one."""

    slug: str
    record: dict[str, Any]

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
    """Resolves one name/phone/email query against index.json.

    Read-only: index.json plus, at render time, the dossier markdown files.
    """

    def __init__(
        self,
        *,
        name: str = "",
        phone: str = "",
        email: str = "",
        index_json: Path = INDEX_JSON,
        dossier_dir: Path = DOSSIER_DIR,
    ) -> None:
        self.name = name
        self.phone = phone
        self.email = email
        self.index_json = Path(index_json)
        self.dossier_dir = Path(dossier_dir)

    def run(self) -> LookupResult:
        if not self.index_json.exists():
            return LookupResult(status="no_index")
        # An unreadable index raises here, before the empty-query check, exactly
        # as it always has.
        index = json.loads(self.index_json.read_text(encoding="utf-8"))
        if not (self.name or self.phone or self.email):
            return LookupResult(status="no_query")

        slugs = find_slugs(index, name=self.name, phone=self.phone, email=self.email)
        if not slugs:
            return LookupResult(status="no_match")
        return LookupResult(status="found", matches=tuple(
            PersonMatch(slug=s, record=index.get("slugs", {}).get(s, {"slug": s}) | {"slug": s})
            for s in slugs
        ))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Look up a person's deep-context dossier by name/phone/email.")
    p.add_argument("--name", default="")
    p.add_argument("--phone", default="")
    p.add_argument("--email", default="")
    p.add_argument("--index-json", default=str(INDEX_JSON))
    p.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    p.add_argument("--json", action="store_true", help="Emit match metadata as JSON instead of dossier text")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lookup = PersonLookup(
        name=args.name,
        phone=args.phone,
        email=args.email,
        index_json=Path(args.index_json),
        dossier_dir=Path(args.dossier_dir),
    )
    result = lookup.run()
    if result.status in FAILURES:
        message, code = FAILURES[result.status]
        print(message.format(index_json=lookup.index_json), file=sys.stderr)
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
        path = lookup.dossier_dir / f"{m.slug}.md"
        if not path.exists():
            continue
        if i:
            print("\n" + "=" * 80 + "\n")
        print(path.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
