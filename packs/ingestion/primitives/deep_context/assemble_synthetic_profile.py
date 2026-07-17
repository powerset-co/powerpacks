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
    current_parent_by_person_id,
    effective_network_worth,
    is_candidate_id,
)
from packs.ingestion.primitives.deep_context.common import (
    ENRICH_MANIFEST,
    INDEX_JSON,
    LINKEDIN_OVERRIDES_CSV,
    VERDICTS_JSONL,
    now_iso,
)
from packs.ingestion.primitives.import_contacts_pipeline.common import write_manifest
from packs.ingestion.primitives.deep_context.reconcile_deep_research import DR_OUT_DIR, QUEUE_CSV
from packs.ingestion.primitives.deep_context.reconcile_linkedin import USER_APPROVED, load_override_rows
from packs.ingestion.schemas.candidates_schema import candidate_key_for
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS

ROOT = Path(__file__).resolve().parents[4]
DEFAULT_OUT = ROOT / ".powerpacks/network-import/overrides/synthetic-people.csv"
DEFAULT_PEOPLE_CSV = ROOT / ".powerpacks/network-import/merged/people.csv"
SYNTHETIC_PROVENANCE_COLUMNS = [
    "source_parent_slug",
    "source_person_ids",
    "source_candidate_public_identifier",
]
SYNTHETIC_COLUMNS = (
    PEOPLE_SCHEMA_COLUMNS
    + SYNTHETIC_PROVENANCE_COLUMNS
    + ["approved", "synthetic_metadata"]
)

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


def _inherit_decision(existing: dict[str, dict[str, str]], pubs: set[str]) -> str:
    """The strongest human decision across a set of colliding synthetic pubs.

    When merged children's stale synthetic rows collapse onto one current parent,
    the survivor must not silently drop a decision any of them carried. Precedence:
    an explicit user gate (`yes`/`no`) wins over a machine gate (`auto`/blank);
    among competing user gates `no` (exclude) wins over `yes` (keep) — the safer,
    more-conservative call — so a person the user excluded stays excluded. Returns
    '' when no colliding row carries a user gate.
    """
    decisions = {
        (existing.get(pub, {}).get("approved") or "").strip().lower()
        for pub in pubs
    }
    if "no" in decisions:
        return "no"
    if "yes" in decisions:
        return "yes"
    return ""


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
                        auto_completeness: float = DEFAULT_AUTO_COMPLETENESS,
                        provenance: dict[str, str] | None = None) -> dict[str, str]:
    """Pure mapping: research JSON + contact identity (+ original people row for carry
    columns) -> synthetic people-schema row. No IO."""
    person = profile.get("person") or {}
    loc = profile.get("location") or {}
    meta = profile.get("metadata") or {}
    social = profile.get("social") or {}
    positions = [p for p in profile.get("positions") or [] if p.get("company_name") or p.get("title")]
    education = profile.get("education") or []
    current = next((p for p in positions if p.get("is_current")), None)
    provenance = provenance or {}

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
        "source_parent_slug": provenance.get("source_parent_slug") or "",
        "source_person_ids": provenance.get("source_person_ids") or "",
        "source_candidate_public_identifier": (
            provenance.get("source_candidate_public_identifier") or ""
        ),
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


def _completeness(profile: dict[str, Any]) -> float:
    return float((profile.get("metadata") or {}).get("estimated_completeness") or 0.0)


def _position_key(pos: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(pos.get("company_name") or "").strip().lower(),
        str(pos.get("title") or "").strip().lower(),
        str(pos.get("start_date") or "").strip().lower(),
    )


def _education_key(edu: dict[str, Any]) -> tuple[str, str]:
    return (
        str(edu.get("school_name") or edu.get("school") or "").strip().lower(),
        str(edu.get("degree") or "").strip().lower(),
    )


def merge_research_profiles(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministically union >1 research JSON for the SAME merged parent into one.

    Each merged child was researched separately, so a collapsed parent can carry two
    research outputs. We union positions and education (order-stable, deduped), keep
    the best headline/summary/location (from the most complete profile that has one),
    take the max completeness, and union gaps/identity — no LLM. Called only when a
    parent owns more than one child research dir; a single profile passes through.
    """
    usable = [p for p in profiles if isinstance(p, dict)]
    if len(usable) <= 1:
        return usable[0] if usable else {}
    # Most complete first — its scalars (headline/summary/name/location) win ties.
    ordered = sorted(usable, key=_completeness, reverse=True)

    def first_text(getter) -> str:
        for prof in ordered:
            value = getter(prof)
            if value:
                return value
        return ""

    def first_location() -> dict[str, Any]:
        for prof in ordered:
            loc = prof.get("location") or {}
            if loc.get("city") or loc.get("country"):
                return loc
        return ordered[0].get("location") or {}

    positions: list[dict[str, Any]] = []
    seen_pos: set[tuple[str, str, str]] = set()
    education: list[dict[str, Any]] = []
    seen_edu: set[tuple[str, str]] = set()
    for prof in ordered:
        for pos in prof.get("positions") or []:
            if not isinstance(pos, dict) or not (pos.get("company_name") or pos.get("title")):
                continue
            key = _position_key(pos)
            if key not in seen_pos:
                seen_pos.add(key)
                positions.append(pos)
        for edu in prof.get("education") or []:
            if not isinstance(edu, dict):
                continue
            key = _education_key(edu)
            if key != ("", "") and key not in seen_edu:
                seen_edu.add(key)
                education.append(edu)

    gaps: list[str] = []
    for prof in ordered:
        for gap in (prof.get("metadata") or {}).get("gaps") or []:
            text = str(gap or "").strip()
            if text and text not in gaps:
                gaps.append(text)

    best = ordered[0]
    person = dict(best.get("person") or {})
    metadata = dict(best.get("metadata") or {})
    metadata["estimated_completeness"] = max(_completeness(p) for p in ordered)
    metadata["gaps"] = gaps
    return {
        **best,
        "person": person,
        "headline": {"text": first_text(lambda p: (p.get("headline") or {}).get("text"))},
        "summary": {"text": first_text(lambda p: (p.get("summary") or {}).get("text"))},
        "location": first_location(),
        "positions": positions,
        "education": education,
        "metadata": metadata,
    }


def load_rows(path: Path) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                pub = (row.get("public_identifier") or "").strip().lower()
                if pub:
                    rows[pub] = row
    return rows


def _json_list(value: str) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in parsed if str(item).strip()))


def load_verdict_provenance(path: Path) -> dict[str, dict[str, str]]:
    """Recover stable dossier lineage for legacy research directories.

    The current research queue carries these fields directly. Older fixed-name
    queues were overwritten between runs, so verdicts.jsonl is the durable local
    fallback for already-produced research output such as detached LinkedIns.
    """
    by_parent: dict[str, dict[str, str]] = {}
    if not path.exists():
        return by_parent
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        parent_slug = str(row.get("parent_slug") or "").strip()
        if not parent_slug:
            continue
        emails = [str(value).strip() for value in row.get("match_emails") or [] if str(value).strip()]
        phones = [str(value).strip() for value in row.get("match_phones") or [] if str(value).strip()]
        by_parent[parent_slug] = {
            "source_parent_slug": parent_slug,
            "source_person_ids": json.dumps(row.get("person_ids") or [], ensure_ascii=False),
            "source_candidate_public_identifier": str(row.get("candidate_key") or "").strip(),
            "display_name": str(row.get("name") or "").strip(),
            "primary_email": emails[0] if emails else "",
            "phone_e164": phones[0] if phones else "",
        }
    return by_parent


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
    ap.add_argument("--verdicts-jsonl", default=str(VERDICTS_JSONL))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--index-json", default=str(INDEX_JSON),
                    help="Deep-context index.json (child->current-parent membership) for re-keying merged parents")
    ap.add_argument("--auto-completeness", type=float, default=DEFAULT_AUTO_COMPLETENESS,
                    help="Research completeness at/above this auto-approves the row (default %(default)s)")
    ap.add_argument("--manifest", help="Fixed Enrich Contacts manifest (defaults on the canonical research path)")
    args = ap.parse_args(argv)

    started = time.monotonic()
    research_dir = Path(args.research_dir)
    queue: dict[str, dict[str, str]] = {}
    qpath = Path(args.queue_csv)
    queue_is_current = qpath.exists()
    if qpath.exists():
        with qpath.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("handle"):
                    queue[row["handle"]] = row

    by_email, by_phone = people_lookup(Path(args.people_csv))
    existing = load_rows(Path(args.out))
    verdict_provenance = load_verdict_provenance(Path(args.verdicts_jsonl))
    overrides = load_override_rows(LINKEDIN_OVERRIDES_CSV)

    # The output is fixed and overwrite-in-place. Rebuild machine-owned rows
    # only from this queue; otherwise an old model-Yes synthetic could survive
    # after the current People decision moved to No. Explicit user gates remain
    # sticky and are never pruned here.
    pruned_stale = 0
    if queue_is_current:
        for pub, row in list(existing.items()):
            approved = str(row.get("approved") or "").strip().lower()
            handle = str(row.get("source_parent_slug") or "").strip()
            if not handle and pub.startswith("synth-x-"):
                handle = pub.removeprefix("synth-x-")
            if handle and approved not in USER_APPROVED:
                existing.pop(pub, None)
                pruned_stale += 1

    # Child -> current-parent membership. A later cluster_merge can fold two former
    # parents into one; the per-person research dirs keyed on the OLD parent slugs are
    # re-keyed here so their outputs GROUP on the current parent instead of minting a
    # stale row each. No re-fetch — the existing research JSON is reused as-is.
    parent_map = current_parent_by_person_id(Path(args.index_json))

    # Phase 1: collect every usable no-LinkedIn research output, resolving each to the
    # current parent that owns its person_ids. Entries sharing a current parent collapse
    # into one synthetic row in phase 2.
    built = auto = pending = preserved = with_linkedin = unusable = worth_no = 0
    collapsed = 0
    groups: dict[str, dict[str, Any]] = {}
    for pdir in sorted(research_dir.iterdir()) if research_dir.exists() else []:
        if queue_is_current and pdir.name not in queue:
            continue
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
        contact: dict[str, str] = {"handle": pdir.name}
        for source in (verdict_provenance.get(pdir.name) or {}, queue.get(pdir.name) or {}):
            for key, value in source.items():
                if value:
                    contact[key] = value
        stale_slug = contact.get("source_parent_slug") or pdir.name
        source_person_ids = _json_list(contact.get("source_person_ids") or "")
        # The CURRENT parent that owns any of these person_ids (via index membership).
        # Falls back to the stale slug when the parent is still live/unindexed.
        current_slug = ""
        for pid in source_person_ids:
            current_slug = parent_map.get(pid.strip().lower(), "")
            if current_slug:
                break
        current_slug = current_slug or stale_slug
        group_key = current_slug or pdir.name
        entry = groups.setdefault(group_key, {
            "current_slug": current_slug,
            "profiles": [],
            "contacts": [],
            "person_ids": [],
            "candidate_pubs": [],
            "handles": [],
        })
        entry["profiles"].append(profile)
        entry["contacts"].append(contact)
        entry["handles"].append(pdir.name)
        for pid in source_person_ids:
            if pid not in entry["person_ids"]:
                entry["person_ids"].append(pid)
        cand_pub = contact.get("source_candidate_public_identifier") or ""
        if cand_pub and cand_pub not in entry["candidate_pubs"]:
            entry["candidate_pubs"].append(cand_pub)

    # Phase 2: one synthetic per current parent (union the research; retain both
    # candidate identities so the human can still pick), preserving any prior decision.
    for group_key in sorted(groups):
        entry = groups[group_key]
        profiles = entry["profiles"]
        if len(profiles) > 1:
            collapsed += 1
        profile = merge_research_profiles(profiles)
        # The strongest contact anchor wins the primary identity/pub; the rest ride
        # along in provenance so both LinkedIn options stay visible in review.
        primary = entry["contacts"][0]
        for c in entry["contacts"]:
            if c.get("primary_email") or c.get("phone_e164"):
                primary = c
                break
        contact = dict(primary)
        contact["handle"] = entry["current_slug"] or entry["handles"][0]
        source_person_ids = entry["person_ids"]
        provenance = {
            "source_parent_slug": entry["current_slug"] or entry["handles"][0],
            "source_person_ids": json.dumps(source_person_ids, ensure_ascii=False),
            "source_candidate_public_identifier": (
                contact.get("source_candidate_public_identifier")
                or (entry["candidate_pubs"][0] if entry["candidate_pubs"] else "")
            ),
        }
        email = (contact.get("primary_email") or "").strip().lower()
        digits = "".join(c for c in (contact.get("phone_e164") or "") if c.isdigit())[-10:]
        original = by_email.get(email) or (by_phone.get(digits) if digits else None)
        person_id = (original or {}).get("id", "") or (source_person_ids[0] if source_person_ids else "")
        if original is None:
            # Not in people.csv -> the subject is an import candidate: carry its
            # contact identity (emails/phones/counts/channels) onto the minted row.
            crow = candidate_row(candidate_key_for(email, contact.get("phone_e164") or ""))
            if crow:
                original = candidate_carry(crow)
                person_id = candidate_person_id(crow.get("candidate_key", ""))
        if is_candidate_id(person_id) and effective_network_worth(person_id, overrides)["decision"] == "no":
            worth_no += 1  # user/LLM said not worth adding — never mint a synthetic row
            continue
        row = build_synthetic_row(
            profile,
            contact,
            original,
            person_id,
            args.auto_completeness,
            provenance=provenance,
        )
        # Before provenance was persisted, handle-only subjects minted a
        # ``synth-x-<parent>`` key. Keep that stable identity when backfilling so
        # review decisions and any prior fan-in references do not fork.
        for handle in entry["handles"]:
            legacy_pub = f"synth-x-{handle}".lower()
            if legacy_pub in existing:
                row["public_identifier"] = legacy_pub
                row["id"] = existing[legacy_pub].get("id") or row["id"]
                row["entity_urn"] = existing[legacy_pub].get("entity_urn") or f"synthetic:{row['id']}"
                break
        pub = row["public_identifier"].lower()
        # When two stale rows collapse onto one current parent, the survivor inherits
        # the strongest human decision across every colliding row (its own pub + the
        # pubs the merged children would have minted — see _inherit_decision). An
        # explicit user gate (yes/no) beats a machine gate (auto/blank), and among user
        # gates `no` (exclude) beats `yes` (keep). A human decision is never silently
        # dropped on collapse, even when the survivor's own gate was the weaker one.
        colliding_pubs = {pub}
        for c in entry["contacts"]:
            colliding_pubs.add(synth_public_identifier(
                c.get("primary_email", ""), c.get("phone_e164", ""), c.get("handle", "")).lower())
        inherited = _inherit_decision(existing, colliding_pubs)
        previous = existing.get(pub) or {}
        if (previous.get("approved") or "").strip().lower() in USER_APPROVED:
            # The user's gate is sticky, but missing lineage is safe to repair.
            for field in SYNTHETIC_PROVENANCE_COLUMNS:
                if not previous.get(field) and row.get(field):
                    previous[field] = row[field]
            # A collapsing sibling may carry a STRONGER decision than the survivor's own
            # (inherited already folds in previous's gate, so it never weakens it).
            if inherited:
                previous["approved"] = inherited
            existing[pub] = previous
            preserved += 1
        else:
            if inherited:
                row["approved"] = inherited
            existing[pub] = row
            built += 1
            auto += row["approved"] == "auto"
            pending += row["approved"] == ""
        # Drop the sibling rows the merged children would have minted so a collapse
        # leaves exactly one synthetic per current parent.
        for other in colliding_pubs:
            if other != pub:
                existing.pop(other, None)

    write_rows(Path(args.out), existing)
    result = {
        "primitive": "assemble_synthetic_profile", "status": "completed",
        "built": built, "auto_approved": auto, "pending_review": pending,
        "preserved_user_rows": preserved, "skipped_with_linkedin": with_linkedin,
        "skipped_unusable": unusable, "skipped_worth_no": worth_no,
        "pruned_stale_machine_rows": pruned_stale,
        "collapsed_merged_parents": collapsed,
        "total_rows": len(existing),
        "out": str(args.out), "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    manifest_text = str(args.manifest or "").strip()
    if not manifest_text:
        try:
            if research_dir.resolve() == DR_OUT_DIR.resolve():
                manifest_text = str(ENRICH_MANIFEST)
        except (OSError, RuntimeError):
            pass
    if manifest_text:
        manifest_path = Path(manifest_text)
        if manifest_path.name != "manifest.json":
            raise SystemExit("--manifest must end in manifest.json")
        try:
            current = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
        payload = {
            **current,
            "stage": "enrich",
            "status": "completed",
            "assembly": result,
            "outputs": {
                **(current.get("outputs") or {}),
                "synthetic_people_csv": str(args.out),
            },
        }
        payload.pop("updated_at", None)
        payload.pop("created_at", None)
        write_manifest(
            manifest_path.parent.name, payload,
            import_dir=manifest_path.parent.parent)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
