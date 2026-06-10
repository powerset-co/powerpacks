"""Shared people schema for ingestion primitives.

This is the local interchange shape used by Gmail/LinkedIn/Twitter/messages
merge flows. It is intentionally a superset: source-specific columns may be
blank for channels that do not provide them. Canonical exports should be named
`people.csv`; legacy-compatible aliases such as `people_harmonic_all.csv` may
exist temporarily, but this module is provider-neutral.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import uuid
from typing import Any

PEOPLE_SCHEMA_COLUMNS = [
    "id",
    "public_identifier",
    "linkedin_url",
    "first_name",
    "last_name",
    "full_name",
    "headline",
    "summary",
    "city",
    "state",
    "country",
    "location_raw",
    "profile_picture_url",
    "work_experiences",
    "education",
    "current_title",
    "current_company",
    "current_company_urn",
    "entity_urn",
    "enrichment_provider",
    "enriched_at",
    "harmonic_response",
    "harmonic_location",
    "rapidapi_response",
    # Source-specific / merge-friendly extensions.
    "twitter_handle",
    "twitter_response",
    "primary_email",
    "all_emails",
    "primary_phone",
    "all_phones",
    "source_channels",
    "source_artifacts",
]

JSON_LIST_COLUMNS = {"work_experiences", "education"}
JSON_OBJECT_COLUMNS = {"harmonic_response", "harmonic_location", "rapidapi_response", "twitter_response"}
# Multi-value identity columns. Stored as a JSON string list (legacy rows may
# be comma/semicolon separated). When merging rows for the same person, these
# are set-unioned across all source rows — never first-value-wins — so every
# directory-resolved alias (e.g. work + personal email) survives into the
# merged profile and downstream interaction-count joins.
LIST_VALUE_COLUMNS = {"all_emails", "all_phones"}
PERSON_ID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def extract_public_identifier(linkedin_url: str) -> str:
    if not linkedin_url:
        return ""
    match = re.search(r"linkedin\.com/in/([^/?#]+)", linkedin_url, re.IGNORECASE)
    if not match:
        return ""
    return urllib.parse.unquote(match.group(1).strip().rstrip("/")).lower()


def normalize_linkedin_url(value: str) -> str:
    url = (value or "").strip()
    if not url:
        return ""
    if url.startswith("linkedin.com/"):
        url = "https://www." + url
    elif url.startswith("www.linkedin.com/"):
        url = "https://" + url
    url = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    public_id = extract_public_identifier(url)
    return f"https://www.linkedin.com/in/{public_id}" if public_id else url


def stable_linkedin_key(row: dict[str, Any]) -> str:
    public_id = (row.get("public_identifier") or "").strip().lower()
    if not public_id:
        public_id = extract_public_identifier(row.get("linkedin_url") or "")
    return f"linkedin:{public_id}" if public_id else ""


def stable_person_id_from_key(key: str) -> str:
    """Return the Aleph-compatible deterministic person UUID for a stable key."""

    return str(uuid.uuid5(PERSON_ID_NAMESPACE, str(key or "").strip().lower()))


def generate_person_id(public_identifier: str) -> str:
    """Return Aleph's canonical UUIDv5 for a LinkedIn public identifier."""

    public_id = str(public_identifier or "").strip().lower()
    return stable_person_id_from_key(f"linkedin:{public_id}")


def normalize_people_row(row: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {col: "" for col in PEOPLE_SCHEMA_COLUMNS}
    for col in PEOPLE_SCHEMA_COLUMNS:
        value = row.get(col, "")
        if value is None:
            normalized[col] = ""
        elif isinstance(value, (dict, list)):
            normalized[col] = json.dumps(value, ensure_ascii=False)
        else:
            normalized[col] = str(value)
    normalized["linkedin_url"] = normalize_linkedin_url(normalized.get("linkedin_url", ""))
    if not normalized.get("public_identifier"):
        normalized["public_identifier"] = extract_public_identifier(normalized.get("linkedin_url", ""))
    return normalized


def parse_jsonish(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default
