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
import json
import time
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.shared.common import (
    CANONICAL_DB,
    ENRICH_MANIFEST,
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
from packs.ingestion.primitives.deep_context.manifests.enrichment_receipt import (
    EnrichmentReceipt,
)
from packs.ingestion.primitives.deep_context.manifests.receipt_status import ReceiptStatus
from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult

USER_DECIDED = frozenset({ApprovedState.YES.value, ApprovedState.NO.value})


class AssembleSyntheticProfile:
    """SQLite-first synthetic projection over current research eligibility."""

    def __init__(
        self,
        *,
        db: Db,
        manifest: str | Path | None = ENRICH_MANIFEST,
    ) -> None:
        self.db = db
        self.manifest_path = Path(manifest) if manifest else None

    def execute(self) -> dict[str, Any]:
        started = time.monotonic()
        counts = {
            "built": 0,
            "pending_review": 0,
            "preserved_user_rows": 0,
            "skipped_with_linkedin": 0,
            "skipped_unusable": 0,
        }
        existing: dict[str, str] = {}
        groups: dict[str, list[tuple[ResearchResult, SyntheticFallbackRow]]] = {}
        for source in synthetic_fallback(self.db):
            for item in source.existing_synthetics:
                existing[item.public_identifier] = item.approved.lower()
            result = ResearchResult.from_json(source.result_json)
            if result is None:
                continue
            if result.linkedin_url and not source.research_link_rejected:
                counts["skipped_with_linkedin"] += 1
            elif not result.usable:
                counts["skipped_unusable"] += 1
            else:
                groups.setdefault(source.parent_id, []).append((result, source))

        active_keys = tuple(sorted(groups))
        counts["pruned_stale_machine_rows"] = self.db.prune_synthetic_candidates(active_keys)
        rows: list[LinkRow | CandidatePeopleProjection | SyntheticProfileRow] = []
        for parent_id, items in sorted(groups.items()):
            if len(items) != 1:
                raise ValueError(f"parent has multiple research profiles: {parent_id}")
            result, source = items[0]
            if existing.get(parent_id) in USER_DECIDED:
                counts["preserved_user_rows"] += 1
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
                    profile_json=json.dumps(
                        result.to_payload(), ensure_ascii=False, separators=(",", ":")
                    ),
                    source_artifact_key=source.artifact_key,
                    name=result.person.full_name or source.display_name or None,
                    updated_at=updated_at,
                ),
            ))
            counts["built"] += 1
            counts["pending_review"] += 1

        self.db.project_rows(tuple(rows))
        total_rows = len(synthetic_profiles(self.db))
        summary = {
            "status": "completed",
            "primitive": "assemble_synthetic_profile",
            **counts,
            "total_rows": total_rows,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        if self.manifest_path:
            EnrichmentReceipt(self.manifest_path).write({
                "stage": "enrich",
                "status": ReceiptStatus.RUNNING,
                "phase": "profiles_pending",
                "assembly": summary,
            })
        return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--manifest", default=str(ENRICH_MANIFEST))
    args = parser.parse_args(argv)
    payload = AssembleSyntheticProfile(
        db=open_existing_db(args.db),
        manifest=args.manifest,
    ).execute()
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
