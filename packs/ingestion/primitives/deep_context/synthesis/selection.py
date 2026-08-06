"""Select projected source bundles and skip unchanged paid synthesis work."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from packs.ingestion.primitives.deep_context.common import owner_background_block
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.synthesis import prompting


@dataclass(frozen=True)
class SynthesisPlan:
    owner: dict[str, Any] | None
    system_prompt: str
    bundles: list[dict[str, Any]]


def pending_target_bundles(
    db: Db,
    *,
    system_prompt: str,
    chunk_chars: int,
    max_batches: int,
    force: bool,
    parent_id: str,
    rejudge: bool = False,
    _snapshot: Any = None,
) -> list[dict[str, Any]]:
    snapshot = _snapshot or canonical_snapshot(db)
    cached = {
        str(row.parent_id): (
            str(row.input_fingerprint or ""),
            str(json.loads(row.payload_json or "{}").get("synthesis_version") or ""),
        )
        for row in snapshot.artifacts
        if row.kind == "facts" and row.person_id is None
    }
    bundles: list[dict[str, Any]] = []
    source_rows = sorted(
        (
            row for row in snapshot.artifacts
            if row.kind == "source_bundle" and row.person_id is None and row.status == "projected"
        ),
        key=lambda row: str(row.parent_id),
    )
    for row in source_rows:
        pid = str(row.parent_id)
        if parent_id and pid != parent_id:
            continue
        try:
            bundle = json.loads(row.payload_json or "{}")
        except json.JSONDecodeError:
            bundle = {}
        if not isinstance(bundle, dict):
            continue
        if not force and not rejudge:
            fingerprint, version = cached.get(pid, ("", ""))
            if (
                version == prompting.SYNTHESIS_VERSION
                and fingerprint == prompting.input_evidence_fingerprint(
                    bundle,
                    system_prompt=system_prompt,
                    chunk_chars=chunk_chars,
                    max_batches=max_batches,
                )
            ):
                continue
        bundles.append(bundle)
    return bundles


def build_plan(
    db: Db,
    *,
    chunk_chars: int,
    max_batches: int,
    no_owner: bool,
    force: bool,
    rejudge: bool,
    person_id: str,
) -> SynthesisPlan:
    snapshot = canonical_snapshot(db)
    target_parent = next(
        (row.parent_id for row in snapshot.people if row.person_id == person_id),
        person_id,
    )
    owner = snapshot.owner if not no_owner else None
    system_prompt = prompting.SYSTEM_PROMPT + (
        prompting.owner_identity_block(owner)
        + prompting.OWNER_PROMPT_SUFFIX
        + owner_background_block(owner)
        if owner else ""
    )
    return SynthesisPlan(owner, system_prompt, pending_target_bundles(
        db,
        system_prompt=system_prompt,
        chunk_chars=chunk_chars,
        max_batches=max_batches,
        force=force,
        rejudge=rejudge,
        parent_id=target_parent,
        _snapshot=snapshot,
    ))
