#!/usr/bin/env python3
"""Unified local people enrichment flow.

Self-contained Powerpacks RapidAPI enrichment implementation. No imports from
aleph-mvp or network-search-api.

Input: a shared people schema CSV, usually merge_network_sources output.
Output: enriched people schema CSV plus raw provider responses.

RapidAPI LinkedIn hydration runs directly when RAPIDAPI_LINKEDIN_KEY or
RAPIDAPI_KEY is present. Missing keys fail clearly instead of opening an
approval step.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

try:
    from packs.ingestion.schemas.company_identity import build_company_identity_lookup, rapidapi_experience_to_powerpacks
    from packs.ingestion.schemas.linkedin_profile_normalizer import normalize_linkedin_profile
    from packs.ingestion.schemas.people_schema import (
        PEOPLE_SCHEMA_COLUMNS,
        extract_public_identifier,
        generate_person_id as generate_linkedin_person_id,
        normalize_linkedin_url,
        normalize_people_row,
        parse_jsonish,
        stable_person_id_from_key,
    )
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.schemas.company_identity import build_company_identity_lookup, rapidapi_experience_to_powerpacks
    from packs.ingestion.schemas.linkedin_profile_normalizer import normalize_linkedin_profile
    from packs.ingestion.schemas.people_schema import (
        PEOPLE_SCHEMA_COLUMNS,
        extract_public_identifier,
        generate_person_id as generate_linkedin_person_id,
        normalize_linkedin_url,
        normalize_people_row,
        parse_jsonish,
        stable_person_id_from_key,
    )

DEFAULT_LEDGER = Path(".powerpacks/network-import/enrichment/import-run.json")
DEFAULT_BASE_DIR = Path(".powerpacks/network-import")
RAPIDAPI_BASE_URL = "https://professional-network-data.p.rapidapi.com"
DEFAULT_RAPIDAPI_MAX_WORKERS = int(os.environ.get("POWERPACKS_RAPIDAPI_LINKEDIN_MAX_WORKERS", "10"))
DEFAULT_RAPIDAPI_MAX_RPM = float(os.environ.get("POWERPACKS_RAPIDAPI_LINKEDIN_MAX_RPM", "300"))
DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS = float(os.environ.get("POWERPACKS_RAPIDAPI_LINKEDIN_FAILURE_RETRY_HOURS", "24"))
DEFAULT_PROGRESS_INTERVAL_SECONDS = float(os.environ.get("POWERPACKS_RAPIDAPI_PROGRESS_INTERVAL_SECONDS", "60"))
DEFAULT_PROGRESS_INTERVAL_ROWS = int(os.environ.get("POWERPACKS_RAPIDAPI_PROGRESS_INTERVAL_ROWS", "100"))
PIPELINE_STEPS = ["prepare_queue", "enrich_linkedin", "merge_people"]

QUEUE_COLUMNS = PEOPLE_SCHEMA_COLUMNS + ["enrichment_route", "enrichment_reason"]
CACHE_COLUMNS = QUEUE_COLUMNS + ["cache_status", "cache_path", "cache_reason"]
RECENT_FAILURE_COLUMNS = CACHE_COLUMNS + ["last_checked_at", "retry_after", "rapidapi_status_code", "rapidapi_error"]
PROVIDER_COLUMNS = QUEUE_COLUMNS + [
    "rapidapi_status_code",
    "rapidapi_error",
    "rapidapi_response_enriched",
    "rapidapi_from_cache",
    "provider_enriched_at",
]


class PipelineBlocked(Exception):
    def __init__(self, payload: dict[str, Any], code: int = 20) -> None:
        super().__init__(payload.get("message") or "blocked")
        self.payload = payload
        self.code = code


class PipelineFailed(Exception):
    pass


def load_dotenv(path: Path, keys: set[str] | None = None) -> None:
    """Load simple KEY=VALUE entries without overriding the shell env."""
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key or key in os.environ or (keys is not None and key not in keys):
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


load_dotenv(Path(__file__).resolve().parents[4] / ".env", {"RAPIDAPI_LINKEDIN_KEY", "RAPIDAPI_KEY"})


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def emit_progress(message: str) -> None:
    print(f"[enrich-people] {message}", file=sys.stderr, flush=True)


def sha(value: str, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in fieldnames})


def split_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def generate_person_id(public_identifier: str, fallback: str = "") -> str:
    if public_identifier:
        return generate_linkedin_person_id(public_identifier)
    return stable_person_id_from_key(f"person:{fallback}")


def count_items(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return len(parsed) if isinstance(parsed, list) else 0
        except json.JSONDecodeError:
            return 0
    return 0


def profile_richness(experiences: Any, education: Any) -> int:
    return count_items(experiences) + count_items(education)


def _explicit_current_value(exp: dict[str, Any]) -> bool | None:
    for key in ("is_current_position", "is_current", "current"):
        if key not in exp:
            continue
        value = exp.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "y"}:
                return True
            if lowered in {"false", "0", "no", "n"}:
                return False
    return None


def current_position(experiences: list[dict[str, Any]]) -> tuple[str, str, str]:
    first_exp: dict[str, Any] | None = None
    for exp in experiences or []:
        if not isinstance(exp, dict):
            continue
        first_exp = first_exp or exp
        explicit_current = _explicit_current_value(exp)
        if explicit_current is True or (explicit_current is None and not (exp.get("ends_at") or exp.get("end_date"))):
            return (
                str(exp.get("title") or exp.get("position") or ""),
                str(exp.get("company_name") or exp.get("company") or exp.get("organization") or ""),
                "",
            )
    if first_exp:
        return (
            str(first_exp.get("title") or first_exp.get("position") or ""),
            str(first_exp.get("company_name") or first_exp.get("company") or first_exp.get("organization") or ""),
            "",
        )
    return "", "", ""


def http_json(method: str, url: str, *, headers: dict[str, str] | None = None, params: dict[str, str] | None = None, timeout: int = 60) -> tuple[int, dict[str, Any] | None, str]:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(raw) if raw else None, ""
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            data = None
        return exc.code, data, raw[:1000]
    except Exception as exc:
        return 0, None, str(exc)


def rapidapi_key() -> str:
    return os.getenv("RAPIDAPI_LINKEDIN_KEY", "").strip() or os.getenv("RAPIDAPI_KEY", "").strip()


def safe_cache_slug(public_identifier: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in public_identifier.lower().strip())
    return cleaned.strip("._")


def profile_cache_path(cache_dir: Path | str | None, public_identifier: str) -> Path | None:
    slug = safe_cache_slug(public_identifier)
    if not cache_dir or not slug:
        return None
    return Path(cache_dir) / f"{slug}.json"


class StartRateLimiter:
    def __init__(self, max_rpm: float, extra_sleep_seconds: float = 0.0) -> None:
        intervals = []
        if max_rpm and max_rpm > 0:
            intervals.append(60.0 / max_rpm)
        if extra_sleep_seconds and extra_sleep_seconds > 0:
            intervals.append(extra_sleep_seconds)
        self.interval = max(intervals) if intervals else 0.0
        self.lock = Lock()
        self.next_start = 0.0

    def wait(self) -> None:
        if self.interval <= 0:
            return
        with self.lock:
            now = time.monotonic()
            wait_for = max(0.0, self.next_start - now)
            self.next_start = max(now, self.next_start) + self.interval
        if wait_for > 0:
            time.sleep(wait_for)


def read_usable_cached_profile(cache_path: Path | None) -> dict[str, Any] | None:
    if not cache_path or not cache_path.exists():
        return None
    cached = read_json(cache_path, None)
    if not isinstance(cached, dict):
        return None
    normalized = cached.get("normalized_profile")
    raw = cached.get("raw_response")
    if isinstance(normalized, dict) and normalized.get("success") is True and isinstance(raw, dict):
        return cached
    return None


def recent_cached_failure(cache_path: Path | None, retry_hours: float) -> dict[str, Any] | None:
    if not cache_path or not cache_path.exists() or retry_hours <= 0:
        return None
    cached = read_json(cache_path, None)
    if not isinstance(cached, dict):
        return None
    normalized = cached.get("normalized_profile")
    if not isinstance(normalized, dict) or normalized.get("success") is not False:
        return None
    checked_at = parse_iso(str(cached.get("last_checked_at") or cached.get("fetched_at") or ""))
    if checked_at is None:
        return None
    retry_after = checked_at + timedelta(hours=retry_hours)
    if datetime.now(timezone.utc) >= retry_after:
        return None
    result = dict(cached)
    result["retry_after"] = retry_after.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return result


def rapidapi_profile(
    public_identifier: str,
    linkedin_url: str,
    api_key: str,
    *,
    cache_dir: Path | str | None = None,
    refresh_cache: bool = False,
) -> dict[str, Any]:
    cache_path = profile_cache_path(cache_dir, public_identifier)
    if not refresh_cache:
        cached = read_usable_cached_profile(cache_path)
        if cached:
            return {
                "status_code": 200,
                "data": cached.get("raw_response"),
                "error": "",
                "from_cache": True,
                "normalized_profile": cached.get("normalized_profile"),
            }

    status, data, error = http_json(
        "GET",
        f"{RAPIDAPI_BASE_URL}/get-profile-data-by-url",
        headers={"x-rapidapi-host": "professional-network-data.p.rapidapi.com", "x-rapidapi-key": api_key},
        params={"url": linkedin_url or f"https://www.linkedin.com/in/{public_identifier}"},
        timeout=90,
    )
    normalized = normalize_linkedin_profile(data if isinstance(data, dict) else {})
    if cache_path and status == 200 and isinstance(data, dict) and normalized.get("success") is True:
        write_json(cache_path, {
            "fetched_at": now_iso(),
            "last_checked_at": now_iso(),
            "public_identifier": public_identifier,
            "linkedin_url": linkedin_url,
            "raw_response": data,
            "normalized_profile": normalized,
        })
    elif cache_path:
        checked_at = now_iso()
        write_json(cache_path, {
            "fetched_at": checked_at,
            "last_checked_at": checked_at,
            "public_identifier": public_identifier,
            "linkedin_url": linkedin_url,
            "raw_response": data if isinstance(data, dict) else {},
            "normalized_profile": normalized,
            "status_code": status,
            "error": error or normalized.get("error") or "",
        })
    return {"status_code": status, "data": data, "error": error, "from_cache": False, "normalized_profile": normalized}


def normalize_rapidapi(
    data: dict[str, Any] | None,
    public_identifier: str,
    linkedin_url: str,
    company_lookup: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    profile = normalize_linkedin_profile(data)
    if profile.get("success") is not True:
        return {}

    experiences = [rapidapi_experience_to_powerpacks(exp, company_lookup) for exp in profile.get("experiences", []) if isinstance(exp, dict)]
    title, company, _legacy = current_position(experiences)
    profile_public_id = profile.get("public_identifier") or public_identifier
    profile_url = profile.get("linkedin_url") or linkedin_url or (f"https://www.linkedin.com/in/{profile_public_id}" if profile_public_id else "")
    return {
        "public_identifier": profile_public_id,
        "linkedin_url": normalize_linkedin_url(profile_url),
        "first_name": profile.get("first_name") or "",
        "last_name": profile.get("last_name") or "",
        "full_name": profile.get("full_name") or "",
        "headline": profile.get("headline") or "",
        "summary": profile.get("summary") or "",
        "city": profile.get("city") or "",
        "state": profile.get("state") or "",
        "country": profile.get("country") or "",
        "location_raw": profile.get("location_str") or "",
        "profile_picture_url": profile.get("profile_pic_url") or "",
        "work_experiences": experiences,
        "education": profile.get("education") if isinstance(profile.get("education"), list) else [],
        "current_title": title,
        "current_company": company,
    }


def row_has_profile_gaps(row: dict[str, str]) -> bool:
    if not row.get("full_name") and not (row.get("first_name") and row.get("last_name")):
        return True
    if not row.get("headline"):
        return True
    if not row.get("current_company") and not row.get("current_title"):
        return True
    if profile_richness(row.get("work_experiences"), row.get("education")) == 0:
        return True
    return False


def route_row(row: dict[str, str], force: bool = False) -> tuple[str, str]:
    linkedin_url = normalize_linkedin_url(row.get("linkedin_url") or "")
    public_identifier = row.get("public_identifier") or extract_public_identifier(linkedin_url)
    if linkedin_url or public_identifier:
        if force or row_has_profile_gaps(row):
            return "linkedin_provider", "linkedin identifier present and profile has gaps"
        return "skip_complete", "linkedin identifier present and profile appears complete"
    if row.get("primary_email") or row.get("primary_phone") or row.get("twitter_handle") or row.get("full_name"):
        return "needs_resolution", "no linkedin identifier; needs resolution/research before provider enrichment"
    return "skip_no_identifier", "no usable identifier"


def merge_provider_profile(base: dict[str, Any], rapid: dict[str, Any], rapid_raw: dict[str, Any] | None) -> dict[str, Any]:
    base = normalize_people_row(base)
    public_identifier = base.get("public_identifier") or rapid.get("public_identifier") or extract_public_identifier(base.get("linkedin_url") or rapid.get("linkedin_url") or "")
    row: dict[str, Any] = dict(base)
    row["id"] = row.get("id") or generate_person_id(public_identifier, row.get("full_name") or row.get("primary_email") or row.get("primary_phone") or "")
    row["public_identifier"] = public_identifier

    if rapid:
        for key in [
            "linkedin_url", "first_name", "last_name", "full_name", "headline", "summary",
            "city", "state", "country", "location_raw", "profile_picture_url",
            "current_title", "current_company",
        ]:
            value = rapid.get(key)
            if value not in (None, ""):
                row[key] = value
        row["linkedin_url"] = normalize_linkedin_url(row.get("linkedin_url") or (f"https://www.linkedin.com/in/{public_identifier}" if public_identifier else ""))
        if not row.get("full_name"):
            row["full_name"] = f"{row.get('first_name','')} {row.get('last_name','')}".strip()
        row["work_experiences"] = json.dumps(rapid.get("work_experiences") or parse_jsonish(row.get("work_experiences"), []))
        row["education"] = json.dumps(rapid.get("education") or parse_jsonish(row.get("education"), []))
        row["enriched_at"] = now_iso()
        row["enrichment_provider"] = "rapidapi"
        if rapid_raw:
            row["rapidapi_response"] = json.dumps(rapid_raw)
    else:
        row["linkedin_url"] = normalize_linkedin_url(row.get("linkedin_url") or (f"https://www.linkedin.com/in/{public_identifier}" if public_identifier else ""))
        row["enrichment_provider"] = row.get("enrichment_provider") or "existing_only"
    return normalize_people_row(row)


def cached_profile_from_row(row: dict[str, Any], public_identifier: str, linkedin_url: str) -> dict[str, Any] | None:
    for col in ("rapidapi_response_enriched", "rapidapi_response"):
        parsed = parse_jsonish(row.get(col), None)
        if isinstance(parsed, dict) and normalize_linkedin_profile(parsed).get("success") is True:
            return parsed
    return None


def confirmed_people_row(row: dict[str, Any]) -> bool:
    linkedin_url = normalize_linkedin_url(str(row.get("linkedin_url") or ""))
    public_identifier = str(row.get("public_identifier") or extract_public_identifier(linkedin_url) or "").strip()
    if not linkedin_url or not public_identifier:
        return False
    raw = parse_jsonish(row.get("rapidapi_response"), None)
    return isinstance(raw, dict) and normalize_linkedin_profile(raw).get("success") is True


def classify_rapidapi_cache_status(row: dict[str, str], profile_cache_dir: Path, refresh_cache: bool, retry_hours: float) -> tuple[str, str, Path | None, dict[str, Any] | None]:
    public_identifier = row.get("public_identifier") or extract_public_identifier(row.get("linkedin_url") or "")
    cache_path = profile_cache_path(profile_cache_dir, public_identifier)
    if refresh_cache:
        return "miss", "refresh requested", cache_path, None
    if cached_profile_from_row(row, public_identifier, row.get("linkedin_url") or "") is not None:
        return "hit", "input rapidapi_response", cache_path, None
    if read_usable_cached_profile(cache_path):
        return "hit", "profile cache", cache_path, None
    recent_failure = recent_cached_failure(cache_path, retry_hours)
    if recent_failure:
        return "recent_failure", "recent provider failure", cache_path, recent_failure
    if cache_path and cache_path.exists():
        return "miss", "cache entry unusable", cache_path, None
    return "miss", "no usable cache", cache_path, None


def count_rapidapi_cache_misses(cache_misses_csv: Path) -> int:
    if not cache_misses_csv.exists():
        return 0
    return len(read_csv(cache_misses_csv))


def load_ledger(path: Path) -> dict[str, Any]:
    ledger = read_json(path, {}) or {}
    ledger.setdefault("primitive", "enrich_people")
    ledger.setdefault("version", 1)
    ledger.setdefault("created_at", now_iso())
    ledger.setdefault("updated_at", now_iso())
    ledger.setdefault("steps", {})
    ledger.setdefault("approvals", {})
    ledger.setdefault("artifacts", {})
    return ledger


def save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = now_iso()
    write_json(path, ledger)


def mark_step(ledger: dict[str, Any], step_id: str, status: str, **extra: Any) -> None:
    rec = ledger.setdefault("steps", {}).setdefault(step_id, {"id": step_id})
    if status == "running" and "started_at" not in rec:
        rec["started_at"] = now_iso()
    if status in {"completed", "failed", "blocked_approval", "skipped"}:
        rec["finished_at"] = now_iso()
    rec["status"] = status
    rec.update({k: v for k, v in extra.items() if v is not None})


def next_pending_step(ledger: dict[str, Any]) -> str | None:
    for step_id in PIPELINE_STEPS:
        if ledger.setdefault("steps", {}).get(step_id, {}).get("status") != "completed":
            return step_id
    return None


def approval_id(ledger: dict[str, Any], step_id: str) -> str:
    return f"enrich_people:{step_id}"


def artifact_dir_from_ledger(ledger: dict[str, Any]) -> Path:
    return Path(str(ledger.get("artifact_dir") or ledger.get("run_dir") or DEFAULT_BASE_DIR / "enrichment"))


def is_approved(ledger: dict[str, Any], step_id: str) -> bool:
    return bool(ledger.setdefault("approvals", {}).get(approval_id(ledger, step_id)))


def block_for_approval(ledger_path: Path, ledger: dict[str, Any], step_id: str, message: str) -> None:
    app_id = approval_id(ledger, step_id)
    ledger["blocked"] = {"step_id": step_id, "approval_id": app_id, "approval_type": "external_api_spend"}
    mark_step(ledger, step_id, "blocked_approval", approval_id=app_id, approval_type="external_api_spend")
    save_ledger(ledger_path, ledger)
    raise PipelineBlocked({
        "status": "blocked_approval",
        "step_id": step_id,
        "approval_id": app_id,
        "approval_type": "external_api_spend",
        "message": message,
        "ledger": str(ledger_path),
        "continue_command": f"uv run --project . python packs/ingestion/primitives/enrich_people/enrich_people.py approve --ledger {ledger_path} && uv run --project . python packs/ingestion/primitives/enrich_people/enrich_people.py continue --ledger {ledger_path}",
    })


def step_prepare_queue(ledger: dict[str, Any]) -> dict[str, Any]:
    rows = [normalize_people_row(row) for row in read_csv(Path(ledger["input"]["input_csv"]))]
    limit = ledger["input"].get("limit")
    if limit:
        rows = rows[: int(limit)]
    queue: list[dict[str, Any]] = []
    cache_hits: list[dict[str, Any]] = []
    cache_misses: list[dict[str, Any]] = []
    recent_failures: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    route_counts: dict[str, int] = {}
    profile_cache_dir = Path(ledger["input"].get("profile_cache_dir") or DEFAULT_BASE_DIR / "profile_cache_v2")
    refresh_cache = bool(ledger["input"].get("refresh_cache"))
    failure_retry_hours = float(ledger["input"].get("failure_retry_hours") if ledger["input"].get("failure_retry_hours") is not None else DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS)
    for row in rows:
        route, reason = route_row(row, force=bool(ledger["input"].get("force")))
        row["enrichment_route"] = route
        row["enrichment_reason"] = reason
        route_counts[route] = route_counts.get(route, 0) + 1
        if route == "linkedin_provider":
            queue.append(row)
            status, cache_reason, cache_path, recent_failure = classify_rapidapi_cache_status(row, profile_cache_dir, refresh_cache, failure_retry_hours)
            cache_row = dict(row)
            cache_row.update({"cache_status": status, "cache_path": str(cache_path or ""), "cache_reason": cache_reason})
            if status == "hit":
                cache_hits.append(cache_row)
            elif status == "recent_failure":
                normalized = recent_failure.get("normalized_profile") if isinstance(recent_failure, dict) else {}
                cache_row.update({
                    "last_checked_at": recent_failure.get("last_checked_at") or recent_failure.get("fetched_at") or "",
                    "retry_after": recent_failure.get("retry_after") or "",
                    "rapidapi_status_code": recent_failure.get("status_code") or "",
                    "rapidapi_error": recent_failure.get("error") or (normalized.get("error") if isinstance(normalized, dict) else "") or "",
                })
                recent_failures.append(cache_row)
            else:
                cache_misses.append(cache_row)
        elif route == "needs_resolution":
            unresolved.append(row)
        else:
            skipped.append(row)
    run_dir = artifact_dir_from_ledger(ledger)
    queue_path = run_dir / "linkedin_enrichment_queue.csv"
    cache_hits_path = run_dir / "rapidapi_cache_hits.csv"
    cache_misses_path = run_dir / "rapidapi_cache_misses.csv"
    recent_failures_path = run_dir / "rapidapi_recent_failures.csv"
    unresolved_path = run_dir / "needs_resolution_queue.csv"
    skipped_path = run_dir / "skipped_enrichment.csv"
    write_csv(queue_path, QUEUE_COLUMNS, queue)
    write_csv(cache_hits_path, CACHE_COLUMNS, cache_hits)
    write_csv(cache_misses_path, CACHE_COLUMNS, cache_misses)
    write_csv(recent_failures_path, RECENT_FAILURE_COLUMNS, recent_failures)
    write_csv(unresolved_path, QUEUE_COLUMNS, unresolved)
    write_csv(skipped_path, QUEUE_COLUMNS, skipped)
    ledger["artifacts"].update({
        "linkedin_enrichment_queue_csv": str(queue_path),
        "rapidapi_cache_hits_csv": str(cache_hits_path),
        "rapidapi_cache_misses_csv": str(cache_misses_path),
        "rapidapi_recent_failures_csv": str(recent_failures_path),
        "needs_resolution_queue_csv": str(unresolved_path),
        "skipped_enrichment_csv": str(skipped_path),
    })
    ledger["queue_count"] = len(queue)
    ledger["cache_hit_count"] = len(cache_hits)
    ledger["paid_call_count"] = len(cache_misses)
    ledger["recent_failure_count"] = len(recent_failures)
    emit_progress(
        "Prepared LinkedIn enrichment queue: "
        f"{len(queue)} total, {len(cache_hits)} cached, {len(cache_misses)} RapidAPI fetches, "
        f"{len(recent_failures)} recent failures."
    )
    return {
        "input_rows": len(rows),
        "queue_rows": len(queue),
        "cache_hit_rows": len(cache_hits),
        "paid_call_rows": len(cache_misses),
        "recent_failure_rows": len(recent_failures),
        "unresolved_rows": len(unresolved),
        "skipped_rows": len(skipped),
        "route_counts": route_counts,
    }


def step_enrich_linkedin(ledger: dict[str, Any]) -> dict[str, Any]:
    hit_path = Path(ledger["artifacts"].get("rapidapi_cache_hits_csv") or "")
    miss_path = Path(ledger["artifacts"].get("rapidapi_cache_misses_csv") or "")
    rows = []
    if hit_path.exists():
        rows.extend(read_csv(hit_path))
    if miss_path.exists():
        rows.extend(read_csv(miss_path))
    if not rows:
        out_path = artifact_dir_from_ledger(ledger) / "provider_enriched.csv"
        write_csv(out_path, PROVIDER_COLUMNS, [])
        ledger["artifacts"]["provider_enriched_csv"] = str(out_path)
        emit_progress("No LinkedIn enrichment work needed.")
        return {"processed": 0, "cached": 0, "fetched": 0, "output_file": str(out_path), "providers": {"rapidapi": False}}

    paid_call_count = int(ledger.get("paid_call_count") or 0)
    rapid_key = rapidapi_key()
    if paid_call_count > 0 and not rapid_key:
        raise PipelineFailed("RAPIDAPI_LINKEDIN_KEY/RAPIDAPI_KEY is not set")

    profile_cache_dir = Path(ledger["input"].get("profile_cache_dir") or DEFAULT_BASE_DIR / "profile_cache_v2")
    refresh_cache = bool(ledger["input"].get("refresh_cache"))
    max_workers = max(1, int(ledger["input"].get("max_workers") or DEFAULT_RAPIDAPI_MAX_WORKERS))
    max_rpm = float(ledger["input"].get("max_rpm") if ledger["input"].get("max_rpm") is not None else DEFAULT_RAPIDAPI_MAX_RPM)
    sleep_seconds = float(ledger["input"].get("sleep_seconds") or 0.0)
    rate_limiter = StartRateLimiter(max_rpm, sleep_seconds)
    raw_dir = artifact_dir_from_ledger(ledger) / "raw_provider_responses"
    raw_dir.mkdir(parents=True, exist_ok=True)
    cache_rows = sum(1 for row in rows if row.get("cache_status") == "hit")
    emit_progress(
        "Starting LinkedIn profile enrichment: "
        f"{len(rows)} profiles, {cache_rows} cached, {paid_call_count} to fetch, "
        f"max {max_workers} workers, {max_rpm:g} rpm."
    )

    def enrich_one(row: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any], bool]:
        public_identifier = row.get("public_identifier") or extract_public_identifier(row.get("linkedin_url") or "")
        linkedin_url = normalize_linkedin_url(row.get("linkedin_url") or (f"https://www.linkedin.com/in/{public_identifier}" if public_identifier else ""))
        if not public_identifier and linkedin_url:
            public_identifier = extract_public_identifier(linkedin_url)
        is_cache_hit = row.get("cache_status") == "hit"
        if is_cache_hit:
            cached_payload = cached_profile_from_row(row, public_identifier, linkedin_url)
            normalized = normalize_linkedin_profile(cached_payload) if cached_payload else None
            if cached_payload and normalized and normalized.get("success") is True:
                rapid = {"status_code": 200, "data": cached_payload, "error": "", "from_cache": True, "normalized_profile": normalized}
            else:
                rapid = rapidapi_profile(public_identifier, linkedin_url, "", cache_dir=profile_cache_dir, refresh_cache=False)
            if not rapid.get("from_cache"):
                raise PipelineFailed(f"usable RapidAPI cache was expected for {public_identifier or linkedin_url}")
        else:
            rate_limiter.wait()
            rapid = rapidapi_profile(public_identifier, linkedin_url, rapid_key, cache_dir=profile_cache_dir, refresh_cache=refresh_cache)
        out = dict(row)
        out.update({
            "public_identifier": public_identifier,
            "linkedin_url": linkedin_url,
            "rapidapi_status_code": rapid.get("status_code", ""),
            "rapidapi_error": rapid.get("error", ""),
            "rapidapi_response_enriched": json.dumps(rapid.get("data")) if rapid.get("data") else "",
            "rapidapi_from_cache": "true" if rapid.get("from_cache") else "false",
            "provider_enriched_at": now_iso(),
        })
        raw_payload = {"input": row, "rapidapi": rapid, "cache_hit": bool(rapid.get("from_cache"))}
        return out, raw_payload, is_cache_hit

    enriched_by_index: dict[int, dict[str, Any]] = {}
    raw_by_index: dict[int, dict[str, Any]] = {}
    cached_count = 0
    fetched_count = 0
    processed_count = 0
    last_progress = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {executor.submit(enrich_one, row): index for index, row in enumerate(rows)}
        for future in concurrent.futures.as_completed(future_to_index):
            index = future_to_index[future]
            out, raw_payload, was_cache_hit = future.result()
            enriched_by_index[index] = out
            raw_by_index[index] = raw_payload
            if was_cache_hit:
                cached_count += 1
            else:
                fetched_count += 1
            processed_count += 1
            now = time.monotonic()
            if (
                processed_count == len(rows)
                or processed_count % DEFAULT_PROGRESS_INTERVAL_ROWS == 0
                or now - last_progress >= DEFAULT_PROGRESS_INTERVAL_SECONDS
            ):
                emit_progress(
                    "LinkedIn profile enrichment progress: "
                    f"{processed_count}/{len(rows)} processed "
                    f"({cached_count} cached, {fetched_count} fetched)."
                )
                last_progress = now
    enriched: list[dict[str, Any]] = []
    for index in range(len(rows)):
        out = enriched_by_index[index]
        raw_payload = raw_by_index[index]
        public_identifier = out.get("public_identifier") or extract_public_identifier(out.get("linkedin_url") or "")
        write_json(raw_dir / f"{public_identifier or sha(out.get('linkedin_url') or out.get('id',''))}.json", raw_payload)
        enriched.append(out)
    out_path = artifact_dir_from_ledger(ledger) / "provider_enriched.csv"
    write_csv(out_path, PROVIDER_COLUMNS, enriched)
    ledger["artifacts"].update({"provider_enriched_csv": str(out_path), "raw_provider_responses_dir": str(raw_dir)})
    emit_progress(f"LinkedIn profile enrichment finished: {len(enriched)} profiles processed.")
    return {
        "processed": len(enriched),
        "cached": cached_count,
        "fetched": fetched_count,
        "output_file": str(out_path),
        "providers": {"rapidapi": True},
        "max_workers": max_workers,
        "max_rpm": max_rpm,
    }


def step_merge_people(ledger: dict[str, Any]) -> dict[str, Any]:
    original_rows = [normalize_people_row(row) for row in read_csv(Path(ledger["input"]["input_csv"]))]
    by_key: dict[str, dict[str, Any]] = {}
    for row in original_rows:
        key = row.get("id") or row.get("public_identifier") or row.get("linkedin_url") or sha(json.dumps(row, sort_keys=True))
        by_key[key] = row
    provider_path = Path(ledger["artifacts"].get("provider_enriched_csv") or ledger["artifacts"].get("linkedin_enrichment_queue_csv"))
    enriched_rows = read_csv(provider_path) if provider_path and provider_path.exists() else []
    company_lookup = build_company_identity_lookup([Path(p) for p in ledger["input"].get("company_corpus_jsonl", [])])
    for row in enriched_rows:
        rapid_raw = json.loads(row["rapidapi_response_enriched"]) if row.get("rapidapi_response_enriched") else (json.loads(row["rapidapi_response"]) if row.get("rapidapi_response") else None)
        public_identifier = row.get("public_identifier") or extract_public_identifier(row.get("linkedin_url") or "")
        rapid = normalize_rapidapi(rapid_raw, public_identifier, row.get("linkedin_url", ""), company_lookup)
        merged = merge_provider_profile(row, rapid, rapid_raw)
        key = row.get("id") or row.get("public_identifier") or row.get("linkedin_url") or sha(json.dumps(row, sort_keys=True))
        by_key[key] = merged
    output = artifact_dir_from_ledger(ledger) / "people.csv"
    unfiltered_rows = list(by_key.values())
    rows = [row for row in unfiltered_rows if confirmed_people_row(row)]
    write_csv(output, PEOPLE_SCHEMA_COLUMNS, rows)
    ledger["artifacts"]["people_csv"] = str(output)
    filtered_rows = len(unfiltered_rows) - len(rows)
    emit_progress(f"Wrote people.csv with {len(rows)} confirmed rows.")
    return {"rows": len(rows), "unfiltered_rows": len(unfiltered_rows), "filtered_rows": filtered_rows, "output_file": str(output)}


def execute_step(ledger: dict[str, Any], step_id: str) -> dict[str, Any]:
    if step_id == "prepare_queue":
        return step_prepare_queue(ledger)
    if step_id == "enrich_linkedin":
        return step_enrich_linkedin(ledger)
    if step_id == "merge_people":
        return step_merge_people(ledger)
    raise PipelineFailed(f"unknown step: {step_id}")


def ensure_keys(ledger: dict[str, Any]) -> None:
    if int(ledger.get("paid_call_count") or 0) > 0 and not rapidapi_key():
        raise PipelineFailed("RAPIDAPI_LINKEDIN_KEY/RAPIDAPI_KEY is not set")


def run_until_blocked_or_done(ledger_path: Path) -> int:
    ledger = load_ledger(ledger_path)
    while True:
        step_id = next_pending_step(ledger)
        if step_id is None:
            ledger["status"] = "completed"
            ledger.pop("blocked", None)
            save_ledger(ledger_path, ledger)
            emit({"status": "completed", "ledger": str(ledger_path), "artifact_dir": ledger.get("artifact_dir") or ledger.get("run_dir"), "artifacts": ledger.get("artifacts", {})})
            return 0
        try:
            paid_call_count = int(ledger.get("paid_call_count") or 0)
            if step_id == "enrich_linkedin" and paid_call_count > 0 and ledger.get("input", {}).get("use_rapidapi") is False:
                raise PipelineFailed("RapidAPI provider is required for enrich_people")
            if step_id == "enrich_linkedin" and paid_call_count > 0:
                ensure_keys(ledger)
            mark_step(ledger, step_id, "running")
            save_ledger(ledger_path, ledger)
            summary = execute_step(ledger, step_id)
            mark_step(ledger, step_id, "completed", summary=summary)
            ledger.pop("blocked", None)
            save_ledger(ledger_path, ledger)
        except PipelineFailed as exc:
            mark_step(ledger, step_id, "failed", error=str(exc))
            ledger["status"] = "failed"
            save_ledger(ledger_path, ledger)
            emit({"status": "failed", "step_id": step_id, "error": str(exc), "ledger": str(ledger_path)})
            return 1
        except PipelineBlocked:
            raise


def command_run(args: argparse.Namespace) -> int:
    artifact_dir = Path(args.artifact_dir) if args.artifact_dir else Path(args.output_dir) / "enrichment"
    ledger_path = Path(args.ledger)
    if ledger_path.exists() and not args.force_ledger:
        existing = load_ledger(ledger_path)
        if existing.get("status") not in {"completed", "failed"}:
            emit({"status": "active_run_exists", "ledger": str(ledger_path), "message": "Use continue/approve or --force-ledger."})
            return 0
    ledger = {
        "primitive": "enrich_people",
        "version": 1,
        "status": "running",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "artifact_dir": str(artifact_dir),
        "ledger": str(ledger_path),
        "input": {
            "input_csv": str(Path(args.input)),
            "limit": args.limit,
            "force": args.force,
            "profile_cache_dir": str(Path(args.profile_cache_dir)),
            "refresh_cache": args.refresh_cache,
            "company_corpus_jsonl": [str(Path(p)) for p in (args.company_corpus_jsonl or [])],
            "sleep_seconds": args.sleep_seconds,
            "max_workers": args.max_workers,
            "max_rpm": args.max_rpm,
            "failure_retry_hours": args.failure_retry_hours,
        },
        "steps": {},
        "approvals": {},
        "artifacts": {},
    }
    save_ledger(ledger_path, ledger)
    try:
        return run_until_blocked_or_done(ledger_path)
    except PipelineBlocked as blocked:
        emit(blocked.payload)
        return blocked.code


def command_continue(args: argparse.Namespace) -> int:
    if not Path(args.ledger).exists():
        emit({"status": "missing_ledger", "ledger": args.ledger})
        return 2
    try:
        return run_until_blocked_or_done(Path(args.ledger))
    except PipelineBlocked as blocked:
        emit(blocked.payload)
        return blocked.code


def command_approve(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger)
    ledger = load_ledger(ledger_path)
    blocked = ledger.get("blocked") or {}
    step_id = args.step or blocked.get("step_id") or next_pending_step(ledger)
    if not step_id:
        emit({"status": "no_pending_approval", "ledger": str(ledger_path)})
        return 1
    app_id = approval_id(ledger, step_id)
    ledger.setdefault("approvals", {})[app_id] = {"approved_at": now_iso(), "source": "operator", "step_id": step_id}
    if ledger.get("blocked", {}).get("step_id") == step_id:
        ledger.pop("blocked", None)
    save_ledger(ledger_path, ledger)
    emit({"status": "approved", "approval_id": app_id, "ledger": str(ledger_path)})
    return 0


def command_status(args: argparse.Namespace) -> int:
    ledger = load_ledger(Path(args.ledger))
    emit({"status": ledger.get("status", "unknown"), "blocked": ledger.get("blocked"), "steps": ledger.get("steps", {}), "artifacts": ledger.get("artifacts", {}), "queue_count": ledger.get("queue_count"), "cache_hit_count": ledger.get("cache_hit_count"), "paid_call_count": ledger.get("paid_call_count"), "recent_failure_count": ledger.get("recent_failure_count")})
    return 0


def command_check_keys(_: argparse.Namespace) -> int:
    emit({
        "status": "ok",
        "provider": "rapidapi",
        "keys_present": {
            "RAPIDAPI_KEY": bool(os.getenv("RAPIDAPI_KEY", "").strip()),
            "RAPIDAPI_LINKEDIN_KEY": bool(os.getenv("RAPIDAPI_LINKEDIN_KEY", "").strip()),
        },
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified people enrichment flow for shared people schema CSVs")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--input", required=True, help="Input shared people schema CSV, e.g. merged people CSV")
    run.add_argument("--output-dir", default=str(DEFAULT_BASE_DIR))
    run.add_argument("--artifact-dir", default="", help=argparse.SUPPRESS)
    run.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    run.add_argument("--force", action="store_true", help="Re-enrich rows even if they appear complete")
    run.add_argument("--force-ledger", action="store_true", help="Overwrite an active ledger")
    run.add_argument("--profile-cache-dir", default=str(DEFAULT_BASE_DIR / "profile_cache_v2"))
    run.add_argument("--refresh-cache", action="store_true", help="Force RapidAPI calls even when a successful local cache entry exists")
    run.add_argument("--company-corpus-jsonl", action="append", default=[])
    run.add_argument("--sleep-seconds", type=float, default=0.0)
    run.add_argument("--max-workers", type=int, default=DEFAULT_RAPIDAPI_MAX_WORKERS)
    run.add_argument("--max-rpm", type=float, default=DEFAULT_RAPIDAPI_MAX_RPM)
    run.add_argument("--failure-retry-hours", type=float, default=DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS)
    run.add_argument("--limit", type=int, help=argparse.SUPPRESS)
    run.set_defaults(func=command_run)

    cont = sub.add_parser("continue")
    cont.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    cont.set_defaults(func=command_continue)

    approve = sub.add_parser("approve")
    approve.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    approve.add_argument("--step", choices=PIPELINE_STEPS)
    approve.set_defaults(func=command_approve)

    status = sub.add_parser("status")
    status.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    status.set_defaults(func=command_status)

    keys = sub.add_parser("check-keys")
    keys.set_defaults(func=command_check_keys)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except PipelineBlocked as blocked:
        emit(blocked.payload)
        return blocked.code
    except ValueError as exc:
        emit({"status": "error", "error": str(exc)})
        return 2
    except KeyboardInterrupt:
        emit({"status": "interrupted"})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
