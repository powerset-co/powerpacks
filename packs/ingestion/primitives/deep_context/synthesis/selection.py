"""Select projected source bundles and skip unchanged paid synthesis work."""

from __future__ import annotations

import json

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.collection.bundle_assembly import union_bundles
from packs.ingestion.primitives.deep_context.shared.common import owner_background_block
from packs.ingestion.primitives.deep_context.db.models import ArtifactKind, OwnerProfile
from packs.ingestion.primitives.deep_context.db.queries import (
    artifacts,
    facts,
    owner_profile,
    parents,
    people,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.synthesis import prompting
from packs.ingestion.primitives.deep_context.synthesis.models import SynthesisPlan


def _effective_parent_bundles(db: Db) -> dict[str, CollectionBundle]:
    """Preview the parent bundles cache normalization will project, without writes."""
    source_artifacts = artifacts(db, kind=ArtifactKind.SOURCE_BUNDLE.value, status="projected")
    bundles: dict[str, CollectionBundle] = {}
    children: dict[str, list[CollectionBundle]] = {}
    for row in source_artifacts:
        bundle = CollectionBundle.from_payload(parse_json_object(row.payload_json))
        if bundle is not None:
            if row.person_id is None:
                bundles[row.parent_id] = bundle
            else:
                children.setdefault(str(row.parent_id), []).append(bundle)
    names = {str(row.parent_id): str(row.display_name or "") for row in parents(db)}
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
) -> list[CollectionBundle]:
    cached = {
        str(row.parent_id): (
            str(row.input_fingerprint or ""),
            str(json.loads(row.payload_json or "{}").get("synthesis_version") or ""),
        )
        for row in artifacts(db, kind=ArtifactKind.FACTS.value, parent_owned=True)
    }
    effective_bundles = _effective_parent_bundles(db)
    child_fact_parents = {str(row.parent_id) for row in facts(db, parent_owned=False)}
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
    person_rows = people(db)
    member_parents = {str(row.parent_id) for row in person_rows}
    non_owner_parents = {str(row.parent_id) for row in person_rows if not row.is_owner}
    owner_only_parents = member_parents - non_owner_parents
    for pid, bundle in sorted(effective_bundles.items()):
        # Collection planning excludes owners; guard cached bundles too.
        if pid in owner_only_parents:
            continue
        if not force and not rejudge:
            fingerprint, version = cached.get(pid, ("", ""))
            if version == prompting.SYNTHESIS_VERSION and fingerprint == prompting.input_evidence_fingerprint(
                bundle,
                system_prompt=system_prompt,
                chunk_chars=chunk_chars,
                max_batches=max_batches,
            ):
                continue
        bundles.append(bundle)
    return bundles


def build_system_prompt(db: Db) -> str:
    """Render the required owner context without scanning source bundles."""
    owner: OwnerProfile | None = owner_profile(db)
    if owner is None:
        raise StoreError("deep context requires an owner profile; run build-owner first")
    return prompting.SYSTEM_PROMPT + (
        prompting.owner_identity_block(owner) + prompting.OWNER_PROMPT_SUFFIX + owner_background_block(owner)
    )


def build_plan(
    db: Db,
    *,
    system_prompt: str,
    chunk_chars: int,
    max_batches: int,
    force: bool,
    rejudge: bool,
) -> SynthesisPlan:
    return SynthesisPlan(
        system_prompt,
        tuple(
            pending_target_bundles(
                db,
                system_prompt=system_prompt,
                chunk_chars=chunk_chars,
                max_batches=max_batches,
                force=force,
                rejudge=rejudge,
            )
        ),
    )
