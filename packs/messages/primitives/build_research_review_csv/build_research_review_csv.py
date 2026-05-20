#!/usr/bin/env python3
"""Flatten deep-research artifacts into a research-review CSV. Stdlib-only.

Emits a CSV in the exact shape consumed by contact-exporter's research review
TUI (`contact-exporter review --file ...`) and `/v2/messages-research/artifacts`
upload. Columns:

    bucket, handle, full_name, phone_e164, area_code, total_messages,
    imessage_message_count, whatsapp_message_count, message_source,
    last_message, imessage_last_message, whatsapp_last_message, group_names,
    location_city, location_country,
    top_titles, top_companies, top_title_company_pairs, schools,
    short_reason, identity_risk, signals, retarget_hint, exclude,
    enrich_decision

Buckets are `yes | maybe | no`. Legacy cached buckets are normalized as
`confident -> yes`, `medium|review -> maybe`.

By default, each researched contact is scored by the network-review LLM
(OpenRouter, mirrors aleph-mvp's review_phone_research.py SYSTEM_PROMPT).
The score is cached per handle at
`<output-dir>/<handle>/03_network_review.json` so re-running is idempotent and
incremental.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE lines into os.environ without overriding env."""
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value


load_dotenv(Path(__file__).resolve().parents[4] / ".env")


CSV_FIELDS = [
    "bucket",
    "handle",
    "full_name",
    "phone_e164",
    "area_code",
    "total_messages",
    "imessage_message_count",
    "whatsapp_message_count",
    "message_source",
    "last_message",
    "imessage_last_message",
    "whatsapp_last_message",
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
    "retarget_hint",
    "exclude",
    "enrich_decision",
    "in_network",
    "network_match_status",
    "network_person_id",
    "network_name",
    "network_linkedin_url",
    "network_match_confidence",
    "network_match_method",
    "network_match_reason",
    "review_source",
]

DEFAULT_RESEARCH_DIR = Path(".powerpacks/messages/research")
DEFAULT_QUEUE_CSV = Path(".powerpacks/messages/research_queue.csv")
DEFAULT_OUTPUT_CSV = Path(".powerpacks/messages/research_review.csv")
NETWORK_REVIEW_CACHE_NAME = "03_network_review.json"

# Canonical bucket order for sorting and reporting. Older 03_network_review.json
# files used confident/medium/review; keep reading them, but write yes/maybe/no.
BUCKET_ORDER = {"yes": 0, "maybe": 1, "no": 2}
BUCKET_ALIASES = {
    "yes": "yes",
    "confident": "yes",
    "maybe": "maybe",
    "medium": "maybe",
    "review": "maybe",
    "no": "no",
}
REVIEW_DECISION_FIELDS = ("exclude", "enrich_decision", "retarget_hint")


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

Treat these as strong positive signals that usually belong in yes when the name, geography, and public profile are plausible:
- sovereign wealth funds, royal/ruling-family business figures, business magnates, major investors, and public company or fund executives
- family offices, large asset allocators, high-profile founders, celebrities building companies, and widely recognized public figures
- elite schools or elite professional tracks when paired with credible career signal

Be discriminating. Do not inflate weak candidates.

Bucket definitions:
- yes: this person is high-signal and likely worth knowing or staying connected to now
- maybe: there is some real signal, but identity fit or value is uncertain enough to need human review
- no: likely the wrong person, ordinary/low-signal, too weakly evidenced, or not worth prioritizing

Important:
- Do not assume the reviewer literally knows the person already.
- Penalize identity ambiguity separately from relevance.
- Do not require public phone-number proof for yes when the display name, country/location, group context, and public profile strongly align.
- Use maybe for high-value profiles only when there is material identity ambiguity, such as multiple plausible same-name people or weak name/geography fit.
- Use no for ordinary service-provider or transactional contacts unless there is independent high-signal evidence: travel agents, drivers, concierges, recruiters with no special signal, local vendors, support reps, generic consultants, and similar random contacts.
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


def linkedin_public_identifier(url: str | None) -> str:
    text = (url or "").strip().split("?", 1)[0].rstrip("/")
    if not text:
        return ""
    if "/in/" in text:
        return text.rsplit("/in/", 1)[-1].strip("/")
    return text.rsplit("/", 1)[-1].strip("/")


def normalize_bucket(value: Any) -> str | None:
    return BUCKET_ALIASES.get(str(value or "").strip().lower())


def normalize_review_payload(review: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(review, dict):
        return None
    bucket = normalize_bucket(review.get("bucket"))
    if bucket not in BUCKET_ORDER:
        return None
    signals = review.get("signals") or []
    if not isinstance(signals, list):
        signals = [str(signals)]
    return {
        "bucket": bucket,
        "short_reason": (review.get("short_reason") or "").strip(),
        "identity_risk": (review.get("identity_risk") or "").strip(),
        "signals": [str(s) for s in signals if str(s)],
    }


def queue_message_source(queue_row: dict[str, str]) -> str:
    """Return the concrete message transport(s) for a queue row.

    `source_channel` in the research queue is the broad upstream enrichment
    channel (`phone` for message imports). The per-message transport lives in
    `message_source` (for example `imessage`, `whatsapp`, or
    `imessage,whatsapp`). Some older/intermediate CSVs may still call that
    column `source`, so keep a fallback for those while preserving `phone` as a
    final legacy default.
    """
    return (
        (queue_row.get("message_source") or "").strip()
        or (queue_row.get("source") or "").strip()
        or (queue_row.get("source_channel") or "").strip()
    )


def network_review_payload(
    handle: str,
    queue_row: dict[str, str],
    research_packet: dict[str, Any],
    model: str,
    review: dict[str, Any],
) -> dict[str, Any]:
    person = research_packet.get("person") or {}
    social = research_packet.get("social") or {}
    full_name = (person.get("full_name") or "").strip() or queue_row.get("display_name", "")
    linkedin_url = (social.get("linkedin_url") or "").strip()
    return {
        "handle": handle,
        "public_identifier": linkedin_public_identifier(linkedin_url),
        "full_name": full_name,
        "linkedin_url": linkedin_url,
        "source_channel": queue_message_source(queue_row) or "phone",
        "message_source": queue_message_source(queue_row),
        "model": model,
        "review": review,
    }


def write_network_review_cache(
    path: Path,
    handle: str,
    queue_row: dict[str, str],
    research_packet: dict[str, Any],
    model: str,
    review: dict[str, Any],
) -> None:
    write_json(path, network_review_payload(handle, queue_row, research_packet, model, review))


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
        "bucket": bucket_payload.get("bucket", "maybe"),
        "handle": handle,
        "full_name": full_name,
        "phone_e164": queue_row.get("phone_e164", "") or "",
        "area_code": queue_row.get("area_code", "") or "",
        "total_messages": queue_row.get("total_messages", "") or "0",
        "imessage_message_count": queue_row.get("imessage_message_count", "") or "",
        "whatsapp_message_count": queue_row.get("whatsapp_message_count", "") or "",
        "message_source": queue_message_source(queue_row),
        "last_message": queue_row.get("last_message", "") or "",
        "imessage_last_message": queue_row.get("imessage_last_message", "") or "",
        "whatsapp_last_message": queue_row.get("whatsapp_last_message", "") or "",
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
        "retarget_hint": queue_row.get("retarget_hint", "") or "",
        "exclude": "",
        "enrich_decision": "",
        "in_network": "",
        "network_match_status": "",
        "network_person_id": "",
        "network_name": "",
        "network_linkedin_url": "",
        "network_match_confidence": "",
        "network_match_method": "",
        "network_match_reason": "",
        "review_source": "llm_network_review",
    }


# ---------------------------------------------------------------------------
# LLM bucketing (OpenRouter or direct OpenAI)
# ---------------------------------------------------------------------------


class NetworkReviewError(RuntimeError):
    pass


def llm_provider_and_key(args: argparse.Namespace) -> tuple[str, str | None, str]:
    if args.api_key:
        return "openrouter", args.api_key, args.model
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter", os.environ.get("OPENROUTER_API_KEY"), args.model
    if os.environ.get("OPENAI_API_KEY"):
        model = args.model.removeprefix("openai/")
        return "openai", os.environ.get("OPENAI_API_KEY"), model
    return "openrouter", None, args.model


def _chat_completion(
    api_key: str,
    model: str,
    messages: list[dict],
    *,
    provider: str,
    timeout: int = 120,
) -> tuple[dict[str, Any] | None, str | None]:
    if provider == "openai":
        base = os.environ.get("POWERPACKS_OPENAI_BASE", os.environ.get("OPENAI_API_BASE", "https://api.openai.com"))
    else:
        base = os.environ.get("POWERPACKS_OPENROUTER_BASE", "https://openrouter.ai/api/v1")
    body = json.dumps({
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base.rstrip('/')}/v1/chat/completions" if provider == "openai" and not base.rstrip('/').endswith('/v1') else f"{base.rstrip('/')}/chat/completions",
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
        retry_after = exc.headers.get("retry-after") if exc.headers else None
        prefix = f"HTTP {exc.code}"
        if retry_after:
            prefix += f" retry_after={retry_after}"
        return None, f"{prefix}: {raw[:300]}"
    except TimeoutError as exc:
        return None, f"timeout: {exc}"
    except socket.timeout as exc:
        return None, f"timeout: {exc}"
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


def _retry_after_seconds(error: str | None, attempt: int) -> float:
    if error:
        match = re.search(r"retry_after=([0-9.]+)", error)
        if match:
            try:
                return max(0.0, float(match.group(1)))
            except ValueError:
                pass
    return min(30.0, float(2 ** attempt))


def _chat_completion_with_retries(
    api_key: str,
    model: str,
    messages: list[dict],
    *,
    provider: str,
    timeout: int = 120,
    max_retries: int = 3,
) -> tuple[dict[str, Any] | None, str | None]:
    last_response: dict[str, Any] | None = None
    last_error: str | None = None
    for attempt in range(max_retries + 1):
        response, err = _chat_completion(api_key, model, messages, provider=provider, timeout=timeout)
        last_response = response
        last_error = err
        if not err:
            return response, None
        status_match = re.match(r"HTTP (\d+)", err or "")
        status_code = int(status_match.group(1)) if status_match else 0
        retryable = (
            status_code == 429
            or 500 <= status_code <= 599
            or "rate" in (err or "").lower()
            or "timeout" in (err or "").lower()
            or "network" in (err or "").lower()
        )
        if not retryable or attempt >= max_retries:
            return last_response, last_error
        time.sleep(_retry_after_seconds(err, attempt))
    return last_response, last_error


def llm_bucket(
    api_key: str,
    model: str,
    queue_row: dict[str, str],
    research_packet: dict[str, Any] | None,
    *,
    provider: str = "openrouter",
) -> dict[str, Any]:
    """Call an LLM with the network-fit SYSTEM_PROMPT to bucket the candidate.

    There is no heuristic mode. If the LLM cannot return a valid bucket, fail
    the build so the review CSV does not silently contain low-quality guesses.
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
            "imessage_message_count": queue_row.get("imessage_message_count", ""),
            "whatsapp_message_count": queue_row.get("whatsapp_message_count", ""),
            "message_source": queue_message_source(queue_row),
            "last_message": queue_row.get("last_message", ""),
            "imessage_last_message": queue_row.get("imessage_last_message", ""),
            "whatsapp_last_message": queue_row.get("whatsapp_last_message", ""),
            "group_names": queue_row.get("group_names", ""),
            "retarget_hint": queue_row.get("retarget_hint", ""),
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
        "(yes | maybe | no). Return ONLY a JSON object with keys: "
        "bucket, short_reason, identity_risk, signals (array of short strings). "
        "Keep short_reason under 25 words.\n\n"
        + json.dumps(payload, indent=2, sort_keys=True)
    )
    response, err = _chat_completion_with_retries(
        api_key,
        model,
        [
            {"role": "system", "content": SCORE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        provider=provider,
    )
    if err or not response:
        raise NetworkReviewError(f"network review failed: {err or 'empty response'}")
    parsed = response["parsed"] if isinstance(response, dict) else None
    if not isinstance(parsed, dict):
        raise NetworkReviewError("network review response missing JSON object")
    bucket = normalize_bucket(parsed.get("bucket"))
    if bucket not in BUCKET_ORDER:
        raw_bucket = (parsed.get("bucket") or "").strip().lower()
        raise NetworkReviewError(f"network review returned invalid bucket: {raw_bucket or '<empty>'}")
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


def network_review_bucket(cache: dict[str, Any]) -> dict[str, Any] | None:
    """Return the cached network-review bucket payload, if present."""
    review = cache.get("review") if isinstance(cache, dict) else None
    return normalize_review_payload(review)


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


def load_previous_review_decisions(path: Path | None) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]], int]:
    """Load human review state from an older CSV without trusting stale buckets."""
    if path is None or not path.exists():
        return {}, {}, 0
    by_handle: dict[str, dict[str, str]] = {}
    by_phone: dict[str, dict[str, str]] = {}
    carried = 0
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            decision = {field: (row.get(field) or "").strip() for field in REVIEW_DECISION_FIELDS}
            bucket = (row.get("bucket") or "").strip().lower()
            if not decision.get("exclude") and bucket == "yes":
                decision["exclude"] = "no"
            elif not decision.get("exclude") and bucket == "no":
                decision["exclude"] = "yes"
            if not any(decision.values()):
                continue
            carried += 1
            row_handle = (row.get("handle") or "").strip()
            phone = (row.get("phone_e164") or "").strip()
            if row_handle:
                by_handle[row_handle] = decision
            if phone:
                by_phone[phone] = decision
    return by_handle, by_phone, carried


def apply_previous_review_decision(
    row: dict[str, str],
    by_handle: dict[str, dict[str, str]],
    by_phone: dict[str, dict[str, str]],
) -> bool:
    decision = by_handle.get(row.get("handle", "")) or by_phone.get(row.get("phone_e164", ""))
    if not decision:
        return False
    applied = False
    for field in REVIEW_DECISION_FIELDS:
        value = decision.get(field, "")
        if value:
            row[field] = value
            applied = True
    return applied


def cmd_build(args: argparse.Namespace) -> int:
    research_dir = Path(args.research_dir)
    queue_rows = load_queue(Path(args.queue_csv))
    output_csv = Path(args.output_csv)
    manifest_path = Path(args.manifest) if args.manifest else output_csv.with_suffix(output_csv.suffix + ".manifest.json")
    previous_csv_arg = getattr(args, "previous_csv", None)
    previous_csv = Path(previous_csv_arg) if previous_csv_arg else None
    previous_by_handle, previous_by_phone, previous_decision_rows = load_previous_review_decisions(previous_csv)

    if not research_dir.exists():
        emit({"primitive": "build_research_review_csv", "command": "build", "status": "failed",
              "error": f"research dir does not exist: {research_dir}"})
        return 1

    handle_dirs = sorted(d for d in research_dir.iterdir() if d.is_dir())
    if not handle_dirs:
        emit({"primitive": "build_research_review_csv", "command": "build", "status": "failed",
              "error": f"no per-handle subdirectories in {research_dir}"})
        return 1

    llm_provider, api_key, llm_model = llm_provider_and_key(args)

    rows: list[dict[str, str]] = []
    counts = {
        "handles_seen": 0,
        "missing_research_packet": 0,
        "filtered_no_queue_row": 0,
        "scored_via_network_review": 0,
        "scored_via_llm": 0,
        "network_review_written": 0,
        "llm_errors": 0,
        "previous_review_decision_rows": previous_decision_rows,
        "previous_review_decisions_applied": 0,
    }
    bucket_counts: dict[str, int] = {bucket: 0 for bucket in BUCKET_ORDER}
    total_input_tokens = 0
    total_output_tokens = 0

    started = time.time()
    last_progress = 0.0
    total_handles = len(handle_dirs)

    def progress(index: int, *, force: bool = False) -> None:
        nonlocal last_progress
        now = time.time()
        if force or now - last_progress >= 30 or index == total_handles:
            action = "started" if index == 0 else "completed"
            print(
                "[build_research_review_csv] "
                f"{action} {index}/{total_handles} handles; rows={len(rows)}; "
                f"cache={counts['scored_via_network_review']}; llm={counts['scored_via_llm']}",
                file=sys.stderr,
                flush=True,
            )
            last_progress = now

    if total_handles:
        progress(0, force=True)
    for index, d in enumerate(handle_dirs, start=1):
        handle = d.name
        counts["handles_seen"] += 1
        research_path = d / "01_research_parallel.json"
        raw_path = d / "00_parallel_raw.json"
        network_review_path = d / NETWORK_REVIEW_CACHE_NAME
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

        bucket_payload: dict[str, Any] | None = None
        if network_review_path.exists() and not args.refresh_cache:
            try:
                bucket_payload = network_review_bucket(read_json(network_review_path))
                if bucket_payload is not None:
                    counts["scored_via_network_review"] += 1
            except (json.JSONDecodeError, OSError):
                pass

        if bucket_payload is not None:
            pass
        else:
            if not api_key:
                emit({"primitive": "build_research_review_csv", "command": "build", "status": "failed",
                      "error": "OPENROUTER_API_KEY or OPENAI_API_KEY not set (pass --api-key or add one to the repo .env)"})
                return 1
            try:
                bucket_payload = llm_bucket(api_key, llm_model, queue_row, research_packet, provider=llm_provider)
            except NetworkReviewError as exc:
                counts["llm_errors"] += 1
                emit({
                    "primitive": "build_research_review_csv",
                    "command": "build",
                    "status": "failed",
                    "handle": handle,
                    "error": str(exc),
                    "counts": counts,
                })
                return 1
            usage = bucket_payload.pop("_usage", None) or {}
            total_input_tokens += int(usage.get("prompt_tokens") or 0)
            total_output_tokens += int(usage.get("completion_tokens") or 0)
            counts["scored_via_llm"] += 1
            write_network_review_cache(network_review_path, handle, queue_row, research_packet, llm_model, bucket_payload)
            counts["network_review_written"] += 1

        flat = flatten_row(handle, queue_row, research_packet, raw_packet, bucket_payload)
        if apply_previous_review_decision(flat, previous_by_handle, previous_by_phone):
            counts["previous_review_decisions_applied"] += 1
        rows.append(flat)
        bucket_counts[flat["bucket"]] = bucket_counts.get(flat["bucket"], 0) + 1
        progress(index)

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
        "review_source": "network_review_llm",
        "model": llm_model,
        "llm_provider": llm_provider,
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
    build.add_argument("--model", default=DEFAULT_SCORE_MODEL,
                       help="Network-review model (OpenRouter slug)")
    build.add_argument("--api-key", help="OpenRouter API key (defaults to OPENROUTER_API_KEY from env or repo .env)")
    build.add_argument("--previous-csv", type=Path, help=argparse.SUPPRESS)
    build.add_argument("--refresh-cache", action="store_true", help=argparse.SUPPRESS)
    build.add_argument("--allow-missing-queue", action="store_true",
                       help="Include handles not present in queue_csv (uses defaults)")
    build.set_defaults(func=cmd_build)
    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
