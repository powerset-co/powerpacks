#!/usr/bin/env python3
"""Judge ambiguous identities once with Sol; preserve each decision in SQLite.

Changelog:
- 2026-10-03: one tri-state Sol/high call replaces binary JEV plus names checks.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.common.gates import exit_code_for_status
from packs.ingestion.primitives.deep_context.db.readiness import CANONICAL_DB
from packs.ingestion.primitives.deep_context.shared.common import (
    DOSSIER_DIR,
    emit,
    MERGE_CSV,
    MERGE_MANIFEST,
    MERGE_MD,
)
from packs.ingestion.primitives.deep_context.db.queries import owner_profile
from packs.ingestion.primitives.deep_context.merge_candidates.judge import (
    ESTIMATED_OUTPUT_TOKENS, JUDGE_LLM, MODEL_ID, REASONING_EFFORT,
    judge_pairs, judge_request,
)
from packs.ingestion.primitives.deep_context.merge_candidates.models import MergePairVerdict, PairSurvey
from packs.ingestion.primitives.deep_context.shared.openai_responses import (
    DEFAULT_OPENAI_CONCURRENCY, OpenAIResponsesConfig, OpenAIUsage, estimate_cost_usd,
)
from packs.ingestion.primitives.deep_context.merge_candidates.receipts import (
    render_results,
    survey_pairs,
    verdict_rows,
)
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.merge_candidates.linkedin_name_matches import apply_linkedin_name_matches, linkedin_name_matches
from packs.ingestion.primitives.deep_context.synthesis.normalization import normalize_parent_cache
from packs.ingestion.primitives.deep_context.manifests.cluster_merge_manifest import (
    ClusterMergeManifest,
)
from packs.ingestion.primitives.pipeline.contract import Artifact, Node


class ClusterMergeCandidates(Node):
    """Run free identity gates, paid judging, and fixed artifact writes."""

    name = "deep_cluster"
    inputs = ()
    outputs = (
        Artifact(path=str(MERGE_CSV), writes="full_rewrite"),
        Artifact(path=str(MERGE_MD), writes="full_rewrite"),
    )
    payload = ClusterMergeManifest
    manifest = str(MERGE_MANIFEST)

    def __init__(
        self,
        *,
        db: Db,
        dossier_dir: Path | None = None,
        out_csv: Path | None = None,
        out_md: Path | None = None,
        concurrency: int = DEFAULT_OPENAI_CONCURRENCY,
        refresh: bool = False,
        limit: int | None = None,
    ) -> None:
        self.db = db
        self.manifest_dir = Path(dossier_dir or DOSSIER_DIR)
        self.out_csv = Path(out_csv or MERGE_CSV)
        self.out_md = Path(out_md or MERGE_MD)
        self.config = replace(OpenAIResponsesConfig.resolve(
            model=MODEL_ID, effort=REASONING_EFFORT, concurrency=concurrency,
            timeout=120, max_retries=2,
        ), effort=REASONING_EFFORT)
        self.limit = limit
        self.refresh = refresh

    def bindings(self) -> dict[str, str]:
        return {
            str(MERGE_CSV): str(self.out_csv),
            str(MERGE_MD): str(self.out_md),
            self.manifest: str(self.manifest_dir / "merge_manifest.json"),
        }

    def survey(self) -> PairSurvey:
        return survey_pairs(self.db, refresh=self.refresh, owner_name=self.owner_name())

    def owner_name(self) -> str:
        owner = owner_profile(self.db)
        return owner.name if owner else ""

    def estimate(self) -> dict[str, Any]:
        started = time.monotonic()
        survey = self.survey()
        name_matches = linkedin_name_matches(self.db)
        matched_group = {parent_id: match.linkedin_url for match in name_matches.matches for parent_id in match.parent_ids}
        owner_name = self.owner_name()
        pending = [pair for pair in survey.to_judge if not (
            pair.first.parent_id in matched_group and matched_group[pair.first.parent_id] == matched_group.get(pair.second.parent_id))]
        chosen = pending[:self.limit] if self.limit is not None else pending
        input_tokens = sum(len(json.dumps(judge_request(pair.first, pair.second, owner_name=owner_name), ensure_ascii=False)) // 4
                           for pair in chosen)
        output_tokens = ESTIMATED_OUTPUT_TOKENS * len(chosen)
        return {
            "source": "cluster_merge_candidates",
            "status": "dry_run",
            "people": len(survey.people),
            "linkedin_name_matches": len(name_matches.matches),
            "linkedin_parents_to_merge": sum(len(match.parent_ids) - 1 for match in name_matches.matches),
            "candidate_pairs": len(survey.pairs),
            "pairs_slam_dunk": len(survey.slam),
            "cached_reused": len(survey.reused),
            "candidate_pairs_to_judge": len(chosen),
            "remaining": len(pending) - len(chosen),
            "estimated_input_tokens": input_tokens,
            "estimated_output_tokens": output_tokens,
            "estimated_cost_usd": estimate_cost_usd(input_tokens, output_tokens, MODEL_ID),
            "reasoning_effort": REASONING_EFFORT,
            "model": MODEL_ID,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "updated_at": now_iso(),
        }

    def execute(self) -> ClusterMergeManifest:
        started = time.monotonic()
        if apply_linkedin_name_matches(self.db):
            normalize_parent_cache(self.db, raw_dir=self.db.db_path.parent / 'raw', facts_dir=self.db.db_path.parent / 'facts')
        survey = self.survey()
        people = survey.people
        to_judge = survey.to_judge[:self.limit] if self.limit is not None else survey.to_judge
        verdicts = survey.initial_verdicts()
        usage = OpenAIUsage()
        self.db.replace_merge_verdicts(())

        def save(verdict: MergePairVerdict) -> None:
            self.db.project_rows(tuple(replace(row, accepted=False) for row in verdict_rows([verdict])))

        errors = 0
        if to_judge:
            judged, usage, errors = judge_pairs(
                to_judge,
                owner_name=self.owner_name(),
                config=self.config,
                on_verdict=save,
            )
            verdicts.extend(judged)
        confirmed, clusters = render_results(
            out_csv=self.out_csv,
            out_md=self.out_md,
            people=people,
            verdicts=verdicts,
            db=self.db,
        )
        # Preserve paid cache entries outside the current blocking survey. The
        # accepted representative edges remain one-way inputs to BuildParents.
        self.db.replace_merge_verdicts(verdict_rows(verdicts, db=self.db))
        return ClusterMergeManifest(
            status="failed" if errors else "incomplete" if len(to_judge) < len(survey.to_judge) else "completed",
            judge=JUDGE_LLM,
            model=MODEL_ID,
            reasoning_effort=REASONING_EFFORT,
            people=len(people),
            pairs_total=len(survey.pairs),
            pairs_slam_dunk=len(survey.slam),
            pairs_judged=len(to_judge) - errors,
            errors=errors,
            remaining=len(survey.to_judge) - len(to_judge) + errors,
            pairs_reused=len(survey.reused),
            candidate_pairs=len(confirmed),
            clusters=len(clusters),
            tokens=usage.as_dict(),
            estimated_cost_usd=estimate_cost_usd(usage.input_tokens, usage.output_tokens, MODEL_ID),
            out_csv=str(self.out_csv),
            out_md=str(self.out_md),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Detect same-person / merge candidates via one Sol/high identity judgment.")
    parser.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--out-csv", default=str(MERGE_CSV))
    parser.add_argument("--out-md", default=str(MERGE_MD))
    parser.add_argument("--concurrency", type=int, default=DEFAULT_OPENAI_CONCURRENCY)
    parser.add_argument("--limit", type=int, help="Maximum uncached pairs to judge")
    parser.add_argument("--dry-run", action="store_true", help="Count candidate pairs + estimate cost; no spend")
    parser.add_argument(
        "--refresh", action="store_true", help="Ignore cached SQLite merge verdicts and re-judge every pair"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be positive")
    db = open_existing_db(args.db)
    node = ClusterMergeCandidates(
        db=db,
        dossier_dir=Path(args.dossier_dir),
        out_csv=Path(args.out_csv),
        out_md=Path(args.out_md),
        concurrency=args.concurrency,
        refresh=args.refresh,
        limit=args.limit,
    )
    payload = node.estimate() if args.dry_run else node.run().to_payload()
    emit(payload)
    return 0 if args.dry_run else exit_code_for_status(payload["status"])


if __name__ == "__main__":
    raise SystemExit(main())
