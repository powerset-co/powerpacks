"""Preview or run the resumable enrichment chain, then report the next action."""

from __future__ import annotations

import argparse
import traceback
from dataclasses import asdict
from pathlib import Path

from packs.ingestion.primitives.common.gates import exit_code_for_status
from packs.ingestion.primitives.deep_context.db.store import open_existing_db
from packs.ingestion.primitives.deep_context.db.workflow_views import enrichment_work, workflow_state
from packs.ingestion.primitives.deep_context.enrich.enrichment_pipeline import EnrichmentPipeline
from packs.ingestion.primitives.deep_context.enrich.estimate import estimate_enrichment, minutes_left
from packs.ingestion.primitives.deep_context.enrich.profiles.prefetch import PrefetchProfiles
from packs.ingestion.primitives.deep_context.merge_candidates.linkedin_name_matches import linkedin_name_matches
from packs.ingestion.primitives.deep_context.db.readiness import CANONICAL_DB
from packs.ingestion.primitives.deep_context.shared.common import ENRICH_MANIFEST, emit, load_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=CANONICAL_DB)
    parser.add_argument("--manifest", type=Path, default=ENRICH_MANIFEST)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    db = open_existing_db(args.db)
    state = workflow_state(db)
    estimate = estimate_enrichment(db, state)
    if args.dry_run:
        matches = linkedin_name_matches(db)
        profiles = PrefetchProfiles(db=db, fetch=False).run()
        emit({"status": "dry_run", **estimate.to_payload(),
            "linkedin_name_matches": len(matches.matches),
            "linkedin_parents_to_merge": sum(len(match.parent_ids) - 1 for match in matches.matches),
            "paid_estimate": "upper_bound_before_name_matches",
            "profile_fetches": profiles.estimated_rapidapi_calls,
            "estimated_minutes": minutes_left(state.progress)})
        return 0

    load_env()
    pipeline = EnrichmentPipeline(db, manifest=args.manifest)
    try:
        payload = pipeline.run(total=estimate.research.deduped_total,
            budget=estimate.research.estimated_usd, request_fingerprint=estimate.research.request_fingerprint)
    except Exception:
        # The failed step is in the manifest; the traceback is what the agent fixes from.
        traceback.print_exc()
        if pipeline.last_job is None:
            raise
        payload = pipeline.last_job
    # How many people each step left for the next run: the agent weighs this against the errors.
    payload = {
        **payload,
        "unfinished": {step: len(keys) for step, keys in asdict(enrichment_work(db)).items()},
        "next_action": workflow_state(db).next_action,
    }
    emit(payload)
    return exit_code_for_status(payload["status"])


if __name__ == "__main__":
    raise SystemExit(main())
