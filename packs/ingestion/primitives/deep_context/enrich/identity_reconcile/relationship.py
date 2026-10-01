"""Resolve parent identity disagreements from saved dossiers and candidate evidence.

SQLite pending parents -> cached identity judgments -> candidate settlement.
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

from packs.ingestion.primitives.common.gates import exit_code_for_status
from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.db.context_queries import dossier_message_count
from packs.ingestion.primitives.deep_context.db.identity_queries import imported_linkedin_urls, links, research_rows
from packs.ingestion.primitives.deep_context.db.identity_views import pending_parent_ids, linkedin_parents
from packs.ingestion.primitives.deep_context.db.queries import parents, facts
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.candidate_selection import (
    RelationshipDecision, cache_relationship_judgment, finish_reviews,
)
from packs.ingestion.primitives.deep_context.enrich.profiles.projection import profile_payloads
from packs.ingestion.primitives.deep_context.prompts.loader import load_prompt
from packs.ingestion.primitives.deep_context.shared.common import CANONICAL_DB, emit
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.shared.openai_responses import (
    OpenAIResponsesCaller, OpenAIResponsesConfig, estimate_cost_usd,
)
from packs.ingestion.primitives.imports.common import write_manifest
from packs.ingestion.primitives.enrich.rapidapi_client import PROFILE_ERROR
from packs.ingestion.schemas.people_schema import normalize_linkedin_url

SYSTEM_PROMPT = load_prompt("relationship_system")
SCHEMA = json.loads(load_prompt("relationship_schema"))
ESTIMATED_OUTPUT_TOKENS = 1500


@dataclass(frozen=True)
class _RelationshipTask:
    parent_id: str
    prompt: str
    fingerprint: str


class ReviewRelationships:
    """Checkpoint each paid identity judgment before settling the parent choices."""

    def __init__(self, *, db: Db, out_dir: Path | None = None,
                 model: str = "gpt-6.1-sol", reasoning_effort: str = "medium",
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
        pending = sorted(pending_parent_ids(self.db))
        records: dict[str, RelationshipDecision] = {}
        saved = {}
        for candidate in links(self.db, parent_ids=pending):
            payload = parse_json_object(candidate.judgment_payload_json)
            raw = payload.get("relationship_judgment") or payload.get("relationship_decision")
            if raw and "candidates" in raw:
                saved[raw["parent_id"]] = RelationshipDecision.from_payload(
                    raw["parent_id"], raw["fingerprint"], raw)
        review_parents = linkedin_parents(self.db, parent_ids=pending)
        candidates = {parent.parent_id: [{
            "url": normalize_linkedin_url(candidate.url), "name": candidate.full_name, "headline": candidate.headline,
            "location": candidate.location, "experiences": candidate.experiences,
            "education": candidate.education, "identity_verdict": candidate.verdict,
            "identity_reason": candidate.reason,
        } for candidate in parent.candidates if candidate.url and not candidate.synthetic] for parent in review_parents}
        hydrated = profile_payloads(self.db, candidate_keys=(candidate.row_key
            for parent in review_parents for candidate in parent.candidates if not candidate.synthetic))
        for parent in review_parents:
            for candidate, evidence in zip((item for item in parent.candidates if item.url and not item.synthetic), candidates[parent.parent_id]):
                result = hydrated.get(candidate.row_key)
                if result is None:
                    continue
                profile = result.normalized_profile
                if normalize_linkedin_url(profile.linkedin_url or '') != evidence['url']:
                    evidence.update(name='', headline='', location='', experiences=(), education=())
                    continue
                evidence['experiences'] = [asdict(item) for item in profile.experiences]
                evidence['education'] = [asdict(item) for item in profile.education]
        known_urls = imported_linkedin_urls(self.db, pending)
        tasks = []
        for parent_id in pending:
            rows = links(self.db, parent_id=parent_id)
            if any(not row.decision_action and (profile := hydrated.get(row.row_key)) is not None
                   and profile.state == PROFILE_ERROR for row in rows):
                continue
            if any(row.decision_action in {"verify", "retarget"} and row.decision_approved in {"yes", "auto"} for row in rows):
                continue
            eligible_urls = {normalize_linkedin_url(row.machine_proposed_url or row.linkedin_url) for row in rows
                if not row.decision_action and row.kind != "synthetic"} - {""}
            if not eligible_urls:
                continue
            profiles = {candidate["url"]: candidate for candidate in candidates.get(parent_id, ())
                if candidate["url"] in eligible_urls}
            for url in eligible_urls:
                profiles.setdefault(url, {"url": url})
            evidence = DossierEvidence.from_parent_db(self.db, parent_id)
            message_count = dossier_message_count(self.db, parent_id)
            context = {key: value for key, value in evidence.as_judge_dict().items() if value}
            context.update(dossier=evidence.dossier, message_count=message_count,
                imported_linkedin_urls=known_urls.get(parent_id, ()),
                human_worth=parents(self.db, parent_id=parent_id)[0].human_worth,
                worth_evidence=[{"machine_worth": row.machine_worth, "reason": row.machine_worth_reason,
                    "labels": parse_json_object(row.facts_json).get("labels"),
                    "network_worth": parse_json_object(row.facts_json).get("network_worth")}
                    for row in facts(self.db, parent_id=parent_id)],
                candidates=[profiles[url] for url in sorted(profiles)],
                research=[parse_json_object(row.result_json) for row in research_rows(self.db, parent_id=parent_id)
                    if row.result_json])
            prompt = json.dumps(context, ensure_ascii=False, sort_keys=True)
            # Machine verdicts change when this decision settles; paid evidence does not.
            evidence_input = dict(context)
            evidence_input["candidates"] = [{key: value for key, value in candidate.items()
                if key not in {"identity_verdict", "identity_reason"}} for candidate in context["candidates"]]
            fingerprint = hashlib.sha256(json.dumps(evidence_input, ensure_ascii=False,
                sort_keys=True).encode()).hexdigest()
            task = _RelationshipTask(parent_id, prompt, fingerprint)
            tasks.append(task)
            if parent_id in saved and saved[parent_id].fingerprint == fingerprint:
                records[parent_id] = saved[parent_id]
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
                decision = RelationshipDecision.from_payload(task.parent_id, task.fingerprint, result.payload)
                expected_urls = {candidate["url"] for candidate in json.loads(task.prompt)["candidates"]}
                if {candidate.url for candidate in decision.candidates} != expected_urls:
                    raise ValueError("identity decision must return exactly the supplied URLs")
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
    parser.add_argument("--model", default="gpt-6.1-sol")
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
