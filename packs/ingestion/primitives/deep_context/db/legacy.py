"""One-time import of pre-v5 Deep Context artifacts.

This is the only tolerant reader of legacy review/facts/research shapes.  It
builds and validates the complete relational graph before one SQLite commit;
normal stage projectors in ``projectors.py`` accept only the current manifest.

Changelog:
  2026-08-05: v5 import preserves the exact facts-backed worth population,
    resolves retired aliases and slug owners, and rejects orphan relations.
"""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.contact_fields import normalize_email, normalize_phone
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.common.legacy import (
    MESSAGE_LINKEDIN_PREFIX,
    message_linkedin_aliases,
)
from packs.ingestion.primitives.deep_context.build_parents import parent_id_for
from packs.ingestion.primitives.deep_context.db import batons
from packs.ingestion.primitives.deep_context.db.schema import (
    ApprovedState,
    ArtifactKind,
    ArtifactRow,
    CandidatePersonRow,
    FactRow,
    HumanWorth,
    LLM_REJECT_VALUES,
    LinkRow,
    MachineWorth,
    ParentRow,
    PersonIdentifierRow,
    PersonRow,
    PersonSourceRow,
    ProjectionStatus,
    ResearchRow,
    ResearchStatus,
    ReviewAction,
    ReviewSource,
    RowKind,
    SpendApprovalRow,
    StageStateRow,
    StageStatus,
    SyntheticProfileRow,
    classify_review_key,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError


class LegacyImportError(StoreError):
    pass


@dataclass(frozen=True)
class _Facts:
    subject: str
    payload: dict[str, Any]
    name: str | None
    worth: str | None
    reason: str | None
    confidence: float | None
    is_owner: bool
    path: Path
    fingerprint: str


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _number(value: object) -> float | None:
    try:
        return float(str(value)) if str(value or "").strip() else None
    except ValueError:
        return None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_merged_people(path: Path | None) -> tuple[dict[str, set[str]], set[str]]:
    sources: dict[str, set[str]] = {}
    candidates: set[str] = set()
    if path is None or not path.exists():
        return sources, candidates
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            person_id = str(row.get("id") or "").strip().lower()
            if not person_id:
                continue
            sources.setdefault(person_id, set()).update(
                value.strip() for value in str(row.get("source_channels") or "").split(",")
                if value.strip()
            )
            if person_id.startswith("candidate:"):
                candidates.add(person_id)
    return sources, candidates


def _read_facts(path: Path) -> _Facts | None:
    """Accumulate every valid direct/enveloped JSONL record like worth_view."""
    merged: dict[str, Any] = {}
    name = worth = reason = None
    confidence: float | None = None
    is_owner = False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        facts = record.get("facts") if isinstance(record.get("facts"), dict) else record
        merged.update(facts)
        name = _text(facts.get("canonical_name")) or name
        is_owner = bool(facts.get("is_owner")) or is_owner
        verdict = facts.get("network_worth")
        if isinstance(verdict, dict):
            decision = str(verdict.get("decision") or "").strip().lower()
            if decision in set(MachineWorth):
                worth, reason = decision, _text(verdict.get("reason"))
        confidence = _number(facts.get("confidence") or record.get("final_confidence")) or confidence
    if not merged:
        return None
    return _Facts(path.stem.lower(), merged, name, worth, reason, confidence, is_owner,
                  path, _sha256(path))


def _read_index(path: Path | None) -> tuple[
    dict[str, ParentRow], dict[str, PersonRow], dict[str, str],
    dict[str, set[tuple[str, str, str | None]]],
]:
    if path is None or not path.exists():
        return {}, {}, {}, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LegacyImportError(f"cannot parse index.json: {exc}") from exc
    slugs = payload.get("slugs") if isinstance(payload.get("slugs"), dict) else {}
    parents: dict[str, ParentRow] = {}
    people: dict[str, PersonRow] = {}
    slug_to_parent: dict[str, str] = {}
    identifiers: dict[str, set[tuple[str, str, str | None]]] = {}
    for parent_slug, raw in (payload.get("parents") or {}).items():
        if not isinstance(raw, dict):
            continue
        child_ids = [
            str((slugs.get(slug) or {}).get("person_id") or "").strip().lower()
            for slug in raw.get("children") or []
        ]
        child_ids = [value for value in child_ids if value]
        parent_id = str(raw.get("parent_id") or "").strip().lower()
        parent_id = parent_id or (parent_id_for(child_ids) if child_ids else "")
        if not parent_id:
            continue
        parent_slug = str(parent_slug)
        slug_to_parent[parent_slug] = parent_id
        parents[parent_id] = ParentRow(
            parent_id, f"parent-worth:{parent_id}", _text(raw.get("name")), parent_slug,
            source=ReviewSource.LEGACY_MIGRATION.value,
        )
        for child_slug in raw.get("children") or []:
            info = slugs.get(child_slug) or {}
            person_id = str(info.get("person_id") or "").strip().lower()
            if person_id:
                people[person_id] = PersonRow(
                    person_id, parent_id, str(child_slug), parent_slug,
                    _text(info.get("name") or info.get("full_name")),
                )
    for field, kind, normalize in (
        ("by_email", "email", normalize_email), ("by_phone", "phone", normalize_phone)
    ):
        for display, owner_slugs in (payload.get(field) or {}).items():
            person_ids = {
                str((slugs.get(slug) or {}).get("person_id") or "").strip().lower()
                for slug in owner_slugs or []
            } - {""}
            normalized = normalize(display)
            if len(person_ids) == 1 and normalized:
                person_id = next(iter(person_ids))
                identifiers.setdefault(person_id, set()).add((kind, normalized, str(display)))
    return parents, people, slug_to_parent, identifiers


def _split(value: object) -> list[str]:
    return sorted({item.strip() for item in str(value or "").split("|") if item.strip()})


def _human_signal(row: dict[str, str]) -> tuple[str, str] | None:
    mark = str(row.get("network_worth") or "").strip().lower()
    if mark not in set(HumanWorth):
        if (str(row.get("action") or "").strip().lower() == ReviewAction.EXCLUDE.value
                and str(row.get("approved") or "").strip().lower() == ApprovedState.YES.value):
            mark = HumanWorth.NO.value
        else:
            return None
    return mark, str(row.get("updated_at") or "")


def _resolve_parent(
    key: str | None, *, person_parent: dict[str, str], slug_parent: dict[str, str],
    aliases: dict[str, str],
) -> str | None:
    value = str(key or "").strip().lower()
    alias = aliases.get(value, value)
    return person_parent.get(value) or person_parent.get(alias) or slug_parent.get(value)


def import_legacy(
    db: Db, *, review_csv: Path, synthetic_csv: Path | None = None,
    index_json: Path | None = None, facts_dir: Path | None = None,
    verdicts_jsonl: Path | None = None, research_dir: Path | None = None,
    merged_people_csv: Path | None = None,
    avatar_dir: Path | None = None,
    manifests: tuple[Path, ...] = (),
) -> dict[str, int]:
    """Absorb old artifacts once; any unresolved owner aborts the whole import."""
    review_rows = batons.load_override_rows(review_csv)
    aliases = message_linkedin_aliases(list(review_rows.values()))
    parents, people, slug_parent, identifiers = _read_index(index_json)
    person_sources, merged_candidate_ids = _read_merged_people(merged_people_csv)
    indexed_person_ids = set(people)
    parsed_facts = [item for item in (
        _read_facts(path) for path in sorted(facts_dir.glob("*.jsonl"))
    ) if item] if facts_dir and facts_dir.exists() else []

    # Facts define worth population. Current index membership wins; the retired
    # alias only supplies a durable sibling when that old key is absent there.
    for fact in parsed_facts:
        alias = aliases.get(fact.subject, fact.subject)
        parent_id = people.get(fact.subject, people.get(alias))
        owner = parent_id.parent_id if parent_id else parent_id_for([alias])
        if owner not in parents:
            parents[owner] = ParentRow(
                owner, f"parent-worth:{owner}", fact.name, alias,
                source=ReviewSource.LEGACY_MIGRATION.value,
            )
        existing = people.get(fact.subject)
        people[fact.subject] = PersonRow(
            fact.subject, owner,
            existing.child_slug if existing else fact.subject,
            existing.parent_slug if existing else alias,
            fact.name or (existing.display_name if existing else None),
            int(fact.is_owner), int(fact.subject.startswith(MESSAGE_LINKEDIN_PREFIX)),
            json.dumps(fact.payload, separators=(",", ":")), fact.confidence, now_iso(),
        )

    person_parent = {key: row.parent_id for key, row in people.items()}
    machine_by_parent: dict[str, list[_Facts]] = {}
    for fact in parsed_facts:
        machine_by_parent.setdefault(person_parent[fact.subject], []).append(fact)
    priority = {MachineWorth.NO.value: 0, MachineWorth.MAYBE.value: 1, MachineWorth.YES.value: 2}
    for parent_id, members in machine_by_parent.items():
        winner = max(members, key=lambda item: (priority[item.worth or "maybe"], item.subject))
        parents[parent_id] = replace(
            parents[parent_id],
            display_name=parents[parent_id].display_name or winner.name,
            machine_worth=winner.worth or MachineWorth.MAYBE.value,
            machine_worth_reason=winner.reason,
            updated_at=now_iso(),
        )

    links: dict[str, LinkRow] = {}
    memberships: dict[str, set[str]] = {}
    human_links: dict[str, tuple[str, str, str, str | None, str | None, str | None]] = {}
    parent_signals: dict[str, tuple[str, str, str | None]] = {}
    child_signals: dict[str, tuple[str, str, str | None]] = {}
    errors: list[str] = []

    for key, row in review_rows.items():
        if key.startswith("parent-worth:"):
            parent_id = key.removeprefix("parent-worth:")
            signal = _human_signal(row)
            if signal and parent_id in parents:
                parent_signals[parent_id] = (signal[0], signal[1], _text(row.get("user_worth_note")))
            elif signal:
                errors.append(f"{key}: worth owner not found")
            continue
        action = str(row.get("action") or "").strip().lower() or None
        approved = str(row.get("approved") or "").strip().lower() or None
        reject = str(row.get("llm_reject") or "").strip().lower() or None
        if action and action not in set(ReviewAction):
            errors.append(f"{key}: unknown action {action!r}")
        if approved and approved not in set(ApprovedState):
            errors.append(f"{key}: unknown approved {approved!r}")
        if reject and reject not in LLM_REJECT_VALUES:
            errors.append(f"{key}: unknown llm_reject {reject!r}")
        person_id = str(row.get("person_id") or "").strip().lower()
        parent_id = _resolve_parent(person_id or key, person_parent=person_parent,
                                    slug_parent=slug_parent, aliases=aliases)
        if not parent_id:
            # A review-only legacy candidate still gets a real singleton owner.
            seed = aliases.get(person_id or key, person_id or key)
            parent_id = parent_id_for([seed])
            parents.setdefault(parent_id, ParentRow(
                parent_id, f"parent-worth:{parent_id}", display_slug=seed,
                source=ReviewSource.LEGACY_MIGRATION.value,
            ))
            if person_id:
                people[person_id] = PersonRow(
                    person_id, parent_id, person_id, seed,
                    is_ghost=int(person_id.startswith(MESSAGE_LINKEDIN_PREFIX)),
                )
                person_parent[person_id] = parent_id
        kind = classify_review_key(key)
        proposed_url = _text(row.get("new_linkedin_url"))
        proposed_pub = _text(row.get("new_public_identifier"))
        machine_proposal = (
            action == ReviewAction.RETARGET.value
            and approved not in {ApprovedState.YES.value, ApprovedState.NO.value}
        )
        links[key] = LinkRow(
            key, parent_id, _text(row.get("public_identifier")) or key, kind.value,
            _text(row.get("linkedin_url")),
            machine_action=action if approved != ApprovedState.YES.value else None,
            machine_approved=approved if approved == ApprovedState.AUTO.value else None,
            machine_proposed_url=proposed_url if machine_proposal else None,
            machine_proposed_public_identifier=proposed_pub if machine_proposal else None,
            machine_confidence=_number(row.get("confidence")),
            machine_reason=_text(row.get("reason")),
            machine_reject=reject,
            machine_reject_confidence=_number(row.get("llm_reject_confidence")),
            machine_reject_reason=_text(row.get("llm_reject_reason")),
            authoritative_detach=int(
                action == ReviewAction.DETACH.value
                and (_number(row.get("confidence")) or 0) >= .85
            ),
            candidate_origin=int(key.startswith("candidate:")),
            raw_import=int(key.startswith("candidate:") and not (proposed_url or proposed_pub)),
            judgment_fingerprint=_text(row.get("llm_judge_fingerprint")),
            source=_text(row.get("source")), updated_at=_text(row.get("updated_at")),
        )
        if person_id:
            memberships.setdefault(key, set()).add(person_id)
            for value in _split(row.get("match_emails")):
                norm = normalize_email(value)
                if norm:
                    identifiers.setdefault(person_id, set()).add(("email", norm, value))
            for value in _split(row.get("match_phones")):
                norm = normalize_phone(value)
                if norm:
                    identifiers.setdefault(person_id, set()).add(("phone", norm, value))
        if approved in {ApprovedState.YES.value, ApprovedState.NO.value} and action:
            human_links[key] = (
                action, approved, _text(row.get("source")) or ReviewSource.REVIEW.value,
                _text(row.get("updated_at")), _text(row.get("new_linkedin_url")),
                _text(row.get("new_public_identifier")),
            )
        signal = _human_signal(row)
        if signal:
            for child in {key, person_id} - {""}:
                previous = child_signals.get(child)
                candidate = (signal[0], signal[1], _text(row.get("user_worth_note")))
                if previous is None or candidate[1] > previous[1]:
                    child_signals[child] = candidate

    verdict_keys: set[str] = set()
    # Verdict JSONL owns the many-child relation and candidate machine verdict.
    if verdicts_jsonl and verdicts_jsonl.exists():
        for line in verdicts_jsonl.read_text(encoding="utf-8").splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            person_ids = [str(value).strip().lower() for value in payload.get("person_ids") or [] if str(value).strip()]
            raw_candidates = [value for value in person_ids if value.startswith("candidate:")]
            key = (
                next(iter(raw_candidates), "") if payload.get("no_link") and raw_candidates
                else str(payload.get("candidate_key") or "").strip().lower()
                or next(iter(person_ids), "")
            )
            parent_ids = {person_parent.get(person_id) for person_id in person_ids} - {None}
            slug_owner = slug_parent.get(str(payload.get("parent_slug") or "").strip())
            if slug_owner:
                parent_ids.add(slug_owner)
            if len(parent_ids) != 1:
                errors.append(f"verdict:{key or '?'}: cannot resolve one parent")
                continue
            parent_id = next(iter(parent_ids))
            for person_id in person_ids:
                if person_id not in people:
                    people[person_id] = PersonRow(person_id, parent_id, person_id, str(payload.get("parent_slug") or ""))
                    person_parent[person_id] = parent_id
                elif people[person_id].parent_id != parent_id:
                    errors.append(f"verdict:{key}: person {person_id} belongs to another parent")
            if not key:
                continue
            verdict_keys.add(key)
            verdict = payload.get("verdict") if isinstance(payload.get("verdict"), dict) else {}
            prior = links.get(key)
            links[key] = replace(
                prior or LinkRow(key, parent_id, key, classify_review_key(key).value),
                parent_id=parent_id,
                machine_judgment=_text(verdict.get("verdict")),
                machine_confidence=_number(verdict.get("confidence")),
                machine_reason=_text(verdict.get("reason")), paid_profile=1,
                authoritative_detach=int(
                    bool(prior and prior.machine_action == ReviewAction.DETACH.value)
                    and
                    str(verdict.get("verdict") or "") == "wrong_person"
                    and (_number(verdict.get("confidence")) or 0) >= .85
                ),
                judgment_fingerprint=_text(payload.get("fingerprint") or payload.get("judge_input_fingerprint")),
                judgment_artifact_path=str(verdicts_jsonl),
                judgment_payload_json=json.dumps(payload, separators=(",", ":")),
            )
            memberships.setdefault(key, set()).update(person_ids)

    # Synthetic rows are candidates first; their gate lives on the candidate.
    synthetics: list[SyntheticProfileRow] = []
    synthetic_artifacts: list[ArtifactRow] = []
    stale_synthetic_memberships = 0
    synthetic_source_memberships: set[str] = set()
    synthetic_fingerprint = _sha256(synthetic_csv) if synthetic_csv and synthetic_csv.exists() else None
    for row in batons.load_synthetic_rows(synthetic_csv):
        pub = str(row.get("public_identifier") or "").strip().lower()
        person_ids = [str(value).strip().lower() for value in json.loads(row.get("source_person_ids") or "[]")]
        synthetic_source_memberships.update(person_ids)
        indexed_owners = {person_parent.get(value) for value in person_ids
                          if value in indexed_person_ids} - {None}
        parent_ids = indexed_owners or ({person_parent.get(value) for value in person_ids} - {None})
        slug_owner = slug_parent.get(str(row.get("source_parent_slug") or "").strip())
        source_candidate = str(row.get("source_candidate_public_identifier") or "").strip().lower()
        # Current child membership is authoritative.  Source candidate then
        # stale slug are fallbacks for pre-recluster synthetic artifacts.
        if not indexed_owners and source_candidate in links:
            parent_ids = {links[source_candidate].parent_id}
        elif not indexed_owners and slug_owner:
            parent_ids = {slug_owner}
        if not parent_ids:
            seeds = person_ids or [str(row.get("id") or pub).strip().lower()]
            parent_id = parent_id_for(seeds)
            parent_ids.add(parent_id)
            parents.setdefault(parent_id, ParentRow(
                parent_id, f"parent-worth:{parent_id}",
                _text(row.get("full_name")), _text(row.get("source_parent_slug")) or pub,
                source=ReviewSource.LEGACY_MIGRATION.value,
            ))
        if not pub or len(parent_ids) != 1:
            errors.append(f"synthetic:{pub or '?'}: cannot resolve one parent")
            continue
        parent_id = next(iter(parent_ids))
        if not person_ids:
            person_ids = [str(row.get("id") or pub).strip().lower()]
        current_person_ids: list[str] = []
        for person_id in person_ids:
            prior_person = people.get(person_id)
            if prior_person and prior_person.parent_id != parent_id:
                stale_synthetic_memberships += 1
                continue
            elif not prior_person:
                people[person_id] = PersonRow(
                    person_id, parent_id, person_id,
                    _text(row.get("source_parent_slug")) or pub,
                    _text(row.get("full_name")),
                )
                person_parent[person_id] = parent_id
            current_person_ids.append(person_id)
        person_ids = current_person_ids
        for person_id in person_ids:
            person_sources.setdefault(person_id, set()).update(
                value.strip() for value in str(row.get("source_channels") or "").split(",")
                if value.strip()
            )
        candidate_key = pub
        artifact_key = f"synthetic:{pub}"
        approved = str(row.get("approved") or "").strip().lower()
        links[candidate_key] = LinkRow(
            candidate_key, parent_id, pub, RowKind.SYNTHETIC.value,
            _text(row.get("linkedin_url")), _text(row.get("full_name")),
            machine_action=ReviewAction.VERIFY.value if approved == "auto" else None,
            machine_approved="auto" if approved == "auto" else None,
            source=ReviewSource.LEGACY_MIGRATION.value,
        )
        memberships[candidate_key] = set(person_ids)
        if approved in {"yes", "no"}:
            human_links[candidate_key] = (
                ReviewAction.VERIFY.value if approved == "yes" else ReviewAction.DETACH.value,
                ApprovedState.YES.value, ReviewSource.REVIEW.value,
                _text(row.get("enriched_at")), None, None,
            )
        synthetics.append(SyntheticProfileRow(
            pub, candidate_key, json.dumps(row, separators=(",", ":")),
            artifact_key,
            linkedin_url=_text(row.get("linkedin_url")),
            name=_text(row.get("full_name")) or " ".join(filter(None, (
                _text(row.get("first_name")), _text(row.get("last_name"))))) or None,
            updated_at=_text(row.get("enriched_at")),
        ))
        synthetic_artifacts.append(ArtifactRow(
            artifact_key, ArtifactKind.SYNTHETIC.value, parent_id, str(synthetic_csv),
            synthetic_fingerprint or "0" * 64, ProjectionStatus.PROJECTED.value,
            candidate_key=candidate_key,
            payload_json=json.dumps(row, separators=(",", ":")), projected_at=now_iso(),
        ))

    # The legacy loader emitted one import shell for every undisplayed
    # worth-bearing facts subject, including UUID subjects. review.csv still
    # contains thousands of unrelated baton/history rows; those remain out.
    candidate_fact_keys = {
        fact.subject for fact in parsed_facts
        if fact.worth is not None or fact.subject in merged_candidate_ids
    }
    displayed_memberships = {
        person_id
        for displayed_key in verdict_keys | {
            key for key, row in links.items() if row.kind == RowKind.SYNTHETIC.value
        }
        for person_id in memberships.get(displayed_key, set())
    } | synthetic_source_memberships
    covered_fact_keys = {
        key for key in candidate_fact_keys
        if key in displayed_memberships and key not in verdict_keys and key not in human_links
    }
    for key in covered_fact_keys:
        links.pop(key, None)
        memberships.pop(key, None)
    candidate_fact_keys -= covered_fact_keys
    for key in candidate_fact_keys - set(links):
        parent_id = person_parent[key]
        links[key] = LinkRow(
            key, parent_id, key, classify_review_key(key).value,
            candidate_origin=int(key.startswith("candidate:")), raw_import=1,
            source=ReviewSource.LEGACY_MIGRATION.value,
        )
        memberships[key] = {key}
    links = {key: replace(row, candidate_origin=0, raw_import=0)
             for key, row in links.items()}
    for key in candidate_fact_keys:
        row = links[key]
        identity_result = (
            row.machine_action == ReviewAction.RETARGET.value
            and bool(row.machine_proposed_url or row.machine_proposed_public_identifier)
        ) or (
            row.machine_action in {ReviewAction.VERIFY.value, ReviewAction.DETACH.value}
            and row.machine_approved in {
                ApprovedState.AUTO.value, ApprovedState.YES.value, ApprovedState.NO.value,
            }
        ) or key in human_links
        links[key] = replace(
            row,
            candidate_origin=int(key.startswith("candidate:")),
            raw_import=int(not identity_result),
        )

    # Synthetic provenance can fold an unindexed candidate identity into the
    # source candidate's current owner. Recompute worth after that legacy-only
    # repair so facts and parent projection share the same owner.
    machine_by_parent = {}
    for fact in parsed_facts:
        machine_by_parent.setdefault(person_parent[fact.subject], []).append(fact)
    for parent_id, members in machine_by_parent.items():
        winner = max(members, key=lambda item: (priority[item.worth or "maybe"], item.subject))
        parents[parent_id] = replace(
            parents[parent_id], machine_worth=winner.worth or MachineWorth.MAYBE.value,
            machine_worth_reason=winner.reason,
            display_name=parents[parent_id].display_name or winner.name,
            updated_at=now_iso(),
        )

    # Parent rows beat child marks; otherwise migrate the latest child signal.
    for parent_id, members in ((pid, [p.person_id for p in people.values() if p.parent_id == pid])
                               for pid in parents):
        if parent_id in parent_signals:
            continue
        candidates = [child_signals[value] for value in members if value in child_signals]
        if candidates:
            parent_signals[parent_id] = max(candidates, key=lambda item: item[1])

    stale_memberships = 0
    for key, person_ids in memberships.items():
        mismatched = [value for value in person_ids
                      if value not in people or people[value].parent_id != links[key].parent_id]
        if (mismatched and all(value in indexed_person_ids for value in mismatched)
                and links[key].source == ReviewSource.RECONCILE.value):
            memberships[key] -= set(mismatched)
            stale_memberships += len(mismatched)
            mismatched = []
        if key in verdict_keys and not memberships[key]:
            errors.append("legacy verdict has no current candidate membership")
        if mismatched:
            errors.append(
                f"{classify_review_key(key).value} candidate has {len(mismatched)} cross-parent "
                f"members (indexed={sum(value in indexed_person_ids for value in mismatched)}, "
                f"members={len(person_ids)}, source={links[key].source or 'none'})"
            )
    if errors:
        raise LegacyImportError("; ".join(errors[:20]))

    artifacts: list[ArtifactRow] = list(synthetic_artifacts)
    facts: list[FactRow] = []
    for fact in parsed_facts:
        artifact_key = f"facts:{fact.subject}"
        parent_id = person_parent[fact.subject]
        artifacts.append(ArtifactRow(
            artifact_key, ArtifactKind.FACTS.value, parent_id, str(fact.path), fact.fingerprint,
            ProjectionStatus.PROJECTED.value, person_id=fact.subject, projected_at=now_iso(),
        ))
        facts.append(FactRow(
            fact.subject, parent_id, artifact_key, fact.subject, fact.worth, fact.reason,
            fact.confidence, int(fact.is_owner), json.dumps(fact.payload, separators=(",", ":")), now_iso(),
        ))

    if avatar_dir and avatar_dir.exists():
        for key, link in links.items():
            human = human_links.get(key)
            profile_pubs = (
                human[5] if human else None,
                link.machine_proposed_public_identifier,
                link.public_identifier,
            )
            for profile_pub in dict.fromkeys(profile_pubs):
                if not profile_pub:
                    continue
                digest = hashlib.sha256(profile_pub.strip().lower().encode()).hexdigest()[:24]
                path = avatar_dir / f"{digest}.image"
                if not path.is_file():
                    continue
                artifacts.append(ArtifactRow(
                    f"avatar:{key}", ArtifactKind.AVATAR.value, link.parent_id,
                    str(path.resolve()), _sha256(path), ProjectionStatus.PROJECTED.value,
                    candidate_key=key, projected_at=now_iso(),
                ))
                break

    research: list[ResearchRow] = []
    if research_dir and research_dir.exists():
        for directory in sorted(path for path in research_dir.iterdir() if path.is_dir()):
            path = directory / "01_research_parallel.json"
            if not path.exists():
                continue
            owner = slug_parent.get(directory.name)
            if not owner:
                continue  # old unowned paid output stays on disk but cannot enter SQL
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise LegacyImportError(f"cannot parse research {directory.name}: {exc}") from exc
            artifact_key = f"research:{directory.name}"
            artifacts.append(ArtifactRow(
                artifact_key, ArtifactKind.RESEARCH.value, owner, str(path), _sha256(path),
                ProjectionStatus.PROJECTED.value,
                payload_json=json.dumps(payload, separators=(",", ":")), projected_at=now_iso(),
            ))
            research.append(ResearchRow(
                directory.name, owner, ResearchStatus.COMPLETE.value, artifact_key=artifact_key,
                result_json=json.dumps(payload, separators=(",", ":")), updated_at=now_iso(),
            ))

    stages: list[StageStateRow] = []
    approvals: list[SpendApprovalRow] = []
    for path in manifests:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        stage = str(payload.get("stage") or path.parent.name).strip()
        raw_status = str(payload.get("status") or "pending").strip().lower()
        status = (
            StageStatus.NEEDS_APPROVAL.value if raw_status == "needs_approval"
            else StageStatus.FAILED.value if raw_status == "failed"
            else StageStatus.COMPLETE.value if raw_status in {
                "complete", "completed", "completed_with_errors", "research_complete"
            }
            else StageStatus.RUNNING.value if raw_status in {"running", "submitted"}
            else StageStatus.PENDING.value
        )
        selection = payload.get("selection")
        selection_fingerprint = (
            _text(selection.get("fingerprint")) if isinstance(selection, dict) else _text(selection)
        )
        stages.append(StageStateRow(
            stage, status, selection_fingerprint, _sha256(path),
            _text(payload.get("completed_at")) if status == StageStatus.COMPLETE.value else None,
            _text(payload.get("error")), _text(payload.get("updated_at")),
        ))
        approval = payload.get("approval")
        if isinstance(approval, dict) and selection_fingerprint:
            approvals.append(SpendApprovalRow(
                stage, selection_fingerprint, int(approval.get("approved_count") or 0),
                _number(approval.get("approved_amount")), _text(approval.get("approved_at")),
            ))

    tables = ("parents", "people", "person_identifiers", "person_sources",
              "links", "candidate_people",
              "artifacts", "facts", "synthetic_profiles", "research", "guidance", "jobs",
              "stage_state", "spend_approvals")
    with db.connect() as conn:
        occupied = [name for name in tables if conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()]
        if occupied:
            raise LegacyImportError(f"canonical DB is not empty: {', '.join(occupied)}")
        for row in parents.values():
            db.project_parent(row, conn=conn)
        for row in people.values():
            db.project_person(row, conn=conn)
        for person_id, values in identifiers.items():
            db.replace_person_identifiers(person_id, tuple(
                PersonIdentifierRow(person_id, kind, normalized, display)
                for kind, normalized, display in sorted(values)
            ), conn=conn)
        for person_id, values in person_sources.items():
            if person_id not in people:
                continue
            db.replace_person_sources(person_id, tuple(
                PersonSourceRow(person_id, source) for source in sorted(values)
            ), conn=conn)
        for row in links.values():
            db.project_candidate(row, conn=conn)
        for key, person_ids in memberships.items():
            parent_id = links[key].parent_id
            db.replace_candidate_people(key, tuple(
                CandidatePersonRow(key, person_id, parent_id) for person_id in sorted(person_ids)
            ), conn=conn)
        for row in artifacts:
            db.project_artifact(row, conn=conn)
        for row in facts:
            db.project_fact(row, conn=conn)
        for row in synthetics:
            db.project_synthetic_profile(row, conn=conn)
        for row in research:
            db.project_research(row, conn=conn)
        for row in stages:
            db.save_stage(row, conn=conn)
        for row in approvals:
            conn.execute(
                "INSERT INTO spend_approvals VALUES (?, ?, ?, ?, ?)",
                (row.stage, row.selection_fingerprint, row.approved_count,
                 row.approved_amount, row.approved_at),
            )
        for parent_id, (value, decided_at, note) in parent_signals.items():
            conn.execute(
                "UPDATE parents SET human_worth=?, human_worth_note=?, human_worth_source=?, "
                "human_worth_at=? WHERE parent_id=?",
                (value, note, ReviewSource.REVIEW.value, decided_at or now_iso(), parent_id),
            )
        for key, (action, approved, source, at, url, pub) in human_links.items():
            conn.execute(
                "UPDATE links SET decision_action=?, decision_approved=?, decision_source=?, "
                "decided_at=?, replacement_url=?, replacement_public_identifier=? WHERE row_key=?",
                (action, approved, source, at or now_iso(), url, pub, key),
            )
        conn.execute("INSERT INTO meta (key, value) VALUES ('legacy_imported_at', ?)", (now_iso(),))
        violations = list(conn.execute("PRAGMA foreign_key_check"))
        if violations:
            raise LegacyImportError(f"legacy import left {len(violations)} orphan relations")

    return {
        "people": len(people), "parents": len(parents), "links": len(links),
        "person_sources": sum(len(values) for key, values in person_sources.items() if key in people),
        "candidate_people": sum(map(len, memberships.values())), "artifacts": len(artifacts),
        "facts": len(facts), "synthetic_profiles": len(synthetics), "research": len(research),
        "human_worth": len(parent_signals), "human_identity": len(human_links),
        "stale_memberships": stale_memberships,
        "stale_synthetic_memberships": stale_synthetic_memberships,
        "stage_state": len(stages), "spend_approvals": len(approvals),
    }
