"""Project one pending synthetic identity for each usable no-LinkedIn result.

Flow::

    SQLite research + worth -> select fallback -> prune stale machine rows
    -> project candidate membership + native Parallel result into SQLite

The Parallel result remains in its provider-owned native shape. A synthetic is
only an identity-review marker; it never invents confidence or approval and it
does not create a CSV or a second per-person JSON artifact.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.shared.common import (
    CANONICAL_DB,
    emit,
)
from packs.ingestion.primitives.deep_context.db.identity_views import synthetic_fallback
from packs.ingestion.primitives.deep_context.db.identity_queries import synthetic_profiles
from packs.ingestion.primitives.deep_context.db.models import (
    ApprovedState,
    CandidatePeopleProjection,
    CandidatePersonRow,
    LinkRow,
    RowKind,
    SyntheticProfileRow,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.db.view_models import SyntheticFallbackRow
from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult

USER_DECIDED = frozenset({ApprovedState.YES.value, ApprovedState.NO.value})


@dataclass(frozen=True)
class SyntheticAssemblyCounts:
    built: int
    pending_review: int
    preserved_user_rows: int
    skipped_with_linkedin: int
    skipped_unusable: int
    pruned_stale_machine_rows: int
    total_rows: int


@dataclass(frozen=True)
class SyntheticAssemblyResult:
    status: str
    counts: SyntheticAssemblyCounts
    duration_seconds: float

    def to_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "primitive": "assemble_synthetic_profile",
            "built": self.counts.built,
            "pending_review": self.counts.pending_review,
            "preserved_user_rows": self.counts.preserved_user_rows,
            "skipped_with_linkedin": self.counts.skipped_with_linkedin,
            "skipped_unusable": self.counts.skipped_unusable,
            "pruned_stale_machine_rows": self.counts.pruned_stale_machine_rows,
            "total_rows": self.counts.total_rows,
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True)
class AssembleSyntheticProfile:
    """SQLite-first synthetic projection over current research eligibility."""

    db: Db

    def run(self) -> SyntheticAssemblyResult:
        started = time.monotonic()
        built = 0
        preserved_user_rows = 0
        skipped_with_linkedin = 0
        skipped_unusable = 0
        groups: dict[str, list[tuple[ResearchResult, SyntheticFallbackRow]]] = {}
        for source in synthetic_fallback(self.db):
            result = ResearchResult.from_json(source.result_json)
            if result is None:
                continue
            if result.linkedin_url and not source.research_link_rejected:
                skipped_with_linkedin += 1
            elif not result.usable:
                skipped_unusable += 1
            else:
                groups.setdefault(source.parent_id, []).append((result, source))

        active_keys = tuple(sorted(groups))
        pruned_stale_machine_rows = self.db.prune_synthetic_candidates(active_keys)
        rows: list[LinkRow | CandidatePeopleProjection | SyntheticProfileRow] = []
        for parent_id, items in sorted(groups.items()):
            if len(items) != 1:
                raise ValueError(f"parent has multiple research profiles: {parent_id}")
            result, source = items[0]
            if source.existing_approved.lower() in USER_DECIDED:
                preserved_user_rows += 1
                continue

            member_ids = tuple(sorted({
                str(person_id).strip().lower()
                for person_id in source.person_ids
                if str(person_id).strip()
            }))
            updated_at = now_iso()
            rows.extend((
                LinkRow(
                    row_key=parent_id,
                    parent_id=parent_id,
                    public_identifier=parent_id,
                    kind=RowKind.SYNTHETIC.value,
                    display_name=result.person.full_name or source.display_name or None,
                    source=WriterSource.DEEP_RESEARCH.value,
                    updated_at=updated_at,
                ),
                CandidatePeopleProjection(
                    parent_id,
                    tuple(
                        CandidatePersonRow(parent_id, person_id, parent_id)
                        for person_id in member_ids
                    ),
                ),
                SyntheticProfileRow(
                    public_identifier=parent_id,
                    candidate_key=parent_id,
                    profile_json=result.output.model_dump_json(exclude_none=True),
                    source_artifact_key=source.artifact_key,
                    name=result.person.full_name or source.display_name or None,
                    updated_at=updated_at,
                ),
            ))
            built += 1

        self.db.project_rows(tuple(rows))
        return SyntheticAssemblyResult(
            status="completed",
            counts=SyntheticAssemblyCounts(
                built=built,
                pending_review=built,
                preserved_user_rows=preserved_user_rows,
                skipped_with_linkedin=skipped_with_linkedin,
                skipped_unusable=skipped_unusable,
                pruned_stale_machine_rows=pruned_stale_machine_rows,
                total_rows=len(synthetic_profiles(self.db)),
            ),
            duration_seconds=round(time.monotonic() - started, 2),
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(CANONICAL_DB))
    args = parser.parse_args(argv)
    result = AssembleSyntheticProfile(db=open_existing_db(args.db)).run()
    emit(result.to_payload())


if __name__ == "__main__":
    main()
