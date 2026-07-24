"""[Context, step 0] Build the mailbox owner's profile (owner.json) from THEIR LinkedIn.

owner.json is the user's own bio timeline — schools/jobs/locations with year ranges. It is
injected as a reasoning anchor so synthesis infers SHARED context (same school/employer/era)
with each contact, and so the LinkedIn self-heal judge can weigh overlaps with you. Without it
that whole signal is lost.

This builds it deterministically from the owner's LinkedIn via the RapidAPI cache (cache-first;
a hit costs nothing) — NEVER from a web fetch of linkedin.com, which hallucinates. Run it FIRST.

Changelog:
  2026-07-23 (audit dedup): now_iso import from common.jsonio instead of deep_context.common (deduped there); no behavior change.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.common import (
    OWNER_JSON,
    PROFILE_CACHE_DIR,
    emit,
    load_env,
)
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.enrich.enrich_people import (
    profile_cache_path,
    rapidapi_key,
    rapidapi_profile,
    read_usable_cached_profile,
)
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url


def _year(value: Any) -> int | None:
    return value.get("year") if isinstance(value, dict) else None


def owner_from_profile(normalized: dict[str, Any], *, email: str = "") -> dict[str, Any]:
    """Map a normalized LinkedIn profile into the owner.json schema."""
    education = []
    for ed in normalized.get("education") or []:
        education.append({
            "school": ed.get("school") or ed.get("school_name") or "",
            "start": _year(ed.get("starts_at")), "end": _year(ed.get("ends_at")),
            "note": " ".join(x for x in [ed.get("degree"), ed.get("field")] if x),
        })
    work = []
    for ex in normalized.get("experiences") or []:
        work.append({
            "company": ex.get("company_name") or ex.get("company") or "",
            "title": ex.get("title") or "",
            "start": _year(ex.get("starts_at")), "end": _year(ex.get("ends_at")),
        })
    location = normalized.get("location_str") or ", ".join(
        x for x in [normalized.get("city"), normalized.get("state"), normalized.get("country")] if x)
    return {
        "name": normalized.get("full_name") or "",
        "emails": [email] if email else [],
        "education": [e for e in education if e["school"]],
        "work": [w for w in work if w["company"]],
        "locations": [location] if location else [],
        "notes": normalized.get("headline") or "",
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out)
    if out.exists() and not args.force:
        try:
            existing = json.loads(out.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}
        return {"source": "build_owner", "status": "exists", "path": str(out),
                "name": existing.get("name", ""),
                "schools": [e.get("school") for e in existing.get("education", [])],
                "employers": [w.get("company") for w in existing.get("work", [])],
                "hint": "pass --force to rebuild, or --linkedin-url to point at a different profile"}

    url = normalize_linkedin_url(args.linkedin_url or "")
    pub = extract_public_identifier(url).lower()
    if not pub:
        return {"source": "build_owner", "status": "error",
                "error": "no --linkedin-url given (your own LinkedIn) and owner.json not present"}

    load_env()
    cache_dir = Path(args.profile_cache_dir)
    cached = read_usable_cached_profile(profile_cache_path(cache_dir, pub))
    from_cache = cached is not None
    if cached:
        normalized = cached.get("normalized_profile") or {}
    else:
        result = rapidapi_profile(pub, url, rapidapi_key(), cache_dir=cache_dir)
        normalized = result.get("normalized_profile") or {}
        if normalized.get("success") is not True:
            return {"source": "build_owner", "status": "error",
                    "error": result.get("error") or "could not fetch the owner profile (set RAPIDAPI_KEY?)"}

    owner = owner_from_profile(normalized, email=args.email)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(owner, indent=2) + "\n", encoding="utf-8")
    return {
        "source": "build_owner", "status": "written", "path": str(out), "from_cache": from_cache,
        "name": owner["name"], "schools": [e["school"] for e in owner["education"]],
        "employers": [w["company"] for w in owner["work"]], "locations": owner["locations"],
        "updated_at": now_iso(),
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Build owner.json (your bio) from your LinkedIn, cache-first.")
    p.add_argument("--linkedin-url", default="", help="The OWNER's LinkedIn URL (you)")
    p.add_argument("--email", default="", help="The owner's email (for owner identity)")
    p.add_argument("--profile-cache-dir", default=str(PROFILE_CACHE_DIR))
    p.add_argument("--out", default=str(OWNER_JSON))
    p.add_argument("--force", action="store_true", help="Rebuild even if owner.json exists")
    return p


def main(argv: list[str] | None = None) -> int:
    emit(run(build_parser().parse_args(argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
