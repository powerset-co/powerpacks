"""Collapse projected child facts into parent-owned synthesis cache records.

Reuses already-paid per-child SYNTHESIS output so a parent that predates
parent-owned fact caching gets migrated for free instead of re-billed. Once no
child-owned FACTS rows remain, ``grouped`` is empty and this is a query and a
return.
"""

# Legacy (2026-08-07): delete once no install still carries child-owned FACTS artifacts.

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.collection.normalization import (
    normalize_cached_bundles,
)
from packs.ingestion.primitives.deep_context.collection.planning import (
    projected_bundles,
)
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactReplacement,
    ArtifactRow,
    FactRow,
)
from packs.ingestion.primitives.deep_context.db.projectors import project_parent_fact
from packs.ingestion.primitives.deep_context.db.queries import artifacts, facts
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.synthesis.facts import merge_fact_records
from packs.ingestion.primitives.deep_context.synthesis.models import (
    FactRecord,
    SynthesizedFacts,
)
from packs.ingestion.primitives.deep_context.synthesis import prompting


def normalize_parent_cache(
    db: Db,
    *,
    raw_dir: Path,
    facts_dir: Path,
    system_prompt: str,
    chunk_chars: int,
    max_batches: int,
) -> int:
    """Reuse paid child facts while changing only their canonical owner."""
    normalize_cached_bundles(db, raw_dir)
    bundles = projected_bundles(db)
    artifact_rows = {row.artifact_key: row for row in artifacts(db, kind=ArtifactKind.FACTS.value)}
    fact_rows = facts(db)
    parent_facts = {row.parent_id for row in fact_rows if row.person_id is None}
    grouped: dict[str, list[FactRow]] = {}
    for fact in fact_rows:
        if fact.person_id:
            grouped.setdefault(fact.parent_id, []).append(fact)
    facts_dir = Path(facts_dir)
    facts_dir.mkdir(parents=True, exist_ok=True)
    migrated = 0
    priority = {"no": 0, "maybe": 1, "yes": 2}
    for parent_id, child_facts in sorted(grouped.items()):
        bundle: CollectionBundle | None = bundles.get(parent_id)
        parent_ready = parent_id in parent_facts
        judged_facts = [row for row in child_facts if row.machine_worth in priority]
        if parent_id not in parent_facts and bundle and judged_facts:
            # Fields merge from every child equally (merge_fact_records below), but
            # network_worth is a judgment call: it comes only from the single child
            # with the most favorable verdict (yes > maybe > no), subject_key just
            # breaking ties deterministically.
            winner = max(
                judged_facts,
                key=lambda row: (priority[row.machine_worth], row.subject_key),
            )
            source_records = [
                parse_json_object(artifact_rows[row.artifact_key].payload_json)
                for row in child_facts
                if row.artifact_key in artifact_rows
            ]
            merged = merge_fact_records(
                record
                for row in child_facts
                if (
                    record := FactRecord.from_payload(
                        {
                            "facts": parse_json_object(row.facts_json),
                        }
                    )
                )
                is not None
            )
            if merged is None:
                continue
            winner_facts: SynthesizedFacts | None = SynthesizedFacts.from_payload(parse_json_object(winner.facts_json))
            if winner_facts and winner_facts.network_worth:
                merged = replace(
                    merged,
                    network_worth=winner_facts.network_worth,
                )
            # Base payload is the winner's own raw record (carries its model/token
            # metadata); if that exact record can't be matched back, fall back to
            # whichever child record was seen last rather than fail the migration.
            record = dict(
                next(
                    (item for item in source_records if item.get("facts") == parse_json_object(winner.facts_json)),
                    source_records[-1] if source_records else {},
                )
            )
            record.update(
                {
                    "facts": merged.to_payload(),
                    "synthesis_version": prompting.SYNTHESIS_VERSION,
                    "input_evidence_fingerprint": prompting.input_evidence_fingerprint(
                        bundle,
                        system_prompt=system_prompt,
                        chunk_chars=chunk_chars,
                        max_batches=max_batches,
                    ),
                    "final_confidence": max(
                        (float(row.confidence or 0) for row in child_facts),
                        default=0.0,
                    ),
                    "messages_available": int(bundle.messages_available or len(bundle.messages)),
                }
            )
            path = facts_dir / f"{parent_id}.jsonl"
            path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
            project_parent_fact(db, path, parent_id)
            migrated += 1
            parent_ready = True

        if not parent_ready:
            continue

        for fact in child_facts:
            artifact: ArtifactRow | None = artifact_rows.get(fact.artifact_key)
            db.project_rows(
                (
                    ArtifactReplacement(
                        ArtifactKind.FACTS.value,
                        (),
                        person_id=fact.person_id,
                    ),
                )
            )
            if artifact:
                old = Path(artifact.path)
                # Only delete files this migration itself would have written —
                # a per-child file living outside facts_dir isn't ours to remove.
                if old.parent.resolve() == facts_dir.resolve():
                    old.unlink(missing_ok=True)
    return migrated
