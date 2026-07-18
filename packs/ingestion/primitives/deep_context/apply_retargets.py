"""[Phase 3, retarget] Re-attach the CORRECT LinkedIn to people whose link was detached.

The decisions table (review.csv) can carry `retarget` rows: the wrong link is
detached AND a `new_linkedin_url` is the correct person. people.csv is LinkedIn-only and
requires a RapidAPI profile, so re-attaching means ENRICHING the new link and producing a
valid people-schema row. This step does exactly that for every approved retarget:

  1. Enrich `new_linkedin_url` cache-first (profile_cache_v2; RapidAPI only on a miss — auto).
  2. Build a people row (valid rapidapi_response + work_experiences/education) and CARRY the
     original contact's emails/phones/interaction_counts so the merge keeps the person whole.
  3. Write all rows to overrides/retarget-people.csv — which the fan-in merge auto-ingests.

Only rows with action=retarget AND approved ∈ {auto, yes} are applied (a user `no`/pending
retarget is skipped). Enrichment is automatic (RapidAPI is cache-first + effectively free).
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.candidates import (
    candidate_carry,
    candidate_key_of,
    candidate_row,
)
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    LINKEDIN_OVERRIDES_CSV,
    PROFILE_CACHE_DIR,
    RETARGET_PEOPLE_CSV,
    emit,
    load_env,
    now_iso,
)
from packs.ingestion.primitives.deep_context.reconcile_linkedin import (
    USER_APPROVED,
    load_override_rows,
)
from packs.ingestion.primitives.deep_context.review_store import write_override_rows
from packs.ingestion.primitives.enrich_people.enrich_people import (
    merge_provider_profile,
    normalize_rapidapi,
    rapidapi_key,
    rapidapi_profile,
)
from packs.ingestion.schemas.people_schema import (
    PEOPLE_SCHEMA_COLUMNS,
    extract_public_identifier,
    normalize_linkedin_url,
)

# Contact identity carried from the original (detached) person onto the re-attached row,
# so the merge groups the re-enriched person with their real messages/contacts.
CARRY_COLUMNS = ["primary_email", "all_emails", "primary_phone", "all_phones",
                 "interaction_counts", "last_interaction", "source_channels"]
APPLY_APPROVED = {"auto"} | USER_APPROVED  # auto or yes (never pending / no)


def load_people_index(people_csv: Path) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """(by public_identifier, by id) for looking up the original contact's metadata."""
    by_pub: dict[str, dict[str, str]] = {}
    by_id: dict[str, dict[str, str]] = {}
    if not people_csv.exists():
        return by_pub, by_id
    with people_csv.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pub = (row.get("public_identifier") or "").strip().lower()
            if pub:
                by_pub[pub] = row
            pid = (row.get("id") or "").strip()
            if pid:
                by_id[pid] = row
    return by_pub, by_id


def enrich_one(new_url: str, new_pub: str, cache_dir: Path, api_key: str) -> dict[str, Any]:
    """Cache-first enrichment of one LinkedIn URL -> {raw, normalized, from_cache, error}."""
    result = rapidapi_profile(new_pub, new_url, api_key, cache_dir=cache_dir)
    normalized = result.get("normalized_profile") or {}
    if normalized.get("success") is not True:
        return {"raw": None, "from_cache": result.get("from_cache", False),
                "error": result.get("error") or "enrichment failed / no profile"}
    return {"raw": result.get("data"), "from_cache": result.get("from_cache", False), "error": ""}


def build_retarget_row(new_url: str, new_pub: str, raw: dict[str, Any],
                       original: dict[str, str]) -> dict[str, str]:
    """Enriched people row for the correct LinkedIn, carrying the contact's identity."""
    rapid = normalize_rapidapi(raw, new_pub, new_url)
    row = merge_provider_profile({}, rapid, raw)  # valid rapidapi_response + profile columns
    for col in CARRY_COLUMNS:
        if original.get(col):
            row[col] = original[col]
    out = {col: "" for col in PEOPLE_SCHEMA_COLUMNS}
    for col in PEOPLE_SCHEMA_COLUMNS:
        if row.get(col) not in (None, ""):
            out[col] = row[col]
    out["public_identifier"] = new_pub
    out["linkedin_url"] = new_url
    return out


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    overrides = load_override_rows(Path(args.overrides_csv))
    by_pub, by_id = load_people_index(Path(args.people_csv))

    # Marker lifecycle: retarget-people.csv is overwritten each run and the
    # fan-in merge consumes it, so nothing used to close out the SOURCE row —
    # applied retargets kept reading as "pending" forever (and re-enriched on
    # later runs), while proposals whose old identity left the review model
    # became invisible limbo. Reconcile both here, before selecting work:
    #  - a row whose new pub already lives in people.csv is REALIZED: stamp
    #    approved=yes (recording what the merge already did) and skip it;
    #  - a still-pending proposal that resolves to NO current identity (old pub,
    #    person id, and candidate row all gone) and is not realized is STRANDED:
    #    reported so the agent can re-propose or drop it, never silently lost.
    all_markers = [r for r in overrides.values()
                   if (r.get("action") or "").strip().lower() == "retarget"]
    finalized = 0
    stranded: list[dict[str, str]] = []
    realized_pubs: set[str] = set()
    for r in all_markers:
        new_url = normalize_linkedin_url(r.get("new_linkedin_url") or "")
        new_pub = (r.get("new_public_identifier") or "").strip().lower() or \
            extract_public_identifier(new_url).lower()
        old_pub = (r.get("public_identifier") or "").strip().lower()
        approved = (r.get("approved") or "").strip().lower()
        if new_pub and new_pub in by_pub:
            realized_pubs.add(old_pub)
            if approved not in ("yes", "no"):
                r["approved"] = "yes"
                finalized += 1
            continue
        if approved:
            continue
        pid = (r.get("person_id") or "").strip()
        resolvable = bool(
            by_pub.get(old_pub) or by_id.get(pid)
            or candidate_row(candidate_key_of(pid) or candidate_key_of(old_pub)))
        if not resolvable:
            stranded.append({"old": old_pub, "new": new_pub})
    if finalized:
        write_override_rows(Path(args.overrides_csv), overrides)

    retargets = [r for r in all_markers
                 if (r.get("public_identifier") or "").strip().lower() not in realized_pubs
                 and (r.get("approved") or "").strip().lower() in APPLY_APPROVED]

    if retargets:
        load_env()
    api_key = rapidapi_key()
    rows: list[dict[str, str]] = []
    enriched = cache_hits = misses = skipped = 0
    details: list[dict[str, Any]] = []
    for r in retargets:
        new_url = normalize_linkedin_url(r.get("new_linkedin_url") or "")
        new_pub = (r.get("new_public_identifier") or "").strip().lower() or extract_public_identifier(new_url).lower()
        old_pub = (r.get("public_identifier") or "").strip().lower()
        if not new_url or not new_pub:
            skipped += 1
            details.append({"old": old_pub, "status": "skipped", "reason": "no new_linkedin_url"})
            continue
        result = enrich_one(new_url, new_pub, Path(args.profile_cache_dir), api_key)
        if result["error"]:
            skipped += 1
            details.append({"old": old_pub, "new": new_pub, "status": "skipped", "reason": result["error"]})
            continue
        enriched += 1
        cache_hits += bool(result["from_cache"])
        misses += not result["from_cache"]
        pid = (r.get("person_id") or "").strip()
        original = by_pub.get(old_pub) or by_id.get(pid) or {}
        if not original:
            # candidate:<key> parent -> contact identity lives in candidates.csv, not people.csv
            crow = candidate_row(candidate_key_of(pid) or candidate_key_of(old_pub))
            if crow:
                original = candidate_carry(crow)
        rows.append(build_retarget_row(new_url, new_pub, result["raw"], original))
        details.append({"old": old_pub, "new": new_pub, "status": "enriched",
                        "from_cache": result["from_cache"]})

    out_path = Path(args.out_csv)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=PEOPLE_SCHEMA_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    return {
        "source": "apply_retargets", "status": "completed",
        "approved_retargets": len(retargets), "enriched": enriched,
        "cache_hits": cache_hits, "rapidapi_misses": misses, "skipped": skipped,
        "finalized_applied": finalized,
        "stranded_count": len(stranded), "stranded": stranded[:25],
        "retarget_people_csv": str(out_path), "rows": len(rows),
        "details": details[:50],
        "elapsed_ms": int((time.monotonic() - started) * 1000), "updated_at": now_iso(),
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Enrich + build re-attach rows for approved retargets.")
    p.add_argument("--overrides-csv", default=str(LINKEDIN_OVERRIDES_CSV))
    p.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    p.add_argument("--profile-cache-dir", default=str(PROFILE_CACHE_DIR))
    p.add_argument("--out-csv", default=str(RETARGET_PEOPLE_CSV))
    return p


def main(argv: list[str] | None = None) -> int:
    emit(run(build_parser().parse_args(argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
