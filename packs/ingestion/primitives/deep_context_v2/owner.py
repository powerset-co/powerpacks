"""The `owner` command: owner.json from the owner's own LinkedIn, loaded into the store.

Writes <data root>/deep-context/owner.json once from the owner's profile (cache first, one RapidAPI
fetch on a miss) and the emails given: the name, work, education and location LinkedIn lists, plus
the phone numbers this Mac's Messages account answers to. The import flags the owner's own
candidates by these emails and phones, and the judges read the bio (db/owner.py renders it).

Created: 2026-10-07
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.contact_fields import normalize_email, normalize_phone
from packs.ingestion.primitives.deep_context_v2.db.owner import OwnerProfile, load_owner
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import (
    CACHE_RELATIVE_DIR, CacheDate, CacheProfile, ProfileRecord, load_profiles, read_profile_record)
from packs.ingestion.primitives.discover.messages import chatdb
from packs.ingestion.primitives.discover.messages.extract_imessage import DEFAULT_CHAT_DB
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url

OWNER_JSON = Path("deep-context") / "owner.json"


def _year(value: CacheDate | None) -> int:
    """A cache date's year as owner.json stores it: 0 when unknown or current."""
    if value is None:
        return 0
    return value.year


def _text(value: str | None) -> str:
    return (value or "").strip()


def owner_payload(profile: CacheProfile, url: str, emails: list[str], phones: list[str]) -> dict[str, Any]:
    """owner.json from a cached profile record's normalized_profile, in the shape owner_from_payload reads."""
    work: list[dict[str, Any]] = []
    for job in profile.experiences:
        work.append({"company": _text(job.company_name), "title": _text(job.title),
                     "start": _year(job.starts_at), "end": _year(job.ends_at)})
    education: list[dict[str, Any]] = []
    for school in profile.education:
        note: list[str] = []
        for value in (school.degree, school.fieldOfStudy):
            if _text(value):
                note.append(_text(value))
        education.append({"school": _text(school.schoolName) or _text(school.school),
                          "start": _year(school.starts_at), "end": _year(school.ends_at),
                          "note": ", ".join(note)})
    locations: list[str] = []
    if _text(profile.location_str):
        locations.append(_text(profile.location_str))
    return {"name": _text(profile.full_name), "emails": emails, "phones": phones,
            "education": education, "work": work, "locations": locations, "notes": "", "linkedin_url": url}


def build_owner(data_root: Path, linkedin_url: str, emails: list[str], chat_db: Path = DEFAULT_CHAT_DB) -> dict[str, Any]:
    """owner.json from the owner's LinkedIn (cache first, one fetch on a miss) and emails, loaded into the
    store; returns the counts. Raises ValueError when the URL names no profile or the profile cannot be read."""
    url: str = normalize_linkedin_url(linkedin_url)
    public_id: str = extract_public_identifier(url)
    if not public_id:
        raise ValueError(f"not a LinkedIn profile URL: {linkedin_url}")
    profiles = load_profiles(data_root, [url], fetch=True)
    if url not in profiles.found:
        raise ValueError(f"could not fetch the profile at {url}")
    record: ProfileRecord | None = read_profile_record(data_root / CACHE_RELATIVE_DIR, public_id)
    assert record is not None
    normalized: list[str] = []
    for email in emails:
        if normalize_email(email) not in normalized:
            normalized.append(normalize_email(email))
    phones: list[str] = []
    for value in chatdb.owner_phone_identifiers(chat_db):
        if normalize_phone(value) and normalize_phone(value) not in phones:
            phones.append(normalize_phone(value))
    payload: dict[str, Any] = owner_payload(record.normalized_profile, url, normalized, phones)
    out: Path = data_root / OWNER_JSON
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    conn = open_store(store_path(data_root))
    try:
        owner: OwnerProfile = load_owner(conn, out)
        conn.commit()
    finally:
        conn.close()
    return {"owner_json": str(out), "name": owner.name, "fetched": profiles.fetched, "emails": len(owner.emails),
            "phones": len(owner.phones), "work": len(owner.work), "education": len(owner.education)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write owner.json from the owner's own LinkedIn and load it into the store.")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    parser.add_argument("--linkedin-url", required=True, help="the owner's own LinkedIn profile URL")
    parser.add_argument("--email", action="append", default=[], help="an email of the owner's; repeatable")
    parser.add_argument("--chat-db", type=Path, default=DEFAULT_CHAT_DB, help="the Messages store the owner's numbers are read from")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build_owner(args.data_root, args.linkedin_url, args.email, args.chat_db)))
    except ValueError as error:
        print(error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
