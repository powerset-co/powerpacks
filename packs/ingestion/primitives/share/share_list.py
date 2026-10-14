"""The share node: `person_labels` and `share` for every family, in one pass. Free, local.

Flow: `ShareEvidence.load()` -> for each family the deterministic labels, the labels and share call
worth saved, the flag and the share decision (labels.py) -> one label row and one share row per
member candidate, both tables rewritten together (queries_share.replace_share).

A family's human tags (current_tags) take part in its decision; the node never writes tags.

Changelog:
  2026-10-13: Luna's labels and share call replace JEV's labels and confirm flags.
  2026-10-07: v2. One node over the v2 store; the JEV labels are worth's, not the facts'.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.db import queries_share
from packs.ingestion.primitives.deep_context_v2.db.queries_share import LabelRow as LabelTableRow
from packs.ingestion.primitives.deep_context_v2.db.queries_share import ShareRow as ShareTableRow
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso, open_store, store_path
from packs.ingestion.primitives.deep_context_v2.node import Node
from packs.ingestion.primitives.share.evidence import ShareEvidence
from packs.ingestion.primitives.share.labels import deterministic_labels, flag, labels_from_saved, share_decision
from packs.ingestion.primitives.share.models import HumanTags, LabelRow, Labels, PersonEvidence, label_payload
from packs.ingestion.primitives.share.store import TagStore
from packs.ingestion.schemas.share_schema import SHARE_NO, SHARE_YES


def decide(person: PersonEvidence, held: HumanTags | None, reference_date: str, now: str) -> tuple[list[LabelTableRow], list[ShareTableRow]]:
    """A family's label and share rows, one of each per member."""
    labels: Labels | None = labels_from_saved(person.worth_labels) if person.worth_labels else None
    deterministic = deterministic_labels(person, reference_date=reference_date)
    held_back: str = flag(labels)
    decision = share_decision(
        LabelRow(person_id=person.person_id, public_identifier=person.public_identifier, is_owner=deterministic.is_owner,
                 worth=person.network_worth, flag=held_back,
                 probabilities=dict(labels.probabilities) if labels else {},
                 share_call=labels.share_call if labels else "", share_p=labels.share_p if labels else 0.0),
        held, updated_at=now)
    labels_json: str = label_payload(deterministic, labels)
    label_rows: list[LabelTableRow] = []
    share_rows: list[ShareTableRow] = []
    for candidate_id in person.candidate_ids:
        label_rows.append((candidate_id, person.public_identifier, person.full_name, person.network_worth, held_back,
                           labels_json, now))
        share_rows.append((candidate_id, person.public_identifier, decision.share, decision.reason, decision.labels,
                           decision.source, now))
    return label_rows, share_rows


class Share(Node):
    name = "share"
    reads = ("current_parent", "current_worth", "current_profile", "current_tags", "candidates", "candidate_identifiers",
             "candidate_sources", "bundles", "facts")
    writes = ("person_labels", "share")

    def execute(self) -> dict[str, int]:
        people: list[PersonEvidence] = ShareEvidence(self.conn, self.data_root).load()
        tags: dict[str, HumanTags] = TagStore(self.conn).load()
        reference_date: str = date.today().isoformat()
        now: str = now_iso()
        labels: list[LabelTableRow] = []
        shares: list[ShareTableRow] = []
        counts: dict[str, int] = {"families": len(people), "with_labels": 0, SHARE_YES: 0, SHARE_NO: 0}
        for person in people:
            label_rows, share_rows = decide(person, tags.get(person.person_id), reference_date, now)
            labels.extend(label_rows)
            shares.extend(share_rows)
            counts["with_labels"] += int(person.worth_labels is not None)
            counts[share_rows[0][2]] += 1
        queries_share.replace_share(self.conn, labels, shares)
        counts["rows"] = len(shares)
        return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Share: the label export and the share list for every family.")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    args = parser.parse_args(argv)
    conn: sqlite3.Connection = open_store(store_path(args.data_root))
    manifest = Share(conn, args.data_root).run()
    print(manifest.status, json.dumps(manifest.counts), manifest.error or "")
    return 0 if manifest.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
