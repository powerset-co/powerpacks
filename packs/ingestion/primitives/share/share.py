#!/usr/bin/env python3
"""CLI for the share stage: the human's tags and the share list.

Flow: `share` -> the ShareList node; `tag` -> tags.csv upsert, then the node,
then that person's share row.

Changelog:
  2026-09-24: created; `label` folded into the share node (JEV answers during
    deep_synthesize, so there is nothing to spend here).
"""

from __future__ import annotations

import argparse
import re
import sys

from packs.ingestion.primitives.common.gates import exit_code_for_status
from packs.ingestion.primitives.deep_context.common import emit
from packs.ingestion.primitives.share.models import SHARE_DIR, SHARE_FILENAME
from packs.ingestion.primitives.share.share_list import ShareList
from packs.ingestion.primitives.share.tags import TAG_VOCABULARY, TagStore, lookup_targets
from packs.shared.csv_io import CsvIO

EXIT_BAD_REQUEST = 2

_TAG_TOKEN = re.compile(r"^([+-])(\w+)$")


def _split_tag_args(argv: list[str]) -> tuple[list[str], set[str], set[str], set[str]]:
    """Pull `+tag` / `-tag` words out of argv before argparse sees them — argparse
    cannot tell `-friend` from an option. A `+word` outside the vocabulary is
    reported; a `-word` outside it stays an argparse error."""
    rest: list[str] = []
    add: set[str] = set()
    remove: set[str] = set()
    unknown: set[str] = set()
    for token in argv:
        match = _TAG_TOKEN.fullmatch(token)
        if match and match[2] in TAG_VOCABULARY:
            (add if match[1] == "+" else remove).add(match[2])
        elif token.startswith("+"):
            unknown.add(token[1:])
        else:
            rest.append(token)
    return rest, add, remove, unknown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Share stage: the human's tags and the share list.")
    sub = parser.add_subparsers(dest="command", required=True)

    tag = sub.add_parser("tag", help="set human tags on one person: tag --name X +private -friend")
    tag.add_argument("--person-id", default="")
    tag.add_argument("--name", default="")
    tag.add_argument("--phone", default="")
    tag.add_argument("--email", default="")
    tag.add_argument("--note", default=None)

    sub.add_parser("share", help="write labels.csv + share.csv for every merged person (free, local)")
    return parser


def main(argv: list[str] | None = None) -> int:
    rest, add, remove, unknown = _split_tag_args(list(sys.argv[1:] if argv is None else argv))
    args = build_parser().parse_args(rest)

    if args.command == "share":
        payload = ShareList().run().to_payload()
        emit(payload)
        return exit_code_for_status(payload["status"])

    if unknown:
        print(f"unknown tags: {', '.join(sorted(unknown))}", file=sys.stderr)
        print(f"vocabulary: {', '.join(sorted(TAG_VOCABULARY))}", file=sys.stderr)
        return EXIT_BAD_REQUEST
    person_id = args.person_id.strip()
    if not person_id:
        targets = lookup_targets(name=args.name, phone=args.phone, email=args.email)
        if len(targets) != 1:
            for target in targets:
                print(f"- {target.name} [{target.slug}] {target.person_id}", file=sys.stderr)
            print(f"{len(targets)} matches; pass --person-id.", file=sys.stderr)
            return EXIT_BAD_REQUEST
        person_id = targets[0].person_id
    tags = TagStore().apply(person_id, add=add, remove=remove, note=args.note)
    rebuilt = ShareList().run().to_payload()
    if rebuilt["status"] != "completed":
        emit(rebuilt)
        return exit_code_for_status(rebuilt["status"])
    row = next(
        (row for row in CsvIO.read_dict_rows_normalized(SHARE_DIR / SHARE_FILENAME) if row["person_id"] == person_id),
        None,
    )
    emit({"primitive": "share_tag", "status": "completed", "person_id": person_id,
          "tags": sorted(tags.tags), "note": tags.note, "share": row})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
