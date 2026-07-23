"""Durable worth and identity mutations for review."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.candidates import (
    NETWORK_WORTH_VALUES,
)
from packs.ingestion.primitives.deep_context.common import (
    now_iso,
    parse_list,
    read_jsonl,
)
from packs.ingestion.primitives.deep_context.reconcile_linkedin import (
    OVERRIDE_COLUMNS,
    _VERDICT_TO_ACTION,
    _write_override_rows,
    load_override_rows,
)
from packs.ingestion.schemas.people_schema import (
    PEOPLE_SCHEMA_COLUMNS,
    extract_public_identifier,
    normalize_linkedin_url,
)

from .model import _fold_contact_row, parent_contact_union

def apply_synthetic_decision(path: Path, pub: str, decision: str) -> dict[str, str]:
    """The only mutation for synthetic rows: flip the approved gate in synthetic-people.csv.
    keep -> yes (merges), detach/exclude -> no (never merges), reset -> pending."""
    approved = {"keep": "yes", "detach": "no", "exclude": "no", "reset": ""}.get(decision)
    if approved is None:
        raise ValueError(f"decision '{decision}' not supported for synthetic rows")
    pub = (pub or "").strip().lower()
    rows: list[dict[str, str]] = []
    fieldnames: list[str] = []
    hit = False
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        for row in reader:
            if (row.get("public_identifier") or "").strip().lower() == pub:
                row["approved"] = approved
                hit = True
            rows.append(row)
    if not hit:
        raise ValueError(f"synthetic row not found: {pub}")
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    return {"action": "verify", "approved": approved, "new_url": ""}


def union_contacts_into_synthetic_row(path: Path, kept_pub: str,
                                      contacts: dict[str, Any]) -> bool:
    """Union a contact set onto the KEPT synthetic-people.csv row's people-schema
    contact columns (primary/all emails+phones, interaction_counts, source_channels).

    This is the survivor row that flows through the fan-in merge, so folding the
    union directly onto it (rather than a separate consolidate row that would only
    re-converge by a fragile shared source key) is the robust carry-forward for an
    all-synthetic multi-option pick. Idempotent: re-picking unions the same values and
    dedups. Returns True when the row was found and rewritten."""
    kept_pub = (kept_pub or "").strip().lower()
    rows: list[dict[str, str]] = []
    fieldnames: list[str] = []
    hit = False
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        for row in reader:
            if (row.get("public_identifier") or "").strip().lower() == kept_pub:
                agg: dict[str, Any] = {"emails": [], "phones": [],
                                       "interaction_counts": {}, "source_channels": set()}
                # Start from what the row already carries, then fold the union in — so
                # nothing already on the survivor is dropped and re-picks stay stable.
                _fold_contact_row(
                    agg,
                    [row.get("primary_email", ""), *parse_list(row.get("all_emails"))],
                    [row.get("primary_phone", ""), *parse_list(row.get("all_phones"))],
                    row.get("interaction_counts", ""), row.get("source_channels", ""))
                _fold_contact_row(agg, contacts["emails"], contacts["phones"],
                                  json.dumps(contacts["interaction_counts"])
                                  if contacts["interaction_counts"] else "",
                                  contacts["source_channels"])
                emails, phones = agg["emails"], agg["phones"]
                updates = {
                    "primary_email": row.get("primary_email") or (emails[0] if emails else ""),
                    "all_emails": json.dumps(emails) if emails else "",
                    "primary_phone": row.get("primary_phone") or (phones[0] if phones else ""),
                    "all_phones": json.dumps(phones) if phones else "",
                    "interaction_counts": (json.dumps(agg["interaction_counts"])
                                           if agg["interaction_counts"] else ""),
                    "source_channels": ",".join(sorted(agg["source_channels"])),
                }
                # Only touch columns the file actually declares (a production synthetic
                # row carries every PEOPLE_SCHEMA contact column; a minimal fixture may not).
                for col, value in updates.items():
                    if col in fieldnames:
                        row[col] = value
                hit = True
            rows.append(row)
    if not hit:
        return False
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    return True


def upsert_consolidation_row(path: Path, kept_pub: str, kept_url: str,
                             contacts: dict[str, Any]) -> None:
    """Upsert ONE contact-only consolidate-people.csv row keyed by the kept LinkedIn's
    public_identifier, carrying the union of the parent's contacts.

    Mirrors ``write_consolidations`` (same people-schema contact-only shape) so the
    fan-in auto-ingests it and unions the sibling contacts onto the real kept profile —
    the equivalent carry-forward when the picked option is a real LinkedIn rather than a
    synthetic row. Keyed upsert: re-picking replaces the row for that pub (idempotent)."""
    kept_pub = (kept_pub or "").strip().lower()
    if not kept_pub:
        return
    existing: dict[str, dict[str, str]] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                key = (row.get("public_identifier") or "").strip().lower()
                if key:
                    existing[key] = row
    emails, phones = contacts["emails"], contacts["phones"]
    ic = contacts["interaction_counts"]
    row = {c: "" for c in PEOPLE_SCHEMA_COLUMNS}
    row["public_identifier"] = kept_pub
    row["linkedin_url"] = kept_url or ""
    row["primary_email"] = emails[0] if emails else ""
    row["all_emails"] = json.dumps(emails) if emails else ""
    row["primary_phone"] = phones[0] if phones else ""
    row["all_phones"] = json.dumps(phones) if phones else ""
    row["interaction_counts"] = json.dumps(ic) if ic else ""
    row["source_channels"] = ",".join(contacts["source_channels"])
    existing[kept_pub] = row
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=PEOPLE_SCHEMA_COLUMNS)
        w.writeheader()
        for key in sorted(existing):
            w.writerow({c: existing[key].get(c, "") for c in PEOPLE_SCHEMA_COLUMNS})


def carry_forward_multi_option_contacts(
        parent: dict[str, Any], kept_candidate: dict[str, Any],
        *, synthetic_path: Path, people_csv: Path,
        consolidate_path: Path | None = None) -> dict[str, Any]:
    """When a keep/fix resolves a MULTI-option parent, land the UNION of all its
    candidates' contacts on the KEPT identity so a withdrawn sibling's real
    email/phone is never lost.

    Kept synthetic -> union into its synthetic-people.csv row's contact columns.
    Kept real LinkedIn -> upsert a consolidate-people.csv row (fan-in folds it onto the
    real profile). A single-candidate parent is a no-op. Returns a small summary."""
    # consolidate-people.csv sits next to synthetic-people.csv in the overrides dir;
    # deriving it from synthetic_path keeps test fixtures self-contained.
    consolidate_path = (consolidate_path if consolidate_path is not None
                        else synthetic_path.parent / "consolidate-people.csv")
    candidates = parent.get("candidates") or []
    if len(candidates) <= 1:
        return {"carried": False, "reason": "single candidate"}
    contacts = parent_contact_union(parent, people_csv, synthetic_path)
    if not contacts["emails"] and not contacts["phones"]:
        return {"carried": False, "reason": "no contacts to carry"}
    kept_pub = str(kept_candidate.get("pub") or "").strip().lower()
    if kept_candidate.get("synthetic"):
        found = union_contacts_into_synthetic_row(synthetic_path, kept_pub, contacts)
        target = "synthetic-people.csv" if found else "none"
    else:
        # A real (or retargeted) LinkedIn: key the consolidation on the profile pub the
        # fan-in groups by, falling back to the row's pub for an attached-link keep.
        kept_url = str(kept_candidate.get("new_url") or kept_candidate.get("url") or "")
        kept_link_pub = (str(kept_candidate.get("profile_pub") or "").strip().lower()
                         or extract_public_identifier(kept_url).lower() or kept_pub)
        upsert_consolidation_row(consolidate_path, kept_link_pub, kept_url, contacts)
        target = "consolidate-people.csv"
    return {"carried": True, "target": target,
            "emails": contacts["emails"], "phones": contacts["phones"]}


def apply_worth_decision(review_path: Path, pub: str, worth: str,
                         rows: dict[str, dict[str, str]] | None = None) -> dict[str, str]:
    """Upsert the USER-owned `network_worth` mark for one review.csv row (keyed by the
    row's key — a verdict row's pub, a candidate/synthetic row's person_id). '' clears
    the mark (back to the LLM's judgment). Never touches action/approved — with ONE
    exception: a worth-Yes on an excluded row clears the exclude (an approved exclude
    IS a user no, so the rescue must clear both stores). ``rows`` lets a caller pass
    already-parsed override rows (mutated in place) so a hot decision path does not
    re-read a large review.csv per click."""
    pub = (pub or "").strip().lower()
    worth = (worth or "").strip().lower()
    if not pub:
        raise ValueError("worth mark needs a row key")
    if worth not in ("", *NETWORK_WORTH_VALUES):
        raise ValueError(f"unknown worth mark: {worth}")
    if rows is None:
        rows = load_override_rows(review_path)
    row = rows.get(pub) or {k: "" for k in OVERRIDE_COLUMNS}
    row["public_identifier"] = pub
    row["network_worth"] = worth
    if worth == "yes" and (row.get("action") or "").strip().lower() == "exclude":
        row["action"], row["approved"] = "", ""
    row["source"] = row.get("source") or "deep-context-review"
    row["updated_at"] = now_iso()
    rows[pub] = row
    _write_override_rows(review_path, rows)
    return {"network_worth": worth}


_WORTH_TO_SYNTHETIC = {"no": "detach", "yes": "keep", "": "reset"}


def sync_synthetic_gate(path: Path, worth_key: str, worth: str) -> dict[str, str] | None:
    """Mirror a worth mark onto the synthetic-people.csv approved gate when the key
    belongs to a synthetic row. Returns the gate's resulting decision state
    ({'action','approved'} — flipped for no/yes/↺, current for 'maybe') so the client
    can repaint the row's status chip in place; None when the key is not synthetic."""
    key = (worth_key or "").strip().lower()
    if not key or not path.exists():
        return None
    decision = _WORTH_TO_SYNTHETIC.get((worth or "").strip().lower())
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pub = (row.get("public_identifier") or "").strip().lower()
            if pub.startswith("synth-") and ((row.get("id") or "").strip().lower() or pub) == key:
                if decision is not None:
                    result = apply_synthetic_decision(path, pub, decision)
                    return {"action": result["action"], "approved": result["approved"]}
                return {"action": "verify", "approved": (row.get("approved") or "").strip().lower()}
    return None


def apply_decision(review_path: Path, verdicts_path: Path, pub: str, decision: str,
                   new_url: str, confirm_threshold: float, detach_threshold: float | None = None) -> dict[str, str]:
    """Upsert a single decision into review.csv (keyed by public_identifier)."""
    pub = (pub or "").strip().lower()
    rows = load_override_rows(review_path)
    row = rows.get(pub) or {k: "" for k in OVERRIDE_COLUMNS}
    row["public_identifier"] = pub
    if decision == "keep":
        if ((row.get("action") or "").strip().lower() == "retarget"
                and (row.get("new_linkedin_url") or "").strip()):
            # Yes approves the replacement being shown; never turn it back into
            # verification of the original wrong/missing LinkedIn.
            row["action"], row["approved"] = "retarget", "yes"
        else:
            row["action"], row["approved"], row["new_linkedin_url"], row["new_public_identifier"] = "verify", "yes", "", ""
    elif decision == "detach":
        row["action"], row["approved"], row["new_linkedin_url"], row["new_public_identifier"] = "detach", "yes", "", ""
    elif decision == "exclude":
        # "I don't want this person indexed at all." The fan-in merge drops the row entirely
        # (not just the link), and deep-research recovery skips it — unlike detach.
        row["action"], row["approved"], row["new_linkedin_url"], row["new_public_identifier"] = "exclude", "yes", "", ""
    elif decision == "fix":
        url = normalize_linkedin_url(new_url or "")
        if not url:
            raise ValueError("fix needs a LinkedIn URL")
        row["action"], row["approved"] = "retarget", "yes"
        row["new_linkedin_url"] = url
        row["new_public_identifier"] = extract_public_identifier(url).lower()
    elif decision == "reset":
        # restore the model's original (non-conflict) call. Asymmetric, keep-biased bars:
        # confirmed auto-applies at the (low) confirm bar, wrong_person at the (high) detach bar.
        detach_threshold = confirm_threshold if detach_threshold is None else detach_threshold
        rec = next((r for r in read_jsonl(verdicts_path)
                    if (r.get("candidate_key") or "").strip().lower() == pub), None)
        v = (rec or {}).get("verdict") or {}
        vd = v.get("verdict", "")
        bar = detach_threshold if vd == "wrong_person" else confirm_threshold
        row["action"] = _VERDICT_TO_ACTION.get(vd, "verify")
        row["approved"] = "auto" if float(v.get("confidence") or 0) >= bar and vd in ("confirmed", "wrong_person") else ""
        row["new_linkedin_url"], row["new_public_identifier"] = "", ""
    else:
        raise ValueError(f"unknown decision: {decision}")
    row["source"] = "deep-context-review"
    row["updated_at"] = now_iso()
    rows[pub] = row
    _write_override_rows(review_path, rows)
    return {"action": row["action"], "approved": row["approved"], "new_url": row.get("new_linkedin_url", "")}
