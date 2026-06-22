"""Retrieve a person's dossier by name and/or phone (or email).

The only user-facing query surface. Pure local file read against ``index.json`` —
no DB, no embeddings, no network. Phone matches on normalized digits (US country
code dropped), email is exact-lowercased, name is exact-normalized then falls
back to an all-tokens-contained fuzzy match.

Usage:
  lookup_person.py --phone "+1 415 555 1234"
  lookup_person.py --name "Jane Doe"
  lookup_person.py --email jane@acme.com --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.common import (
    DOSSIER_DIR,
    INDEX_JSON,
    normalize_email,
    normalize_name,
    phone_digits,
)


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


def run(args: argparse.Namespace) -> int:
    index_path = Path(args.index_json)
    if not index_path.exists():
        print(f"No deep-context index at {index_path}. Build dossiers first.", file=sys.stderr)
        return 2
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if not (args.name or args.phone or args.email):
        print("Provide at least one of --name / --phone / --email.", file=sys.stderr)
        return 2

    slugs = find_slugs(index, name=args.name, phone=args.phone, email=args.email)
    if not slugs:
        print("No matching dossier found.", file=sys.stderr)
        return 1

    records = [index.get("slugs", {}).get(s, {"slug": s}) | {"slug": s} for s in slugs]
    if args.json:
        print(json.dumps({"matches": records}, ensure_ascii=False, indent=2))
        return 0

    if len(slugs) > 1:
        print(f"{len(slugs)} matching dossiers:\n", file=sys.stderr)
        for rec in records:
            print(f"- {rec.get('name', rec['slug'])} — {rec.get('headline', '')}  [{rec['slug']}]", file=sys.stderr)
        print("", file=sys.stderr)

    dossier_dir = Path(args.dossier_dir)
    for i, slug in enumerate(slugs):
        path = dossier_dir / f"{slug}.md"
        if not path.exists():
            continue
        if i:
            print("\n" + "=" * 80 + "\n")
        print(path.read_text(encoding="utf-8"))
    return 0


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
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
