#!/usr/bin/env python3
"""LLM ENRICH/SKIP review of message contacts via OpenRouter. Stdlib-only.

Subcommands:
    review     Send unmatched/suggested named contacts to an LLM in batches
               and update the `skip` column based on ENRICH/SKIP verdicts.
    estimate   Estimate cost without making API calls.

Privacy contract:

- Only `name`, `source`, `message_count`, last-contact recency, group flags,
  and group names are sent. No phone numbers, no message content.
- The `skip` column is the only field updated in the contacts CSV.
- A reviews JSONL artifact is written so verdicts can be audited.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CSV_HEADERS = [
    "phone",
    "name",
    "source",
    "is_in_group_chats",
    "group_names",
    "message_count",
    "last_message",
    "skip",
    "match_status",
    "matched_person_id",
    "matched_name",
    "matched_linkedin_url",
    "match_confidence",
    "match_method",
    "match_reason",
]


OPENROUTER_BASE = os.environ.get("POWERPACKS_OPENROUTER_BASE", "https://openrouter.ai/api/v1")
DEFAULT_MODEL = os.environ.get("POWERPACKS_LLM_REVIEW_MODEL", "anthropic/claude-sonnet-4-6")
BATCH_SIZE = 40
DEFAULT_REVIEW_STATUSES = {"unmatched", "suggested", ""}


# Pricing per 1M tokens in USD. Coarse estimates good enough for cost preview.
MODEL_PRICING = {
    "anthropic/claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "anthropic/claude-haiku-4-5": {"input": 0.80, "output": 4.00},
    "openai/gpt-4.1": {"input": 2.00, "output": 8.00},
    "openai/gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "openai/gpt-4.1-nano": {"input": 0.10, "output": 0.40},
}


REVIEW_PROMPT = """\
You are evaluating phone contacts to determine which ones represent real \
professional relationships worth looking up on LinkedIn.

For each contact, decide: ENRICH or SKIP.

Consider these factors (in priority order):
- **Name quality & notability**: Use your training data and world knowledge \
to actively identify whether a name matches or resembles known public figures, \
business leaders, prominent families, or well-known professionals. If you \
recognize the name or surname from business, tech, politics, entertainment, \
finance, or any professional domain — say so in the reason and ENRICH. Even \
partial recognition counts — if the name *could* belong to someone notable, \
ENRICH and explain the possible association. The user's phone contacts are \
already filtered to people they actually know.
- **Message volume**: Higher message counts suggest a real relationship.
- **Recency**: Recently contacted people are more valuable, but a recognizable \
full name can override low recency.
- **Skip patterns**: Skip entries that are:
  - Service providers, businesses, or roles rather than people \
(e.g., "Plumber", "HVAC", "Vet", "Groomer")
  - Clearly placeholder, coded, or non-standard naming patterns that wouldn't \
map to a real LinkedIn profile (e.g., numbered labels, pet names, inside jokes, \
private aliases for informal relationships)
  - Just a single first name with no last name AND zero message count \
(impossible to look up)
- **Group chat only**: If someone ONLY appears in group chats with low \
individual message count, they're less valuable. Named groups can still add \
useful context.

Be optimistic — these are real phone contacts, not random leads. When in doubt \
about a full name, ALWAYS lean ENRICH. Only SKIP names that clearly cannot map \
to a LinkedIn profile.

Contacts to evaluate:
{contacts_json}

Respond with a JSON object containing a "results" array, one entry per contact, \
in the same order:
{{"results": [{{"idx": 0, "name": "...", "verdict": "ENRICH" or "SKIP", \
"reason": "15 words max explaining why"}}]}}

Return ONLY the JSON object, no other text."""


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


def format_recency(last_message: str) -> str:
    if not last_message:
        return "unknown"
    try:
        dt = datetime.fromisoformat(last_message.replace("Z", "+00:00"))
        days_ago = (datetime.now(timezone.utc) - dt).days
        if days_ago == 0:
            return "today"
        if days_ago == 1:
            return "yesterday"
        if days_ago < 30:
            return f"{days_ago} days ago"
        if days_ago < 365:
            return f"{days_ago // 30} months ago"
        return f"{days_ago // 365}y ago"
    except (ValueError, TypeError):
        return "unknown"


# ---------------------------------------------------------------------------
# Contact loading + filtering
# ---------------------------------------------------------------------------

def load_contacts_for_review(
    csv_path: Path, *, include_matched: bool, include_skipped: bool
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not csv_path.exists():
        raise SystemExit(f"contacts CSV not found: {csv_path}")
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            if not include_skipped and (row.get("skip") or "").strip().lower() in {"yes", "true", "1"}:
                continue
            match_status = (row.get("match_status") or "").strip().lower()
            matched_person_id = (row.get("matched_person_id") or "").strip()
            effective = match_status or ""
            if matched_person_id:
                effective = "matched"
            if not include_matched and effective not in DEFAULT_REVIEW_STATUSES:
                continue
            out.append({
                "phone": row.get("phone", ""),
                "name": name,
                "source": row.get("source", ""),
                "is_in_group_chats": row.get("is_in_group_chats", "false"),
                "group_names": row.get("group_names", ""),
                "message_count": row.get("message_count", "") or "0",
                "last_message": row.get("last_message", ""),
                "match_status": effective or "unmatched",
                "matched_person_id": matched_person_id,
            })
    return out


def build_batch_payload(batch: list[dict[str, str]]) -> list[dict[str, Any]]:
    payload = []
    for idx, c in enumerate(batch):
        try:
            msg_count = int(c.get("message_count") or 0)
        except ValueError:
            msg_count = 0
        payload.append({
            "idx": idx,
            "name": c["name"],
            "source": c.get("source", ""),
            "message_count": msg_count,
            "last_contacted": format_recency(c.get("last_message", "")),
            "in_group_chats": c.get("is_in_group_chats", "false"),
            "group_names": c.get("group_names", ""),
        })
    return payload


# ---------------------------------------------------------------------------
# OpenRouter call
# ---------------------------------------------------------------------------

def call_openrouter(
    api_key: str, contacts_json: str, model: str, *, timeout: int = 120
) -> tuple[list[dict[str, Any]], int, int, str | None]:
    """Return (results, prompt_tokens, completion_tokens, error_or_None)."""
    prompt = REVIEW_PROMPT.format(contacts_json=contacts_json)
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{OPENROUTER_BASE}/chat/completions",
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
        raw = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return [], 0, 0, f"HTTP {exc.code}: {raw[:300]}"
    except urllib.error.URLError as exc:
        return [], 0, 0, f"network: {exc.reason}"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], 0, 0, f"non-json response: {exc}"

    usage = data.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)

    try:
        content = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        return [], prompt_tokens, completion_tokens, f"missing choices: {exc}"

    # Strip markdown fences models occasionally add despite response_format.
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
        return [], prompt_tokens, completion_tokens, f"json parse: {exc}"

    if isinstance(parsed, dict):
        for key in ("results", "contacts", "evaluations", "data"):
            value = parsed.get(key)
            if isinstance(value, list):
                return value, prompt_tokens, completion_tokens, None
        for value in parsed.values():
            if isinstance(value, list):
                return value, prompt_tokens, completion_tokens, None
    elif isinstance(parsed, list):
        return parsed, prompt_tokens, completion_tokens, None

    return [], prompt_tokens, completion_tokens, "no results array"


# ---------------------------------------------------------------------------
# Cost estimate
# ---------------------------------------------------------------------------

def estimate_cost(contacts: list[dict[str, str]], model: str) -> dict[str, Any]:
    pricing = MODEL_PRICING.get(model, {"input": 2.0, "output": 8.0})
    total_in = 0
    total_out = 0
    batches = 0
    for i in range(0, len(contacts), BATCH_SIZE):
        batch = contacts[i:i + BATCH_SIZE]
        batches += 1
        payload = build_batch_payload(batch)
        prompt = REVIEW_PROMPT.format(contacts_json=json.dumps(payload, indent=2))
        # ~4 chars per token, ~50 output tokens per contact.
        total_in += len(prompt) // 4
        total_out += len(batch) * 50
    cost = (total_in / 1_000_000) * pricing["input"] + (total_out / 1_000_000) * pricing["output"]
    return {
        "model": model,
        "batches": batches,
        "estimated_input_tokens": total_in,
        "estimated_output_tokens": total_out,
        "estimated_usd": round(cost, 4),
    }


# ---------------------------------------------------------------------------
# CSV update
# ---------------------------------------------------------------------------

def update_csv_with_verdicts(
    csv_path: Path, verdicts: dict[str, str]
) -> dict[str, int]:
    rows: list[dict[str, str]] = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or CSV_HEADERS
        for row in reader:
            phone = row.get("phone", "")
            verdict = verdicts.get(phone)
            if verdict == "SKIP":
                row["skip"] = "yes"
            rows.append(row)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return {
        "skip": sum(1 for v in verdicts.values() if v == "SKIP"),
        "enrich": sum(1 for v in verdicts.values() if v == "ENRICH"),
        "rows": len(rows),
    }


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_estimate(args: argparse.Namespace) -> int:
    contacts = load_contacts_for_review(
        Path(args.input),
        include_matched=args.all,
        include_skipped=args.include_skipped,
    )
    estimate = estimate_cost(contacts, args.model)
    emit({
        "primitive": "llm_review_contacts",
        "command": "estimate",
        "input": str(args.input),
        "candidates": len(contacts),
        "estimate": estimate,
    })
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    api_key = args.api_key or os.environ.get("OPENROUTER_API_KEY")
    if not api_key and not args.dry_run:
        emit({
            "primitive": "llm_review_contacts",
            "command": "review",
            "status": "failed",
            "error": "OPENROUTER_API_KEY not provided (use --api-key or env var)",
        })
        return 1

    contacts = load_contacts_for_review(
        Path(args.input),
        include_matched=args.all,
        include_skipped=args.include_skipped,
    )

    estimate = estimate_cost(contacts, args.model)
    base_manifest = {
        "primitive": "llm_review_contacts",
        "command": "review",
        "started_at": now_iso(),
        "model": args.model,
        "input": str(args.input),
        "candidate_count": len(contacts),
        "include_matched": bool(args.all),
        "include_skipped": bool(args.include_skipped),
        "estimate": estimate,
        "dry_run": bool(args.dry_run),
    }

    if not contacts:
        emit({**base_manifest, "status": "no_candidates"})
        return 0

    if args.dry_run:
        emit({**base_manifest, "status": "dry_run"})
        return 0

    results_path = Path(args.results) if args.results else Path(args.input).with_suffix(
        Path(args.input).suffix + ".llm_review.jsonl"
    )
    manifest_path = Path(args.manifest) if args.manifest else results_path.with_suffix(
        results_path.suffix + ".manifest.json"
    )

    verdicts: dict[str, str] = {}
    reasons: dict[str, str] = {}
    total_in = 0
    total_out = 0
    errors: list[dict[str, Any]] = []
    started = time.time()

    results_path.parent.mkdir(parents=True, exist_ok=True)
    with results_path.open("w", encoding="utf-8") as handle:
        for i in range(0, len(contacts), BATCH_SIZE):
            batch = contacts[i:i + BATCH_SIZE]
            payload = build_batch_payload(batch)
            results, in_tok, out_tok, err = call_openrouter(
                api_key,
                json.dumps(payload, indent=2),
                args.model,
                timeout=args.timeout,
            )
            total_in += in_tok
            total_out += out_tok
            if err:
                errors.append({"batch_index": i // BATCH_SIZE, "error": err})
                continue
            for result in results:
                idx = result.get("idx")
                if not isinstance(idx, int) or not (0 <= idx < len(batch)):
                    continue
                phone = batch[idx]["phone"]
                verdict = (result.get("verdict") or "").strip().upper()
                reason = (result.get("reason") or "").strip()
                if verdict in {"ENRICH", "SKIP"}:
                    verdicts[phone] = verdict
                    reasons[phone] = reason
                    handle.write(json.dumps({
                        "phone": phone,
                        "name": batch[idx]["name"],
                        "verdict": verdict,
                        "reason": reason,
                        "match_status": batch[idx].get("match_status"),
                    }, sort_keys=True) + "\n")

    update_stats = update_csv_with_verdicts(Path(args.input), verdicts)
    pricing = MODEL_PRICING.get(args.model, {"input": 2.0, "output": 8.0})
    cost = round((total_in / 1_000_000) * pricing["input"] + (total_out / 1_000_000) * pricing["output"], 4)
    elapsed_ms = int((time.time() - started) * 1000)

    manifest = {
        **base_manifest,
        "status": "completed" if not errors else "completed_with_errors",
        "elapsed_ms": elapsed_ms,
        "artifacts": {
            "results_jsonl": str(results_path),
            "manifest": str(manifest_path),
        },
        "counts": {
            **update_stats,
            "verdicts": len(verdicts),
            "errors": len(errors),
        },
        "tokens": {"input": total_in, "output": total_out},
        "cost_usd": cost,
        "errors": errors,
    }
    write_json(manifest_path, manifest)
    emit(manifest)
    return 0 if not errors else 2


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM ENRICH/SKIP review of message contacts")
    sub = parser.add_subparsers(dest="command", required=True)

    common = lambda p: (
        p.add_argument("--input", "-f", required=True, help="Path to the contacts CSV"),
        p.add_argument("--model", default=DEFAULT_MODEL),
        p.add_argument("--all", action="store_true",
                       help="Review all named contacts (default: only unmatched/suggested)"),
        p.add_argument("--include-skipped", action="store_true",
                       help="Include rows already marked skip=yes"),
    )

    estimate = sub.add_parser("estimate", help="Estimate cost without making API calls")
    common(estimate)
    estimate.set_defaults(func=cmd_estimate)

    review = sub.add_parser("review", help="Run the LLM review and update the CSV in place")
    common(review)
    review.add_argument("--api-key", help="OpenRouter API key (or set OPENROUTER_API_KEY)")
    review.add_argument("--dry-run", action="store_true",
                        help="Estimate cost only; do not call the API")
    review.add_argument("--timeout", type=int, default=120)
    review.add_argument("--results", help="Path to write the per-contact verdicts JSONL")
    review.add_argument("--manifest", help="Path to write the run manifest JSON")
    review.set_defaults(func=cmd_review)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
