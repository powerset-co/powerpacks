"""Original source and retained fact names constrain human dossier publication."""

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.db.models import FactRow
from packs.ingestion.primitives.deep_context.db.queries import facts, source_names_by_parent
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.merge_candidates.candidate_pairs import source_names_can_match
from packs.ingestion.primitives.deep_context.synthesis.models import SynthesizedFacts


SOURCE_IDENTITY_REVIEW_REASON = (
    "Source identity needs review; original contact names are missing or disagree "
    "with each other or retained facts."
)


def unresolved_source_parents(db: Db) -> set[str]:
    """Check original contacts and nonempty retained canonical names in batched reads."""
    return unresolved_source_parent_ids(source_names_by_parent(db), facts(db))


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
