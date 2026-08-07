#!/usr/bin/env python3
"""[2/4] Synthesize structured facts from each canonical parent's message bundle.

This is the stable node and CLI surface. Selection/cache policy, prompt
rendering, paid Responses execution, and SQLite projection live in their
concrete single-concern modules under ``deep_context/synthesis`` and ``db``.

The stage keeps the fixed artifacts and payload contract:
``<out-dir>/<parent_id>.jsonl`` plus ``<out-dir>/manifest.json``.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.ingestion.primitives.common.jsonio import now_iso
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
from packs.ingestion.primitives.deep_context.synthesis.prompting import (
    DEFAULT_TARGET_CONFIDENCE,
)
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
DEFAULT_SATURATION_ROUNDS = 2
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
        target_confidence: float = DEFAULT_TARGET_CONFIDENCE,
        saturation_rounds: int = DEFAULT_SATURATION_ROUNDS,
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
            target_confidence=target_confidence,
            saturation_rounds=saturation_rounds,
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
        )

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
        billed_output = tally.tokens["output_tokens"] + tally.tokens["reasoning_tokens"]
        return SynthesizePersonContextManifest(
            status="completed",
            people=len(plan.bundles),
            people_done=tally.people_done,
            batches_run=tally.batches,
            avg_batches_per_person=round(tally.batches / max(1, tally.people_done), 2),
            stop_reasons=tally.stop_reasons,
            errors=tally.errors,
            model=self.config.responses.model,
            synthesis_version=prompting.SYNTHESIS_VERSION,
            reasoning_effort=self.config.responses.effort,
            owner_context=True,
            orphan_facts_removed=0,
            rejudge=self.config.rejudge,
            target_confidence=self.config.target_confidence,
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
    parser.add_argument("--target-confidence", type=float, default=DEFAULT_TARGET_CONFIDENCE)
    parser.add_argument("--saturation-rounds", type=int, default=DEFAULT_SATURATION_ROUNDS)
    parser.add_argument("--max-batches", type=int, default=DEFAULT_MAX_BATCHES)
    parser.add_argument("--concurrency", type=int, default=None, help="Override usage tier")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
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
        target_confidence=args.target_confidence,
        saturation_rounds=args.saturation_rounds,
        max_batches=args.max_batches,
        concurrency=args.concurrency,
        timeout=args.timeout,
        max_retries=args.max_retries,
        force=args.force,
        rejudge=args.rejudge,
    )
    if args.dry_run:
        emit(node.estimate())
        return 0
    emit(node.run().to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
