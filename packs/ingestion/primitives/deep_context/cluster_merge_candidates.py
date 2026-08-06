#!/usr/bin/env python3
"""[4/4] Detect same-person candidates and cluster judge-confirmed pairs.

This is the stable Node and CLI. Typed loading, deterministic blocking, paid
judging, signature-cache receipts, and artifact rendering live under the
concrete ``merge_candidates`` package modules.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.indexing.lib.openai_responses import estimate_cost_usd
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import (
    DOSSIER_DIR,
    DOSSIER_TEMPLATE,
    emit,
    FACTS_DIR,
    FACTS_TEMPLATE,
    INDEX_JSON,
    MERGE_CSV,
    MERGE_MANIFEST,
    MERGE_MD,
    MERGE_VERDICTS_CSV,
    OWNER_JSON,
    RAW_BUNDLE_TEMPLATE,
    RAW_DIR,
)
from packs.ingestion.primitives.deep_context.merge_candidates.blocking import (
    deterministic_verdict,
)
from packs.ingestion.primitives.deep_context.merge_candidates.judge import (
    JUDGE_LLM,
    judge_pairs,
)
from packs.ingestion.primitives.deep_context.merge_candidates.receipts import (
    PairSurvey,
    load_cached_verdicts,
    pair_sig,
    render_results,
    survey_pairs,
)
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, StageManifest

DEFAULT_CONFIDENCE = 0.7


class ClusterMergeManifest(StageManifest):
    source: str = "cluster_merge_candidates"
    judge: str = ""
    people: int = 0
    pairs_total: int = 0
    pairs_deterministic: int = 0
    pairs_judged: int = 0
    pairs_reused: int = 0
    pairs_unsettled: int = 0
    pairs_legacy_adopted: int = 0
    candidate_pairs: int = 0
    clusters: int = 0
    confidence_threshold: float = 0.0
    tokens: dict[str, int] = {}
    estimated_cost_usd: float = 0.0
    out_csv: str = ""
    out_md: str = ""
    elapsed_ms: int = 0


class ClusterMergeCandidates(Node):
    """Run free identity gates, optional paid judging, and fixed artifact writes."""

    name = "deep_cluster"
    inputs = (
        Artifact(path=str(INDEX_JSON), required=False),
        Artifact(path=DOSSIER_TEMPLATE, required=False),
        Artifact(path=RAW_BUNDLE_TEMPLATE, required=False),
        Artifact(path=FACTS_TEMPLATE, required=False),
        Artifact(path=str(OWNER_JSON), required=False),
        Artifact(path=str(MERGE_VERDICTS_CSV), required=False),
    )
    outputs = (
        Artifact(path=str(MERGE_CSV), writes="full_rewrite"),
        Artifact(path=str(MERGE_VERDICTS_CSV), writes="full_rewrite"),
    )
    payload = ClusterMergeManifest
    manifest = str(MERGE_MANIFEST)

    def __init__(
        self,
        *,
        dossier_dir: Path | None = None,
        index_json: Path | None = None,
        raw_dir: Path | None = None,
        facts_dir: Path | None = None,
        out_csv: Path | None = None,
        out_md: Path | None = None,
        confidence: float = DEFAULT_CONFIDENCE,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = "high",
        concurrency: int = 0,
        timeout: int = 120,
        max_retries: int = 6,
        deterministic_only: bool = False,
        no_llm: bool = False,
        refresh: bool = False,
    ) -> None:
        self.dossier_dir = Path(dossier_dir or DOSSIER_DIR)
        self.index_json = Path(index_json or INDEX_JSON)
        self.raw_dir = Path(raw_dir or RAW_DIR)
        self.facts_dir = Path(facts_dir or FACTS_DIR)
        self.out_csv = Path(out_csv or MERGE_CSV)
        self.out_md = Path(out_md or MERGE_MD)
        self.verdicts_csv = self.out_csv.with_name("merge-verdicts.csv")
        self.owner_json = self.dossier_dir.parent / "owner.json"
        self.confidence = confidence
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.concurrency = concurrency
        self.timeout = timeout
        self.max_retries = max_retries
        self.deterministic_only = deterministic_only
        self.no_llm = no_llm
        self.refresh = refresh

    def bindings(self) -> dict[str, str]:
        return {
            str(INDEX_JSON): str(self.index_json),
            DOSSIER_TEMPLATE: str(self.dossier_dir / "{slug}.md"),
            RAW_BUNDLE_TEMPLATE: str(self.raw_dir / "{person_id}.json"),
            FACTS_TEMPLATE: str(self.facts_dir / "{person_id}.jsonl"),
            str(OWNER_JSON): str(self.owner_json),
            str(MERGE_VERDICTS_CSV): str(self.verdicts_csv),
            str(MERGE_CSV): str(self.out_csv),
            str(MERGE_MD): str(self.out_md),
            self.manifest: str(self.dossier_dir / "merge_manifest.json"),
        }

    def survey(self) -> PairSurvey:
        return survey_pairs(
            index_json=self.index_json,
            dossier_dir=self.dossier_dir,
            raw_dir=self.raw_dir,
            facts_dir=self.facts_dir,
            verdicts_csv=self.verdicts_csv,
            refresh=self.refresh,
        )

    def estimate(self) -> dict[str, Any]:
        started = time.monotonic()
        survey = self.survey()
        return {
            "source": "cluster_merge_candidates", "status": "dry_run",
            "people": len(survey.people), "candidate_pairs": len(survey.pairs),
            "pairs_deterministic": len(survey.slam), "cached_reused": len(survey.reused),
            "legacy_adopted": 0, "candidate_pairs_to_judge": len(survey.to_judge),
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
        verdicts: list[dict[str, Any]] = [
            {"a": left, "b": right, "sig": pair_sig(people[left], people[right]), **verdict}
            for left, right, verdict in survey.slam
        ] + [
            {"a": left, "b": right, "sig": signature, **verdict}
            for left, right, signature, verdict in survey.reused
        ]
        usage = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
        use_llm = not (self.deterministic_only or self.no_llm)
        unsettled = 0
        if self.deterministic_only:
            carry = load_cached_verdicts(self.verdicts_csv)
            for left, right, _signature in to_judge:
                prior = carry.get(frozenset({people[left].slug, people[right].slug}))
                if prior:
                    verdicts.append({"a": left, "b": right, "sig": prior[0], **prior[1]})
                else:
                    unsettled += 1
        elif use_llm and to_judge:
            judged, usage = judge_pairs(
                people,
                to_judge,
                model=self.model,
                requested_effort=self.reasoning_effort,
                requested_concurrency=self.concurrency,
                timeout=self.timeout,
                max_retries=self.max_retries,
            )
            verdicts.extend(judged)
        else:
            verdicts.extend(
                {"a": left, "b": right, "sig": signature,
                 **deterministic_verdict(people[left], people[right])}
                for left, right, signature in to_judge
            )
        confirmed, clusters = render_results(
            dossier_dir=self.dossier_dir,
            out_csv=self.out_csv,
            out_md=self.out_md,
            verdicts_csv=self.verdicts_csv,
            people=people,
            verdicts=verdicts,
            confidence=self.confidence,
        )
        billed_output = usage["output_tokens"] + usage["reasoning_tokens"]
        return ClusterMergeManifest(
            status="completed",
            judge=JUDGE_LLM if use_llm else (
                "tier0" if self.deterministic_only else "deterministic"
            ),
            people=len(people),
            pairs_total=len(survey.pairs),
            pairs_deterministic=len(survey.slam),
            pairs_judged=len(to_judge) if use_llm else 0,
            pairs_reused=len(survey.reused),
            pairs_unsettled=unsettled,
            pairs_legacy_adopted=0,
            candidate_pairs=len(confirmed),
            clusters=len(clusters),
            confidence_threshold=self.confidence,
            tokens=usage,
            estimated_cost_usd=estimate_cost_usd(usage["input_tokens"], billed_output, self.model),
            out_csv=str(self.out_csv),
            out_md=str(self.out_md),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Detect same-person / merge candidates via an LLM tone-aware judge.")
    parser.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    parser.add_argument("--index-json", default=str(INDEX_JSON))
    parser.add_argument("--raw-dir", default=str(RAW_DIR))
    parser.add_argument("--facts-dir", default=str(FACTS_DIR))
    parser.add_argument("--out-csv", default=str(MERGE_CSV))
    parser.add_argument("--out-md", default=str(MERGE_MD))
    parser.add_argument("--confidence", type=float, default=DEFAULT_CONFIDENCE, help="Min judge confidence to merge")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning-effort", default="high", choices=["minimal", "low", "medium", "high"])
    parser.add_argument("--concurrency", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-retries", type=int, default=6)
    parser.add_argument("--dry-run", action="store_true", help="Count candidate pairs + estimate cost; no spend")
    parser.add_argument("--deterministic-only", action="store_true",
                        help="Free TIER 0: merge only the code-decided pairs (identical name + shared "
                             "identifier), carry prior verdicts forward, leave the rest unjudged. No spend.")
    parser.add_argument("--refresh", action="store_true",
                        help="Ignore the cached merge-verdicts.csv and re-judge every pair from scratch")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    node = ClusterMergeCandidates(
        dossier_dir=Path(args.dossier_dir),
        index_json=Path(args.index_json),
        raw_dir=Path(args.raw_dir),
        facts_dir=Path(args.facts_dir),
        out_csv=Path(args.out_csv),
        out_md=Path(args.out_md),
        confidence=args.confidence,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        concurrency=args.concurrency,
        timeout=args.timeout,
        max_retries=args.max_retries,
        deterministic_only=args.deterministic_only,
        refresh=args.refresh,
    )
    emit(node.estimate() if args.dry_run else node.run().to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
