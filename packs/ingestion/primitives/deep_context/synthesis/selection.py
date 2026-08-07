"""Select projected source bundles and skip unchanged paid synthesis work."""
from __future__ import annotations

import json

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.collection.state import (
    projected_bundles,
    union_bundles,
)
from packs.ingestion.primitives.deep_context.common import owner_background_block
from packs.ingestion.primitives.deep_context.db.models import CanonicalSnapshot, OwnerProfile
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.synthesis import prompting
from packs.ingestion.primitives.deep_context.synthesis.models import SynthesisPlan


def _effective_parent_bundles(
    snapshot: CanonicalSnapshot,
) -> dict[str, CollectionBundle]:
    """Preview the parent bundles cache normalization will project, without writes."""
    bundles = projected_bundles(snapshot)
    children: dict[str, list[CollectionBundle]] = {}
    for row in snapshot.artifacts:
        if (
            row.kind == "source_bundle"
            and row.person_id is not None
            and row.status == "projected"
        ):
            bundle = CollectionBundle.from_payload(
                parse_json_object(row.payload_json)
            )
            if bundle is not None:
                children.setdefault(str(row.parent_id), []).append(bundle)
    names = {str(row.parent_id): str(row.display_name or "") for row in snapshot.parents}
    for parent_id, child_bundles in children.items():
        if parent_id not in bundles:
            bundles[parent_id] = union_bundles(
                parent_id,
                names.get(parent_id, ""),
                child_bundles,
            )
    return bundles


def pending_target_bundles(
    db: Db,
    *,
    system_prompt: str,
    chunk_chars: int,
    max_batches: int,
    force: bool,
    rejudge: bool = False,
    _snapshot: CanonicalSnapshot | None = None,
) -> list[CollectionBundle]:
    snapshot = _snapshot or canonical_snapshot(db)
    cached = {
        str(row.parent_id): (
            str(row.input_fingerprint or ""),
            str(json.loads(row.payload_json or "{}").get("synthesis_version") or ""),
        )
        for row in snapshot.artifacts
        if row.kind == "facts" and row.person_id is None
    }
    effective_bundles = _effective_parent_bundles(snapshot)
    child_fact_parents = {
        str(row.parent_id) for row in snapshot.facts if row.person_id is not None
    }
    for pid in child_fact_parents - cached.keys():
        bundle: CollectionBundle | None = effective_bundles.get(pid)
        if bundle:
            cached[pid] = (
                prompting.input_evidence_fingerprint(
                    bundle,
                    system_prompt=system_prompt,
                    chunk_chars=chunk_chars,
                    max_batches=max_batches,
                ),
                prompting.SYNTHESIS_VERSION,
            )
    bundles: list[CollectionBundle] = []
    member_parents = {str(row.parent_id) for row in snapshot.people}
    non_owner_parents = {
        str(row.parent_id) for row in snapshot.people if not row.is_owner
    }
    owner_only_parents = member_parents - non_owner_parents
    for pid, bundle in sorted(effective_bundles.items()):
        # collection.state.source_parents excludes owners; guard cached bundles too.
        if pid in owner_only_parents:
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
) -> SynthesisPlan:
    snapshot = canonical_snapshot(db)
    owner: OwnerProfile | None = snapshot.owner if not no_owner else None
    system_prompt = prompting.SYSTEM_PROMPT + (
        prompting.owner_identity_block(owner)
        + prompting.OWNER_PROMPT_SUFFIX
        + owner_background_block(owner)
        if owner else ""
    )
    return SynthesisPlan(
        owner,
        system_prompt,
        tuple(pending_target_bundles(
            db,
            system_prompt=system_prompt,
            chunk_chars=chunk_chars,
            max_batches=max_batches,
            force=force,
            rejudge=rejudge,
            _snapshot=snapshot,
        )),
    )
