#!/usr/bin/env python3
"""Judge attached LinkedIn identities against canonical Deep Context evidence.

This stable Node and CLI delegate queue/profile policy, canonical SQLite
projection, and stage execution to concrete ``identity_reconcile`` modules.

The ``$deep-context`` skill invokes this directly by file path for the
LinkedIn reconcile pass. A normal run stops with ``needs_approval`` until the
caller passes ``--approve-spend``; ``--dry-run`` prints the same pre-flight
estimate and ``--reapply`` replays already-paid verdicts, so neither needs
spend approval.

There is no offline confirmation mode. Profile-less tasks settle to review;
judgeable tasks require the provider.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.ingestion.primitives.common.gates import (
    exit_code_for_status,
    needs_approval_payload,
)
from packs.ingestion.primitives.deep_context.shared.common import (
    CANONICAL_DB,
    PROFILE_CACHE_DIR,
    RECONCILE_DIR,
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
from packs.ingestion.primitives.pipeline.contract import (
    STATUS_NEEDS_APPROVAL,
    Node,
)

DEFAULT_CONFIRM, DEFAULT_DETACH = JUDGE_CONFIRM_THRESHOLD, JUDGE_DETACH_THRESHOLD


class ReconcileLinkedin(Node):
    """Run the SQLite-selected attached-link judge."""

    name = "deep_reconcile"
    inputs = ()
    outputs = ()
    payload = ReconcileLinkedinManifest
    manifest = str(RECONCILE_DIR / "manifest.json")

    def __init__(
        self,
        *,
        db: Db,
        profile_cache_dir: Path | None = None,
        out_dir: Path | None = None,
        confirm_threshold: float = DEFAULT_CONFIRM,
        detach_threshold: float = DEFAULT_DETACH,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = "high",
        concurrency: int | None = None,
        timeout: int = 120,
        max_retries: int = 6,
        reapply: bool = False,
        force: bool = False,
        approve_spend: bool = False,
    ) -> None:
        self.db = db
        self.profile_cache_dir = Path(profile_cache_dir or PROFILE_CACHE_DIR)
        self.out_dir = Path(out_dir or RECONCILE_DIR)
        self.confirm_threshold = confirm_threshold
        self.detach_threshold = detach_threshold
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.concurrency = concurrency
        self.timeout = timeout
        self.max_retries = max_retries
        self.reapply = reapply
        self.force = force
        self.approve_spend = approve_spend

    def bindings(self) -> dict[str, str]:
        return {self.manifest: str(self.out_dir / "manifest.json")}

    # Node.run() (called from main() below) wraps this: on success it verifies
    # declared outputs and writes the typed manifest; on an exception here it
    # writes a Failed manifest and RE-RAISES (see pipeline/contract.Node docs).
    def execute(self) -> ReconcileLinkedinManifest:
        if not self.reapply and not self.approve_spend:
            estimate = dry_run_estimate(
                db=self.db,
                model=self.model,
                effort=self.reasoning_effort,
                force=self.force,
            )
            profile_fetches = estimate.profile_fetch_misses
            known_judgments = estimate.billed
            possible_judgments = known_judgments + profile_fetches
            estimated_calls = profile_fetches + possible_judgments
            if estimated_calls:
                return ReconcileLinkedinManifest(
                    status=STATUS_NEEDS_APPROVAL,
                    parents=estimate.parents,
                    tasks=estimate.tasks,
                    reused=estimate.reused,
                    human_settled=estimate.human_settled,
                    ground_truth_connections=estimate.ground_truth_connections,
                    conflicts=estimate.conflicts,
                    needs_approval=needs_approval_payload(
                        step="reconcile_linkedin",
                        provider="RapidAPI and OpenAI",
                        estimated_calls=estimated_calls,
                        message=(
                            "Approve up to "
                            f"{profile_fetches} LinkedIn profile fetches and "
                            f"{possible_judgments} identity judgments."
                        ),
                    ),
                )
        return run_stage(
            ReconcileLinkedinManifest,
            db=self.db, profile_cache_dir=self.profile_cache_dir,
            confirm_threshold=self.confirm_threshold, detach_threshold=self.detach_threshold,
            model=self.model, requested_effort=self.reasoning_effort,
            concurrency=self.concurrency, timeout=self.timeout, max_retries=self.max_retries,
            reapply=self.reapply, force=self.force,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Judge attached LinkedIn profiles against Deep Context evidence",
    )
    paths = {
        "profile-cache-dir": PROFILE_CACHE_DIR,
        "out-dir": RECONCILE_DIR,
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
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--approve-spend", action="store_true")
    parser.add_argument("--reapply", action="store_true")
    parser.add_argument(
        "--force", action="store_true",
        help="Re-judge every task, ignoring verdicts already bought for the same input",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = open_existing_db(args.db)
    # --dry-run only short-circuits when NOT --reapply: reapply already never
    # spends (it replays stored verdicts through the threshold policy), so
    # there's nothing to estimate and it falls through to run_stage below.
    if args.dry_run and not args.reapply:
        emit(
            dry_run_estimate(
                db=db,
                model=args.model,
                effort=args.reasoning_effort,
                force=args.force,
            ).to_payload()
        )
        return 0
    payload = ReconcileLinkedin(
        db=db,
        profile_cache_dir=Path(args.profile_cache_dir),
        out_dir=Path(args.out_dir),
        confirm_threshold=args.confirm_threshold, detach_threshold=args.detach_threshold,
        model=args.model, reasoning_effort=args.reasoning_effort,
        concurrency=args.concurrency, timeout=args.timeout, max_retries=args.max_retries,
        reapply=args.reapply,
        force=args.force, approve_spend=args.approve_spend,
    ).run()
    emit(payload.to_payload())
    return exit_code_for_status(payload.status)


if __name__ == "__main__":
    sys.exit(main())
