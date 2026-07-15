"""Assemble synthetic people-rows from deep research (no real LinkedIn found).

Implements packs/ingestion/docs/synthetic-profiles-plan.md: for people the
Parallel.ai deep-research pass could NOT find a real LinkedIn for (stealth
founders, no-LinkedIn contacts, user detaches), build a people-schema row from
the research JSON so they stop being invisible in search. A synthetic row looks
exactly like a real one to the merge/index — no downstream special-casing —
except `enrichment_provider="synthetic"`, a `synth-…` public_identifier, and an
`approved` gate: high-completeness rows are `auto`, the rest sit PENDING in
`overrides/synthetic-people.csv` until the user approves (search is never
polluted by un-reviewed researched profiles). Idempotent upsert keyed by
public_identifier; a row the user decided (approved yes/no) is never rewritten.

Reads:  .powerpacks/deep-context/reconcile/deep-research/<handle>/01_research_parallel.json
        (+ research_queue.csv for contact identity, people.csv for carry columns)
Writes: .powerpacks/network-import/overrides/synthetic-people.csv
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.apply_retargets import CARRY_COLUMNS
from packs.ingestion.primitives.deep_context.candidates import (
    candidate_carry,
    candidate_person_id,
    candidate_row,
)
from packs.ingestion.primitives.deep_context.common import now_iso
from packs.ingestion.primitives.deep_context.reconcile_deep_research import DR_OUT_DIR, QUEUE_CSV
from packs.ingestion.primitives.deep_context.reconcile_linkedin import USER_APPROVED
from packs.ingestion.schemas.candidates_schema import candidate_key_for
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS

ROOT = Path(__file__).resolve().parents[4]
DEFAULT_OUT = ROOT / ".powerpacks/network-import/overrides/synthetic-people.csv"
DEFAULT_PEOPLE_CSV = ROOT / ".powerpacks/network-import/merged/people.csv"
SYNTHETIC_COLUMNS = PEOPLE_SCHEMA_COLUMNS + ["approved", "synthetic_metadata"]

# Auto-approve bar: research completeness at/above this flows straight into the
# merge (approved=auto); below it the row waits for the user in the review file.
DEFAULT_AUTO_COMPLETENESS = 0.6


def synth_public_identifier(email: str, phone: str, handle: str) -> str:
    """Stable synthetic identity key, preferring the strongest contact anchor."""
    if email:
        return f"synth-email-{hashlib.sha1(email.strip().lower().encode()).hexdigest()[:12]}"
    if phone:
        return f"synth-phone-{hashlib.sha1(phone.strip().encode()).hexdigest()[:12]}"
    return f"synth-x-{handle.strip().lower()}"


def profile_is_usable(profile: dict[str, Any]) -> bool:
    """Completeness floor: a name plus at least one position or a location."""
    name = ((profile.get("person") or {}).get("full_name") or "").strip()
    if not name:
        return False
    has_position = any((p.get("company_name") or p.get("title")) for p in profile.get("positions") or [])
    loc = profile.get("location") or {}
    has_location = bool(loc.get("city") or loc.get("country"))
    return has_position or has_location


def build_synthetic_row(profile: dict[str, Any], contact: dict[str, str],
                        original: dict[str, str] | None, person_id: str,
                        auto_completeness: float = DEFAULT_AUTO_COMPLETENESS) -> dict[str, str]:
    """Pure mapping: research JSON + contact identity (+ original people row for carry
    columns) -> synthetic people-schema row. No IO."""
    person = profile.get("person") or {}
    loc = profile.get("location") or {}
    meta = profile.get("metadata") or {}
    social = profile.get("social") or {}
    positions = [p for p in profile.get("positions") or [] if p.get("company_name") or p.get("title")]
    education = profile.get("education") or []
    current = next((p for p in positions if p.get("is_current")), None)

    row = {col: "" for col in SYNTHETIC_COLUMNS}
    pub = synth_public_identifier(contact.get("primary_email", ""), contact.get("phone_e164", ""),
                                  contact.get("handle", ""))
    completeness = float(meta.get("estimated_completeness") or 0.0)
    row.update({
        "id": person_id or pub,
        "public_identifier": pub,
        "linkedin_url": "",  # that's the point
        "first_name": person.get("first_name") or "",
        "last_name": person.get("last_name") or "",
        "full_name": person.get("full_name") or contact.get("display_name", ""),
        "headline": (profile.get("headline") or {}).get("text") or "",
        "summary": (profile.get("summary") or {}).get("text") or "",
        "city": loc.get("city") or "",
        "state": loc.get("state") or "",
        "country": loc.get("country") or "",
        "location_raw": loc.get("raw") or ", ".join(v for v in (loc.get("city"), loc.get("country")) if v),
        "work_experiences": json.dumps(positions, ensure_ascii=False) if positions else "",
        "education": json.dumps(education, ensure_ascii=False) if education else "",
        "current_title": (current or {}).get("title") or "",
        "current_company": (current or {}).get("company_name") or "",
        "entity_urn": f"synthetic:{person_id or pub}",
        "enrichment_provider": "synthetic",
        "enriched_at": now_iso(),
        "twitter_handle": social.get("twitter_handle") or "",
        "approved": "auto" if completeness >= auto_completeness else "",
        "synthetic_metadata": json.dumps({
            "completeness": completeness,
            "name_confidence": person.get("confidence"),
            "gaps": meta.get("gaps") or [],
            "research_date": meta.get("research_date") or "",
            "research_method": meta.get("research_method") or "",
            "source_channel": meta.get("source_channel") or contact.get("source_channel") or "",
        }, ensure_ascii=False),
    })
    for col in CARRY_COLUMNS:
        if original and original.get(col):
            row[col] = original[col]
    if not row.get("primary_email") and contact.get("primary_email"):
        row["primary_email"] = contact["primary_email"]
    if not row.get("primary_phone") and contact.get("phone_e164"):
        row["primary_phone"] = contact["phone_e164"]
    return row


def load_rows(path: Path) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                pub = (row.get("public_identifier") or "").strip().lower()
                if pub:
                    rows[pub] = row
    return rows


def write_rows(path: Path, rows: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SYNTHETIC_COLUMNS)
        w.writeheader()
        for pub in sorted(rows):
            w.writerow({k: rows[pub].get(k, "") for k in SYNTHETIC_COLUMNS})


def people_lookup(people_csv: Path) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """(by normalized email, by last-10-digit phone) for carry-column lookup."""
    by_email: dict[str, dict[str, str]] = {}
    by_phone: dict[str, dict[str, str]] = {}
    if people_csv.exists():
        with people_csv.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                email = (row.get("primary_email") or "").strip().lower()
                if email and email not in by_email:
                    by_email[email] = row
                digits = "".join(c for c in (row.get("primary_phone") or "") if c.isdigit())[-10:]
                if digits and digits not in by_phone:
                    by_phone[digits] = row
    return by_email, by_phone


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Build synthetic people-rows for researched people with no real LinkedIn (free, local — reads existing research artifacts).")
    ap.add_argument("--research-dir", default=str(DR_OUT_DIR))
    ap.add_argument("--queue-csv", default=str(QUEUE_CSV))
    ap.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--auto-completeness", type=float, default=DEFAULT_AUTO_COMPLETENESS,
                    help="Research completeness at/above this auto-approves the row (default %(default)s)")
    args = ap.parse_args(argv)

    started = time.monotonic()
    research_dir = Path(args.research_dir)
    queue: dict[str, dict[str, str]] = {}
    qpath = Path(args.queue_csv)
    if qpath.exists():
        with qpath.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("handle"):
                    queue[row["handle"]] = row

    by_email, by_phone = people_lookup(Path(args.people_csv))
    existing = load_rows(Path(args.out))

    built = auto = pending = preserved = with_linkedin = unusable = 0
    for pdir in sorted(research_dir.iterdir()) if research_dir.exists() else []:
        rj = pdir / "01_research_parallel.json"
        if not rj.is_file():
            continue
        try:
            profile = json.loads(rj.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if ((profile.get("social") or {}).get("linkedin_url") or "").strip():
            with_linkedin += 1  # the retarget path owns this person
            continue
        if not profile_is_usable(profile):
            unusable += 1
            continue
        contact = queue.get(pdir.name) or {"handle": pdir.name}
        email = (contact.get("primary_email") or "").strip().lower()
        digits = "".join(c for c in (contact.get("phone_e164") or "") if c.isdigit())[-10:]
        original = by_email.get(email) or (by_phone.get(digits) if digits else None)
        person_id = (original or {}).get("id", "")
        if original is None:
            # Not in people.csv -> the subject is an import candidate: carry its
            # contact identity (emails/phones/counts/channels) onto the minted row.
            crow = candidate_row(candidate_key_for(email, contact.get("phone_e164") or ""))
            if crow:
                original = candidate_carry(crow)
                person_id = candidate_person_id(crow.get("candidate_key", ""))
        row = build_synthetic_row(profile, contact, original, person_id, args.auto_completeness)
        pub = row["public_identifier"].lower()
        if (existing.get(pub, {}).get("approved") or "").strip().lower() in USER_APPROVED:
            preserved += 1  # sticky: the user already decided this synthetic row
            continue
        existing[pub] = row
        built += 1
        auto += row["approved"] == "auto"
        pending += row["approved"] == ""

    write_rows(Path(args.out), existing)
    print(json.dumps({
        "primitive": "assemble_synthetic_profile", "status": "completed",
        "built": built, "auto_approved": auto, "pending_review": pending,
        "preserved_user_rows": preserved, "skipped_with_linkedin": with_linkedin,
        "skipped_unusable": unusable, "total_rows": len(existing),
        "out": str(args.out), "elapsed_ms": int((time.monotonic() - started) * 1000),
    }, indent=2))


if __name__ == "__main__":
    main()
