"""Shared candidates schema for ingestion primitives.

A candidate is a contact worth researching that does NOT yet have a resolved
identity (no LinkedIn attachment). Import stages write candidates next to
their people.csv (`import/<source>/candidates.csv`); the deep-context processing
layer consumes them to build cross-channel context and run one reverse lookup
per person. people.csv keeps meaning "resolved identity" — candidates never
enter it directly.
"""

from __future__ import annotations

import json
import re
from typing import Any

CANDIDATES_SCHEMA_COLUMNS = [
    # Stable identity key: "email:<addr>" or "phone:<e164>".
    "candidate_key",
    # Channel the candidate was discovered on: gmail | imessage | whatsapp.
    "source",
    "full_name",
    "primary_email",
    "all_emails",
    "primary_phone",
    "all_phones",
    # Free-text guesses from discovery (header parsing, signature heuristics).
    "company_guess",
    "title_guess",
    # Same semantics as people_schema: JSON {channel: count} + ISO-8601 UTC.
    "interaction_counts",
    "last_interaction",
    # JSON object of source-specific evidence (match suggestion, header names,
    # sending domain, group flags) for the downstream researcher/judge.
    "evidence",
]

CANDIDATE_JSON_COLUMNS = {"all_emails", "all_phones", "interaction_counts", "evidence"}

PHONE_DIGITS_RE = re.compile(r"\d")


def candidate_key_for(email: str = "", phone: str = "") -> str:
    """Stable key: email wins over phone; both normalized. Empty if neither."""
    email = (email or "").strip().lower()
    if email and "@" in email:
        return f"email:{email}"
    phone = (phone or "").strip()
    digits = "".join(PHONE_DIGITS_RE.findall(phone))
    if digits:
        prefix = "+" if phone.lstrip().startswith("+") else ""
        return f"phone:{prefix}{digits}"
    return ""


def normalize_candidate_row(row: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {col: "" for col in CANDIDATES_SCHEMA_COLUMNS}
    for col in CANDIDATES_SCHEMA_COLUMNS:
        value = row.get(col, "")
        if value is None:
            normalized[col] = ""
        elif isinstance(value, (dict, list)):
            normalized[col] = json.dumps(value, ensure_ascii=False)
        else:
            normalized[col] = str(value)
    if not normalized["candidate_key"]:
        normalized["candidate_key"] = candidate_key_for(
            normalized.get("primary_email", ""), normalized.get("primary_phone", "")
        )
    return normalized
