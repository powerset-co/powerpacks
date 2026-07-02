#!/usr/bin/env python3
"""Resolve a LinkedIn URL into a normalized profile for similar-people search.

Lookup order (cheapest first):
1. Local RapidAPI profile cache (.powerpacks/network-import/profile_cache_v2)
2. Local DuckDB search index (local_person_profiles + positions)
3. Postgres persons.hydrated_context (when remote creds are available)
4. RapidAPI get-profile-data-by-url (runs by default; disable with --no-fetch)

Output is a compact, source-agnostic profile summary $search deep mode
uses to derive traits and build one similar-person candidate search. The
RapidAPI path reuses enrich_people's fetch + cache handling, so a paid fetch
seeds the same cache used by ingestion.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packs/search/primitives/lib"))
ENRICH_DIR = ROOT / "packs/ingestion/primitives/enrich_people"
sys.path.insert(0, str(ENRICH_DIR))

from packs.ingestion.schemas.people_schema import (  # noqa: E402
    extract_public_identifier,
    normalize_linkedin_url,
)

DEFAULT_CACHE_DIR = ROOT / ".powerpacks/network-import/profile_cache_v2"
DEFAULT_LOCAL_DB = ROOT / ".powerpacks/search-index/local-search.duckdb"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def load_env_file(env_file: str | None) -> None:
    if not env_file:
        return
    path = Path(env_file)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        return
    for raw in path.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value and not os.environ.get(key.strip()):
            os.environ[key.strip()] = value


# ---------------------------------------------------------------------------
# Profile summary shape
# ---------------------------------------------------------------------------


def summary_from_normalized(normalized: dict[str, Any], *, source: str) -> dict[str, Any]:
    """Compact summary from a RapidAPI-normalized profile (cache or live)."""
    experiences = [exp for exp in normalized.get("experiences", []) or [] if isinstance(exp, dict)]
    positions = []
    for exp in experiences:
        positions.append({
            "title": exp.get("title") or "",
            "company": exp.get("company") or exp.get("company_name") or "",
            "start_date": exp.get("start_date") or "",
            "end_date": exp.get("end_date") or "",
            "is_current": not (exp.get("end_date") or "").strip(),
            "description": (exp.get("description") or "")[:500],
        })
    education = [
        {
            "school": edu.get("school") or edu.get("school_name") or "",
            "degree": edu.get("degree") or "",
            "field_of_study": edu.get("field_of_study") or "",
        }
        for edu in normalized.get("education", []) or []
        if isinstance(edu, dict)
    ]
    return {
        "source": source,
        "public_identifier": normalized.get("public_identifier") or "",
        "linkedin_url": normalized.get("linkedin_url") or "",
        "full_name": normalized.get("full_name") or "",
        "headline": normalized.get("headline") or "",
        "location": normalized.get("location_str") or ", ".join(
            part for part in [normalized.get("city"), normalized.get("state"), normalized.get("country")] if part
        ),
        "positions": positions,
        "education": education,
        "skills": [s for s in normalized.get("skills", []) or [] if isinstance(s, str)][:30],
    }


def summary_from_hydrated(profile: dict[str, Any], *, source: str) -> dict[str, Any]:
    """Compact summary from a hydrated_context-shaped profile (DuckDB/Postgres)."""
    positions = []
    for pos in profile.get("positions", []) or []:
        if not isinstance(pos, dict):
            continue
        positions.append({
            "title": pos.get("position_title") or pos.get("title") or "",
            "company": pos.get("company_name") or pos.get("company") or "",
            "start_date": pos.get("start_date") or "",
            "end_date": pos.get("end_date") or "",
            "is_current": bool(pos.get("is_current")) or not (pos.get("end_date") or ""),
            "description": (pos.get("description") or pos.get("dense_text") or "")[:500],
            "seniority_band": pos.get("seniority_band") or "",
            "role_track": pos.get("role_track") or "",
        })
    education = [
        {
            "school": edu.get("school_name") or edu.get("school") or "",
            "degree": edu.get("degree") or "",
            "field_of_study": edu.get("field_of_study") or "",
        }
        for edu in profile.get("education", []) or []
        if isinstance(edu, dict)
    ]
    return {
        "source": source,
        "public_identifier": profile.get("public_identifier") or "",
        "linkedin_url": profile.get("linkedin_url") or "",
        "full_name": profile.get("name") or profile.get("full_name") or "",
        "headline": profile.get("headline") or "",
        "location": profile.get("location") or "",
        "positions": positions,
        "education": education,
        "skills": [s for s in profile.get("tech_skills", []) or [] if isinstance(s, str)][:30],
    }


# ---------------------------------------------------------------------------
# Lookup tiers
# ---------------------------------------------------------------------------


def lookup_profile_cache(public_identifier: str, cache_dir: Path) -> dict[str, Any] | None:
    import enrich_people  # noqa: PLC0415

    cache_path = enrich_people.profile_cache_path(cache_dir, public_identifier)
    cached = enrich_people.read_usable_cached_profile(cache_path)
    if not cached:
        return None
    normalized = cached.get("normalized_profile") or {}
    summary = summary_from_normalized(normalized, source="profile_cache")
    summary["fetched_at"] = cached.get("fetched_at") or ""
    return summary


def lookup_local_duckdb(public_identifier: str, db_path: Path) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    try:
        import duckdb  # noqa: PLC0415
    except ImportError:
        return None
    try:
        conn = duckdb.connect(str(db_path), read_only=True)
    except Exception:
        return None
    try:
        row = conn.execute(
            """
            select person_id, public_identifier, linkedin_url, full_name, headline,
                   location_raw, hydrated_context
            from local_person_profiles
            where lower(public_identifier) = lower(?)
               or lower(linkedin_url) like lower(?)
            limit 1
            """,
            [public_identifier, f"%linkedin.com/in/{public_identifier}%"],
        ).fetchone()
    except Exception:
        return None
    finally:
        conn.close()
    if not row:
        return None
    context = row[6]
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except Exception:
            context = {}
    context = context if isinstance(context, dict) else {}
    context.setdefault("public_identifier", row[1])
    context.setdefault("linkedin_url", row[2])
    context.setdefault("name", row[3])
    context.setdefault("headline", row[4])
    context.setdefault("location", row[5])
    summary = summary_from_hydrated(context, source="local_duckdb")
    summary["person_id"] = str(row[0])
    return summary


def lookup_postgres(public_identifier: str) -> dict[str, Any] | None:
    try:
        from postgres_client import database_url, ensure_psycopg2  # noqa: PLC0415
    except ImportError:
        return None
    try:
        psycopg2 = ensure_psycopg2()
        url = database_url()
    except Exception:
        return None
    query = """
        SELECT id::text, public_identifier, public_profile_url, full_name, headline,
               location_raw, hydrated_context
        FROM persons
        WHERE lower(public_identifier) = lower(%s)
          AND hydrated_context IS NOT NULL
        LIMIT 1
    """
    try:
        with psycopg2.connect(url) as conn:
            with conn.cursor() as cur:
                cur.execute(query, (public_identifier,))
                row = cur.fetchone()
    except Exception:
        return None
    if not row:
        return None
    context = row[6]
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except Exception:
            context = {}
    context = context if isinstance(context, dict) else {}
    context.setdefault("public_identifier", row[1])
    context.setdefault("linkedin_url", row[2])
    context.setdefault("name", row[3])
    context.setdefault("headline", row[4])
    context.setdefault("location", row[5])
    summary = summary_from_hydrated(context, source="postgres")
    summary["person_id"] = str(row[0])
    return summary


def fetch_rapidapi(public_identifier: str, linkedin_url: str, cache_dir: Path) -> tuple[dict[str, Any] | None, str]:
    import enrich_people  # noqa: PLC0415

    api_key = (
        os.environ.get("RAPIDAPI_LINKEDIN_KEY", "").strip()
        or os.environ.get("RAPIDAPI_KEY", "").strip()
    )
    if not api_key:
        return None, "RAPIDAPI_LINKEDIN_KEY/RAPIDAPI_KEY is not set"
    result = enrich_people.rapidapi_profile(
        public_identifier,
        linkedin_url,
        api_key,
        cache_dir=cache_dir,
    )
    normalized = result.get("normalized_profile") or {}
    if normalized.get("success") is not True:
        return None, str(result.get("error") or normalized.get("error") or f"status={result.get('status_code')}")
    summary = summary_from_normalized(normalized, source="rapidapi")
    summary["from_cache"] = bool(result.get("from_cache"))
    return summary, ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def resolve(args: argparse.Namespace) -> dict[str, Any]:
    linkedin_url = normalize_linkedin_url(args.linkedin_url)
    public_identifier = extract_public_identifier(linkedin_url)
    if not public_identifier:
        return {
            "primitive": "fetch_person_profile",
            "status": "failed",
            "error": f"could not extract a LinkedIn public identifier from: {args.linkedin_url}",
        }

    cache_dir = Path(args.cache_dir)
    db_path = Path(args.local_db)
    attempted: list[str] = []

    tiers = [
        ("profile_cache", lambda: lookup_profile_cache(public_identifier, cache_dir)),
        ("local_duckdb", lambda: lookup_local_duckdb(public_identifier, db_path)),
        ("postgres", lambda: lookup_postgres(public_identifier)),
    ]
    for name, fn in tiers:
        attempted.append(name)
        summary = fn()
        if summary and (summary.get("positions") or summary.get("headline")):
            return {
                "primitive": "fetch_person_profile",
                "status": "completed",
                "created_at": now_iso(),
                "linkedin_url": linkedin_url,
                "public_identifier": public_identifier,
                "attempted_sources": attempted,
                "profile": summary,
            }

    if args.no_fetch:
        return {
            "primitive": "fetch_person_profile",
            "status": "not_found",
            "created_at": now_iso(),
            "linkedin_url": linkedin_url,
            "public_identifier": public_identifier,
            "attempted_sources": attempted,
            "message": "Profile not found in the local cache, local index, or Postgres; RapidAPI fetch disabled by --no-fetch.",
        }

    attempted.append("rapidapi")
    summary, error = fetch_rapidapi(public_identifier, linkedin_url, cache_dir)
    if summary:
        return {
            "primitive": "fetch_person_profile",
            "status": "completed",
            "created_at": now_iso(),
            "linkedin_url": linkedin_url,
            "public_identifier": public_identifier,
            "attempted_sources": attempted,
            "profile": summary,
        }
    return {
        "primitive": "fetch_person_profile",
        "status": "failed",
        "created_at": now_iso(),
        "linkedin_url": linkedin_url,
        "public_identifier": public_identifier,
        "attempted_sources": attempted,
        "error": error or "profile not found",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve a LinkedIn URL to a normalized profile summary")
    parser.add_argument("--linkedin-url", required=True, help="LinkedIn profile URL or /in/ identifier")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--local-db", default=str(DEFAULT_LOCAL_DB))
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--no-fetch", action="store_true", help="Disable the RapidAPI fallback (local/remote lookups only)")
    parser.add_argument("--allow-fetch", action="store_true", help=argparse.SUPPRESS)  # legacy no-op; RapidAPI runs by default
    parser.add_argument("--out", help="Optional path to also write the result JSON")
    args = parser.parse_args()
    load_env_file(args.env_file)
    result = resolve(args)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    emit(result)
    raise SystemExit(0 if result.get("status") in {"completed", "not_found"} else 1)


if __name__ == "__main__":
    main()
