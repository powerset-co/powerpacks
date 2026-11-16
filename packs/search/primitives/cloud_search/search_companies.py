#!/usr/bin/env python3
"""Find companies, as the Companies page does: a set's through the Powerset API (POST /v2/companies/search),
this Mac's in its local index.

Changelog:
  2026-11-16: created; replaces the TurboPuffer company resolver behind /search-company.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packs.shared.web import companies  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--set-id", help="The Powerset set the chat's instructions name")
    scope.add_argument("--local", action="store_true", help="This Mac's local index")
    parser.add_argument("--name", help="Company name (fuzzy)")
    parser.add_argument("--sector-types", default="", help="Comma-separated sectors, e.g. fintech,ai_ml (set search only)")
    parser.add_argument("--entity-types", default="", help="Comma-separated entity types, e.g. vc_firm (set search only)")
    parser.add_argument("--include-people", action="store_true", help="Also list the people at each company")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    body = {"scope": "local" if args.local else args.set_id, "name": args.name, "limit": args.limit,
            "include_people": args.include_people}
    for key in ("sector_types", "entity_types"):
        values = [value.strip() for value in getattr(args, key).split(",") if value.strip()]
        if values:
            body[key] = values
    print(json.dumps(companies.search({key: value for key, value in body.items() if value not in (None, "")}), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
