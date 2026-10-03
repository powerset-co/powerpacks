"""Source names can veto profile associations; compatible names never prove one."""

from __future__ import annotations

from itertools import combinations

from packs.ingestion.primitives.deep_context.db.identity_queries import research_rows
from packs.ingestion.primitives.deep_context.db.queries import source_names
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import IdentityVerdict
from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult
from packs.ingestion.primitives.deep_context.enrich.profiles.projection import profile_payloads
from packs.ingestion.primitives.deep_context.merge_candidates.candidate_pairs import name_words, names_can_match
from packs.ingestion.schemas.people_schema import normalize_linkedin_url


def profile_names(db: Db, parent_id: str, candidate_key: str, url: str) -> tuple[str, ...]:
    """Names from profile/research content for this exact proposed URL."""
    names = []
    profile = profile_payloads(db, (candidate_key,)).get(candidate_key)
    target = normalize_linkedin_url(url)
    if profile is not None and target and normalize_linkedin_url(profile.normalized_profile.linkedin_url or "") == target:
        names.append(profile.normalized_profile.full_name or "")
    for row in research_rows(db, parent_id=parent_id):
        result = ResearchResult.from_json(row.result_json)
        if result is not None and target and normalize_linkedin_url(result.linkedin_url) == target:
            names.append(result.person.full_name or "")
    return tuple(names)


def profile_name_verdict(db: Db, parent_id: str, proposed_names: tuple[str, ...]) -> IdentityVerdict | None:
    """Check every original source child, excluding model and parent names."""
    source = tuple(name_words(name) for name in source_names(db, parent_id))
    proposed = tuple(name_words(name) for name in proposed_names)
    verdict = "needs_review"
    if not source or not proposed or not all((*source, *proposed)):
        reason = "Source or proposed profile name is missing; identity requires review."
    elif any(not names_can_match(left, right) for names in (source, proposed) for left, right in combinations(names, 2)):
        reason = "Source or proposed profile names conflict; identity requires review."
    elif any(not names_can_match(left, right) for left in source for right in proposed):
        verdict = "wrong_person"
        reason = "Source names are incompatible with the proposed profile name."
    else:
        return None
    return IdentityVerdict.from_payload({"verdict": verdict, "confidence": 0.0, "reason": reason})
