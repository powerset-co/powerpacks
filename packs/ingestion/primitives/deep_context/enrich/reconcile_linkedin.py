#!/usr/bin/env python3
"""Judge attached LinkedIn identities against canonical Deep Context evidence.

This stable Node and CLI delegate queue/profile policy, file-first result
projection, and stage execution to concrete ``identity_reconcile`` modules.

The ``$deep-context`` skill invokes this directly by file path for the
LinkedIn reconcile pass. Unlike ``reconcile_deep_research.py``'s
``--approve``/``--budget`` gate, there is no in-primitive spend gate here: a
real run bills a RapidAPI profile fetch per cache miss plus one OpenAI judge
call per judgeable task, by default. The skill discloses this cost and treats
invocation itself as consent. ``--dry-run`` (without ``--reapply``) prints a
pre-flight estimate and spends nothing; ``--no-llm`` skips both the paid judge
and the RapidAPI profile fetch that feeds it, settling every task through the
deterministic policy instead; ``--reapply`` replays already-paid verdicts
through the threshold policy and never spends.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.ingestion.primitives.deep_context.shared.common import (
    CANONICAL_DB,
    PROFILE_CACHE_DIR,
    RECONCILE_DIR,
    VERDICTS_JSONL,
    emit,
)
from packs.ingestion.primitives.deep_context.db.models import (
    JUDGE_CONFIRM_THRESHOLD,
    JUDGE_DETACH_THRESHOLD,
)
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.queue import dry_run_estimate
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.runner import run_stage
from packs.ingestion.primitives.deep_context.manifests.reconcile_linkedin_manifest import (
    ReconcileLinkedinManifest,
)
from packs.ingestion.primitives.pipeline.contract import Artifact, Node

DEFAULT_CONFIRM, DEFAULT_DETACH = JUDGE_CONFIRM_THRESHOLD, JUDGE_DETACH_THRESHOLD


class ReconcileLinkedin(Node):
    """Run the SQLite-selected attached-link judge and fixed verdict artifact."""

    name = "deep_reconcile"
    inputs = ()
    outputs = (Artifact(path=str(VERDICTS_JSONL), writes="full_rewrite"),)
    payload = ReconcileLinkedinManifest
    manifest = str(RECONCILE_DIR / "manifest.json")

    def __init__(
        self,
        *,
        db: Db,
        profile_cache_dir: Path | None = None,
        verdicts_jsonl: Path | None = None,
        confirm_threshold: float = DEFAULT_CONFIRM,
        detach_threshold: float = DEFAULT_DETACH,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = "high",
        concurrency: int | None = None,
        timeout: int = 120,
        max_retries: int = 6,
        limit: int | None = None,
        no_overrides: bool = False,
        no_llm: bool = False,
        reapply: bool = False,
    ) -> None:
        self.db = db
        self.profile_cache_dir = Path(profile_cache_dir or PROFILE_CACHE_DIR)
        self.verdicts_jsonl = Path(verdicts_jsonl or VERDICTS_JSONL)
        self.confirm_threshold = confirm_threshold
        self.detach_threshold = detach_threshold
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.concurrency = concurrency
        self.timeout = timeout
        self.max_retries = max_retries
        self.limit = limit
        self.no_overrides = no_overrides
        self.no_llm = no_llm
        self.reapply = reapply

    def bindings(self) -> dict[str, str]:
        return {
            str(VERDICTS_JSONL): str(self.verdicts_jsonl),
            self.manifest: str(self.verdicts_jsonl.parent / "manifest.json"),
        }

    # Node.run() (called from main() below) wraps this: on success it verifies
    # declared outputs and writes the typed manifest; on an exception here it
    # writes a Failed manifest and RE-RAISES (see pipeline/contract.Node docs).
    def execute(self) -> ReconcileLinkedinManifest:
        return run_stage(
            ReconcileLinkedinManifest,
            db=self.db, profile_cache_dir=self.profile_cache_dir,
            verdicts_jsonl=self.verdicts_jsonl,
            confirm_threshold=self.confirm_threshold, detach_threshold=self.detach_threshold,
            model=self.model, requested_effort=self.reasoning_effort,
            concurrency=self.concurrency, timeout=self.timeout, max_retries=self.max_retries,
            # No CLI/caller ever scopes a ReconcileLinkedin run to a slug
            # subset — run_stage's slugs param exists for callers that do.
            slugs=[], limit=self.limit, no_overrides=self.no_overrides,
            no_llm=self.no_llm, reapply=self.reapply,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Judge attached LinkedIn profiles against Deep Context evidence",
    )
    paths = {
        "profile-cache-dir": PROFILE_CACHE_DIR,
        "verdicts-jsonl": VERDICTS_JSONL,
        "db": CANONICAL_DB,
    }
    for flag, default in paths.items():
        parser.add_argument(f"--{flag}", default=str(default))
    parser.add_argument("--confirm-threshold", type=float, default=DEFAULT_CONFIRM)
    parser.add_argument("--detach-threshold", type=float, default=DEFAULT_DETACH)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--reasoning-effort", default="high", choices=["minimal", "low", "medium", "high"],
    )
    for flag, default in (("concurrency", None), ("timeout", 120), ("max-retries", 6)):
        parser.add_argument(f"--{flag}", type=int, default=default)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-overrides", action="store_true")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--reapply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = open_existing_db(args.db)
    # --dry-run only short-circuits when NOT --reapply: reapply already never
    # spends (it replays stored verdicts through the threshold policy), so
    # there's nothing to estimate and it falls through to run_stage below.
    if args.dry_run and not args.reapply:
        emit(dry_run_estimate(
            db=db, model=args.model, effort=args.reasoning_effort,
            slug=None, limit=args.limit,
        ))
        return 0
    payload = ReconcileLinkedin(
        db=db,
        profile_cache_dir=Path(args.profile_cache_dir),
        verdicts_jsonl=Path(args.verdicts_jsonl),
        confirm_threshold=args.confirm_threshold, detach_threshold=args.detach_threshold,
        model=args.model, reasoning_effort=args.reasoning_effort,
        concurrency=args.concurrency, timeout=args.timeout, max_retries=args.max_retries,
        limit=args.limit, no_overrides=args.no_overrides, no_llm=args.no_llm, reapply=args.reapply,
    ).run()
    emit(payload.to_payload())
    # Always 0 on a normal completion — run_stage's manifest.status is always
    # "completed" (there is no needs_approval branch to report here). A failure
    # instead surfaces as an uncaught exception from Node.run() above,
    # propagating past sys.exit(main()) to a nonzero process exit.
    return 0


if __name__ == "__main__":
    sys.exit(main())
