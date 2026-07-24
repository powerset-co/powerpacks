#!/usr/bin/env python3
"""Pure people-row transforms for LinkedIn enrichment: route, normalize, merge.

No I/O, no HTTP, no cache — every function here maps dict rows / provider
payloads to dict rows, so it is safely importable from any surface (the
enrich_people orchestrator, deep-context retargeting, tests).

- `route_row` / `row_has_profile_gaps` — which enrichment route a people row
  takes (linkedin_provider / needs_resolution / skip_*), and whether a
  LinkedIn-identified row still has profile gaps worth a fetch.
- `normalize_rapidapi` — a raw RapidAPI payload to the shared profile shape
  (experiences through `rapidapi_experience_to_powerpacks`, current title /
  company via `current_position`).
- `merge_provider_profile` — overlay a normalized provider profile onto a base
  people row (non-empty provider values win; JSON-encodes experiences /
  education; stamps enrichment provenance).
- `confirmed_people_row` — the people.csv confirmation predicate (LinkedIn
  identifier + successful rapidapi_response).
- `stamp_enrichment_outcome` / `provider_failure_reason` — the ONLY writer of the
  shared schema's `enrichment_status` / `enrichment_error` columns, and the one
  place a provider failure is turned into human-readable text.
- `generate_person_id` — LinkedIn person id with a stable-key fallback.

Company identity: work experiences preserve `rapidapi_company_id`,
`company_public_identifier`, `company_linkedin_url`, and `company_key`
(`rapidapi:{id}` preferred over `linkedin_company:{slug}`).
`current_company_urn` is a legacy shared-schema field not populated here.

Changelog:
  2026-07-24: added `stamp_enrichment_outcome` / `provider_failure_reason`;
    `merge_provider_profile` now stamps `enrichment_status`/`enrichment_error`
    on every row it returns, so a row the provider could not hydrate is
    annotated instead of silently dropped downstream.
  2026-07-23 (audit decomposition): split out of enrich_people.py verbatim,
    minus the dead `split_name` (its only real consumer imports the
    discover/gmail/msgvault copy).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import now_iso  # noqa: E402
from packs.ingestion.schemas.company_identity import rapidapi_experience_to_powerpacks  # noqa: E402
from packs.ingestion.schemas.linkedin_profile_normalizer import normalize_linkedin_profile  # noqa: E402
from packs.ingestion.schemas.people_schema import (  # noqa: E402
    ENRICHMENT_STATUS_ENRICHED,
    ENRICHMENT_STATUS_FAILED,
    ENRICHMENT_STATUS_SKIPPED,
    extract_public_identifier,
    generate_person_id as generate_linkedin_person_id,
    normalize_linkedin_url,
    normalize_people_row,
    parse_jsonish,
    stable_person_id_from_key,
)

# Longest provider error text kept on a people row. The full text stays in the
# stage's raw_provider_responses/ payloads; this column is a human-readable hint.
MAX_ENRICHMENT_ERROR_CHARS = 300


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
    # Read the provider outcome BEFORE normalizing: the rapidapi_* status columns
    # live on the provider_enriched row, not in the shared people schema, so
    # normalize_people_row is about to drop them.
    failure_reason = provider_failure_reason(base)
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
    return stamp_enrichment_outcome(normalize_people_row(row), attempted=True, error=failure_reason)


def confirmed_people_row(row: dict[str, Any]) -> bool:
    linkedin_url = normalize_linkedin_url(str(row.get("linkedin_url") or ""))
    public_identifier = str(row.get("public_identifier") or extract_public_identifier(linkedin_url) or "").strip()
    if not linkedin_url or not public_identifier:
        return False
    raw = parse_jsonish(row.get("rapidapi_response"), None)
    return isinstance(raw, dict) and normalize_linkedin_profile(raw).get("success") is True


def provider_failure_reason(row: dict[str, Any]) -> str:
    """Human-readable reason text from a provider/cache row's `rapidapi_error` +
    `rapidapi_status_code` columns (both the provider_enriched and the
    recent-failures CSVs carry them). Empty when the row records no failure."""
    error = str(row.get("rapidapi_error") or "").strip()
    status = str(row.get("rapidapi_status_code") or "").strip()
    if error and status and status != "200":
        return f"rapidapi {status}: {error}"
    if error:
        return error
    if status and status != "200":
        return f"rapidapi status {status}"
    return ""


def stamp_enrichment_outcome(row: dict[str, Any], *, attempted: bool, error: str = "") -> dict[str, Any]:
    """Record this run's terminal enrichment outcome on a people row and return it.

    The ONE writer of `enrichment_status` / `enrichment_error`. A row carrying a
    usable provider payload is `enriched`; otherwise it is `failed` when the
    provider was consulted this run (including a fetch suppressed by a cached
    prior failure, where `error` is the cached reason) and `skipped` when it was
    not. The stamp is recomputed from scratch every run, so a stale status from a
    previous people.csv never survives — nothing here deletes the row, which is
    the point: a failure is annotation, not erasure."""
    if confirmed_people_row(row):
        row["enrichment_status"] = ENRICHMENT_STATUS_ENRICHED
        row["enrichment_error"] = ""
    elif attempted:
        row["enrichment_status"] = ENRICHMENT_STATUS_FAILED
        row["enrichment_error"] = str(error or "enrichment failed")[:MAX_ENRICHMENT_ERROR_CHARS]
    else:
        row["enrichment_status"] = ENRICHMENT_STATUS_SKIPPED
        row["enrichment_error"] = ""
    return row
