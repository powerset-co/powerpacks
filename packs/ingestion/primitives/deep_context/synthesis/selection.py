"""Select projected source bundles and skip unchanged paid synthesis work.

Changelog:
- 2026-08-08: two skip-decision fixes.
  (1) The legacy-child-facts shortcut used to fabricate its cache entry by
  hashing the CURRENT bundle and comparing it to itself a few lines later —
  an unconditional match. On the owner's install this blanketed ~all 542
  legacy parents: --dry-run reported people=0/cost=$0.00 even for parents
  whose bundles had just been re-collected with new messages. It now reuses
  only a real fingerprint recorded on a legacy child FACTS artifact, if one
  exists; none exist on any current install (the field postdates every
  legacy record), so every legacy parent is pending until real synthesis
  writes a genuine parent-owned fingerprint for it.
  (2) pending_target_bundles now also treats a model/reasoning-effort change
  since the stage's last completed run as a full-plan cache miss — see
  SynthesizePersonContext._model_or_effort_changed, which reads that value
  back from the stage's own manifest.json (no new store).
"""

from __future__ import annotations

import json

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
from packs.ingestion.primitives.deep_context.synthesis import prompting
from packs.ingestion.primitives.deep_context.synthesis.models import SynthesisPlan


def _effective_parent_bundles(db: Db) -> dict[str, CollectionBundle]:
    """Preview the parent bundles cache normalization will project, without writes.

    Mirrors what collection/normalization.py:normalize_cached_bundles would
    write to durable SOURCE_BUNDLE rows, computed here read-only so selection
    can fingerprint bundles before that migration runs. The CollectionBundle.union
    branch below only fires for parents still on the legacy per-child bundle
    layout (person_id is not None); a current install never takes it.
    """
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
            bundles[parent_id] = CollectionBundle.union(
                parent_id,
                names.get(parent_id, ""),
                child_bundles,
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
    rejudge: bool = False,
    model_changed: bool = False,
) -> list[CollectionBundle]:
    """Decide, per parent, whether to skip (cache hit) or spend on synthesis.

    Three independent checks must all hold for a skip: prompting.SYNTHESIS_VERSION
    (catches prompt/schema/contract edits), input_evidence_fingerprint (catches
    evidence changes), and ``model_changed`` being False (catches a --model or
    --reasoning-effort switch since the stage's last completed run — see
    SynthesizePersonContext._model_or_effort_changed). See the inline comments
    below for how each is compared.
    """
    cached = {
        str(row.parent_id): (
            str(row.input_fingerprint or ""),
            str(json.loads(row.payload_json or "{}").get("synthesis_version") or ""),
        )
        for row in artifacts(db, kind=ArtifactKind.FACTS.value, parent_owned=True)
    }
    effective_bundles = _effective_parent_bundles(db)
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
        # Force, rejudge, and a model/effort change are explicit paid overrides;
        # normal runs resume only when the prompt contract, the exact bounded
        # evidence, AND the answering model/effort all still match.
        if not force and not rejudge and not model_changed:
            fingerprint, version = cached.get(pid, ("", ""))
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
    model_changed: bool = False,
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
                model_changed=model_changed,
            )
        ),
    )
