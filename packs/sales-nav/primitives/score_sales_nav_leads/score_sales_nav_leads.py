#!/usr/bin/env python3
"""Fan-out LLM scoring for local Sales Nav lead files.

Reads a sales_nav_artifacts state file, evaluates each lead in leads.jsonl
against user criteria, joins mutual edges for context, and writes only matching
leads to a task-specific output folder. Stdlib-only.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


DEFAULT_API_BASE = os.environ.get("OPENAI_API_BASE", "https://api.openai.com")
DEFAULT_MODEL = os.environ.get("SALES_NAV_SCORE_MODEL", "gpt-4o-mini")
DEFAULT_CONCURRENCY = int(os.environ.get("SALES_NAV_SCORE_CONCURRENCY", "50"))
DEFAULT_THRESHOLD = float(os.environ.get("SALES_NAV_SCORE_THRESHOLD", "0.7"))

SYSTEM_PROMPT = """You score Sales Navigator leads against a user's criteria.

Return strict JSON only:
{
  "score": <0.0-1.0>,
  "verdict": "include" | "exclude",
  "reason": "<brief evidence-based reason>",
  "confidence": <0.0-1.0>,
  "matched_traits": ["<short trait/evidence>", ...]
}

Scoring guide:
- 0.9-1.0: direct strong evidence from current/past roles, summary, education, or title/company.
- 0.7-0.89: likely match with credible evidence.
- 0.4-0.69: weak/partial/ambiguous match.
- 0.0-0.39: no clear evidence.

Use only the supplied lead/profile/mutual context. Do not invent facts.
"""

RESULT_FIELDS = [
    "rank",
    "member_id",
    "name",
    "title",
    "company",
    "location",
    "linkedin_url",
    "score",
    "confidence",
    "verdict",
    "reason",
    "matched_traits_json",
    "mutual_count",
    "top_mutuals_json",
]


def slugify(value: str, max_length: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return (slug[:max_length].strip("-") or "criteria")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open() as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"{path}:{line_number}: invalid JSONL") from exc
            if not isinstance(row, dict):
                raise RuntimeError(f"{path}:{line_number}: expected object")
            rows.append(row)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in RESULT_FIELDS})


def run_dir_from_state(state_path: Path, state: dict[str, Any]) -> Path:
    files = state.get("files") or {}
    manifest = files.get("manifest")
    if manifest:
        return Path(str(manifest)).parent
    return state_path.parent


def lead_context(lead: dict[str, Any], mutuals: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "member_id": lead.get("member_id"),
        "name": lead.get("name"),
        "title": lead.get("title"),
        "headline": lead.get("headline"),
        "company": lead.get("company"),
        "location": lead.get("location"),
        "linkedin_url": lead.get("linkedin_url"),
        "summary": lead.get("summary"),
        "enriched": lead.get("enriched"),
        "experiences": lead.get("experiences") or [],
        "education": lead.get("education") or [],
        "mutual_count": lead.get("mutual_count"),
        "source_account_ids": lead.get("source_account_ids") or [],
        "mutuals": [
            {
                "member_id": m.get("mutual_member_id"),
                "name": m.get("mutual_name"),
                "linkedin_url": m.get("mutual_linkedin_url"),
                "operators": m.get("operators") or [],
                "source_account_ids": m.get("source_account_ids") or [],
            }
            for m in mutuals[:10]
        ],
    }


def build_prompt(criteria: str, lead: dict[str, Any], mutuals: list[dict[str, Any]]) -> str:
    return "\n".join([
        f"Criteria: {criteria}",
        "",
        "Lead JSON:",
        json.dumps(lead_context(lead, mutuals), indent=2, sort_keys=True),
    ])


def call_chat_completion(api_base: str, api_key: str, model: str, system_prompt: str, user_prompt: str, timeout: int) -> dict[str, Any]:
    url = api_base.rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def parse_score(raw_response: dict[str, Any]) -> tuple[float, str, str, float, list[str]]:
    try:
        content = raw_response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"unexpected response shape: {raw_response}") from exc
    parsed = json.loads(content)
    try:
        score = float(parsed.get("score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))
    verdict = str(parsed.get("verdict") or ("include" if score >= DEFAULT_THRESHOLD else "exclude")).lower()
    if verdict not in {"include", "exclude"}:
        verdict = "include" if score >= DEFAULT_THRESHOLD else "exclude"
    reason = str(parsed.get("reason") or "")[:500]
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    traits = parsed.get("matched_traits") or []
    if not isinstance(traits, list):
        traits = []
    return score, verdict, reason, confidence, [str(t) for t in traits[:10]]


@dataclass
class ScoreItem:
    position: int
    lead: dict[str, Any]
    mutuals: list[dict[str, Any]]

    @property
    def member_id(self) -> str:
        return str(self.lead.get("member_id") or self.position)


@dataclass
class ScoreResult:
    member_id: str
    score: float
    verdict: str
    reason: str
    confidence: float
    matched_traits: list[str]
    elapsed_ms: int
    lead: dict[str, Any]
    mutuals: list[dict[str, Any]]
    error: Optional[str] = None
    prompt: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "member_id": self.member_id,
            "score": self.score,
            "verdict": self.verdict,
            "reason": self.reason,
            "confidence": self.confidence,
            "matched_traits": self.matched_traits,
            "elapsed_ms": self.elapsed_ms,
            "lead": self.lead,
            "mutuals": self.mutuals,
            "error": self.error,
            "prompt": self.prompt,
        }


async def score_one(
    item: ScoreItem,
    *,
    criteria: str,
    api_base: str,
    api_key: str,
    model: str,
    semaphore: asyncio.Semaphore,
    executor: concurrent.futures.ThreadPoolExecutor,
    timeout: int,
    max_retries: int,
    include_prompt: bool,
) -> ScoreResult:
    prompt = build_prompt(criteria, item.lead, item.mutuals)
    started = time.monotonic()
    error = None
    score = 0.0
    verdict = "exclude"
    reason = ""
    confidence = 0.0
    matched_traits: list[str] = []
    async with semaphore:
        loop = asyncio.get_running_loop()
        for attempt in range(max_retries + 1):
            try:
                raw = await loop.run_in_executor(
                    executor,
                    call_chat_completion,
                    api_base,
                    api_key,
                    model,
                    SYSTEM_PROMPT,
                    prompt,
                    timeout,
                )
                score, verdict, reason, confidence, matched_traits = parse_score(raw)
                error = None
                break
            except urllib.error.HTTPError as exc:
                retryable = exc.code in {429, 502, 503, 504}
                error = f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:500]}"
                if not retryable or attempt >= max_retries:
                    break
                await asyncio.sleep(min(8.0, 0.5 * (2 ** attempt)))
            except Exception as exc:
                error = str(exc)
                if attempt >= max_retries:
                    break
                await asyncio.sleep(min(8.0, 0.5 * (2 ** attempt)))
    return ScoreResult(
        member_id=item.member_id,
        score=score,
        verdict=verdict,
        reason=reason,
        confidence=confidence,
        matched_traits=matched_traits,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        lead=item.lead,
        mutuals=item.mutuals,
        error=error,
        prompt=prompt if include_prompt else None,
    )


async def score_all(items: list[ScoreItem], **kwargs: Any) -> list[ScoreResult]:
    concurrency = kwargs.pop("concurrency")
    semaphore = asyncio.Semaphore(concurrency)
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        tasks = [score_one(item, semaphore=semaphore, executor=executor, **kwargs) for item in items]
        return await asyncio.gather(*tasks)


def load_items(state_path: Path, max_leads: Optional[int]) -> tuple[dict[str, Any], list[ScoreItem]]:
    state = read_json(state_path)
    files = state.get("files") or {}
    leads_path = Path(str(files.get("leads_jsonl") or ""))
    mutuals_path = Path(str(files.get("mutuals_jsonl") or ""))
    if not leads_path.exists():
        raise RuntimeError(f"leads_jsonl missing: {leads_path}")
    leads = read_jsonl(leads_path)
    mutual_rows = read_jsonl(mutuals_path) if mutuals_path.exists() else []
    mutuals_by_lead: dict[str, list[dict[str, Any]]] = {}
    for mutual in mutual_rows:
        mutuals_by_lead.setdefault(str(mutual.get("lead_member_id") or ""), []).append(mutual)
    items = [
        ScoreItem(position=i, lead=lead, mutuals=mutuals_by_lead.get(str(lead.get("member_id") or ""), []))
        for i, lead in enumerate(leads)
        if lead.get("member_id")
    ]
    if max_leads:
        items = items[:max_leads]
    return state, items


def output_dir(state_path: Path, state: dict[str, Any], criteria: str, explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit)
    files = state.get("files") or {}
    manifest = files.get("manifest")
    base = Path(str(manifest)).parent if manifest else state_path.parent
    return base / "scores" / slugify(criteria)


def result_row(rank: int, result: ScoreResult) -> dict[str, Any]:
    lead = result.lead
    top_mutuals = [
        {
            "name": m.get("mutual_name"),
            "linkedin_url": m.get("mutual_linkedin_url"),
            "operators": m.get("operators") or [],
        }
        for m in result.mutuals[:5]
    ]
    return {
        "rank": rank,
        "member_id": result.member_id,
        "name": lead.get("name"),
        "title": lead.get("title"),
        "company": lead.get("company"),
        "location": lead.get("location"),
        "linkedin_url": lead.get("linkedin_url"),
        "score": result.score,
        "confidence": result.confidence,
        "verdict": result.verdict,
        "reason": result.reason,
        "matched_traits_json": json.dumps(result.matched_traits, sort_keys=True),
        "mutual_count": lead.get("mutual_count") or len(result.mutuals),
        "top_mutuals_json": json.dumps(top_mutuals, sort_keys=True),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Score local Sales Nav leads against criteria")
    parser.add_argument("--state", required=True)
    parser.add_argument("--criteria", required=True)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"))
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--max-leads", type=int)
    parser.add_argument("--out-dir")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-prompt", action="store_true")
    parser.add_argument("--dump-debug", action="store_true")
    args = parser.parse_args()

    state_path = Path(args.state)
    try:
        state, items = load_items(state_path, args.max_leads)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not items:
        print("error: no leads to score", file=sys.stderr)
        return 2
    if args.dry_run:
        print(json.dumps({
            "state": str(state_path),
            "criteria": args.criteria,
            "lead_count": len(items),
            "threshold": args.threshold,
            "sample_member_ids": [item.member_id for item in items[:10]],
        }, indent=2, sort_keys=True))
        return 0
    if not args.api_key:
        print("error: --api-key or OPENAI_API_KEY required", file=sys.stderr)
        return 2

    started = time.monotonic()
    results = asyncio.run(score_all(
        items,
        criteria=args.criteria,
        api_base=args.api_base,
        api_key=args.api_key,
        model=args.model,
        concurrency=args.concurrency,
        timeout=args.timeout,
        max_retries=args.max_retries,
        include_prompt=args.include_prompt,
    ))
    matches = [r for r in results if r.error is None and r.verdict == "include" and r.score >= args.threshold]
    matches.sort(key=lambda r: r.score, reverse=True)
    out_dir = output_dir(state_path, state, args.criteria, args.out_dir)
    rows = [result_row(i + 1, result) for i, result in enumerate(matches)]
    matches_jsonl = out_dir / "matches.jsonl"
    matches_csv = out_dir / "matches.csv"
    manifest = out_dir / "manifest.json"
    write_jsonl(matches_jsonl, [r.to_dict() for r in matches])
    write_csv(matches_csv, rows)
    if args.dump_debug:
        write_jsonl(out_dir / "raw_scores.jsonl", [r.to_dict() for r in results])
    summary = {
        "state": str(state_path),
        "criteria": args.criteria,
        "threshold": args.threshold,
        "lead_count": len(items),
        "scored_count": len(results),
        "match_count": len(matches),
        "failed_count": sum(1 for r in results if r.error),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "outputs": {
            "matches_jsonl": str(matches_jsonl),
            "matches_csv": str(matches_csv),
            "manifest": str(manifest),
        },
    }
    write_json(manifest, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["failed_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
