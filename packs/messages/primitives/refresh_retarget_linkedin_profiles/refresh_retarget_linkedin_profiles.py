#!/usr/bin/env python3
"""Refresh exact-LinkedIn retarget feedback through RapidAPI.

The retarget merge path consumes `01_research_parallel.json` artifacts. This
primitive writes the same shape from a direct LinkedIn profile provider when the
human feedback contains a LinkedIn `/in/` URL, so pointed retargets update stale
profile fields instead of reusing older search artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packs.ingestion.primitives.enrich_people.enrich_people import normalize_rapidapi, rapidapi_profile
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url
from packs.shared.csv_io import CsvIO


DEFAULT_REVIEW_CSV = Path(".powerpacks/messages/research_review.csv")
DEFAULT_LEDGER = Path(".powerpacks/messages/retarget_attempts.json")
DEFAULT_OUTPUT_DIR = Path(".powerpacks/messages/research_retarget")
DEFAULT_MAX_WORKERS = 10
LINKEDIN_IN_RE = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[^\s,;)'\"<>]+", re.IGNORECASE)
RESEARCH_METHOD = "rapidapi-linkedin-retarget"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def load_dotenv(path: Path, keys: set[str]) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, raw = text.split("=", 1)
        key = key.strip()
        if key not in keys or key in os.environ:
            continue
        value = raw.strip().strip('"').strip("'")
        if value:
            os.environ[key] = value


def api_key() -> str:
    load_dotenv(ROOT / ".env", {"RAPIDAPI_LINKEDIN_KEY"})
    return os.getenv("RAPIDAPI_LINKEDIN_KEY", "").strip()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [{key: value or "" for key, value in row.items()} for row in CsvIO.dict_reader(handle)]


def normalize_hint(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def hint_hash(value: str) -> str:
    return hashlib.sha256(normalize_hint(value).lower().encode("utf-8")).hexdigest()[:16]


def retarget_handle(source_handle: str, h: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", source_handle or "unknown").strip("_") or "unknown"
    return f"{safe}__retarget_{h[:10]}"


def linkedin_url_from_hint(value: str) -> str:
    match = LINKEDIN_IN_RE.search(value or "")
    if not match:
        return ""
    return normalize_linkedin_url(match.group(0).rstrip("/."))


def split_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").strip().split(None, 1)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("name", "text", "title", "company_name", "school_name"):
            if value.get(key):
                return str(value.get(key) or "")
        return ""
    return str(value)


def date_part(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, dict):
        year = value.get("year")
        month = value.get("month")
        day = value.get("day")
        if year and month and day:
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        if year and month:
            return f"{int(year):04d}-{int(month):02d}"
        if year:
            return str(year)
        return ""
    return str(value)


def normalize_positions(experiences: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(experiences, list):
        return out
    for exp in experiences:
        if not isinstance(exp, dict):
            continue
        company = exp.get("company_name") or exp.get("company") or exp.get("organization")
        company_name = value_text(company)
        starts_at = exp.get("starts_at") or exp.get("start_date") or exp.get("start")
        ends_at = exp.get("ends_at") or exp.get("end_date") or exp.get("end")
        out.append({
            "title": value_text(exp.get("title") or exp.get("position")),
            "company_name": company_name,
            "company_domain": value_text(exp.get("company_domain") or exp.get("company_website")),
            "start_date": date_part(starts_at),
            "end_date": date_part(ends_at),
            "is_current": bool(exp.get("is_current_position") or exp.get("is_current") or exp.get("current") or not ends_at),
            "description": value_text(exp.get("description") or exp.get("summary")),
            "location": value_text(exp.get("location")),
            "confidence": 0.9,
            "source": "RapidAPI LinkedIn profile",
        })
    return out


def normalize_education(education: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(education, list):
        return out
    for edu in education:
        if not isinstance(edu, dict):
            continue
        school = edu.get("school_name") or edu.get("school") or edu.get("name")
        starts_at = edu.get("starts_at") or edu.get("start_date") or edu.get("start")
        ends_at = edu.get("ends_at") or edu.get("end_date") or edu.get("end")
        out.append({
            "school_name": value_text(school),
            "degree": value_text(edu.get("degree") or edu.get("degree_name")),
            "field_of_study": value_text(edu.get("field_of_study") or edu.get("field")),
            "start_year": date_part(starts_at),
            "end_year": date_part(ends_at),
            "confidence": 0.9,
            "source": "RapidAPI LinkedIn profile",
        })
    return out


def completeness(profile: dict[str, Any]) -> float:
    score = 0.0
    if (profile.get("person") or {}).get("full_name"):
        score += 0.3
    if profile.get("positions"):
        score += min(0.3, len(profile["positions"]) * 0.1)
    if profile.get("education"):
        score += min(0.2, len(profile["education"]) * 0.1)
    location = profile.get("location") or {}
    if location.get("city") or location.get("country") or location.get("raw"):
        score += 0.1
    if (profile.get("social") or {}).get("linkedin_url"):
        score += 0.1
    return round(min(score, 1.0), 2)


def profile_has_substance(normalized: dict[str, Any]) -> bool:
    if not normalized:
        return False
    if any((normalized.get(key) or "").strip() for key in (
        "full_name",
        "first_name",
        "last_name",
        "headline",
        "summary",
        "city",
        "country",
        "location_raw",
        "current_title",
        "current_company",
    )):
        return True
    return bool(normalized.get("work_experiences") or normalized.get("education"))


def rapidapi_to_research_json(
    *,
    normalized: dict[str, Any],
    raw: dict[str, Any],
    source_handle: str,
    queue_handle: str,
    hint: str,
    h: str,
    linkedin_url: str,
    fetched_at: str,
) -> dict[str, Any]:
    full_name = normalized.get("full_name") or ""
    first_name = normalized.get("first_name") or ""
    last_name = normalized.get("last_name") or ""
    if full_name and (not first_name or not last_name):
        first_name, last_name = split_name(full_name)
    public_identifier = normalized.get("public_identifier") or extract_public_identifier(linkedin_url)
    headline = normalized.get("headline") or ""
    summary = normalized.get("summary") or headline
    profile = {
        "research_id": f"{queue_handle}-{date.today().isoformat()}",
        "query": linkedin_url,
        "status": "draft",
        "research_method": RESEARCH_METHOD,
        "person": {
            "full_name": full_name,
            "first_name": first_name,
            "last_name": last_name,
            "also_known_as": [source_handle],
            "confidence": 0.99 if full_name else 0.7,
            "sources": [linkedin_url],
            "notes": "Exact LinkedIn URL supplied by review feedback.",
        },
        "location": {
            "city": normalized.get("city") or "",
            "state": normalized.get("state") or "",
            "country": normalized.get("country") or "",
            "raw": normalized.get("location_raw") or "",
            "confidence": 0.8 if (normalized.get("city") or normalized.get("country") or normalized.get("location_raw")) else 0.0,
            "source": "RapidAPI LinkedIn profile",
        },
        "headline": {
            "text": headline,
            "confidence": 0.9 if headline else 0.0,
            "source": linkedin_url,
        },
        "summary": {
            "text": summary,
            "confidence": 0.8 if summary else 0.0,
            "source": "RapidAPI LinkedIn profile",
        },
        "positions": normalize_positions(normalized.get("work_experiences")),
        "education": normalize_education(normalized.get("education")),
        "social": {
            "linkedin_url": normalized.get("linkedin_url") or linkedin_url,
            "linkedin_status": "found",
            "github_url": "",
            "personal_website": "",
        },
        "metadata": {
            "total_sources_consulted": 1,
            "estimated_completeness": 0.0,
            "gaps": [],
            "research_date": date.today().isoformat(),
            "research_method": RESEARCH_METHOD,
            "research_notes": "Refreshed from exact LinkedIn URL retarget feedback via RapidAPI.",
            "source_channel": "retarget",
            "source_identifier": source_handle,
            "retarget_hint": hint,
            "retarget_hint_hash": h,
            "retarget_source_handle": source_handle,
            "retarget_queue_handle": queue_handle,
            "retarget_linkedin_url": linkedin_url,
            "public_identifier": public_identifier,
            "provider": "rapidapi",
            "fetched_at": fetched_at,
        },
    }
    profile["metadata"]["estimated_completeness"] = completeness(profile)
    gaps: list[str] = []
    if not full_name:
        gaps.append("Real name not identified")
    if not profile["positions"]:
        gaps.append("No work experience found")
    if not profile["education"]:
        gaps.append("No education found")
    if not ((profile.get("location") or {}).get("city") or (profile.get("location") or {}).get("country")):
        gaps.append("Location unknown")
    profile["metadata"]["gaps"] = gaps
    profile["metadata"]["raw_response_keys"] = sorted(raw.keys())[:50] if isinstance(raw, dict) else []
    return profile


def existing_rapidapi_artifact(profile_path: Path, h: str, linkedin_url: str) -> bool:
    profile = read_json(profile_path, {})
    if not isinstance(profile, dict):
        return False
    metadata = profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}
    social = profile.get("social") if isinstance(profile.get("social"), dict) else {}
    method = metadata.get("research_method") or profile.get("research_method")
    profile_url = normalize_linkedin_url(metadata.get("retarget_linkedin_url") or social.get("linkedin_url") or "")
    return method == RESEARCH_METHOD and metadata.get("retarget_hint_hash") == h and profile_url == linkedin_url


def candidate_rows(review_csv: Path, output_dir: Path, force: bool = False) -> tuple[list[dict[str, str]], dict[str, int]]:
    rows = []
    seen: set[str] = set()
    counts = {
        "review_rows": 0,
        "with_retarget_hint": 0,
        "with_linkedin_url": 0,
        "skipped_missing_handle": 0,
        "skipped_already_refreshed": 0,
        "skipped_duplicate": 0,
        "would_fetch": 0,
    }
    for row in load_csv(review_csv):
        counts["review_rows"] += 1
        source_handle = (row.get("handle") or "").strip()
        hint = normalize_hint(row.get("retarget_hint", ""))
        if not hint:
            continue
        counts["with_retarget_hint"] += 1
        linkedin_url = linkedin_url_from_hint(hint)
        if not linkedin_url:
            continue
        counts["with_linkedin_url"] += 1
        if not source_handle:
            counts["skipped_missing_handle"] += 1
            continue
        h = hint_hash(hint)
        queue_handle = retarget_handle(source_handle, h)
        if queue_handle in seen:
            counts["skipped_duplicate"] += 1
            continue
        seen.add(queue_handle)
        profile_path = output_dir / queue_handle / "01_research_parallel.json"
        if not force and existing_rapidapi_artifact(profile_path, h, linkedin_url):
            counts["skipped_already_refreshed"] += 1
            continue
        rows.append({
            "source_handle": source_handle,
            "queue_handle": queue_handle,
            "hint": hint,
            "hint_hash": h,
            "linkedin_url": linkedin_url,
        })
        counts["would_fetch"] += 1
    return rows, counts


def record_completed_attempt(
    ledger: dict[str, Any],
    *,
    source_handle: str,
    queue_handle: str,
    h: str,
    hint: str,
    output_dir: Path,
    completed_at: str,
) -> None:
    attempts = ledger.setdefault("attempts", {})
    attempts.setdefault(source_handle, []).append({
        "hint_hash": h,
        "hint": hint,
        "queue_handle": queue_handle,
        "status": "completed",
        "queued_at": completed_at,
        "completed_at": completed_at,
        "queue_csv": "",
        "retarget_output_dir": str(output_dir),
        "provider": "rapidapi",
    })


def cmd_estimate(args: argparse.Namespace) -> int:
    rows, counts = candidate_rows(Path(args.review_csv), Path(args.retarget_output_dir), force=args.force)
    emit({
        "primitive": "refresh_retarget_linkedin_profiles",
        "command": "estimate",
        "status": "ok",
        "review_csv": str(args.review_csv),
        "retarget_output_dir": str(args.retarget_output_dir),
        "api_key_present": bool(api_key()),
        "would_fetch": len(rows),
        "counts": counts,
    })
    return 0


def refresh_one(row: dict[str, str], key: str, output_dir: Path) -> dict[str, Any]:
    source_handle = row["source_handle"]
    queue_handle = row["queue_handle"]
    linkedin_url = row["linkedin_url"]
    public_identifier = extract_public_identifier(linkedin_url)
    fetched_at = now_iso()
    result = rapidapi_profile(public_identifier, linkedin_url, key)
    profile_dir = output_dir / queue_handle
    write_json(profile_dir / "00_rapidapi_raw.json", {
        "input": row,
        "rapidapi": result,
        "fetched_at": fetched_at,
    })
    data = result.get("data")
    normalized = normalize_rapidapi(data, public_identifier, linkedin_url)
    if int(result.get("status_code") or 0) >= 400 or not profile_has_substance(normalized):
        return {
            "ok": False,
            "source_handle": source_handle,
            "queue_handle": queue_handle,
            "linkedin_url": linkedin_url,
            "status_code": result.get("status_code"),
            "error": result.get("error") or "empty_profile",
        }
    profile = rapidapi_to_research_json(
        normalized=normalized,
        raw=data if isinstance(data, dict) else {},
        source_handle=source_handle,
        queue_handle=queue_handle,
        hint=row["hint"],
        h=row["hint_hash"],
        linkedin_url=linkedin_url,
        fetched_at=fetched_at,
    )
    write_json(profile_dir / "01_research_parallel.json", profile)
    return {
        "ok": True,
        "source_handle": source_handle,
        "queue_handle": queue_handle,
        "hint": row["hint"],
        "hint_hash": row["hint_hash"],
        "linkedin_url": linkedin_url,
        "fetched_at": fetched_at,
    }


def cmd_run(args: argparse.Namespace) -> int:
    review_csv = Path(args.review_csv)
    output_dir = Path(args.retarget_output_dir)
    ledger_path = Path(args.ledger)
    manifest_path = Path(args.manifest) if args.manifest else output_dir / "_rapidapi_retarget_manifest.json"
    rows, counts = candidate_rows(review_csv, output_dir, force=args.force)
    key = api_key()
    if rows and not key:
        payload = {
            "primitive": "refresh_retarget_linkedin_profiles",
            "command": "run",
            "status": "skipped",
            "reason": "missing_rapidapi_key",
            "message": "RAPIDAPI_LINKEDIN_KEY is not set",
            "counts": counts,
            "refreshed": 0,
            "failed": 0,
        }
        write_json(manifest_path, payload)
        emit(payload)
        return 0

    ledger = read_json(ledger_path, {"version": 1, "attempts": {}})
    if not isinstance(ledger, dict):
        ledger = {"version": 1, "attempts": {}}
    ledger.setdefault("version", 1)
    ledger.setdefault("attempts", {})

    failures: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    max_workers = max(1, int(getattr(args, "max_workers", DEFAULT_MAX_WORKERS) or 1))
    if rows:
        print(f"[refresh_retarget_linkedin_profiles] started 0/{len(rows)} profiles", file=sys.stderr, flush=True)
    if len(rows) <= 1 or max_workers <= 1:
        results = [refresh_one(row, key, output_dir) for row in rows]
    else:
        results = []
        with ThreadPoolExecutor(max_workers=min(max_workers, len(rows))) as pool:
            futures = [pool.submit(refresh_one, row, key, output_dir) for row in rows]
            done = 0
            for future in as_completed(futures):
                done += 1
                print(f"[refresh_retarget_linkedin_profiles] completed {done}/{len(rows)} profiles", file=sys.stderr, flush=True)
                results.append(future.result())
    if rows and (len(rows) <= 1 or max_workers <= 1):
        print(f"[refresh_retarget_linkedin_profiles] completed {len(rows)}/{len(rows)} profiles", file=sys.stderr, flush=True)

    for result in results:
        if not result.get("ok"):
            failures.append({
                "source_handle": result.get("source_handle"),
                "queue_handle": result.get("queue_handle"),
                "linkedin_url": result.get("linkedin_url"),
                "status_code": result.get("status_code"),
                "error": result.get("error") or "empty_profile",
            })
            continue
        record_completed_attempt(
            ledger,
            source_handle=str(result.get("source_handle") or ""),
            queue_handle=str(result.get("queue_handle") or ""),
            h=str(result.get("hint_hash") or ""),
            hint=str(result.get("hint") or ""),
            output_dir=output_dir,
            completed_at=str(result.get("fetched_at") or now_iso()),
        )
        completed.append(result)

    sleep_seconds = float(args.sleep_seconds or 0)
    if sleep_seconds > 0 and rows:
        time.sleep(sleep_seconds)

    write_json(ledger_path, ledger)
    refreshed = len(completed)
    failed = len(failures)
    payload = {
        "primitive": "refresh_retarget_linkedin_profiles",
        "command": "run",
        "status": "ok" if failed == 0 else "ok_with_failures",
        "review_csv": str(review_csv),
        "retarget_output_dir": str(output_dir),
        "ledger": str(ledger_path),
        "manifest": str(manifest_path),
        "counts": counts,
        "max_workers": max_workers,
        "refreshed": refreshed,
        "failed": failed,
        "failures": failures[:20],
    }
    write_json(manifest_path, payload)
    emit(payload)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh exact LinkedIn retarget profiles through RapidAPI")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("estimate", "run"):
        p = sub.add_parser(name)
        p.add_argument("--review-csv", default=str(DEFAULT_REVIEW_CSV))
        p.add_argument("--retarget-output-dir", default=str(DEFAULT_OUTPUT_DIR))
        p.add_argument("--force", action="store_true", help="Refresh even if a RapidAPI artifact already exists")
        if name == "run":
            p.add_argument("--ledger", default=str(DEFAULT_LEDGER))
            p.add_argument("--manifest")
            p.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
            p.add_argument("--sleep-seconds", type=float, default=0.0)
            p.set_defaults(func=cmd_run)
        else:
            p.set_defaults(func=cmd_estimate)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
