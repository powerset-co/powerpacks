#!/usr/bin/env python3
"""Detect and judge same-person pairs from canonical SQLite evidence.

Changelog:
- 2026-09-25: the merge cutoff is the one constant SAME_PERSON_CUTOFF; the
  --confidence override is gone (below the cutoff it could accept nothing).
- 2026-09-25: the ambiguous remainder goes to JEV (one request per pair, cached
  under deep-context/jev/); the OpenAI model, effort, timeout and retry knobs
  are gone and the dry run prices the actual requests.
"""

from __future__ import annotations

import argparse
import time
from datetime import date
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.shared.common import (
    CANONICAL_DB,
    DOSSIER_DIR,
    ROOT,
    emit,
    MERGE_CSV,
    MERGE_MANIFEST,
    MERGE_MD,
)
from packs.ingestion.primitives.deep_context.db.queries import owner_profile
from packs.ingestion.primitives.deep_context.jev_worth.runner import estimate as estimate_request
from packs.ingestion.primitives.deep_context.merge_candidates.judge import (
    JUDGE_LLM,
    SAME_PERSON_CUTOFF,
    judge_pairs,
    judge_request,
)
from packs.ingestion.primitives.deep_context.merge_candidates.models import MergeUsage, PairSurvey
from packs.ingestion.primitives.deep_context.merge_candidates.receipts import (
    render_results,
    survey_pairs,
    verdict_rows,
)
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.manifests.cluster_merge_manifest import (
    ClusterMergeManifest,
)
from packs.ingestion.primitives.pipeline.contract import Artifact, Node
from packs.search.primitives.llm_rerank_candidates.jev.client import (
    INPUT_PRICE_PER_MILLION,
    MAX_CONCURRENCY,
)
from packs.search.primitives.llm_rerank_candidates.jev.model import MODEL_ID


def _cost_usd(input_tokens: int) -> float:
    return input_tokens * INPUT_PRICE_PER_MILLION / 1_000_000


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
        output_dir: Path | None = None,
        out_csv: Path | None = None,
        out_md: Path | None = None,
        concurrency: int = MAX_CONCURRENCY,
        refresh: bool = False,
    ) -> None:
        self.db = db
        self.manifest_dir = Path(dossier_dir or DOSSIER_DIR)
        # The JEV exact-request cache lands at output_dir/jev/, shared with the worth pass.
        self.output_dir = Path(output_dir or ROOT)
        self.out_csv = Path(out_csv or MERGE_CSV)
        self.out_md = Path(out_md or MERGE_MD)
        self.concurrency = concurrency
        self.refresh = refresh

    def bindings(self) -> dict[str, str]:
        return {
            str(MERGE_CSV): str(self.out_csv),
            str(MERGE_MD): str(self.out_md),
            self.manifest: str(self.manifest_dir / "merge_manifest.json"),
        }

    def survey(self) -> PairSurvey:
        return survey_pairs(self.db, refresh=self.refresh)

    def owner_name(self) -> str:
        owner = owner_profile(self.db)
        return owner.name if owner else ""

    def estimate(self) -> dict[str, Any]:
        started = time.monotonic()
        survey = self.survey()
        owner_name = self.owner_name()
        reference_date = date.today().isoformat()
        input_tokens = sum(
            estimate_request(
                judge_request(pair.first, pair.second, owner_name=owner_name, reference_date=reference_date),
                output_dir=self.output_dir,
            ).input_tokens
            for pair in survey.to_judge
        )
        return {
            "source": "cluster_merge_candidates",
            "status": "dry_run",
            "people": len(survey.people),
            "candidate_pairs": len(survey.pairs),
            "pairs_slam_dunk": len(survey.slam),
            "cached_reused": len(survey.reused),
            "candidate_pairs_to_judge": len(survey.to_judge),
            "estimated_input_tokens": input_tokens,
            "estimated_cost_usd": _cost_usd(input_tokens),
            "model": MODEL_ID,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "updated_at": now_iso(),
        }

    def execute(self) -> ClusterMergeManifest:
        started = time.monotonic()
        survey = self.survey()
        people, to_judge = survey.people, survey.to_judge
        verdicts = survey.initial_verdicts()
        usage = MergeUsage()
        errors = 0
        if to_judge:
            judged, usage, errors = judge_pairs(
                to_judge,
                owner_name=self.owner_name(),
                output_dir=self.output_dir,
                concurrency=self.concurrency,
            )
            verdicts.extend(judged)
        confirmed, clusters = render_results(
            out_csv=self.out_csv,
            out_md=self.out_md,
            people=people,
            verdicts=verdicts,
        )
        # Preserve paid cache entries outside the current blocking survey. The
        # accepted representative edges remain one-way inputs to BuildParents.
        self.db.replace_merge_verdicts(verdict_rows(verdicts))
        return ClusterMergeManifest(
            status="completed",
            judge=JUDGE_LLM,
            model=MODEL_ID,
            people=len(people),
            pairs_total=len(survey.pairs),
            pairs_slam_dunk=len(survey.slam),
            pairs_judged=len(to_judge) - errors,
            errors=errors,
            pairs_reused=len(survey.reused),
            candidate_pairs=len(confirmed),
            clusters=len(clusters),
            confidence_threshold=SAME_PERSON_CUTOFF,
            tokens=usage.as_dict(),
            estimated_cost_usd=_cost_usd(usage.input_tokens),
            out_csv=str(self.out_csv),
            out_md=str(self.out_md),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Detect same-person / merge candidates via the JEV pair judge.")
    parser.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--out-csv", default=str(MERGE_CSV))
    parser.add_argument("--out-md", default=str(MERGE_MD))
    parser.add_argument("--concurrency", type=int, default=MAX_CONCURRENCY)
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
        concurrency=args.concurrency,
        refresh=args.refresh,
    )
    emit(node.estimate() if args.dry_run else node.run().to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
