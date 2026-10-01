"""Judge useful owner-answerable questions from saved dossiers, then limit review.

SQLite pending parents -> cached question judgments -> at most 100 questions.
Dry-run previews uncached OpenAI calls; completed outputs resume from SQLite.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import jsonschema

from packs.indexing.lib.llm_config import DEFAULT_IDENTITY_MODEL
from packs.ingestion.primitives.common.gates import exit_code_for_status
from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.db.context_queries import dossier_message_count
from packs.ingestion.primitives.deep_context.db.identity_queries import links
from packs.ingestion.primitives.deep_context.db.identity_views import pending_parent_ids, linkedin_queue
from packs.ingestion.primitives.deep_context.db.queries import parents
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.review_cap import (
    RelationshipDecision, cache_relationship_judgment, finish_reviews,
)
from packs.ingestion.primitives.deep_context.prompts.loader import load_prompt
from packs.ingestion.primitives.deep_context.shared.common import CANONICAL_DB, emit
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.shared.openai_responses import (
    OpenAIResponsesCaller, OpenAIResponsesConfig, estimate_cost_usd,
)
from packs.ingestion.primitives.imports.common import write_manifest

SYSTEM_PROMPT = load_prompt("relationship_system")
SCHEMA = json.loads(load_prompt("relationship_schema"))
ESTIMATED_OUTPUT_TOKENS = 1500


@dataclass(frozen=True)
class _RelationshipTask:
    parent_id: str
    prompt: str
    fingerprint: str
    message_count: int


class ReviewRelationships:
    """Checkpoint each paid question judgment before finishing the complete queue."""

    def __init__(self, *, db: Db, out_dir: Path | None = None,
                 model: str = DEFAULT_IDENTITY_MODEL, reasoning_effort: str = "medium",
                 concurrency: int | None = None, approve_spend: bool = False,
                 dry_run: bool = False, limit: int | None = None):
        self.db = db
        self.out_dir = out_dir or db.db_path.parent / "reconcile" / "relationships"
        self.decisions_path = self.out_dir / "decisions.jsonl"
        self.config = OpenAIResponsesConfig.resolve(model=model, effort=reasoning_effort,
            concurrency=concurrency, timeout=120, max_retries=2)
        self.approve_spend = approve_spend
        self.dry_run = dry_run
        self.limit = limit

    def run(self) -> dict[str, object]:
        records: dict[str, RelationshipDecision] = {}
        for candidate in links(self.db):
            payload = parse_json_object(candidate.judgment_payload_json)
            raw = payload.get("relationship_judgment") or payload.get("relationship_decision")
            if raw:
                decision = RelationshipDecision(**raw)
                records[decision.parent_id] = decision
        candidates = {parent.parent_id: [{
            "url": candidate.url, "name": candidate.full_name, "headline": candidate.headline,
            "location": candidate.location, "experiences": candidate.experiences,
            "education": candidate.education, "identity_verdict": candidate.verdict,
            "identity_reason": candidate.reason,
        } for candidate in parent.candidates] for parent in linkedin_queue(self.db)}
        tasks = []
        for parent_id in sorted(pending_parent_ids(self.db)):
            evidence = DossierEvidence.from_parent_db(self.db, parent_id)
            message_count = dossier_message_count(self.db, parent_id)
            context = {key: value for key, value in evidence.as_judge_dict().items() if value}
            context.update(dossier=evidence.dossier,
                human_worth=parents(self.db, parent_id=parent_id)[0].human_worth,
                message_count=message_count, candidates=candidates[parent_id])
            prompt = json.dumps(context, ensure_ascii=False, sort_keys=True)
            fingerprint = hashlib.sha256(json.dumps({
                "parent_id": parent_id, "system": SYSTEM_PROMPT, "input": prompt, "schema": SCHEMA,
                "model": self.config.model, "effort": self.config.effort,
            }, sort_keys=True).encode()).hexdigest()
            tasks.append(_RelationshipTask(parent_id, prompt, fingerprint, message_count))
        missing = [task for task in tasks if task.parent_id not in records]
        chosen = missing[:self.limit] if self.limit is not None else missing
        estimate = estimate_cost_usd(
            sum(len(task.prompt + SYSTEM_PROMPT) // 4 for task in chosen),
            ESTIMATED_OUTPUT_TOKENS * len(chosen), self.config.model)
        payload = {"parents": len(tasks), "calls": len(chosen), "reused": len(tasks) - len(missing),
                   "estimated_cost_usd": estimate, "remaining": len(missing)}
        if self.dry_run or (missing and not self.approve_spend):
            return {"status": "dry_run" if self.dry_run else "needs_approval", **payload}
        self.out_dir.mkdir(parents=True, exist_ok=True)
        if chosen:
            try:
                completed, errors = asyncio.run(self._judge(chosen))
            except Exception as exc:
                payload.update(status="failed", error=str(exc))
                return write_manifest(self.out_dir.name, payload, import_dir=self.out_dir.parent)
            records.update(completed)
            if errors:
                payload.update(status="failed", error=f"{len(errors)} relationship judgments failed: {errors[0]}",
                    remaining=sum(task.parent_id not in records for task in tasks))
                return write_manifest(self.out_dir.name, payload, import_dir=self.out_dir.parent)
        remaining = sum(task.parent_id not in records for task in tasks)
        if remaining:
            payload.update(status="incomplete", remaining=remaining)
        else:
            result = finish_reviews(self.db, tuple(records[task.parent_id] for task in tasks))
            payload.update(status="completed", remaining=0, reviews=result)
        return write_manifest(self.out_dir.name, payload, import_dir=self.out_dir.parent)

    async def _judge(self, tasks: list[_RelationshipTask]) -> tuple[dict[str, RelationshipDecision], list[Exception]]:
        async with OpenAIResponsesCaller(self.config) as caller:
            async def one(task: _RelationshipTask) -> RelationshipDecision:
                result = await caller.call(system_prompt=SYSTEM_PROMPT, user_prompt=task.prompt,
                    schema=SCHEMA, schema_name="relationship", context=task.parent_id)
                jsonschema.validate(result.payload, SCHEMA)
                decision = RelationshipDecision(parent_id=task.parent_id, fingerprint=task.fingerprint,
                    message_count=task.message_count, **result.payload)
                with self.decisions_path.open("a") as target:
                    target.write(json.dumps({"judgment": asdict(decision), "model": self.config.model,
                        "effort": self.config.effort, "usage": result.usage.as_dict()}, ensure_ascii=False) + "\n")
                cache_relationship_judgment(self.db, decision)
                return decision
            results = await asyncio.gather(*(one(task) for task in tasks), return_exceptions=True)
        errors = [result for result in results if isinstance(result, Exception)]
        return {result.parent_id: result for result in results if isinstance(result, RelationshipDecision)}, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=CANONICAL_DB)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--model", default=DEFAULT_IDENTITY_MODEL)
    parser.add_argument("--reasoning-effort", default="medium", choices=["minimal", "low", "medium", "high"])
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--approve-spend", action="store_true")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    payload = ReviewRelationships(db=open_existing_db(args.db), out_dir=args.out_dir,
        model=args.model, reasoning_effort=args.reasoning_effort, concurrency=args.concurrency,
        approve_spend=args.approve_spend, dry_run=args.dry_run, limit=args.limit).run()
    emit(payload)
    return 0 if args.dry_run else exit_code_for_status(str(payload["status"]))


if __name__ == "__main__":
    raise SystemExit(main())
