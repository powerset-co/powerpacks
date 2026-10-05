"""Match source contacts to a unique imported LinkedIn by first name and full surname."""

from __future__ import annotations

import re
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, replace
from itertools import combinations

from packs.ingestion.primitives.common.jsonio import now_iso, parse_json_object
from packs.ingestion.primitives.deep_context.db import identity_queries, queries
from packs.ingestion.primitives.deep_context.db.models import ArtifactKind, CandidatePeopleProjection, CandidatePersonRow, HUMAN_DECISION_SOURCES, ParentRow, SourceChannel, WriterSource
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.merge_candidates.candidate_pairs import GENERATION_SUFFIXES, name_words
from packs.ingestion.primitives.deep_context.synthesis.models import SynthesizedFacts
from packs.ingestion.primitives.deep_context.shared.dossier_policy import resolved_parent_ids
from packs.ingestion.schemas.people_schema import normalize_linkedin_url
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.settlement import MachineIdentitySettlement, settle_machine_identities


_LINKEDIN_URL = re.compile(r"(?:https?://)?(?:[a-zA-Z-]+\.)?linkedin\.com/in/[^\s<>\"'\\)\],;]+", re.IGNORECASE)


@dataclass(frozen=True)
class LinkedInNameMatch:
    parent_ids: tuple[str, ...]
    connection_parent_id: str
    connection_person_id: str
    candidate_row_key: str
    linkedin_url: str
    source_names: tuple[str, ...]
    member_person_ids: tuple[str, ...]


@dataclass(frozen=True)
class WithheldNameMatch:
    parent_id: str
    reason: str


@dataclass(frozen=True)
class LinkedInNamePlan:
    matches: tuple[LinkedInNameMatch, ...]
    withheld: tuple[WithheldNameMatch, ...]
    human_resolved: tuple[str, ...]


def match_names(first: str, second: str) -> bool:
    """Exact full surname; the given name equals, is an initial, or is a spelled prefix."""
    left, right = name_words(first), name_words(second)
    if len(left) < 2 or len(right) < 2 or min(len(left[-1]), len(right[-1])) < 2:
        return False
    if GENERATION_SUFFIXES & set(left + right):
        return left == right
    if left[-1] != right[-1]:
        return False
    given, other = sorted((left[0], right[0]), key=len)
    if not other.startswith(given):
        return False
    middle, other_middle = left[1:-1], right[1:-1]
    return not middle or not other_middle or len(middle) == len(other_middle) and all(
        a == b or min(len(a), len(b)) == 1 and a[0] == b[0]
        for a, b in zip(middle, other_middle)
    )


def _urls(text: str) -> set[str]:
    return {url for value in _LINKEDIN_URL.findall(text) if (url := normalize_linkedin_url(value.rstrip('.')))}


def linkedin_name_matches(db: Db) -> LinkedInNamePlan:
    """Read-only proposals: hold a whole prospective family on ambiguity or contradictory evidence."""
    people = {row.person_id: row for row in queries.people(db) if not row.is_owner and not row.is_ghost}
    parents = {row.parent_id: row for row in queries.parents(db)}
    names = queries.source_names_by_parent(db)
    links = identity_queries.links(db)
    candidate_people = identity_queries.memberships(db)
    human_resolved = resolved_parent_ids(tuple(people.values()), tuple(row for row in links
        if row.decision_action and row.decision_source in HUMAN_DECISION_SOURCES), candidate_people)
    human_accepted = {row.parent_id for row in links if row.decision_source in HUMAN_DECISION_SOURCES
                      and row.decision_action in {'verify', 'retarget'} and row.decision_approved in {'yes', 'auto'}}
    members: dict[str, list[str]] = defaultdict(list)
    for row in people.values():
        members[row.parent_id].append(row.person_id)
    urls: dict[str, set[str]] = defaultdict(set)
    fact_names: dict[str, list[str]] = defaultdict(list)
    native: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    non_native: set[str] = set()
    for row in queries.imported_people(db):
        person = people.get(row.id)
        if person is None or person.parent_id in human_resolved:
            continue
        url = normalize_linkedin_url(row.linkedin_url)
        if SourceChannel.LINKEDIN.value in row.source_channels.split(',') and url:
            urls[person.parent_id].add(url)
            native[url].append((person.parent_id, row.id, row.full_name))
        else:
            non_native.add(person.parent_id)
    blocked = {parent_id for parent_id, row in parents.items() if row.human_worth == 'no'}
    candidate_members: dict[str, set[str]] = defaultdict(set)
    for row in candidate_people:
        candidate_members[row.row_key].add(row.person_id)
    for row in links:
        if row.decision_source in HUMAN_DECISION_SOURCES and (
                row.decision_action in {'detach', 'exclude'} or row.decision_approved == 'no'):
            blocked.add(row.parent_id)
        if row.decision_source in HUMAN_DECISION_SOURCES and row.decision_action in {'verify', 'retarget'} and row.decision_approved in {'yes', 'auto'}:
            url = row.replacement_url if row.decision_action == 'retarget' else row.linkedin_url
        else:
            continue
        if normalized := normalize_linkedin_url(url or ''):
            urls[row.parent_id].add(normalized)
    for row in queries.facts(db):
        facts = SynthesizedFacts.from_payload(parse_json_object(row.facts_json))
        if facts is not None and facts.canonical_name:
            fact_names[row.parent_id].append(facts.canonical_name)
        urls[row.parent_id].update(_urls(row.facts_json or ''))
    for row in queries.artifacts(db, kind=ArtifactKind.DOSSIER.value):
        urls[row.parent_id].update(_urls(row.payload_json or ''))
    rejected = {frozenset((people[row.person_a].parent_id, people[row.person_b].parent_id))
                for row in queries.merge_verdicts(db) if row.same_person is False
                and row.person_a in people and row.person_b in people}
    by_surname: dict[str, set[str]] = defaultdict(set)
    for url, connections in native.items():
        for _, _, name in connections:
            words = name_words(name)
            if len(words) >= 2 and min(len(words[0]), len(words[-1])) > 1:
                by_surname[words[-1]].add(url)
    proposals: dict[str, set[str]] = defaultdict(set)
    withheld = []
    for parent_id in sorted(non_native):
        source_names = names.get(parent_id, ())
        if not source_names or not all(source_names):
            continue
        words = name_words(source_names[0])
        candidates = {url for url in by_surname.get(words[-1] if words else '', ())
                      if all(match_names(name, connection_name) for name in source_names
                             for _, _, connection_name in native[url])}
        if len(candidates) > 1:
            withheld.append(WithheldNameMatch(parent_id, 'ambiguous_linkedin'))
        elif candidates:
            proposals[next(iter(candidates))].add(parent_id)
    matches = []
    for url, targets in sorted(proposals.items()):
        connections = native[url]
        parent_ids = tuple(sorted(targets | {parent for parent, _, _ in connections}))
        source_names = tuple(name for parent in parent_ids for name in names.get(parent, ()))
        reason = ''
        if set(parent_ids) & blocked:
            reason = 'human_rejection'
        elif set(parent_ids) & human_accepted:
            reason = 'human_scope_incomplete'
        elif any(pair <= set(parent_ids) for pair in rejected):
            reason = 'different_person'
        elif any(urls[parent] - {url} for parent in parent_ids):
            reason = 'conflicting_linkedin'
        elif (not source_names or not all(source_names)
              or any(not match_names(a, b) for a, b in combinations(source_names, 2))
              or any(not _fact_name_matches(name, source) for parent in parent_ids
                     for name in fact_names[parent] for source in source_names)):
            reason = 'conflicting_names'
        if reason:
            withheld.extend(WithheldNameMatch(parent, reason) for parent in sorted(targets))
            continue
        connection_parent, connection_person, _ = min(connections)
        link = next((row for row in links if row.parent_id == connection_parent
                     and row.kind == 'pub' and connection_person in candidate_members[row.row_key]
                     and normalize_linkedin_url(row.linkedin_url or '') == url), None)
        if link is None:
            continue
        if link.decision_action:
            withheld.extend(WithheldNameMatch(parent, 'human_scope_incomplete') for parent in sorted(targets))
            continue
        matches.append(LinkedInNameMatch(parent_ids, connection_parent, connection_person, link.row_key,
            url, source_names, tuple(person for parent in parent_ids for person in members[parent])))
    return LinkedInNamePlan(tuple(matches), tuple(withheld), tuple(sorted(human_resolved)))


def _fact_name_matches(fact_name: str, source_name: str) -> bool:
    words, source = name_words(fact_name), name_words(source_name)
    if len(words) == 1 and source:
        return words[0].startswith(source[0]) or source[0].startswith(words[0])
    return match_names(fact_name, source_name)


def apply_linkedin_name_matches(db: Db, plan: LinkedInNamePlan | None = None) -> int:
    """Merge source contacts, approve their imported URL, and settle machine Worth Yes."""
    plan = plan if plan is not None else linkedin_name_matches(db)
    valid_parents = {parent for match in plan.matches for parent in match.parent_ids} | set(plan.human_resolved)
    reasons = {row.parent_id: row.reason for row in plan.withheld}
    withdrawals = []
    for row in identity_queries.links(db):
        if (row.source != WriterSource.NAME_MATCH.value or row.parent_id in valid_parents
                or row.decision_action or row.machine_action not in {'verify', 'retarget', 'review'}):
            continue
        reason = 'Imported LinkedIn name match needs review: ' + reasons.get(row.parent_id, 'names no longer match')
        withdrawals.append(replace(MachineIdentitySettlement.from_link(row),
            judgment_fingerprint=hashlib.sha256((row.parent_id + reason).encode()).hexdigest(),
            judgment_payload_json=json.dumps({'verdict': 'needs_review', 'reason': reason}),
            machine_action='review', machine_approved=None, machine_confidence=None,
            machine_reason=reason, machine_judgment='needs_review',
            machine_proposed_url=None, machine_proposed_public_identifier=None))
    settle_machine_identities(db, withdrawals)
    merged = 0
    memberships: dict[str, set[str]] = defaultdict(set)
    for row in identity_queries.memberships(db):
        memberships[row.row_key].add(row.person_id)
    for match in plan.matches:
        survivor = match.connection_parent_id
        for parent_id in match.parent_ids:
            if parent_id != survivor:
                db.merge_parents(survivor, parent_id)
                merged += 1
        fingerprint = hashlib.sha256(json.dumps((match.linkedin_url, sorted(match.member_person_ids),
            sorted(match.source_names))).encode()).hexdigest()
        rows = identity_queries.links(db, parent_id=survivor)
        membership = tuple(sorted(match.member_person_ids))
        existing_members = tuple(sorted(memberships[match.candidate_row_key]))
        if existing_members != membership:
            db.project_rows((CandidatePeopleProjection(match.candidate_row_key, tuple(
                CandidatePersonRow(match.candidate_row_key, person_id, survivor) for person_id in membership)),))
        settlements = []
        for row in rows:
            if row.decision_action:
                continue
            accepted = row.row_key == match.candidate_row_key
            action = 'verify' if accepted else 'detach'
            payload = {'verdict': 'confirmed' if accepted else 'needs_review', 'confidence': 1.0 if accepted else 0.0,
                       'reason': 'Unique imported LinkedIn name match' if accepted else 'Superseded by imported LinkedIn name match'}
            settlements.append(replace(MachineIdentitySettlement.from_link(row),
                judgment_fingerprint=fingerprint, judgment_payload_json=json.dumps(payload),
                machine_action=action, machine_approved='auto', machine_confidence=payload['confidence'],
                machine_reason=payload['reason'], machine_judgment=payload['verdict'],
                machine_proposed_url=None, machine_proposed_public_identifier=None,
                source=WriterSource.NAME_MATCH.value))
        settle_machine_identities(db, settlements)
        parent = queries.parents(db, parent_id=survivor)[0]
        if parent.machine_worth != 'yes' or parent.source != WriterSource.NAME_MATCH.value:
            db.project_rows((ParentRow(parent.parent_id, parent.public_identifier, parent.display_name,
                parent.display_slug, 'yes', 'Unique imported LinkedIn name match', WriterSource.NAME_MATCH.value, now_iso()),))
    return merged
