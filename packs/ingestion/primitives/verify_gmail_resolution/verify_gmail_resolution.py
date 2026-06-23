"""LLM judge: is a resolved LinkedIn profile actually the same person as the email contact?

Joins the email evidence (markers from infer_linkedin_markers) with the resolved
LinkedIn candidate (from resolve_linkedin_queue) and asks an LLM to confirm or
reject the match. A same name is NOT enough -- the judge looks for corroboration
(employer / school / location / role) and especially CONTRADICTIONS that rule the
profile out (the same-name-wrong-person case). The verdict drives a yes/maybe/no
bucket so only confirmed contacts flow into people.csv.

Outputs:
  <out-dir>/verifications.jsonl   one record per contact (verdict + evidence)
  <out-dir>/verifications.csv      flat review table (mirrors the messages review)
  <out-dir>/manifest.json          counts + token/cost totals
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import tiktoken
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from packs.indexing.lib.llm_config import (
    CHAT_MODEL_PRICES_PER_1K_USD,
    DEFAULT_OPENAI_CONCURRENCY,
    api_call_kwargs,
    openai_price_multiplier,
)
from packs.indexing.lib.openai_stream import drain_pool
from packs.indexing.lib.openai_usage_tiers import env_or_profile_int

DEFAULT_MARKERS = Path(".powerpacks/network-import/discover/email-context/markers/markers.jsonl")
DEFAULT_OUT_DIR = Path(".powerpacks/network-import/discover/email-context/verify")
DEFAULT_MODEL = "gpt-5.2"

VERDICTS = ["confirmed", "wrong_person", "needs_review"]
BUCKET = {"confirmed": "yes", "needs_review": "maybe", "wrong_person": "no"}

SYSTEM_PROMPT = (
    "You verify whether a resolved LinkedIn profile is the SAME PERSON as an email "
    "contact. You are given (A) evidence about the contact mined from my own emails "
    "(employer, title, school, location, handles, recent subjects) and (B) the LinkedIn "
    "profile a resolver picked for them (name, headline, company, location).\n\n"
    "A shared name is NOT enough. Decide using corroboration and contradiction:\n"
    "- confirmed: the profile's employer / school / location / role clearly lines up with "
    "the email evidence (people change jobs, so a PAST employer match still counts).\n"
    "- wrong_person: the profile contradicts the evidence (different industry, location, or "
    "career stage that can't be the same person), OR the only thing linking them is the name "
    "with no corroboration.\n"
    "- needs_review: too little evidence either way.\n\n"
    "List exactly which evidence AGREED and which CONTRADICTED. Be skeptical: a confident "
    "resolver pick on a common name with no corroborating evidence is wrong_person, not confirmed."
)

JUDGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": VERDICTS},
        "confidence": {"type": "number", "description": "0..1 confidence in the verdict"},
        "evidence_agreed": {"type": "array", "items": {"type": "string"}},
        "evidence_contradicted": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string", "description": "One-line rationale for the review UI."},
    },
    "required": ["verdict", "confidence", "evidence_agreed", "evidence_contradicted", "reason"],
}


def load_dotenv_upward(start: Path) -> None:
    for directory in [start, *start.parents]:
        env_path = directory / ".env"
        if env_path.is_file():
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
            return


def get_encoder() -> "tiktoken.Encoding":
    try:
        return tiktoken.get_encoding("o200k_base")
    except Exception:
        return tiktoken.get_encoding("cl100k_base")


def maybe_open(path: Path, do_open: bool) -> None:
    if not do_open or sys.platform != "darwin" or not path.exists():
        return
    try:
        subprocess.run(["open", str(path)], check=False)
    except Exception as exc:
        print(f"[verify_gmail_resolution] could not open {path}: {exc}", file=sys.stderr)


def price_for(model: str) -> dict[str, float]:
    return CHAT_MODEL_PRICES_PER_1K_USD.get(model, {"input": 0.00175, "output": 0.01400})


def resolved_profile(cand_cell: str, linkedin_url: str) -> dict[str, Any]:
    """Best resolved candidate (stand-in for the hydrated profile) from resolve_linkedin_queue."""
    try:
        cands = json.loads(cand_cell or "[]")
    except json.JSONDecodeError:
        cands = []
    top = cands[0] if cands and isinstance(cands[0], dict) else {}
    return {
        "linkedin_url": linkedin_url or top.get("linkedin_url", ""),
        "name": top.get("name", ""),
        "headline": top.get("headline", ""),
        "location": top.get("location", ""),
        "match_confidence": top.get("match_confidence"),
        "evidence": top.get("evidence", ""),
    }


def load_join(markers_path: Path, resolutions_path: Path) -> list[dict[str, Any]]:
    markers = {json.loads(l)["email"]: json.loads(l) for l in markers_path.read_text(encoding="utf-8").splitlines() if l.strip()}
    joined: list[dict[str, Any]] = []
    for row in csv.DictReader(resolutions_path.open(encoding="utf-8")):
        email = str(row.get("email", "")).strip().lower()
        prof = resolved_profile(row.get("candidates", ""), row.get("linkedin_url", ""))
        if not email or not prof["linkedin_url"]:
            continue  # nothing resolved -> nothing to verify
        joined.append({"email": email, "marker_rec": markers.get(email, {}), "profile": prof, "full_name": row.get("full_name", "")})
    return joined


def build_user_prompt(item: dict[str, Any]) -> str:
    m = (item.get("marker_rec") or {}).get("markers") or {}
    facts = "; ".join(f"{x['category']}={x['value']}" for x in m.get("markers", []))
    p = item["profile"]
    lines = [
        "(A) EMAIL EVIDENCE about the contact:",
        f"  name: {item.get('full_name') or '(unknown)'}",
        f"  email: {item['email']}",
        f"  linkedin_query: {m.get('linkedin_query', '')}",
        f"  known facts: {facts or '(none)'}",
        "",
        "(B) RESOLVED LinkedIn PROFILE the resolver picked:",
        f"  url: {p['linkedin_url']}",
        f"  name: {p['name']}",
        f"  headline: {p['headline']}",
        f"  location: {p['location']}",
        f"  resolver_confidence: {p['match_confidence']}",
        f"  resolver_evidence: {p['evidence']}",
        "",
        "Is (B) the same person as (A)?",
    ]
    return "\n".join(lines)


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (APITimeoutError, APIConnectionError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return False


async def judge_one(client: AsyncOpenAI, model: str, item: dict[str, Any], encoder, semaphore, max_retries: int) -> dict[str, Any]:
    user_prompt = build_user_prompt(item)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}]
    tok_in = len(encoder.encode(SYSTEM_PROMPT)) + len(encoder.encode(user_prompt))
    base = {
        "email": item["email"], "full_name": item.get("full_name", ""),
        "linkedin_url": item["profile"]["linkedin_url"], "headline": item["profile"]["headline"],
        "resolver_confidence": item["profile"]["match_confidence"],
        "usage": {"tiktoken_input": tok_in, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    kwargs = api_call_kwargs(model)
    async with semaphore:
        attempt = 0
        while True:
            try:
                resp = await client.chat.completions.create(
                    model=model, messages=messages,
                    response_format={"type": "json_schema", "json_schema": {"name": "verdict", "strict": True, "schema": JUDGE_SCHEMA}},
                    **kwargs,
                )
                break
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                if _is_retryable(exc) and attempt <= max_retries:
                    await asyncio.sleep(min(2 ** attempt, 30))
                    continue
                base["judgment"] = {"_error": f"{type(exc).__name__}: {exc}"[:300]}
                return base
    try:
        base["judgment"] = json.loads(resp.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        base["judgment"] = {"_parse_error": True}
    u = resp.usage
    base["usage"].update({
        "prompt_tokens": int(getattr(u, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(u, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(u, "total_tokens", 0) or 0),
    })
    return base


def write_csv(records: list[dict[str, Any]], out_dir: Path) -> Path:
    path = out_dir / "verifications.csv"
    header = ["email", "full_name", "verdict", "bucket", "confidence", "resolver_confidence",
              "linkedin_url", "headline", "evidence_agreed", "evidence_contradicted", "reason"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        for r in records:
            j = r.get("judgment") or {}
            verdict = j.get("verdict", "needs_review")
            w.writerow({
                "email": r["email"], "full_name": r.get("full_name", ""),
                "verdict": verdict, "bucket": BUCKET.get(verdict, "maybe"),
                "confidence": j.get("confidence", ""), "resolver_confidence": r.get("resolver_confidence", ""),
                "linkedin_url": r.get("linkedin_url", ""), "headline": r.get("headline", ""),
                "evidence_agreed": "; ".join(j.get("evidence_agreed", []) or []),
                "evidence_contradicted": "; ".join(j.get("evidence_contradicted", []) or []),
                "reason": j.get("reason", ""),
            })
    return path


async def run_async(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    load_dotenv_upward(Path(__file__).resolve().parent)
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY not found in environment or .env")

    items = load_join(Path(args.markers), Path(args.resolutions))
    only = {e.strip().lower() for e in (args.only_email or []) if e.strip()}
    if only:
        items = [it for it in items if it["email"] in only]
    if args.limit and args.limit > 0:
        items = items[: args.limit]

    encoder = get_encoder()
    concurrency = args.concurrency or env_or_profile_int("POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency", fallback=DEFAULT_OPENAI_CONCURRENCY)
    client = AsyncOpenAI(api_key=api_key, timeout=args.timeout, max_retries=0)
    semaphore = asyncio.Semaphore(max(1, concurrency))

    records: list[dict[str, Any]] = []
    try:
        coros = [judge_one(client, args.model, it, encoder, semaphore, args.max_retries) for it in items]
        await drain_pool(coros, records.append)
    finally:
        await client.close()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "verifications.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    csv_path = write_csv(records, out_dir)

    from collections import Counter
    verdicts = Counter((r.get("judgment") or {}).get("verdict", "error") for r in records)
    prompt_tokens = sum(r["usage"]["prompt_tokens"] for r in records)
    completion_tokens = sum(r["usage"]["completion_tokens"] for r in records)
    price = price_for(args.model); mult = openai_price_multiplier()
    cost = (prompt_tokens / 1000.0) * price["input"] * mult + (completion_tokens / 1000.0) * price["output"] * mult
    manifest = {
        "source": "verify_gmail_resolution", "status": "completed", "model": args.model,
        "people_total": len(records), "verdicts": dict(verdicts),
        "estimated_cost_usd": round(cost, 4),
        "output_csv": str(csv_path), "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    maybe_open(csv_path, args.open)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="LLM judge: verify a resolved LinkedIn profile matches the email contact.")
    p.add_argument("--markers", default=str(DEFAULT_MARKERS), help="markers.jsonl (email evidence)")
    p.add_argument("--resolutions", required=True, help="linkedin_resolutions.csv (resolved candidates)")
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--only-email", action="append", default=[], help="Verify only these emails (repeatable)")
    p.add_argument("--limit", type=int, default=0, help="Cap contacts (0 = all)")
    p.add_argument("--concurrency", type=int, default=0)
    p.add_argument("--max-retries", type=int, default=4)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--open", action="store_true", help="Open verifications.csv when done (macOS)")
    return p


def main(argv: list[str] | None = None) -> int:
    print(json.dumps(asyncio.run(run_async(build_parser().parse_args(argv))), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
