"""Select unseen message evidence; explicit config changes re-extract the bundle."""

from __future__ import annotations

import json
import hashlib
from dataclasses import replace

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.shared.common import owner_background_block
from packs.ingestion.primitives.deep_context.db.models import ArtifactKind, OwnerProfile
from packs.ingestion.primitives.deep_context.db.queries import (
    artifacts,
    owner_profile,
    parents,
    people,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.db.context_queries import aggregate_people, person_histories, singleton_people
from packs.ingestion.primitives.deep_context.synthesis import prompting
from packs.ingestion.primitives.deep_context.synthesis.models import SynthesisPlan


def effective_parent_bundles(db: Db) -> dict[str, CollectionBundle]:
    """Union every cached bundle under its current parent, including merged parents."""
    source_artifacts = artifacts(db, kind=ArtifactKind.SOURCE_BUNDLE.value, status="projected")
    bundles: dict[str, CollectionBundle] = {}
    children: dict[str, list[CollectionBundle]] = {}
    contact_parents = {row.parent_id for row in source_artifacts if row.person_id}
    for row in source_artifacts:
        if row.person_id is None and row.parent_id in contact_parents:
            continue
        bundle = CollectionBundle.from_payload(parse_json_object(row.payload_json))
        if bundle is not None:
            children.setdefault(str(row.parent_id), []).append(bundle)
    names = {str(row.parent_id): str(row.display_name or "") for row in parents(db)}
    for parent_id, child_bundles in children.items():
        bundles[parent_id] = (
            replace(child_bundles[0], person_id=parent_id)
            if len(child_bundles) == 1
            else CollectionBundle.union(parent_id, names.get(parent_id, ""), child_bundles)
        )
    return bundles


def effective_person_bundles(db: Db) -> dict[str, CollectionBundle]:
    """Read contact-owned bundles; only singleton parent bundles are reusable."""
    singleton = singleton_people(db)
    aggregate_ids = aggregate_people(db)
    rows = artifacts(db, kind=ArtifactKind.SOURCE_BUNDLE.value, status="projected")
    bundles = {}
    for row in sorted(rows, key=lambda item: bool(item.person_id)):
        person_id = row.person_id or singleton.get(row.parent_id)
        if person_id is None or person_id in aggregate_ids:
            continue
        bundle = CollectionBundle.from_payload(parse_json_object(row.payload_json))
        if bundle is not None:
            bundles[person_id] = replace(bundle, person_id=person_id)
    return bundles


def pending_target_bundles(
    db: Db,
    *,
    system_prompt: str,
    chunk_chars: int,
    max_batches: int,
    force: bool,
    model: str = "",
    reasoning_effort: str = "",
) -> list[CollectionBundle]:
    """Use successful per-record coverage and config; retain old fingerprint caches."""
    singleton = singleton_people(db)
    cached = {
        str(row.person_id or singleton.get(row.parent_id, "")): (
            str(row.input_fingerprint or ""),
            str(json.loads(row.payload_json or "{}").get("synthesis_version") or ""),
        )
        for row in sorted(artifacts(db, kind=ArtifactKind.FACTS.value, status="projected"), key=lambda item: bool(item.person_id))
        if not row.artifact_key.startswith('parent-facts:') and (row.person_id or row.parent_id in singleton)
    }
    histories = person_histories(db)
    effective_bundles = effective_person_bundles(db)
    bundles: list[CollectionBundle] = []
    owner_ids = {row.person_id for row in people(db) if row.is_owner}
    # Deterministic work order: a partial or interrupted run resumes in the same
    # sequence every time instead of whatever dict/artifact-scan order produced.
    for pid, bundle in sorted(effective_bundles.items()):
        if pid in owner_ids:
            continue
        history = histories.get(pid)
        if history and history.processed:
            latest = history.records[-1].record
            seeded = latest.input_evidence_fingerprint.startswith(prompting.SEED_FINGERPRINT_PREFIX)
            changed = bool(latest.model and model and (
                latest.model != model or latest.reasoning_effort != reasoning_effort
            )) or (not seeded and (
                bool(latest.synthesis_version and latest.synthesis_version != prompting.SYNTHESIS_VERSION)
                or bool(latest.system_prompt_hash and latest.system_prompt_hash != hashlib.sha256(system_prompt.encode()).hexdigest())
            ))
            if not force and not changed:
                unseen = tuple(message for message in bundle.messages if message.fingerprint() not in history.processed)
                if not unseen:
                    continue
                bundles.append(replace(bundle, messages=unseen))
                continue
            bundles.append(bundle)
            continue
        # Force and a model/effort change are explicit paid overrides; normal
        # runs resume only when the prompt contract, the exact bounded
        # evidence, AND the answering model/effort all still match.
        if not force:
            fingerprint, version = cached.get(pid, ("", ""))
            if (
                fingerprint.startswith(prompting.SEED_FINGERPRINT_PREFIX)
                and fingerprint == prompting.seed_evidence_fingerprint(bundle)
            ):
                continue
            # The version catches prompt/schema edits, while the evidence hash
            # catches message or owner-context changes. Either mismatch must
            # re-run synthesis or the facts would describe stale model input.
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
        raise StoreError("deep context requires an owner profile; run bin/deep-context owner first")
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
    model: str = "",
    reasoning_effort: str = "",
) -> SynthesisPlan:
    bundles = tuple(
        pending_target_bundles(
            db,
            system_prompt=system_prompt,
            chunk_chars=chunk_chars,
            max_batches=max_batches,
            force=force,
            model=model,
            reasoning_effort=reasoning_effort,
        )
    )
    return SynthesisPlan(system_prompt, bundles, owner_profile(db))
