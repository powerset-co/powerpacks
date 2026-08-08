#!/usr/bin/env python3
"""[2/4] Synthesize structured facts from each canonical parent's message bundle.

This is the stable node and CLI surface. Selection/cache policy, prompt
rendering, paid Responses execution, and SQLite projection live in their
concrete single-concern modules under ``deep_context/synthesis`` and ``db``.

The stage keeps the fixed artifacts and payload contract:
``<out-dir>/<parent_id>.jsonl`` plus ``<out-dir>/manifest.json``.

Changelog:
- 2026-08-08: a --model/--reasoning-effort switch since the last completed
  run now forces a full re-plan instead of silently reusing facts a
  different model produced. See _model_or_effort_changed.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.ingestion.primitives.common.jsonio import now_iso, read_json
from packs.ingestion.primitives.deep_context.shared.common import (
    CANONICAL_DB,
    emit,
    FACTS_DIR,
    FACTS_MANIFEST,
    FACTS_TEMPLATE,
    OWNER_JSON,
    RAW_BUNDLE_TEMPLATE,
    RAW_DIR,
)
from packs.ingestion.primitives.deep_context.db.queries import parent_fact_counts
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.manifests.synthesize_person_context_manifest import (
    SynthesizePersonContextManifest,
)
from packs.ingestion.primitives.deep_context.synthesis import normalization, prompting, runner, selection
from packs.ingestion.primitives.deep_context.synthesis.models import (
    SynthesisConfig,
    SynthesisPlan,
    WorthSyncResult,
)
from packs.ingestion.primitives.deep_context.shared.openai_responses import (
    OpenAIResponsesConfig,
    estimate_cost_usd,
)
from packs.ingestion.primitives.pipeline.contract import Artifact, Node

DEFAULT_CHUNK_CHARS = 9000
DEFAULT_MAX_BATCHES = 20
DEFAULT_MAX_RETRIES = 6


class SynthesizePersonContext(Node):
    """Build per-parent facts with checkpointed, bounded OpenAI Responses calls."""

    name = "deep_synthesize"
    inputs = (
        Artifact(path=RAW_BUNDLE_TEMPLATE, required=False),
        Artifact(path=str(OWNER_JSON), required=False),
    )
    outputs = (Artifact(path=FACTS_TEMPLATE, required=False),)
    payload = SynthesizePersonContextManifest
    manifest = str(FACTS_MANIFEST)

    def __init__(
        self,
        *,
        db: Db,
        raw_dir: Path | None = None,
        out_dir: Path | None = None,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = "medium",
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
        max_batches: int = DEFAULT_MAX_BATCHES,
        concurrency: int | None = None,
        timeout: int = 120,
        max_retries: int = DEFAULT_MAX_RETRIES,
        force: bool = False,
        rejudge: bool = False,
    ) -> None:
        self.db = db
        self.config = SynthesisConfig(
            raw_dir=Path(raw_dir or RAW_DIR),
            facts_dir=Path(out_dir or FACTS_DIR),
            responses=OpenAIResponsesConfig.resolve(
                model=model,
                effort=reasoning_effort,
                concurrency=concurrency,
                timeout=timeout,
                max_retries=max_retries,
            ),
            chunk_chars=chunk_chars,
            max_batches=max_batches,
            force=force,
            rejudge=rejudge,
        )

    def bindings(self) -> dict[str, str]:
        return {self.manifest: str(self.config.facts_dir / "manifest.json")}

    def _plan(self, system_prompt: str | None = None) -> SynthesisPlan:
        prompt = system_prompt or selection.build_system_prompt(self.db)
        return selection.build_plan(
            self.db,
            system_prompt=prompt,
            chunk_chars=self.config.chunk_chars,
            max_batches=self.config.max_batches,
            force=self.config.force,
            rejudge=self.config.rejudge,
            model_changed=self._model_or_effort_changed(),
        )

    def _model_or_effort_changed(self) -> bool:
        """True when this run's model/effort differ from the last completed run's.

        Read back from this stage's own manifest.json (facts_dir/manifest.json,
        the same durable receipt every Deep Context stage already writes — not a
        new store) rather than any per-parent record, because no per-parent
        artifact stores which model produced it. A missing manifest (first run)
        or one written before these fields existed reads as unchanged, so a
        fresh install never looks "changed" against nothing.
        """
        previous = read_json(self.config.facts_dir / "manifest.json", default=None)
        if not isinstance(previous, dict):
            return False
        prior_model = str(previous.get("model") or "")
        prior_effort = str(previous.get("reasoning_effort") or "")
        if not prior_model and not prior_effort:
            return False
        return prior_model != self.config.responses.model or prior_effort != self.config.responses.effort

    def _migrate_parent_cache(self) -> SynthesisPlan:
        """Normalize paid caches only after the caller enters the run path."""
        system_prompt = selection.build_system_prompt(self.db)
        normalization.normalize_parent_cache(
            self.db,
            raw_dir=self.config.raw_dir,
            facts_dir=self.config.facts_dir,
            system_prompt=system_prompt,
            chunk_chars=self.config.chunk_chars,
            max_batches=self.config.max_batches,
        )
        return self._plan(system_prompt)

    def estimate(self) -> dict[str, Any]:
        """Estimate calls and cost without spending or replacing the manifest."""
        payload = runner.estimate(self.config, self._plan())
        payload["updated_at"] = now_iso()
        return payload

    def execute(self) -> SynthesizePersonContextManifest:
        """The paid path: migrates cached parent bundles, then bills OpenAI for
        every pending person via runner.run_paid. Reached only through
        run() -> Node.run(), which also writes the manifest and records the
        facts/*.jsonl output artifacts; there is no needs_approval gate here —
        --dry-run below (estimate()) is the only free path.
        """
        started = time.monotonic()
        self.config.facts_dir.mkdir(parents=True, exist_ok=True)
        plan = self._migrate_parent_cache()
        tally = runner.run_paid(self.db, self.config, plan)
        fact_count, without_worth = parent_fact_counts(self.db)
        worth_sync = WorthSyncResult(
            path=str(self.db.db_path),
            synced_people=fact_count,
            synced_rows=tally.projected_rows,
            without_worth=without_worth,
            total_rows=fact_count,
        )
        # OpenAI bills reasoning tokens at the output rate, so combine before costing.
        billed_output = tally.tokens["output_tokens"] + tally.tokens["reasoning_tokens"]
        return SynthesizePersonContextManifest(
            status="completed",
            people=len(plan.bundles),
            people_done=tally.people_done,
            batches_run=tally.batches,
            avg_batches_per_person=round(tally.batches / max(1, tally.people_done), 2),
            stop_reasons=tally.stop_reasons,
            errors=tally.errors,
            total_failures=tally.total_failures,
            model=self.config.responses.model,
            synthesis_version=prompting.SYNTHESIS_VERSION,
            reasoning_effort=self.config.responses.effort,
            owner_context=True,
            orphan_facts_removed=0,
            rejudge=self.config.rejudge,
            max_batches=self.config.max_batches,
            concurrency=self.config.responses.concurrency,
            tokens=tally.tokens,
            estimated_cost_usd=estimate_cost_usd(
                tally.tokens["input_tokens"],
                billed_output,
                self.config.responses.model,
            ),
            out_dir=str(self.config.facts_dir),
            worth_sync=worth_sync,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            updated_at=now_iso(),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synthesize structured facts from message bundles (OpenAI Responses).",
    )
    parser.add_argument("--raw-dir", default=str(RAW_DIR))
    parser.add_argument("--out-dir", default=str(FACTS_DIR))
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning-effort", default="medium", choices=["minimal", "low", "medium", "high"])
    parser.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS)
    parser.add_argument("--max-batches", type=int, default=DEFAULT_MAX_BATCHES)
    parser.add_argument("--concurrency", type=int, default=None, help="Override usage tier")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    # Skips the fingerprint/version match in selection.pending_target_bundles
    # entirely — every eligible person is resynthesized and re-billed, not just
    # the ones whose cache actually missed.
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--rejudge",
        action="store_true",
        help="Rejudge every message-backed dossier despite cached machine/human worth; preserve the human column",
    )
    parser.add_argument("--dry-run", action="store_true", help="Estimate calls/cost, spend nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    node = SynthesizePersonContext(
        db=open_existing_db(args.db),
        raw_dir=Path(args.raw_dir),
        out_dir=Path(args.out_dir),
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        chunk_chars=args.chunk_chars,
        max_batches=args.max_batches,
        concurrency=args.concurrency,
        timeout=args.timeout,
        max_retries=args.max_retries,
        force=args.force,
        rejudge=args.rejudge,
    )
    if args.dry_run:
        # estimate() is not execute(): a free tiktoken-only projection, no db
        # writes and no manifest recorded via the Node template (contrast with
        # collect_person_context's execute()/run() dry-run split, where
        # execute() still runs and records artifacts).
        emit(node.estimate())
        return 0
    # The only path that spends: Node.run() wraps execute() (the billed
    # OpenAI calls) with the typed-manifest template. No needs_approval gate
    # sits in front of it — reaching this line always bills.
    emit(node.run().to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
