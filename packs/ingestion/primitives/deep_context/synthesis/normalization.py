"""Preserve contact extraction histories and derive parent display facts."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from packs.ingestion.primitives.deep_context.collection.normalization import normalize_cached_bundles
from packs.ingestion.primitives.deep_context.db.context_queries import aggregate_people, collection_sources, person_histories
from packs.ingestion.primitives.common.legacy import restore_contact_facts
from packs.ingestion.primitives.deep_context.db.projectors import project_parent_fact
from packs.ingestion.primitives.deep_context.db.queries import people, facts
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.synthesis.facts import merge_disjoint_fact_records
from packs.ingestion.primitives.deep_context.synthesis.history import FactHistory
from packs.ingestion.primitives.deep_context.synthesis.models import FactRecord


def normalize_parent_cache(db: Db, *, raw_dir: Path, facts_dir: Path) -> int:
    """Reuse original contacts without claiming mixed parent facts belong to them."""
    members = {}
    for row in people(db):
        members.setdefault(row.parent_id, []).append(row.person_id)
    restored = restore_contact_facts(db, facts_dir)
    normalize_cached_bundles(db, raw_dir)
    histories = person_histories(db)
    aggregate_ids = aggregate_people(db)
    required = {row.person_id for row in collection_sources(db)}
    contact_facts = [row for row in facts(db, parent_owned=False)]
    priority = {"no": 0, "maybe": 1, "yes": 2}
    out_dir = Path(facts_dir) / "parents"
    out_dir.mkdir(parents=True, exist_ok=True)
    for parent_id, family in members.items():
        if any(person not in histories or not histories[person].facts.present
               for person in required.intersection(family)):
            continue
        contacts = [histories[person] for person in family if person in histories and person not in aggregate_ids]
        if not contacts:
            continue
        history = FactHistory.from_records(item.payload() for contact in contacts for item in contact.records)
        merged = merge_disjoint_fact_records(FactRecord(contact.facts) for contact in contacts)
        if merged is None:
            continue
        judged = [row for row in contact_facts if row.parent_id == parent_id
                  and row.person_id not in aggregate_ids and row.machine_worth in priority]
        winner = max(judged, key=lambda row: (priority[row.machine_worth], row.subject_key)) if judged else None
        tagged = histories[winner.person_id].facts if winner else None
        merged = replace(merged, network_worth=tagged.network_worth if tagged else None,
                         labels=tagged.labels if tagged else {},
                         present=merged.present | (tagged.present & {"labels"} if tagged else set()))
        record = history.payload()
        record["facts"] = merged.to_payload()
        path = out_dir / f"{parent_id}.jsonl"
        path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        project_parent_fact(db, path, parent_id, artifact_key=f"parent-facts:{parent_id}",
                            excluded_person_ids=tuple(aggregate_ids.intersection(family)))
    return restored
