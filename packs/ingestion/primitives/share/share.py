#!/usr/bin/env python3
"""CLI for labeling, tagging, and deriving the share list.

Flow: parse command -> run label, tag, or share -> emit the stage result.

Changelog:
  2026-09-24: split label and share list runners into their own modules.
  2026-09-24: created.
"""

from __future__ import annotations

import argparse
import re
import sys

from packs.ingestion.primitives.common.gates import exit_code_for_status
from packs.ingestion.primitives.deep_context.common import emit
from packs.ingestion.primitives.share.label import ShareLabels
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
    parser = argparse.ArgumentParser(description="Share stage: machine labels, human tags, the share list.")
    sub = parser.add_subparsers(dest="command", required=True)

    label = sub.add_parser("label", help="export saved labels.csv (free, local)")
    label.add_argument("--estimate", action="store_true", help="print the cost and write nothing")
    label.add_argument("--limit", type=int, default=0)

    tag = sub.add_parser("tag", help="set human tags on one person: tag --name X +private -friend")
    tag.add_argument("--person-id", default="")
    tag.add_argument("--name", default="")
    tag.add_argument("--phone", default="")
    tag.add_argument("--email", default="")
    tag.add_argument("--note", default=None)

    sub.add_parser("share", help="rebuild share.csv from labels.csv + tags.csv")
    return parser


def main(argv: list[str] | None = None) -> int:
    rest, add, remove, unknown = _split_tag_args(list(sys.argv[1:] if argv is None else argv))
    args = build_parser().parse_args(rest)

    if args.command == "label":
        payload = ShareLabels(
            estimate_only=args.estimate, limit=args.limit
        ).run()
        emit(payload)
        return exit_code_for_status(payload["status"])

    if args.command == "tag":
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
        rebuilt = ShareList().run()
        if rebuilt["status"] != "completed":
            emit(rebuilt)
            return exit_code_for_status(rebuilt["status"])
        row = next(
            (row for row in CsvIO.read_dict_rows_normalized(SHARE_DIR / SHARE_FILENAME) if row["person_id"] == person_id),
            None,
        )
        emit(
            {
                "primitive": "share_tag",
                "status": "completed",
                "person_id": person_id,
                "tags": sorted(tags.tags),
                "note": tags.note,
                "share": row,
            }
        )
        return 0

    if args.command == "share":
        payload = ShareLabels().run()
        if payload["status"] != "completed":
            emit(payload)
            return exit_code_for_status(payload["status"])
        payload = ShareList().run()
        emit(payload)
        return exit_code_for_status(payload["status"])

    return EXIT_BAD_REQUEST


if __name__ == "__main__":
    raise SystemExit(main())
