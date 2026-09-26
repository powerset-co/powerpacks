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
    facts,
    owner_profile,
    parents,
    people,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.db.context_queries import parent_histories
from packs.ingestion.primitives.deep_context.synthesis import prompting
from packs.ingestion.primitives.deep_context.synthesis.models import SynthesisPlan


def effective_parent_bundles(db: Db) -> dict[str, CollectionBundle]:
    """Union every cached bundle under its current parent, including merged parents."""
    source_artifacts = artifacts(db, kind=ArtifactKind.SOURCE_BUNDLE.value, status="projected")
    bundles: dict[str, CollectionBundle] = {}
    children: dict[str, list[CollectionBundle]] = {}
    for row in source_artifacts:
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


def _stored_legacy_fingerprint(db: Db, pid: str) -> str:
    """The real evidence fingerprint recorded on a parent's legacy child FACTS artifacts.

    ``input_fingerprint`` postdates the legacy per-child layout, so on every
    install seen so far this returns "" for every legacy parent — and "" can
    never equal a live-computed hash, so the caller correctly treats that
    parent as pending (spend) instead of fabricating a match. Returns the
    first non-empty value only so a parent that DOES carry one (a legacy
    artifact re-projected after this field started being written) still gets
    the fast-path skip it has always earned.
    """
    return next(
        (
            str(row.input_fingerprint)
            for row in artifacts(db, kind=ArtifactKind.FACTS.value, parent_id=pid, parent_owned=False)
            if row.input_fingerprint
        ),
        "",
    )


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
    cached = {
        str(row.parent_id): (
            str(row.input_fingerprint or ""),
            str(json.loads(row.payload_json or "{}").get("synthesis_version") or ""),
        )
        for row in artifacts(db, kind=ArtifactKind.FACTS.value, parent_owned=True)
    }
    histories = parent_histories(db)
    effective_bundles = effective_parent_bundles(db)
    child_fact_parents = {str(row.parent_id) for row in facts(db, parent_owned=False)}
    # A parent with child-owned facts but no parent-owned FACTS artifact (legacy
    # per-child layout) borrows a cache entry from a REAL fingerprint recorded on
    # one of those legacy artifacts, if any exists. It almost never does (see
    # _stored_legacy_fingerprint), so this parent falls through to the loop
    # below with no cache entry at all and is correctly treated as pending —
    # never a fabricated match against whatever the current bundle happens to be.
    for pid in child_fact_parents - cached.keys():
        fingerprint = _stored_legacy_fingerprint(db, pid)
        if fingerprint:
            cached[pid] = (fingerprint, prompting.SYNTHESIS_VERSION)
    bundles: list[CollectionBundle] = []
    person_rows = people(db)
    member_parents = {str(row.parent_id) for row in person_rows}
    non_owner_parents = {str(row.parent_id) for row in person_rows if not row.is_owner}
    # Parents whose every person row is the owner: the owner is never a subject
    # of their own dossier, and collection planning already excludes them — this
    # guards cached bundles that predate that exclusion (an earlier layout).
    owner_only_parents = member_parents - non_owner_parents
    # Deterministic work order: a partial or interrupted run resumes in the same
    # sequence every time instead of whatever dict/artifact-scan order produced.
    for pid, bundle in sorted(effective_bundles.items()):
        if pid in owner_only_parents:
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
