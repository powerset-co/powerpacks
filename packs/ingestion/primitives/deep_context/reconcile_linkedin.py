#!/usr/bin/env python3
"""Judge attached LinkedIn identities against canonical Deep Context evidence.

This stable Node and CLI delegate queue/profile policy, file-first result
projection, and stage execution to concrete ``identity_reconcile`` modules.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.ingestion.primitives.deep_context.common import (
    CONSOLIDATE_PEOPLE_CSV,
    DEFAULT_PEOPLE_CSV,
    FACTS_DIR,
    FACTS_TEMPLATE,
    INDEX_JSON,
    LINKEDIN_OVERRIDES_CSV,
    OWNER_JSON,
    PARENTS_DIR,
    PROFILE_CACHE_DIR,
    PROFILE_CACHE_TEMPLATE,
    RAW_BUNDLE_TEMPLATE,
    RAW_DIR,
    RECONCILE_DIR,
    ROOT,
    VERDICTS_CSV,
    VERDICTS_JSONL,
    emit,
)
from packs.ingestion.primitives.deep_context.db.models import (
    JUDGE_CONFIRM_THRESHOLD,
    JUDGE_DETACH_THRESHOLD,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.identity_reconcile.queue import dry_run_estimate
from packs.ingestion.primitives.deep_context.identity_reconcile.runner import run_stage
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, StageManifest

DEFAULT_CONFIRM, DEFAULT_DETACH = JUDGE_CONFIRM_THRESHOLD, JUDGE_DETACH_THRESHOLD
CANONICAL_DB = ROOT / "deep-context.sqlite"


class ReconcileLinkedinManifest(StageManifest):
    source: str = "reconcile_linkedin"
    judge: str = ""
    parents: int = 0
    tasks: int = 0
    judged: int = 0
    ground_truth_connections: int = 0
    self_reported_retargets: int = 0
    name_match_reviews: int = 0
    verdicts: dict[str, int] = {}
    conflicts: int = 0
    conflicts_auto_resolved: int = 0
    conflicts_to_review: int = 0
    profile_fetch: dict[str, int] | None = None
    no_link: int = 0
    errors: int = 0
    overrides: dict[str, Any] = {}
    consolidation: dict[str, Any] = {}
    summary_md: str = ""
    applied_csv: str = ""
    needs_review: int = 0
    deep_research_eligible: int = 0
    deep_research_est_usd: float = 0.0
    tokens: dict[str, int] = {}
    estimated_cost_usd: float = 0.0
    elapsed_ms: int = 0


class ReconcileLinkedin(Node):
    """Run the SQLite-selected attached-link judge and fixed verdict artifact."""

    name = "deep_reconcile"
    inputs = (
        Artifact(path=FACTS_TEMPLATE, required=False),
        Artifact(path=RAW_BUNDLE_TEMPLATE, required=False),
        Artifact(path=PROFILE_CACHE_TEMPLATE, external=True, required=False),
        Artifact(path=str(OWNER_JSON), required=False),
    )
    outputs = (Artifact(path=str(VERDICTS_JSONL), writes="full_rewrite"),)
    payload = ReconcileLinkedinManifest
    manifest = str(RECONCILE_DIR / "manifest.json")

    def __init__(
        self,
        *,
        db: Db,
        index_json: Path | None = None,
        people_csv: Path | None = None,
        profile_cache_dir: Path | None = None,
        facts_dir: Path | None = None,
        raw_dir: Path | None = None,
        parents_dir: Path | None = None,
        verdicts_jsonl: Path | None = None,
        verdicts_csv: Path | None = None,
        confirm_threshold: float = DEFAULT_CONFIRM,
        detach_threshold: float = DEFAULT_DETACH,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = "high",
        concurrency: int = 0,
        timeout: int = 120,
        max_retries: int = 6,
        overrides_csv: Path | None = None,
        consolidate_people_csv: Path | None = None,
        slug: list[str] | None = None,
        limit: int = 0,
        no_overrides: bool = False,
        no_llm: bool = False,
        reapply: bool = False,
    ) -> None:
        self.db = db
        self.profile_cache_dir = Path(profile_cache_dir or PROFILE_CACHE_DIR)
        self.facts_dir = Path(facts_dir or FACTS_DIR)
        self.raw_dir = Path(raw_dir or RAW_DIR)
        self.verdicts_jsonl = Path(verdicts_jsonl or VERDICTS_JSONL)
        self.confirm_threshold = confirm_threshold
        self.detach_threshold = detach_threshold
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.concurrency = concurrency
        self.timeout = timeout
        self.max_retries = max_retries
        self.slug = list(slug or [])
        self.limit = limit
        self.no_overrides = no_overrides
        self.no_llm = no_llm
        self.reapply = reapply

    def bindings(self) -> dict[str, str]:
        return {
            FACTS_TEMPLATE: str(self.facts_dir / "{person_id}.jsonl"),
            RAW_BUNDLE_TEMPLATE: str(self.raw_dir / "{person_id}.json"),
            PROFILE_CACHE_TEMPLATE: str(self.profile_cache_dir / "{public_identifier}.json"),
            str(VERDICTS_JSONL): str(self.verdicts_jsonl),
            self.manifest: str(self.verdicts_jsonl.parent / "manifest.json"),
        }

    def execute(self) -> ReconcileLinkedinManifest:
        return run_stage(
            ReconcileLinkedinManifest,
            db=self.db, facts_dir=self.facts_dir, raw_dir=self.raw_dir,
            profile_cache_dir=self.profile_cache_dir, verdicts_jsonl=self.verdicts_jsonl,
            confirm_threshold=self.confirm_threshold, detach_threshold=self.detach_threshold,
            model=self.model, requested_effort=self.reasoning_effort,
            concurrency=self.concurrency, timeout=self.timeout, max_retries=self.max_retries,
            slugs=self.slug, limit=self.limit, no_overrides=self.no_overrides,
            no_llm=self.no_llm, reapply=self.reapply,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Judge attached LinkedIn profiles against Deep Context evidence",
    )
    paths = {
        "index-json": INDEX_JSON, "people-csv": DEFAULT_PEOPLE_CSV,
        "profile-cache-dir": PROFILE_CACHE_DIR, "facts-dir": FACTS_DIR,
        "raw-dir": RAW_DIR, "parents-dir": PARENTS_DIR,
        "verdicts-jsonl": VERDICTS_JSONL, "verdicts-csv": VERDICTS_CSV,
        "overrides-csv": LINKEDIN_OVERRIDES_CSV, "db": CANONICAL_DB,
        "consolidate-people-csv": CONSOLIDATE_PEOPLE_CSV,
    }
    for flag, default in paths.items():
        parser.add_argument(f"--{flag}", default=str(default))
    parser.add_argument("--confirm-threshold", type=float, default=DEFAULT_CONFIRM)
    parser.add_argument("--detach-threshold", type=float, default=DEFAULT_DETACH)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--reasoning-effort", default="high", choices=["minimal", "low", "medium", "high"],
    )
    for flag, default in (("concurrency", 0), ("timeout", 120), ("max-retries", 6)):
        parser.add_argument(f"--{flag}", type=int, default=default)
    parser.add_argument("--slug", action="append", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-overrides", action="store_true")
    parser.add_argument("--reapply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = Db(Path(args.db))
    if args.dry_run and not args.reapply:
        emit(dry_run_estimate(
            db=db, profile_cache_dir=Path(args.profile_cache_dir),
            facts_dir=Path(args.facts_dir), raw_dir=Path(args.raw_dir),
            model=args.model, effort=args.reasoning_effort,
            slug=args.slug, limit=args.limit,
        ))
        return 0
    payload = ReconcileLinkedin(
        db=db, index_json=Path(args.index_json), people_csv=Path(args.people_csv),
        profile_cache_dir=Path(args.profile_cache_dir), facts_dir=Path(args.facts_dir),
        raw_dir=Path(args.raw_dir), parents_dir=Path(args.parents_dir),
        verdicts_jsonl=Path(args.verdicts_jsonl), verdicts_csv=Path(args.verdicts_csv),
        confirm_threshold=args.confirm_threshold, detach_threshold=args.detach_threshold,
        model=args.model, reasoning_effort=args.reasoning_effort,
        concurrency=args.concurrency, timeout=args.timeout, max_retries=args.max_retries,
        overrides_csv=Path(args.overrides_csv),
        consolidate_people_csv=Path(args.consolidate_people_csv),
        slug=args.slug, limit=args.limit, no_overrides=args.no_overrides, reapply=args.reapply,
    ).run()
    emit(payload.to_payload())
    return 0


if __name__ == "__main__":
    sys.exit(main())
