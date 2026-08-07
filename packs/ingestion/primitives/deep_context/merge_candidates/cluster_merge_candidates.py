#!/usr/bin/env python3
"""Detect and judge same-person pairs from canonical SQLite evidence."""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.shared.common import (
    CANONICAL_DB,
    DOSSIER_DIR,
    emit,
    MERGE_CSV,
    MERGE_MANIFEST,
    MERGE_MD,
)
from packs.ingestion.primitives.deep_context.merge_candidates.judge import (
    JUDGE_LLM,
    judge_pairs,
)
from packs.ingestion.primitives.deep_context.merge_candidates.models import MergeUsage, PairSurvey
from packs.ingestion.primitives.deep_context.merge_candidates.receipts import (
    render_results,
    survey_pairs,
    verdict_rows,
)
from packs.ingestion.primitives.deep_context.shared.openai_responses import (
    estimate_cost_usd,
)
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.manifests.cluster_merge_manifest import (
    ClusterMergeManifest,
)
from packs.ingestion.primitives.pipeline.contract import Artifact, Node

DEFAULT_CONFIDENCE = 0.7


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
        confidence: float = DEFAULT_CONFIDENCE,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = "high",
        concurrency: int | None = None,
        timeout: int = 120,
        max_retries: int = 6,
        refresh: bool = False,
    ) -> None:
        self.db = db
        self.manifest_dir = Path(dossier_dir or DOSSIER_DIR)
        self.out_csv = Path(out_csv or MERGE_CSV)
        self.out_md = Path(out_md or MERGE_MD)
        self.confidence = confidence
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.concurrency = concurrency
        self.timeout = timeout
        self.max_retries = max_retries
        self.refresh = refresh

    def bindings(self) -> dict[str, str]:
        return {
            str(MERGE_CSV): str(self.out_csv),
            str(MERGE_MD): str(self.out_md),
            self.manifest: str(self.manifest_dir / "merge_manifest.json"),
        }

    def survey(self) -> PairSurvey:
        return survey_pairs(self.db, refresh=self.refresh)

    def estimate(self) -> dict[str, Any]:
        started = time.monotonic()
        survey = self.survey()
        return {
            "source": "cluster_merge_candidates",
            "status": "dry_run",
            "people": len(survey.people),
            "candidate_pairs": len(survey.pairs),
            "pairs_slam_dunk": len(survey.slam),
            "cached_reused": len(survey.reused),
            "pairs_unsettled": len(survey.shared_unsettled),
            "candidate_pairs_to_judge": len(survey.to_judge),
            "estimated_cost_usd_low": round(len(survey.to_judge) * 0.004, 2),
            "estimated_cost_usd_high": round(len(survey.to_judge) * 0.02, 2),
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "updated_at": now_iso(),
        }

    def execute(self) -> ClusterMergeManifest:
        started = time.monotonic()
        survey = self.survey()
        people, to_judge = survey.people, survey.to_judge
        verdicts = survey.initial_verdicts()
        usage = MergeUsage()
        unsettled = len(survey.shared_unsettled)
        if to_judge:
            judged, usage = judge_pairs(
                to_judge,
                model=self.model,
                requested_effort=self.reasoning_effort,
                requested_concurrency=self.concurrency,
                timeout=self.timeout,
                max_retries=self.max_retries,
            )
            verdicts.extend(judged)
        confirmed, clusters = render_results(
            out_csv=self.out_csv,
            out_md=self.out_md,
            people=people,
            verdicts=verdicts,
            confidence=self.confidence,
        )
        # Preserve paid cache entries outside the current blocking survey. The
        # accepted representative edges remain one-way inputs to BuildParents.
        self.db.replace_merge_verdicts(verdict_rows(verdicts, self.confidence))
        billed_output = usage.output_tokens + usage.reasoning_tokens
        return ClusterMergeManifest(
            status="completed",
            judge=JUDGE_LLM,
            people=len(people),
            pairs_total=len(survey.pairs),
            pairs_slam_dunk=len(survey.slam),
            pairs_judged=len(to_judge),
            pairs_reused=len(survey.reused),
            pairs_unsettled=unsettled,
            candidate_pairs=len(confirmed),
            clusters=len(clusters),
            confidence_threshold=self.confidence,
            tokens=usage.as_dict(),
            estimated_cost_usd=estimate_cost_usd(
                usage.input_tokens,
                billed_output,
                self.model,
            ),
            out_csv=str(self.out_csv),
            out_md=str(self.out_md),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Detect same-person / merge candidates via an LLM tone-aware judge.")
    parser.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--out-csv", default=str(MERGE_CSV))
    parser.add_argument("--out-md", default=str(MERGE_MD))
    parser.add_argument("--confidence", type=float, default=DEFAULT_CONFIDENCE, help="Min judge confidence to merge")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning-effort", default="high", choices=["minimal", "low", "medium", "high"])
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-retries", type=int, default=6)
    parser.add_argument("--dry-run", action="store_true", help="Count candidate pairs + estimate cost; no spend")
    parser.add_argument(
        "--refresh", action="store_true", help="Ignore cached SQLite merge verdicts and re-judge every pair"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = open_existing_db(args.db)
    node = ClusterMergeCandidates(
        db=db,
        dossier_dir=Path(args.dossier_dir),
        out_csv=Path(args.out_csv),
        out_md=Path(args.out_md),
        confidence=args.confidence,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        concurrency=args.concurrency,
        timeout=args.timeout,
        max_retries=args.max_retries,
        refresh=args.refresh,
    )
    emit(node.estimate() if args.dry_run else node.run().to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
