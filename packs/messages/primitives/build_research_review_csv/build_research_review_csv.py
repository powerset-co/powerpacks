#!/usr/bin/env python3
"""Flatten deep-research artifacts into a research-review CSV. Stdlib-only.

Emits a CSV in the exact shape consumed by contact-exporter's research review
TUI (`contact-exporter review --file ...`) and `/v2/messages-research/artifacts`
upload. Columns:

    bucket, handle, full_name, phone_e164, area_code, total_messages,
    message_source, group_names, location_city, location_country,
    top_titles, top_companies, top_title_company_pairs, schools,
    short_reason, identity_risk, signals

Buckets are `confident | medium | review`. The TUI maps these onto its
yes / maybe / no tabs.

Two bucketing modes:

    --bucket-mode heuristic   (default, free, stdlib-only)
    --bucket-mode llm         (OpenRouter, mirrors aleph-mvp's
                               review_phone_research.py SYSTEM_PROMPT)

LLM scoring is cached per handle at `<output-dir>/<handle>/02_review_cache.json`
so re-running is idempotent and incremental.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CSV_FIELDS = [
    "bucket",
    "handle",
    "full_name",
    "phone_e164",
    "area_code",
    "total_messages",
    "message_source",
    "group_names",
    "location_city",
    "location_country",
    "top_titles",
    "top_companies",
    "top_title_company_pairs",
    "schools",
    "short_reason",
    "identity_risk",
    "signals",
]

DEFAULT_RESEARCH_DIR = Path(".powerpacks/messages/research")
DEFAULT_QUEUE_CSV = Path(".powerpacks/messages/research_queue.csv")
DEFAULT_OUTPUT_CSV = Path(".powerpacks/messages/research_review.csv")

# Bucket order for sorting and reporting.
BUCKET_ORDER = {"confident": 0, "medium": 1, "review": 2}
BUCKET_TO_TAB = {"confident": "yes", "medium": "maybe", "review": "no"}


# ---------------------------------------------------------------------------
# LLM scoring (mirrors aleph-mvp/review_phone_research.py SYSTEM_PROMPT)
# ---------------------------------------------------------------------------

DEFAULT_SCORE_MODEL = "openai/gpt-4.1"
SCORE_SYSTEM_PROMPT = """You are reviewing deep-researched contacts for a venture-oriented founder/operator network.

The reviewer is startup and venture oriented. Prefer people who are plausibly high-value to know or stay close to:
- founders, investors, executives, strong operators, technical builders, researchers
- people from top-tier startups, funds, labs, universities, or elite professional tracks
- people with credible wealth, influence, public profile, or unusual network centrality
- people who look directionally important even if the identity match is not fully certain

The reviewer is especially likely to value people who overlap with a high-context Bay Area / New York venture network:
- Stanford, Yale, MIT, Oxford, Harvard, CMU, Berkeley, and similarly elite schools
- San Francisco, Silicon Valley, New York, Los Angeles, London, and other dense startup hubs
- top-tier venture firms, breakout startups, frontier research labs, high-agency operators, and repeat builders
- people who are obviously additive to a founder/investor network even if they are not personally famous

Be discriminating. Do not inflate weak candidates.

Bucket definitions:
- confident: this person is high-signal and likely worth knowing or staying connected to now
- medium: there is some real signal, but identity fit or value is uncertain
- review: likely the wrong person, too low-signal, or too weakly evidenced for prioritization

Important:
- Do not assume the reviewer literally knows the person already.
- Penalize identity ambiguity separately from relevance.
- Keep output terse and concrete.
- Return valid JSON only.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def shared_token(a: str, b: str) -> bool:
    """True when the two name strings share at least one >=3-char alpha token."""
    def toks(s: str) -> set[str]:
        cleaned = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
        return {t for t in cleaned.split() if len(t) >= 3 and t.isalpha()}
    return bool(toks(a) & toks(b))


# ---------------------------------------------------------------------------
# Field flattening from the artifacts
# ---------------------------------------------------------------------------

def positions_summary(research_packet: dict[str, Any], limit: int = 3) -> tuple[list[str], list[str], str]:
    """Return (top_titles, top_companies, top_title_company_pairs as ' | ' joined string)."""
    positions = research_packet.get("positions") or []
    titles: list[str] = []
    companies: list[str] = []
    pairs: list[str] = []
    for pos in positions[:limit]:
        title = (pos.get("title") or "").strip()
        company = (pos.get("company_name") or "").strip()
        if title:
            titles.append(title)
        if company:
            companies.append(company)
        if title and company:
            pairs.append(f"{title} @ {company}")
        elif title:
            pairs.append(title)
        elif company:
            pairs.append(f"@ {company}")
    return titles, companies, " | ".join(pairs)


def schools_summary(research_packet: dict[str, Any], limit: int = 2) -> list[str]:
    out: list[str] = []
    for edu in (research_packet.get("education") or [])[:limit]:
        school = (edu.get("school_name") or "").strip()
        if school:
            out.append(school)
    return out


def flatten_row(
    handle: str,
    queue_row: dict[str, str],
    research_packet: dict[str, Any] | None,
    raw_packet: dict[str, Any] | None,
    bucket_payload: dict[str, Any],
) -> dict[str, str]:
    person = (research_packet or {}).get("person", {}) or {}
    location = (research_packet or {}).get("location", {}) or {}
    titles, companies, pairs = positions_summary(research_packet or {})
    schools = schools_summary(research_packet or {})

    full_name = (person.get("full_name") or "").strip() or queue_row.get("display_name", "")
    return {
        "bucket": bucket_payload.get("bucket", "review"),
        "handle": handle,
        "full_name": full_name,
        "phone_e164": queue_row.get("phone_e164", "") or "",
        "area_code": queue_row.get("area_code", "") or "",
        "total_messages": queue_row.get("total_messages", "") or "0",
        "message_source": queue_row.get("message_source", "") or "",
        "group_names": queue_row.get("group_names", "") or "",
        "location_city": (location.get("city") or "").strip(),
        "location_country": (location.get("country") or "").strip(),
        "top_titles": " | ".join(titles),
        "top_companies": " | ".join(companies),
        "top_title_company_pairs": pairs,
        "schools": " | ".join(schools),
        "short_reason": bucket_payload.get("short_reason", "") or "",
        "identity_risk": bucket_payload.get("identity_risk", "") or "",
        "signals": " | ".join(bucket_payload.get("signals") or []),
    }


# ---------------------------------------------------------------------------
# Heuristic bucketing
# ---------------------------------------------------------------------------

def heuristic_bucket(
    queue_row: dict[str, str],
    research_packet: dict[str, Any] | None,
    raw_packet: dict[str, Any] | None,
) -> dict[str, Any]:
    """Bucket using only the artifacts we already have. No API spend.

    Rules:
      - if no real_name surfaced → review
      - if input first name doesn't share a token with returned name → review (likely wrong person)
      - if has linkedin_url AND name_confidence>=0.85 AND >=1 position → confident
      - elif has linkedin_url AND >=1 position → medium
      - elif has real_name AND (>=1 position OR location surfaced) → medium
      - else review
    """
    person = (research_packet or {}).get("person", {}) or {}
    social = (research_packet or {}).get("social", {}) or {}
    real_name = (person.get("full_name") or "").strip()
    name_conf = float(person.get("confidence") or 0)
    linkedin_url = (social.get("linkedin_url") or "").strip()
    positions = (research_packet or {}).get("positions") or []
    location = (research_packet or {}).get("location", {}) or {}
    has_location = bool((location.get("city") or "").strip() or (location.get("country") or "").strip())

    input_name = queue_row.get("display_name", "") or ""
    signals: list[str] = []
    identity_risk = ""

    if not real_name or real_name.lower() == "unknown":
        return {
            "bucket": "review",
            "short_reason": "No real name surfaced by deep research.",
            "identity_risk": "no_real_name",
            "signals": signals,
        }

    if input_name and not shared_token(input_name, real_name):
        identity_risk = "wrong_person"
        return {
            "bucket": "review",
            "short_reason": f"Returned name '{real_name}' shares no token with input '{input_name}'.",
            "identity_risk": identity_risk,
            "signals": signals,
        }

    if linkedin_url:
        signals.append("linkedin")
    if positions:
        signals.append(f"{len(positions)}_positions")
    if has_location:
        signals.append("location")
    if name_conf >= 0.9:
        signals.append("high_name_confidence")

    if linkedin_url and name_conf >= 0.85 and positions:
        return {
            "bucket": "confident",
            "short_reason": (
                f"{real_name} — {positions[0].get('title','')} @ {positions[0].get('company_name','')}"
                if positions else real_name
            ).strip(" —@"),
            "identity_risk": "" if name_conf >= 0.95 else "low_certainty_name",
            "signals": signals,
        }
    if linkedin_url and positions:
        return {
            "bucket": "medium",
            "short_reason": f"LinkedIn found, lower name confidence ({name_conf:.2f}).",
            "identity_risk": "low_certainty_name" if name_conf < 0.85 else "",
            "signals": signals,
        }
    if positions or has_location:
        return {
            "bucket": "medium",
            "short_reason": f"Real name + {('career' if positions else 'location')} signal but no LinkedIn.",
            "identity_risk": "no_linkedin",
            "signals": signals,
        }
    return {
        "bucket": "review",
        "short_reason": "Real name returned but no career / LinkedIn / location evidence.",
        "identity_risk": "no_evidence",
        "signals": signals,
    }


# ---------------------------------------------------------------------------
# LLM bucketing (OpenRouter)
# ---------------------------------------------------------------------------

def _openrouter_chat(api_key: str, model: str, messages: list[dict], *, timeout: int = 90) -> tuple[dict[str, Any] | None, str | None]:
    base = os.environ.get("POWERPACKS_OPENROUTER_BASE", "https://openrouter.ai/api/v1")
    body = json.dumps({
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        return None, f"HTTP {exc.code}: {raw[:300]}"
    except urllib.error.URLError as exc:
        return None, f"network: {exc.reason}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"json: {exc}"
    try:
        content = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        return None, "missing choices"
    if content.startswith("```"):
        lines = content.split("\n")
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        content = "\n".join(lines).strip()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        return None, f"content json: {exc}"
    usage = data.get("usage") or {}
    return {"parsed": parsed, "usage": usage}, None


def llm_bucket(
    api_key: str,
    model: str,
    queue_row: dict[str, str],
    research_packet: dict[str, Any] | None,
    raw_packet: dict[str, Any] | None,
) -> dict[str, Any]:
    """Call OpenRouter with the network-fit SYSTEM_PROMPT to bucket the candidate.

    On API failure falls back to heuristic bucketing and stamps `identity_risk`
    with the failure reason so the run never throws.
    """
    person = (research_packet or {}).get("person", {}) or {}
    social = (research_packet or {}).get("social", {}) or {}
    titles, companies, pairs = positions_summary(research_packet or {})
    schools = schools_summary(research_packet or {})
    payload = {
        "input": {
            "display_name": queue_row.get("display_name", ""),
            "phone_e164": queue_row.get("phone_e164", ""),
            "area_code": queue_row.get("area_code", ""),
            "total_messages": queue_row.get("total_messages", "0"),
            "message_source": queue_row.get("message_source", ""),
            "group_names": queue_row.get("group_names", ""),
            "priority_reason": queue_row.get("priority_reason", ""),
        },
        "research": {
            "real_name": person.get("full_name", ""),
            "name_confidence": person.get("confidence"),
            "name_evidence": person.get("notes", ""),
            "linkedin_url": social.get("linkedin_url"),
            "github_url": social.get("github_url"),
            "location_city": (research_packet or {}).get("location", {}).get("city", ""),
            "location_country": (research_packet or {}).get("location", {}).get("country", ""),
            "top_title_company_pairs": pairs,
            "schools": " | ".join(schools),
            "summary": (research_packet or {}).get("summary", {}).get("text", ""),
            "research_notes": (research_packet or {}).get("metadata", {}).get("research_notes", ""),
        },
    }
    user_prompt = (
        "Classify the following deep-researched phone contact into a bucket "
        "(confident | medium | review). Return ONLY a JSON object with keys: "
        "bucket, short_reason, identity_risk, signals (array of short strings). "
        "Keep short_reason under 25 words.\n\n"
        + json.dumps(payload, indent=2, sort_keys=True)
    )
    response, err = _openrouter_chat(api_key, model, [
        {"role": "system", "content": SCORE_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ])
    if err or not response:
        fallback = heuristic_bucket(queue_row, research_packet, raw_packet)
        fallback.setdefault("signals", []).append(f"llm_fallback:{err}")
        fallback["identity_risk"] = fallback.get("identity_risk") or "llm_unavailable"
        return fallback
    parsed = response["parsed"] if isinstance(response, dict) else None
    if not isinstance(parsed, dict):
        return heuristic_bucket(queue_row, research_packet, raw_packet)
    bucket = (parsed.get("bucket") or "").strip().lower()
    if bucket not in BUCKET_ORDER:
        bucket = "review"
    signals = parsed.get("signals") or []
    if not isinstance(signals, list):
        signals = [str(signals)]
    return {
        "bucket": bucket,
        "short_reason": (parsed.get("short_reason") or "").strip(),
        "identity_risk": (parsed.get("identity_risk") or "").strip(),
        "signals": [str(s) for s in signals],
        "_usage": response.get("usage"),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def load_queue(queue_path: Path) -> dict[str, dict[str, str]]:
    if not queue_path.exists():
        return {}
    rows: dict[str, dict[str, str]] = {}
    with queue_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            h = (row.get("handle") or "").strip()
            if h:
                rows[h] = row
    return rows


def cmd_build(args: argparse.Namespace) -> int:
    research_dir = Path(args.research_dir)
    queue_rows = load_queue(Path(args.queue_csv))
    output_csv = Path(args.output_csv)
    manifest_path = Path(args.manifest) if args.manifest else output_csv.with_suffix(output_csv.suffix + ".manifest.json")

    if not research_dir.exists():
        emit({"primitive": "build_research_review_csv", "command": "build", "status": "failed",
              "error": f"research dir does not exist: {research_dir}"})
        return 1

    handle_dirs = sorted(d for d in research_dir.iterdir() if d.is_dir())
    if not handle_dirs:
        emit({"primitive": "build_research_review_csv", "command": "build", "status": "failed",
              "error": f"no per-handle subdirectories in {research_dir}"})
        return 1

    api_key = None
    if args.bucket_mode == "llm":
        api_key = args.api_key or os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            emit({"primitive": "build_research_review_csv", "command": "build", "status": "failed",
                  "error": "OPENROUTER_API_KEY not set (use --api-key or env)"})
            return 1

    rows: list[dict[str, str]] = []
    counts = {
        "handles_seen": 0,
        "missing_research_packet": 0,
        "filtered_no_queue_row": 0,
        "scored_via_cache": 0,
        "scored_via_llm": 0,
        "scored_via_heuristic": 0,
        "llm_errors": 0,
    }
    bucket_counts: dict[str, int] = {bucket: 0 for bucket in BUCKET_ORDER}
    total_input_tokens = 0
    total_output_tokens = 0

    started = time.time()
    for d in handle_dirs:
        handle = d.name
        counts["handles_seen"] += 1
        research_path = d / "01_research_parallel.json"
        raw_path = d / "00_parallel_raw.json"
        cache_path = d / "02_review_cache.json"
        if not research_path.exists():
            counts["missing_research_packet"] += 1
            continue
        try:
            research_packet = read_json(research_path)
        except (json.JSONDecodeError, OSError):
            counts["missing_research_packet"] += 1
            continue
        raw_packet = None
        if raw_path.exists():
            try:
                raw_packet = read_json(raw_path)
            except (json.JSONDecodeError, OSError):
                pass

        queue_row = queue_rows.get(handle)
        if queue_row is None and not args.allow_missing_queue:
            counts["filtered_no_queue_row"] += 1
            continue
        if queue_row is None:
            queue_row = {"handle": handle, "phone_e164": "", "area_code": "",
                         "total_messages": "0", "message_source": "", "group_names": "",
                         "display_name": (research_packet.get("person") or {}).get("full_name", "")}

        if args.bucket_mode == "llm":
            bucket_payload: dict[str, Any] | None = None
            if cache_path.exists() and not args.refresh_cache:
                try:
                    cached = read_json(cache_path)
                    if isinstance(cached, dict) and (cached.get("review") or {}).get("bucket"):
                        bucket_payload = cached["review"]
                        counts["scored_via_cache"] += 1
                except (json.JSONDecodeError, OSError):
                    pass
            if bucket_payload is None:
                bucket_payload = llm_bucket(api_key, args.model, queue_row, research_packet, raw_packet)
                usage = bucket_payload.pop("_usage", None) or {}
                total_input_tokens += int(usage.get("prompt_tokens") or 0)
                total_output_tokens += int(usage.get("completion_tokens") or 0)
                if any(s.startswith("llm_fallback:") for s in (bucket_payload.get("signals") or [])):
                    counts["llm_errors"] += 1
                    counts["scored_via_heuristic"] += 1
                else:
                    counts["scored_via_llm"] += 1
                    write_json(cache_path, {
                        "scored_at": now_iso(),
                        "model": args.model,
                        "review": bucket_payload,
                    })
        else:
            bucket_payload = heuristic_bucket(queue_row, research_packet, raw_packet)
            counts["scored_via_heuristic"] += 1

        flat = flatten_row(handle, queue_row, research_packet, raw_packet, bucket_payload)
        rows.append(flat)
        bucket_counts[flat["bucket"]] = bucket_counts.get(flat["bucket"], 0) + 1

    rows.sort(key=lambda r: (
        BUCKET_ORDER.get(r["bucket"], 99),
        -int(r["total_messages"] or 0),
        (r["full_name"] or "").lower(),
    ))

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as handle_out:
        writer = csv.DictWriter(handle_out, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    cost_usd = 0.0
    if args.bucket_mode == "llm":
        # Same coarse pricing table used by llm_review_contacts.
        pricing = {
            "anthropic/claude-sonnet-4-6": (3.00, 15.00),
            "anthropic/claude-haiku-4-5": (0.80, 4.00),
            "openai/gpt-4.1": (2.00, 8.00),
            "openai/gpt-4.1-mini": (0.40, 1.60),
            "openai/gpt-4.1-nano": (0.10, 0.40),
        }.get(args.model, (2.00, 8.00))
        cost_usd = round((total_input_tokens / 1e6) * pricing[0]
                         + (total_output_tokens / 1e6) * pricing[1], 4)

    manifest = {
        "primitive": "build_research_review_csv",
        "command": "build",
        "status": "ok",
        "bucket_mode": args.bucket_mode,
        "model": args.model if args.bucket_mode == "llm" else None,
        "research_dir": str(research_dir),
        "queue_csv": str(args.queue_csv),
        "output_csv": str(output_csv),
        "manifest_path": str(manifest_path),
        "rows_written": len(rows),
        "elapsed_ms": int((time.time() - started) * 1000),
        "started_at": now_iso(),
        "counts": counts,
        "bucket_counts": bucket_counts,
        "tokens": {"input": total_input_tokens, "output": total_output_tokens},
        "cost_usd": cost_usd,
        "next_steps": {
            "review_locally": (
                f"cd ../powerset-contacts && uv run contact-exporter review --file "
                f"{output_csv}"
            ),
            "upload_back": (
                "cd ../powerset-contacts && uv run contact-exporter research-review "
                f"--upload {output_csv}"
            ),
        },
    }
    write_json(manifest_path, manifest)
    emit(manifest)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a research-review CSV from deep-research artifacts")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="Flatten + bucket research artifacts into a TUI-compatible CSV")
    build.add_argument("--research-dir", type=Path, default=DEFAULT_RESEARCH_DIR,
                       help="Directory containing per-handle research artifacts")
    build.add_argument("--queue-csv", default=str(DEFAULT_QUEUE_CSV),
                       help="research_queue.csv used as input to deep_research_contacts")
    build.add_argument("--output-csv", default=str(DEFAULT_OUTPUT_CSV))
    build.add_argument("--manifest", help="Path to write the run manifest JSON")
    build.add_argument("--bucket-mode", choices=["heuristic", "llm"], default="heuristic")
    build.add_argument("--model", default=DEFAULT_SCORE_MODEL,
                       help="Model used when --bucket-mode llm (OpenRouter slug)")
    build.add_argument("--api-key", help="OpenRouter API key (or set OPENROUTER_API_KEY)")
    build.add_argument("--refresh-cache", action="store_true",
                       help="Ignore cached LLM scores in 02_review_cache.json and re-score")
    build.add_argument("--allow-missing-queue", action="store_true",
                       help="Include handles not present in queue_csv (uses defaults)")
    build.set_defaults(func=cmd_build)
    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
