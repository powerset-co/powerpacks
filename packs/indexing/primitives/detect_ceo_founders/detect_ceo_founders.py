#!/usr/bin/env python3
"""CEO/CTO founder detection via LLM.

Reads flattened_people.jsonl, finds CEO/CTO positions that do NOT already have
"founder" in the title, calls the LLM to classify, and writes
founder_enrichment.jsonl. The processing pipeline then reads that artifact and
injects "founder" into d2q_tokens + role_ids for detected founders.

Ported from aleph-mvp/data_pipeline_v2/pipelines/people/processing/detect_ceo_founders.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402
from packs.indexing.lib.io import read_jsonl, write_json  # noqa: E402

CEO_CTO_RE = re.compile(
    r"\bCEO\b|\bCTO\b|\bchief\s+executive\b|\bchief\s+technology\b",
    re.IGNORECASE,
)
FOUNDER_RE = re.compile(r"\bfounder\b|\bfounding\b|\bco.?found", re.IGNORECASE)

DEFAULT_MODEL = "gpt-5.1"
DEFAULT_CONFIDENCE_THRESHOLD = 0.7
DEFAULT_CONCURRENCY = 30
CHECKPOINT_EVERY = 200

SYSTEM_PROMPT = """You are a startup/business expert. Given a person's name, headline, title, and company, determine if they are a FOUNDER or CO-FOUNDER of that company.

Rules:
- Answer ONLY with a JSON object: {"is_founder": true/false, "confidence": 0.0-1.0, "reasoning": "brief explanation"}
- "Founder" means they started/co-founded the company, not just that they hold a C-level title
- A CEO who joined an existing company is NOT a founder
- A CEO who started the company IS a founder
- If the headline mentions "founder", "co-founder", or "founding", treat that as strong positive evidence
- The headline is ONLY a positive signal — if the headline does NOT mention founder, that tells you nothing
- If you're unsure or don't know the person/company, set confidence < 0.5
- Use your training knowledge about the person and company"""

USER_PROMPT_TEMPLATE = """Is this person a founder of their company?

Name: {name}
Headline: {headline}
Title: {title}
Company: {company}"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def load_candidates(flattened_path: Path) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for person in read_jsonl(flattened_path):
        person_id = str(person.get("id") or person.get("person_id") or "").strip()
        full_name = str(person.get("full_name") or "").strip()
        headline = str(person.get("headline") or "").strip()
        if not person_id:
            continue
        for idx, exp in enumerate(person.get("work_experiences") or []):
            if not isinstance(exp, dict):
                continue
            title = str(exp.get("title") or exp.get("position_title") or "").strip()
            if not title:
                continue
            is_current = bool(exp.get("is_current") or (exp.get("end_date") in (None, "", "present", "Present")))
            if not is_current:
                continue
            if not CEO_CTO_RE.search(title):
                continue
            if FOUNDER_RE.search(title):
                continue
            company = str(exp.get("company_name") or exp.get("company") or "").strip()
            if not company:
                continue
            position_id = str(exp.get("id") or exp.get("position_id") or f"{person_id}-{idx}").strip()
            candidates.append({
                "position_id": position_id,
                "person_id": person_id,
                "person_name": full_name,
                "headline": headline,
                "position_title": title,
                "company_name": company,
            })
    return candidates


def load_checkpoint(output_path: Path) -> set[str]:
    if not output_path.exists():
        return set()
    return {
        str(row.get("position_id"))
        for row in read_jsonl(output_path)
        if row.get("position_id")
    }


async def classify_one(
    client: AsyncOpenAI,
    candidate: dict[str, Any],
    model: str,
    now: str,
    semaphore: asyncio.Semaphore,
    counters: dict[str, int],
) -> dict[str, Any]:
    async with semaphore:
        try:
            response = await client.chat.completions.create(
                model=model,
                temperature=0.0,
                max_completion_tokens=200,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": USER_PROMPT_TEMPLATE.format(
                        name=candidate["person_name"],
                        headline=candidate.get("headline", ""),
                        title=candidate["position_title"],
                        company=candidate["company_name"],
                    )},
                ],
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or "{}"
            result = json.loads(raw)
            if response.usage:
                counters["input_tokens"] += response.usage.prompt_tokens
                counters["output_tokens"] += response.usage.completion_tokens
            record = {
                "position_id": candidate["position_id"],
                "person_id": candidate["person_id"],
                "person_name": candidate["person_name"],
                "position_title": candidate["position_title"],
                "company_name": candidate["company_name"],
                "is_founder": bool(result.get("is_founder")),
                "confidence": float(result.get("confidence", 0.0)),
                "reasoning": str(result.get("reasoning", "")),
                "model": model,
                "classified_at": now,
            }
            if result.get("is_founder"):
                counters["founders"] += 1
            else:
                counters["non_founders"] += 1
            return record
        except Exception as exc:
            counters["errors"] += 1
            return {
                "position_id": candidate["position_id"],
                "person_id": candidate["person_id"],
                "person_name": candidate["person_name"],
                "position_title": candidate["position_title"],
                "company_name": candidate["company_name"],
                "is_founder": None,
                "confidence": 0.0,
                "reasoning": f"Error: {exc}",
                "model": model,
                "classified_at": now,
            }


async def classify_batch(
    candidates: list[dict[str, Any]],
    model: str,
    output_path: Path,
    concurrency: int,
) -> dict[str, Any]:
    client = AsyncOpenAI(
        api_key=os.getenv("OPENAI_API_KEY", ""),
        base_url=os.getenv("POWERPACKS_OPENAI_BASE", "https://api.openai.com/v1"),
    )
    now = now_iso()
    semaphore = asyncio.Semaphore(max(1, concurrency))
    counters = {"founders": 0, "non_founders": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0}
    start = time.time()
    batch_size = max(1, concurrency * 2)
    with output_path.open("a", encoding="utf-8") as handle:
        for offset in range(0, len(candidates), batch_size):
            batch = candidates[offset : offset + batch_size]
            results = await asyncio.gather(*[
                classify_one(client, c, model, now, semaphore, counters) for c in batch
            ])
            for record in results:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
    await client.close()
    elapsed = time.time() - start
    total = counters["founders"] + counters["non_founders"] + counters["errors"]
    return {
        "total_classified": total,
        "founders_detected": counters["founders"],
        "non_founders": counters["non_founders"],
        "errors": counters["errors"],
        "founder_rate": counters["founders"] / max(total, 1),
        "model": model,
        "concurrency": concurrency,
        "elapsed_seconds": round(elapsed, 1),
        "tokens": {"input": counters["input_tokens"], "output": counters["output_tokens"]},
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    load_dotenv(ROOT / ".env", override=False)
    flattened = Path(args.flattened)
    output_path = Path(args.output)
    if not flattened.exists():
        raise SystemExit(f"missing flattened input: {flattened}")
    candidates = load_candidates(flattened)
    if args.dry_run:
        return {
            "status": "dry_run",
            "stage": "detect_ceo_founders",
            "total_candidates": len(candidates),
            "limit": args.limit,
            "sample": [{"person_name": c["person_name"], "position_title": c["position_title"], "company_name": c["company_name"]} for c in candidates[:10]],
        }
    already = load_checkpoint(output_path)
    remaining = [c for c in candidates if c["position_id"] not in already]
    if args.limit:
        remaining = remaining[: args.limit]
    if not remaining:
        return {"status": "completed", "stage": "detect_ceo_founders", "total_candidates": len(candidates), "already_classified": len(already), "new_classified": 0}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    stats = asyncio.run(classify_batch(remaining, args.model, output_path, args.concurrency))
    # Build final founder set for pipeline consumption
    founder_ids: set[str] = set()
    threshold = float(args.confidence_threshold)
    for row in read_jsonl(output_path):
        if row.get("is_founder") and float(row.get("confidence", 0)) >= threshold:
            founder_ids.add(str(row.get("position_id")))
            founder_ids.add(str(row.get("person_id")))
    return {
        "status": "completed",
        "stage": "detect_ceo_founders",
        "total_candidates": len(candidates),
        "already_classified": len(already),
        "new_classified": stats.get("total_classified", 0),
        "founders_detected": stats.get("founders_detected", 0),
        "founder_rate": stats.get("founder_rate", 0),
        "high_confidence_founder_ids": len(founder_ids),
        "output": str(output_path),
        "classify_stats": stats,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flattened", required=True, help="Path to flattened_people.jsonl")
    parser.add_argument("--output", required=True, help="Path to founder_enrichment.jsonl")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--confidence-threshold", type=float, default=DEFAULT_CONFIDENCE_THRESHOLD)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    emit(run(args))


if __name__ == "__main__":
    main()
