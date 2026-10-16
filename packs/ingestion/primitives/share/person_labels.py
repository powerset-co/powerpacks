"""Write the People label export for every family. Free and local.

The labels node writes person_labels; human tags and worth decisions stay in their own tables.
"""
from __future__ import annotations

from datetime import date

from packs.ingestion.primitives.deep_context_v2.db import queries_share
from packs.ingestion.primitives.deep_context_v2.db.queries_share import LabelRow
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso
from packs.ingestion.primitives.deep_context_v2.node import Node
from packs.ingestion.primitives.share.evidence import ShareEvidence
from packs.ingestion.primitives.share.labels import deterministic_labels, flag, labels_from_saved
from packs.ingestion.primitives.share.models import label_payload


class PersonLabels(Node):
    name = "labels"
    reads = ("current_parent", "current_worth", "current_profile", "candidates", "candidate_identifiers",
             "candidate_sources", "bundles", "facts")
    writes = ("person_labels",)

    def execute(self) -> dict[str, int]:
        people = ShareEvidence(self.conn, self.data_root).load()
        reference_date = date.today().isoformat()
        now = now_iso()
        rows: list[LabelRow] = []
        with_labels = 0
        for person in people:
            labels = labels_from_saved(person.worth_labels) if person.worth_labels else None
            deterministic = deterministic_labels(person, reference_date=reference_date)
            held_back = flag(labels)
            labels_json = label_payload(deterministic, labels)
            with_labels += int(labels is not None)
            for candidate_id in person.candidate_ids:
                rows.append((candidate_id, person.public_identifier, person.full_name, person.network_worth,
                             held_back, labels_json, now))
        queries_share.replace_labels(self.conn, rows)
        return {"families": len(people), "with_labels": with_labels, "rows": len(rows)}
