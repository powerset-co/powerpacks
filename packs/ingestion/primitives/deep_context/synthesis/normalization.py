"""Collapse projected child facts into parent-owned synthesis cache records."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.collection.normalization import (
    normalize_cached_bundles,
)
from packs.ingestion.primitives.deep_context.collection.state import projected_bundles
from packs.ingestion.primitives.deep_context.db.models import ArtifactKind, ArtifactReplacement
from packs.ingestion.primitives.deep_context.db.projectors import project_parent_fact
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.dossier.facts import merge_facts
from packs.ingestion.primitives.deep_context.synthesis import prompting


def _payload(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


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
    snapshot = canonical_snapshot(db)
    bundles = projected_bundles(snapshot)
    artifacts = {row.artifact_key: row for row in snapshot.artifacts}
    parent_facts = {row.parent_id for row in snapshot.facts if row.person_id is None}
    grouped: dict[str, list] = {}
    for fact in snapshot.facts:
        if fact.person_id:
            grouped.setdefault(fact.parent_id, []).append(fact)
    facts_dir = Path(facts_dir)
    facts_dir.mkdir(parents=True, exist_ok=True)
    migrated = 0
    priority = {"no": 0, "maybe": 1, "yes": 2}
    for parent_id, child_facts in sorted(grouped.items()):
        bundle = bundles.get(parent_id)
        parent_ready = parent_id in parent_facts
        if parent_id not in parent_facts and bundle:
            winner = max(
                child_facts,
                key=lambda row: (priority.get(row.machine_worth or "maybe", 1), row.subject_key),
            )
            source_records = [
                _payload(artifacts[row.artifact_key].payload_json)
                for row in child_facts
                if row.artifact_key in artifacts
            ]
            merged = merge_facts([
                {"facts": _payload(row.facts_json)} for row in child_facts
            ])
            winner_worth = _payload(winner.facts_json).get("network_worth")
            if isinstance(winner_worth, dict):
                merged["network_worth"] = winner_worth
            record = dict(next(
                (item for item in source_records if item.get("facts") == _payload(winner.facts_json)),
                source_records[-1] if source_records else {},
            ))
            record.update({
                "facts": merged,
                "synthesis_version": prompting.SYNTHESIS_VERSION,
                "input_evidence_fingerprint": prompting.input_evidence_fingerprint(
                    bundle,
                    system_prompt=system_prompt,
                    chunk_chars=chunk_chars,
                    max_batches=max_batches,
                ),
                "final_confidence": max(
                    (float(row.confidence or 0) for row in child_facts), default=0.0,
                ),
                "messages_available": int(
                    bundle.get("messages_available") or len(bundle.get("messages") or [])
                ),
            })
            path = facts_dir / f"{parent_id}.jsonl"
            path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
            project_parent_fact(db, path, parent_id)
            migrated += 1
            parent_ready = True

        if not parent_ready:
            continue

        for fact in child_facts:
            artifact = artifacts.get(fact.artifact_key)
            db.project_rows((
                ArtifactReplacement(
                    ArtifactKind.FACTS.value, (), person_id=fact.person_id,
                ),
            ))
            if artifact:
                old = Path(artifact.path)
                if old.parent.resolve() == facts_dir.resolve():
                    old.unlink(missing_ok=True)
    return migrated
