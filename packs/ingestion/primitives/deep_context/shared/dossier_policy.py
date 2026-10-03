"""Original source and retained fact names constrain human dossier publication."""

from packs.ingestion.primitives.common.jsonio import parse_json_object
from collections import defaultdict
from collections.abc import Sequence

from packs.ingestion.primitives.deep_context.db.identity_queries import links, memberships
from packs.ingestion.primitives.deep_context.db.models import CandidatePersonRow, FactRow, HUMAN_DECISION_SOURCES, LinkSnapshotRow, PersonRow, WriterSource
from packs.ingestion.primitives.deep_context.db.queries import facts, people, source_names_by_parent
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.merge_candidates.candidate_pairs import source_names_can_match
from packs.ingestion.primitives.deep_context.synthesis.models import SynthesizedFacts
from packs.ingestion.schemas.people_schema import normalize_linkedin_url


SOURCE_IDENTITY_REVIEW_REASON = (
    "Source identity needs review; original contact names are missing or disagree "
    "with each other or retained facts."
)


def unresolved_source_parents(db: Db) -> set[str]:
    """Check original contacts and nonempty retained canonical names in batched reads."""
    rows = links(db)
    resolved = resolved_parent_ids(people(db), rows, memberships(db))
    return (unresolved_source_parent_ids(source_names_by_parent(db), facts(db)) - resolved
            | name_match_review_parents(db))


def name_match_review_parents(db: Db) -> set[str]:
    """Withhold a withdrawn automatic name match until a human settles its identity."""
    rows = links(db)
    return {row.parent_id for row in rows if row.source == WriterSource.NAME_MATCH.value
            and row.machine_action == 'review' and not row.decision_action} - resolved_parent_ids(
                people(db), tuple(row for row in rows if row.decision_action
                    and row.decision_source in HUMAN_DECISION_SOURCES), memberships(db))


def resolved_parent_ids(
    person_rows: Sequence[PersonRow], link_rows: Sequence[LinkSnapshotRow],
    candidate_members: Sequence[CandidatePersonRow],
) -> set[str]:
    """Release a parent when human or final relationship acceptance covers every child at one URL."""
    accepted: dict[str, str] = {}
    for row in link_rows:
        if row.decision_action:
            if (row.decision_source not in HUMAN_DECISION_SOURCES
                    or row.decision_action not in {'verify', 'retarget'}
                    or row.decision_approved not in {'yes', 'auto'}):
                continue
            url = row.replacement_url if row.decision_action == 'retarget' else row.linkedin_url
        else:
            if (row.machine_action not in {'verify', 'retarget'}
                    or row.machine_approved not in {'yes', 'auto'}
                    or parse_json_object(row.judgment_payload_json).get('relationship_decision', {}).get('fingerprint')
                        != (row.judgment_fingerprint or '')):
                continue
            url = row.machine_proposed_url if row.machine_action == 'retarget' else row.linkedin_url
        accepted[row.row_key] = normalize_linkedin_url(url or '')
    covered: dict[str, set[str]] = defaultdict(set)
    for row in candidate_members:
        if url := accepted.get(row.row_key):
            covered[row.person_id].add(url)
    members: dict[str, set[str]] = defaultdict(set)
    for row in person_rows:
        if not row.is_owner and not row.is_ghost:
            members[row.parent_id].add(row.person_id)
    return {parent_id for parent_id, children in members.items()
            if all(covered[child] for child in children)
            and len({url for child in children for url in covered[child]}) == 1}


def unresolved_source_parent_ids(
    source_names: dict[str, tuple[str, ...]], retained_facts: tuple[FactRow, ...],
) -> set[str]:
    """The same publication policy for typed read-only audit snapshots."""
    names = {parent_id: list(values) for parent_id, values in source_names.items()}
    for row in retained_facts:
        parsed = SynthesizedFacts.from_payload(parse_json_object(row.facts_json))
        if parsed is not None and parsed.canonical_name.strip():
            names[row.parent_id].append(parsed.canonical_name)
    return {parent_id for parent_id, values in names.items()
            if not source_names_can_match(tuple(values))}
