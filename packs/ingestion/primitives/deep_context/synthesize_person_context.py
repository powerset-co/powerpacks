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

from pydantic import Field

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.indexing.lib.openai_responses import estimate_cost_usd
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import (
    CANONICAL_DB,
    emit,
    FACTS_DIR,
    FACTS_MANIFEST,
    FACTS_TEMPLATE,
    OWNER_JSON,
    RAW_BUNDLE_TEMPLATE,
    RAW_DIR,
)
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.synthesis import normalization, prompting, runner, selection
from packs.ingestion.primitives.deep_context.synthesis.prompting import (
    DEFAULT_TARGET_CONFIDENCE,
)
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, StageManifest

DEFAULT_CHUNK_CHARS = 9000
DEFAULT_SATURATION_ROUNDS = 2
DEFAULT_MAX_BATCHES = 20
DEFAULT_MAX_RETRIES = 6
DEFAULT_CHUNK_PEOPLE = 200


class SynthesizePersonContextManifest(StageManifest):
    source: str = "synthesize_person_context"
    people: int = 0
    chunk_people: int = 0
    people_done: int = 0
    batches_run: int = 0
    avg_batches_per_person: float = 0.0
    stop_reasons: dict[str, int] = Field(default_factory=dict)
    errors: int = 0
    model: str = ""
    synthesis_version: str = prompting.SYNTHESIS_VERSION
    reasoning_effort: str = ""
    owner_context: bool = False
    orphan_facts_removed: int = 0
    rejudge: bool = False
    target_confidence: float = DEFAULT_TARGET_CONFIDENCE
    max_batches: int = DEFAULT_MAX_BATCHES
    concurrency: int = 0
    tokens: dict[str, int] = Field(default_factory=dict)
    estimated_cost_usd: float = 0.0
    out_dir: str = ""
    worth_sync: dict[str, Any] = Field(default_factory=dict)
    elapsed_ms: int = 0
    updated_at: str = ""


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
        concurrency: int = 0,
        chunk_people: int = DEFAULT_CHUNK_PEOPLE,
        timeout: int = 120,
        max_retries: int = DEFAULT_MAX_RETRIES,
        person: str = "",
        no_owner: bool = False,
        force: bool = False,
        rejudge: bool = False,
    ) -> None:
        self.db = db
        self.raw_dir = Path(raw_dir or RAW_DIR)
        self.facts_dir = Path(out_dir or FACTS_DIR)
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.chunk_chars = chunk_chars
        self.target_confidence = target_confidence
        self.saturation_rounds = saturation_rounds
        self.max_batches = max_batches
        self.concurrency = concurrency
        self.chunk_people = chunk_people
        self.timeout = timeout
        self.max_retries = max_retries
        self.person = person
        self.no_owner = no_owner
        self.force = force
        self.rejudge = rejudge

    def bindings(self) -> dict[str, str]:
        return {self.manifest: str(self.facts_dir / "manifest.json")}

    def _plan(self) -> selection.SynthesisPlan:
        return selection.build_plan(
            self.db,
            chunk_chars=self.chunk_chars,
            max_batches=self.max_batches,
            no_owner=self.no_owner,
            force=self.force,
            rejudge=self.rejudge,
            person_id=self.person,
        )

    def _migrate_parent_cache(self) -> selection.SynthesisPlan:
        """Normalize paid caches only after the caller enters the run path."""
        plan = self._plan()
        normalization.normalize_parent_cache(
            self.db,
            raw_dir=self.raw_dir,
            facts_dir=self.facts_dir,
            system_prompt=plan.system_prompt,
            chunk_chars=self.chunk_chars,
            max_batches=self.max_batches,
        )
        return self._plan()

    def estimate(self) -> dict[str, Any]:
        """Estimate calls and cost without spending or replacing the manifest."""
        payload = runner.estimate(self)
        payload["updated_at"] = now_iso()
        return payload

    def execute(self) -> SynthesizePersonContextManifest:
        started = time.monotonic()
        self.facts_dir.mkdir(parents=True, exist_ok=True)
        plan = self._migrate_parent_cache()
        tally = runner.SynthesisTally()
        concurrency, effort = runner.run_paid(self, plan, tally)
        parent_facts = [row for row in canonical_snapshot(self.db).facts if row.person_id is None]
        worth_sync = {
            "path": str(self.db.db_path),
            "synced_people": len(parent_facts),
            "synced_rows": tally.projected_rows,
            "skipped_human": 0,
            "without_worth": sum(row.machine_worth is None for row in parent_facts),
            "cleared_legacy_spam": 0,
            "total_rows": len(parent_facts),
        }
        billed_output = tally.tokens["output_tokens"] + tally.tokens["reasoning_tokens"]
        return SynthesizePersonContextManifest(
            status="completed",
            people=len(plan.bundles),
            chunk_people=self.chunk_people,
            people_done=tally.people_done,
            batches_run=tally.batches,
            avg_batches_per_person=round(tally.batches / max(1, tally.people_done), 2),
            stop_reasons=tally.stop_reasons,
            errors=tally.errors,
            model=self.model,
            synthesis_version=prompting.SYNTHESIS_VERSION,
            reasoning_effort=effort,
            owner_context=bool(plan.owner),
            orphan_facts_removed=0,
            rejudge=bool(self.rejudge),
            target_confidence=self.target_confidence,
            max_batches=self.max_batches,
            concurrency=concurrency,
            tokens=tally.tokens,
            estimated_cost_usd=estimate_cost_usd(
                tally.tokens["input_tokens"], billed_output, self.model,
            ),
            out_dir=str(self.facts_dir),
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
    parser.add_argument("--concurrency", type=int, default=0, help="0 = from usage tier")
    parser.add_argument("--chunk-people", type=int, default=DEFAULT_CHUNK_PEOPLE)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--person", default="", help="Only this person id")
    parser.add_argument("--no-owner", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--rejudge", action="store_true",
        help="Rejudge every message-backed dossier despite cached machine/human worth; preserve the human column",
    )
    parser.add_argument("--dry-run", action="store_true", help="Estimate calls/cost, spend nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    node = SynthesizePersonContext(
        db=open_existing_db(args.db), raw_dir=Path(args.raw_dir), out_dir=Path(args.out_dir), model=args.model,
        reasoning_effort=args.reasoning_effort, chunk_chars=args.chunk_chars,
        target_confidence=args.target_confidence, saturation_rounds=args.saturation_rounds,
        max_batches=args.max_batches, concurrency=args.concurrency,
        chunk_people=args.chunk_people, timeout=args.timeout, max_retries=args.max_retries,
        person=args.person, no_owner=args.no_owner, force=args.force, rejudge=args.rejudge,
    )
    if args.dry_run:
        emit(node.estimate())
        return 0
    emit(node.run().to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
