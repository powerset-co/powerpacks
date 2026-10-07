"""The `owner` command: owner.json from the owner's own LinkedIn, loaded into the store.

Writes <data root>/deep-context/owner.json once from the owner's profile (cache first, one RapidAPI
fetch on a miss) and the emails given: the name, work, education and location LinkedIn lists, plus
the phone numbers this Mac's Messages account answers to. The import flags the owner's own
candidates by these emails and phones, and the judges read the bio (db/owner.py renders it).
Replaces v1's `bin/deep-context owner` for v2.

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
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import CACHE_RELATIVE_DIR, load_profiles
from packs.ingestion.primitives.discover.messages import chatdb
from packs.ingestion.primitives.discover.messages.extract_imessage import DEFAULT_CHAT_DB
from packs.ingestion.primitives.enrich.profile_cache import profile_cache_path, read_usable_cached_profile
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url

OWNER_JSON = Path("deep-context") / "owner.json"


def _year(value: object) -> int:
    """A cache date's year as owner.json stores it: 0 when unknown or current."""
    if isinstance(value, dict) and value.get("year"):
        return int(value["year"])
    return 0


def owner_payload(profile: dict[str, Any], url: str, emails: list[str], phones: list[str]) -> dict[str, Any]:
    """owner.json from a cached profile record's normalized_profile, in the shape owner_from_payload reads."""
    work: list[dict[str, Any]] = []
    for row in profile.get("experiences") or []:
        work.append({"company": str(row.get("company_name") or row.get("companyName") or "").strip(),
                     "title": str(row.get("title") or "").strip(),
                     "start": _year(row.get("starts_at")), "end": _year(row.get("ends_at"))})
    education: list[dict[str, Any]] = []
    for row in profile.get("education") or []:
        note: list[str] = []
        for key in ("degree", "fieldOfStudy"):
            if str(row.get(key) or "").strip():
                note.append(str(row[key]).strip())
        education.append({"school": str(row.get("schoolName") or row.get("school") or "").strip(),
                          "start": _year(row.get("starts_at")), "end": _year(row.get("ends_at")),
                          "note": ", ".join(note)})
    locations: list[str] = []
    if str(profile.get("location_str") or "").strip():
        locations.append(str(profile["location_str"]).strip())
    return {"name": str(profile.get("full_name") or "").strip(), "emails": emails, "phones": phones,
            "education": education, "work": work, "locations": locations, "notes": "", "linkedin_url": url}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write owner.json from the owner's own LinkedIn and load it into the store.")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    parser.add_argument("--linkedin-url", required=True, help="the owner's own LinkedIn profile URL")
    parser.add_argument("--email", action="append", default=[], help="an email of the owner's; repeatable")
    parser.add_argument("--chat-db", type=Path, default=DEFAULT_CHAT_DB, help="the Messages store the owner's numbers are read from")
    args = parser.parse_args(argv)
    url: str = normalize_linkedin_url(args.linkedin_url)
    public_id: str = extract_public_identifier(url)
    if not public_id:
        print(f"not a LinkedIn profile URL: {args.linkedin_url}")
        return 1
    profiles = load_profiles(args.data_root, [url], fetch=True)
    if url not in profiles.found:
        print(f"could not fetch the profile at {url}")
        return 1
    record = read_usable_cached_profile(profile_cache_path(args.data_root / CACHE_RELATIVE_DIR, public_id))
    emails: list[str] = []
    for email in args.email:
        if normalize_email(email) not in emails:
            emails.append(normalize_email(email))
    phones: list[str] = []
    for value in chatdb.owner_phone_identifiers(args.chat_db):
        if normalize_phone(value) and normalize_phone(value) not in phones:
            phones.append(normalize_phone(value))
    payload: dict[str, Any] = owner_payload(record["normalized_profile"], url, emails, phones)
    out: Path = args.data_root / OWNER_JSON
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    owner: OwnerProfile = load_owner(open_store(store_path(args.data_root)), out)
    print(json.dumps({"owner_json": str(out), "name": owner.name, "fetched": profiles.fetched, "emails": len(owner.emails),
                      "phones": len(owner.phones), "work": len(owner.work), "education": len(owner.education)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
