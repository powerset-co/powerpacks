#!/usr/bin/env python3
"""LLM-based birth year inference for local processing pipeline.

Reads flattened_people.jsonl, calls the LLM to estimate birth year from
education and work experience timelines, and writes inferred_ages.jsonl.
The processing pipeline then reads that artifact and applies inferred_birth_year
to people records.

Ported from aleph-mvp/data_pipeline_v2/pipelines/people/processing/infer_ages.py
Adapted to read from local flattened_people.jsonl instead of Supabase.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
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

DEFAULT_MODEL = "gpt-5.1"
DEFAULT_CONCURRENCY = 50
CHECKPOINT_EVERY = 200
MIN_BIRTH_YEAR = 1940
MAX_BIRTH_YEAR = 2010

SYSTEM_PROMPT = """You are an age inference engine. Given a person's education history and work experience, estimate their birth year.

Rules:
- Undergraduate degrees (BA, BS, BEng, etc.) typically start at age 18
- High school graduation is typically at age 18
- MBA programs typically start at age 26-32 (avg ~28)
- PhD programs typically start at age 22-26
- JD/MD programs typically start at age 22-24
- Executive education, continuing ed, online courses are NOT age indicators — ignore them
- If someone has both undergrad and grad entries, prefer undergrad start year
- Cross-reference: if first FULL-TIME job starts in year X, person was likely ~22
- Internships, co-ops, summer roles happen during college (age 19-21) — skip for "first job" anchor
- If education has no dates but work does, use earliest full-time role start - 22
- If nothing is available, return null birth year with confidence 0

Non-traditional education:
- Many adults return to school later — a degree starting in 2015 does NOT mean born ~1997
- If work experience exists BEFORE degree start date, anchor to earliest work, not later degree
- High school dates are the most reliable anchor (everyone attends at ~same age)
- Use the EARLIEST education entry for estimation

Cross-validation:
- Use ALL signals: high school dates, earliest education, earliest full-time job, degree sequence
- If multiple signals agree, that's HIGH confidence (0.8+)
- Only reduce confidence when signals genuinely CONFLICT (5+ years apart)
- A later-in-life degree is NOT a conflict

Confidence calibration:
- 0.9+: Strong education dates with corroborating work history
- 0.7-0.9: Single clear signal or multiple agreeing signals
- 0.5-0.7: Indirect signals only
- 0.3-0.5: Weak/ambiguous signals
- <0.3: Very little data, mostly guessing

Output JSON: {"inferred_birth_year": int_or_null, "confidence": float_0_to_1}"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _date_display(value: Any) -> str:
    if isinstance(value, dict):
        year = value.get("year")
        month = value.get("month")
        if year and month:
            return f"{year}-{int(month):02d}"
        return str(year or "?")
    text = str(value or "").strip()
    return text if text and text.lower() not in ("present", "current", "now", "") else "?"


def build_prompt(person: dict[str, Any]) -> str:
    lines: list[str] = []
    education = person.get("education") or []
    work = person.get("work_experiences") or []
    if education:
        lines.append("EDUCATION:")
        for edu in education:
            if not isinstance(edu, dict):
                continue
            degree = str(edu.get("degree") or edu.get("degree_name") or "").strip()
            field = str(edu.get("field_of_study") or edu.get("field") or "").strip()
            school = str(edu.get("school_name") or edu.get("school") or edu.get("name") or "").strip()
            starts_at = edu.get("starts_at") if isinstance(edu.get("starts_at"), dict) else {}
            ends_at = edu.get("ends_at") if isinstance(edu.get("ends_at"), dict) else {}
            start = _date_display(starts_at) if starts_at else _date_display(edu.get("start_year"))
            end = _date_display(ends_at) if ends_at else _date_display(edu.get("end_year"))
            line = f"  - {degree}"
            if field:
                line += f" in {field}"
            if school:
                line += f" @ {school}"
            line += f" ({start}-{end})"
            lines.append(line)
    else:
        lines.append("EDUCATION: none listed")
    if work:
        selected = work[:2] + work[-5:] if len(work) > 7 else work
        lines.append(f"\nWORK ({len(selected)} of {len(work)} positions — current + earliest):")
        for exp in selected:
            if not isinstance(exp, dict):
                continue
            title = str(exp.get("title") or exp.get("position_title") or "").strip()
            company = str(exp.get("company_name") or exp.get("company") or "").strip()
            start = _date_display(exp.get("starts_at") or exp.get("start_date"))
            end_raw = exp.get("ends_at") or exp.get("end_date")
            is_current = bool(exp.get("is_current")) or str(end_raw).lower() in ("present", "current", "now", "")
            end = "present" if is_current else _date_display(end_raw)
            lines.append(f"  - {title} @ {company} ({start} → {end})")
    else:
        lines.append("\nWORK: none listed")
    return "\n".join(lines)


def load_checkpoint(output_path: Path) -> dict[str, dict[str, Any]]:
    if not output_path.exists():
        return {}
    return {
        str(row.get("person_id")): row
        for row in read_jsonl(output_path)
        if row.get("person_id")
    }


async def infer_one(
    client: AsyncOpenAI,
    person_id: str,
    prompt: str,
    model: str,
    semaphore: asyncio.Semaphore,
    counters: dict[str, int],
) -> dict[str, Any]:
    async with semaphore:
        try:
            response = await client.chat.completions.create(
                model=model,
                temperature=0.0,
                max_completion_tokens=100,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or "{}"
            result = json.loads(raw)
            if response.usage:
                counters["input_tokens"] += response.usage.prompt_tokens
                counters["output_tokens"] += response.usage.completion_tokens
            birth_year = result.get("inferred_birth_year")
            if isinstance(birth_year, (int, float)) and MIN_BIRTH_YEAR <= int(birth_year) <= MAX_BIRTH_YEAR:
                birth_year = int(birth_year)
                counters["inferred"] += 1
            else:
                birth_year = None
            return {
                "person_id": person_id,
                "birth_year": birth_year,
                "confidence": float(result.get("confidence", 0.0)),
                "method": "llm" if birth_year else None,
            }
        except Exception as exc:
            counters["errors"] += 1
            return {
                "person_id": person_id,
                "birth_year": None,
                "confidence": 0.0,
                "method": None,
                "error": f"{type(exc).__name__}: {exc}",
            }


async def infer_batch(
    people: list[tuple[str, str]],
    model: str,
    output_path: Path,
    concurrency: int,
) -> dict[str, Any]:
    client = AsyncOpenAI(
        api_key=os.getenv("OPENAI_API_KEY", ""),
        base_url=os.getenv("POWERPACKS_OPENAI_BASE", "https://api.openai.com/v1"),
    )
    semaphore = asyncio.Semaphore(max(1, concurrency))
    counters = {"inferred": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0}
    start = time.time()
    batch_size = max(1, concurrency * 2)
    with output_path.open("a", encoding="utf-8") as handle:
        for offset in range(0, len(people), batch_size):
            batch = people[offset : offset + batch_size]
            results = await asyncio.gather(*[
                infer_one(client, pid, prompt, model, semaphore, counters) for pid, prompt in batch
            ])
            for record in results:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
    await client.close()
    elapsed = time.time() - start
    total = len(people)
    return {
        "total_processed": total,
        "inferred": counters["inferred"],
        "errors": counters["errors"],
        "coverage": counters["inferred"] / max(total, 1),
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
    # Build prompts from flattened people
    people_prompts: list[tuple[str, str]] = []
    for person in read_jsonl(flattened):
        person_id = str(person.get("id") or person.get("person_id") or "").strip()
        if not person_id:
            continue
        education = person.get("education") or []
        work = person.get("work_experiences") or []
        if not education and not work:
            continue
        people_prompts.append((person_id, build_prompt(person)))
    if args.dry_run:
        return {
            "status": "dry_run",
            "stage": "infer_ages",
            "total_people": len(people_prompts),
            "limit": args.limit,
        }
    existing = load_checkpoint(output_path)
    remaining = [(pid, prompt) for pid, prompt in people_prompts if pid not in existing]
    if args.limit:
        remaining = remaining[: args.limit]
    if not remaining:
        return {"status": "completed", "stage": "infer_ages", "total_people": len(people_prompts), "already_processed": len(existing), "new_processed": 0}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    stats = asyncio.run(infer_batch(remaining, args.model, output_path, args.concurrency))
    return {
        "status": "completed",
        "stage": "infer_ages",
        "total_people": len(people_prompts),
        "already_processed": len(existing),
        "new_processed": stats.get("total_processed", 0),
        "inferred": stats.get("inferred", 0),
        "coverage": stats.get("coverage", 0),
        "output": str(output_path),
        "infer_stats": stats,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flattened", required=True, help="Path to flattened_people.jsonl")
    parser.add_argument("--output", required=True, help="Path to inferred_ages.jsonl")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    emit(run(args))


if __name__ == "__main__":
    main()
