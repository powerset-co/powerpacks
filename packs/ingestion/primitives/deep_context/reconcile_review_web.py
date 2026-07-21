#!/usr/bin/env python3
"""Local, file-backed review UI for the staged deep-context workflow.

The browser reviews uncertain People decisions, observes Enrich Contacts progress,
then confirms new identities. Human choices remain the existing durable
``review.csv`` / synthetic gates; two fixed manifests tell the agent when to advance.
No provider call, subprocess, or paid work happens in this server. Profile data is
read from the local RapidAPI cache only; cache misses are surfaced as a passive
note pointing at the offline ``prefetch_profiles`` stage.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import re
import sys
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from packs.ingestion.primitives.deep_context.candidates import (
    NETWORK_WORTH_VALUES,
    current_parent_by_person_id,
    effective_network_worth,
    is_candidate_id,
    llm_network_worth,
    load_candidates,
)
from packs.ingestion.primitives.deep_context import worth_view
from packs.ingestion.primitives.deep_context.enrichment_contract import (
    IN_FLIGHT_STATUSES,
    STATE_DONE,
    STATE_FREE_PENDING,
    STATE_NEEDS_APPROVAL,
    STATE_RUNNING,
    STATUS_COMPLETED,
    STATUS_NEEDS_APPROVAL,
    STATUS_RESEARCH_COMPLETE,
    derive_enrichment_state,
    read_enrichment_manifest,
)
from packs.ingestion.primitives.deep_context.common import (
    DEEP_RESEARCH_DIR,
    DEFAULT_PEOPLE_CSV,
    DOSSIER_DIR,
    ENRICH_MANIFEST,
    FACTS_DIR,
    GMAIL_CHANNEL,
    IMESSAGE_CHANNEL,
    INDEX_JSON,
    LINKEDIN_OVERRIDES_CSV,
    PARENTS_DIR,
    PROFILE_CACHE_DIR,
    RAW_DIR,
    REVIEW_DIR,
    REVIEW_MANIFEST,
    ROOT,
    VERDICTS_JSONL,
    WHATSAPP_CHANNEL,
    normalize_email,
    normalize_phone,
    now_iso,
    parse_list,
    read_jsonl,
    slugify,
)
from packs.ingestion.primitives.import_contacts_pipeline.common import write_manifest
from packs.ingestion.primitives.deep_context.reconcile_linkedin import (
    DEFAULT_CONFIRM,
    DEFAULT_DETACH,
    OVERRIDE_COLUMNS,
    USER_APPROVED,
    _VERDICT_TO_ACTION,
    _write_override_rows,
    linkedin_view,
    load_override_rows,
    load_people_rows,
    union_child_contacts,
)
from packs.ingestion.primitives.deep_context.review_store import (
    judge_accepted_candidate_retarget,
)
from packs.ingestion.schemas.people_schema import (
    PEOPLE_SCHEMA_COLUMNS,
    extract_public_identifier,
    merge_interaction_counts,
    normalize_linkedin_url,
)

APPLIED_APPROVED = {"auto", "yes"}
VALID_TABS = {"all", "review", "verified", "detached", "conflict", "fixed", "excluded", "decided", "rejected"}
VALID_STAGES = {"worth", "enrich", "linkedin", "done"}
USER_WORTH_VALUES = {"yes", "no"}
# Decision tables load rows in chunks over /api/decision-rows (infinite scroll);
# this is both the server-rendered first window and the fetch increment.
DECISION_CHUNK_SIZE = 40
REVIEW_CSS = Path(__file__).with_name("reconcile_review.css")
REVIEW_JS = Path(__file__).with_name("reconcile_review.js")
AVATAR_DIR = REVIEW_DIR / "avatars"

# people.csv / candidates.csv channel label -> user-facing source filter value.
CHANNEL_TO_SOURCE = {GMAIL_CHANNEL: "gmail", IMESSAGE_CHANNEL: "imessage", WHATSAPP_CHANNEL: "whatsapp"}
SOURCE_FILTERS = ("gmail", "imessage", "whatsapp")


def _sources_of(channels: list[str]) -> list[str]:
    """Filterable source labels for a row's source_channels (unknown labels dropped)."""
    out: list[str] = []
    for channel in channels:
        source = CHANNEL_TO_SOURCE.get(str(channel or "").strip())
        if source and source not in out:
            out.append(source)
    return out


def load_people_sources(people_csv: Path) -> dict[str, list[str]]:
    """person_id -> message-source labels from people.csv `source_channels`."""
    out: dict[str, list[str]] = {}
    if not people_csv.exists():
        return out
    with people_csv.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pid = str(row.get("id") or "").strip()
            if not pid:
                continue
            sources = _sources_of((row.get("source_channels") or "").split(","))
            if sources:
                out[pid] = sources
    return out


def load_connection_keys(people_csv: Path) -> set[str]:
    """Keys (person ids + LinkedIn pubs, lowercased) of first-degree LinkedIn
    connections — people whose source_channels include linkedin_csv. Connections
    are GROUND TRUTH in this product: the user is literally connected, so a
    MACHINE no (the synthesis worth judgment) never rejects or drops them; only
    the user's own No/Exclude can."""
    out: set[str] = set()
    if not people_csv.exists():
        return out
    with people_csv.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            channels = {c.strip() for c in (row.get("source_channels") or "").split(",")}
            if "linkedin_csv" not in channels:
                continue
            for key in ((row.get("id") or "").strip(), (row.get("public_identifier") or "").strip()):
                if key:
                    out.add(key.lower())
    return out


def annotate_sources(parents: list[dict[str, Any]], people_sources: dict[str, list[str]]) -> None:
    """Fill missing parent 'sources' from people.csv (candidate/synthetic rows carry their own)."""
    for p in parents:
        if p.get("sources"):
            continue
        sources: list[str] = []
        for pid in p["person_ids"]:
            for s in people_sources.get(pid, []):
                if s not in sources:
                    sources.append(s)
        p["sources"] = sources


# --- model: join verdicts.jsonl (display) with review.csv (decisions) -------

def candidate_state(cand: dict[str, Any]) -> str:
    """The effective per-candidate state from its current decision row."""
    action = (cand.get("action") or "").strip().lower()
    approved = (cand.get("approved") or "").strip().lower()
    if action == "exclude" and approved in APPLIED_APPROVED:
        return "excluded"
    if action == "retarget" and approved in APPLIED_APPROVED:
        return "fixed"
    if approved in APPLIED_APPROVED:
        return "detached" if action == "detach" else "verified"
    if approved == "no":
        return "rejected"
    return "review"  # pending


def parent_status(parent: dict[str, Any]) -> str:
    """One status chip per parent, by priority (what most needs the user's eye first)."""
    states = [candidate_state(c) for c in parent["candidates"]]
    if "excluded" in states:
        return "excluded"
    if "fixed" in states:
        return "fixed"
    if "review" in states:
        return "review"
    if "verified" in states:
        return "verified"
    if states and all(s in {"detached", "rejected"} for s in states):
        return "detached"
    return "review"


def _cand_rank(cand: dict[str, Any]) -> tuple[int, float]:
    """Stable best-first ordering shared by model decisions and the staged UI."""
    state = candidate_state(cand)
    confidence = float(cand.get("confidence") or 0.0)
    if state in {"verified", "fixed"}:
        return (0, -confidence)
    if state in {"detached", "excluded", "rejected"}:
        return (4, -confidence)
    if cand.get("verdict") == "confirmed":
        return (1, -confidence)
    if cand.get("verdict") == "wrong_person":
        return (3, -confidence)
    return (2, -confidence)


def picked_link(parent: dict[str, Any]) -> str:
    """The LinkedIn this parent currently resolves to (verified link, retarget target, or none)."""
    for c in parent["candidates"]:
        st = candidate_state(c)
        if st == "fixed":
            return c.get("new_url") or ""
        if st == "verified":
            return c.get("url") or ""
    return ""


def min_confidence(parent: dict[str, Any]) -> float:
    return min((c.get("confidence", 0.0) for c in parent["candidates"]), default=0.0)


def is_decided(parent: dict[str, Any]) -> bool:
    return any((c.get("approved") or "").strip().lower() in USER_APPROVED for c in parent["candidates"])


def _profile_picture_urls(pub: str, profile_cache_dir: Path = PROFILE_CACHE_DIR) -> list[str]:
    """All profile-photo URLs retained in one local profile-cache record.

    LinkedIn CDN URLs are signed and eventually expire. Keeping every size lets the
    local review app cache the smallest still-live image instead of hotlinking the
    largest URL into the browser forever.
    """
    pub = (pub or "").strip().lower()
    # ``pub`` comes from a local query parameter. Profile-cache filenames can
    # contain unicode and punctuation, but never a path separator; keep the
    # avatar endpoint inside its configured cache directory.
    if (not pub or "\x00" in pub or len(pub) > 512
            or pub in {".", ".."} or Path(pub).name != pub):
        return []
    path = profile_cache_dir / f"{pub}.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return []
    normalized = payload.get("normalized_profile") or {}
    raw = payload.get("raw_response") or {}
    candidates: list[str] = []
    pictures = raw.get("profilePictures") if isinstance(raw, dict) else []
    if isinstance(pictures, list):
        ordered = sorted(
            (pic for pic in pictures if isinstance(pic, dict)),
            key=lambda pic: int(pic.get("width") or 0) * int(pic.get("height") or 0),
        )
        candidates.extend(str(pic.get("url") or "") for pic in ordered)
    candidates.extend([
        str(normalized.get("profile_pic_url") or ""),
        str(raw.get("profilePicture") or "") if isinstance(raw, dict) else "",
    ])
    return list(dict.fromkeys(url for url in candidates if url.startswith("https://")))


def _cached_profile_pic(pub: str) -> str:
    """Best-effort display hint; actual bytes are served by ``/api/avatar``."""
    urls = _profile_picture_urls(pub)
    return urls[0] if urls else ""


def _avatar_cache_path(pub: str, avatar_dir: Path = AVATAR_DIR) -> Path:
    digest = hashlib.sha256((pub or "").strip().lower().encode("utf-8")).hexdigest()[:24]
    return avatar_dir / f"{digest}.image"


def _image_content_type(body: bytes) -> str:
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return ""


def load_avatar(pub: str, *, profile_cache_dir: Path = PROFILE_CACHE_DIR,
                avatar_dir: Path = AVATAR_DIR) -> tuple[bytes, str] | None:
    """Serve a locally cached avatar, or cache the first live signed image URL.

    The endpoint never accepts a URL from the browser, so it cannot be used as an
    arbitrary proxy. Expired legacy URLs simply return ``None`` and the UI keeps its
    initials fallback visible. No provider lookup or paid work happens here.
    """
    pub = (pub or "").strip().lower()
    if not pub:
        return None
    cached = _avatar_cache_path(pub, avatar_dir)
    if cached.exists():
        try:
            body = cached.read_bytes()
        except OSError:
            body = b""
        content_type = _image_content_type(body)
        if content_type:
            return body, content_type
    for url in _profile_picture_urls(pub, profile_cache_dir):
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if host != "licdn.com" and not host.endswith(".licdn.com"):
            continue
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 PowerpacksLocalReview/1.0",
                    "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8",
                },
            )
            with urllib.request.urlopen(request, timeout=8) as response:
                body = response.read(2_000_001)
                final_url = response.geturl() if hasattr(response, "geturl") else url
        except (OSError, urllib.error.URLError, TimeoutError):
            continue
        final = urllib.parse.urlparse(final_url)
        final_host = (final.hostname or "").lower()
        if (final.scheme != "https"
                or (final_host != "licdn.com" and not final_host.endswith(".licdn.com"))):
            continue
        content_type = _image_content_type(body)
        if not content_type or len(body) > 2_000_000:
            continue
        cached.parent.mkdir(parents=True, exist_ok=True)
        temporary = cached.with_name(f".{cached.name}.{threading.get_ident()}.tmp")
        try:
            temporary.write_bytes(body)
            temporary.replace(cached)
        finally:
            temporary.unlink(missing_ok=True)
        return body, content_type
    return None


SYNTHETIC_PEOPLE_CSV = LINKEDIN_OVERRIDES_CSV.parent / "synthetic-people.csv"


def _fmt_experiences(work_json: str) -> list[str]:
    try:
        positions = json.loads(work_json or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    out = []
    for p in positions if isinstance(positions, list) else []:
        title, company = (p.get("title") or "").strip(), (p.get("company_name") or "").strip()
        if title or company:
            span = " / ".join(v for v in ((p.get("start_date") or ""), (p.get("end_date") or ("present" if p.get("is_current") else ""))) if v)
            out.append(f"{title or '?'} @ {company or '?'}" + (f" ({span})" if span else ""))
    return out


def _synthetic_source_ids(value: str) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in parsed if str(item).strip()))


def _synthetic_dossier_slug(row: dict[str, str], pub: str, parents_dir: Path,
                            dossier_dir: Path, facts_dir: Path) -> str:
    source = str(row.get("source_parent_slug") or "").strip()
    if source and Path(source).name == source and (parents_dir / f"{source}.md").is_file():
        return source
    # Safe legacy recovery: old handle-only rows encoded the parent slug in a
    # synth-x key. Accept it only when an exact canonical parent file exists.
    prefix = "synth-x-"
    legacy = pub[len(prefix):] if pub.startswith(prefix) else ""
    if legacy and Path(legacy).name == legacy and (parents_dir / f"{legacy}.md").is_file():
        return legacy
    # Import candidates do not have a canonical parent stub. Their composed
    # child dossier is named from canonical_name + person_id, exactly as in
    # ``load_candidate_parents``. Recover that stable child so a researched
    # synthetic profile keeps the message context it was built from.
    source_ids = _synthetic_source_ids(row.get("source_person_ids") or "")
    source_candidate = str(row.get("source_candidate_public_identifier") or "").strip()
    if source_candidate and source_candidate not in source_ids:
        source_ids.append(source_candidate)
    for person_id in source_ids:
        name = _facts_canonical_name(facts_dir, person_id) or str(row.get("full_name") or pub)
        child_slug = slugify(name, person_id)
        if (dossier_dir / f"{child_slug}.md").is_file():
            return child_slug
    return ""


def load_synthetic_parents(path: Path, parents_dir: Path = PARENTS_DIR,
                           dossier_dir: Path = DOSSIER_DIR,
                           facts_dir: Path = FACTS_DIR) -> list[dict[str, Any]]:
    """Deep-researched people with NO real LinkedIn (assemble_synthetic_profile output),
    surfaced as review rows: pending -> Needs review, auto/yes -> verified, no -> rejected.
    One candidate per person, flagged synthetic (there is no LinkedIn to link to)."""
    parents: list[dict[str, Any]] = []
    if not path.exists():
        return parents
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pub = (row.get("public_identifier") or "").strip().lower()
            if not pub.startswith("synth-"):
                continue
            try:
                meta = json.loads(row.get("synthetic_metadata") or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            edu = []
            try:
                edu = [" — ".join(v for v in ((e.get("degree") or ""), (e.get("school_name") or "")) if v)
                       for e in json.loads(row.get("education") or "[]") if isinstance(e, dict)]
            except (json.JSONDecodeError, TypeError):
                pass
            name = row.get("full_name") or pub
            gaps = ", ".join(meta.get("gaps") or [])
            source_ids = _synthetic_source_ids(row.get("source_person_ids") or "")
            dossier_slug = _synthetic_dossier_slug(
                row, pub, parents_dir, dossier_dir, facts_dir)
            parents.append({
                "slug": f"synthetic-{pub}",
                "dossier_slug": dossier_slug,
                "name": name,
                "person_ids": source_ids or [row.get("id") or pub],
                "sources": _sources_of((row.get("source_channels") or "").split(",")),
                "candidates": [{
                    "pub": pub, "url": "", "full_name": name,
                    "headline": row.get("headline") or "",
                    "profile_pic_url": "",
                    "experiences": _fmt_experiences(row.get("work_experiences") or ""),
                    "education": [e for e in edu if e],
                    "location": row.get("location_raw") or "",
                    "has_profile": True,
                    "verdict": "synthetic",
                    "confidence": float(meta.get("completeness") or 0.0),
                    "supporting": [], "contradicting": [],
                    "reason": (row.get("summary") or row.get("headline") or "deep-researched profile")
                              + (f" · research gaps: {gaps}" if gaps else ""),
                    "plausibly_absent": False, "recommend_dr": False,
                    "match_emails": [e for e in (row.get("primary_email") or "").split("|") if e],
                    "match_phones": [p for p in (row.get("primary_phone") or "").split("|") if p],
                    "conflict": False, "synthetic": True,
                    "action": "verify",
                    "approved": (row.get("approved") or "").strip().lower(),
                    "new_url": "",
                    "llm_reject": "", "llm_reject_confidence": "", "llm_reject_reason": "",
                }],
            })
    return parents


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


def _fold_contact_row(agg: dict[str, Any], emails: list[str], phones: list[str],
                      interaction_counts: Any, source_channels: Any) -> None:
    """Fold one contact source into a running {emails, phones, interaction_counts,
    source_channels} accumulator (order-stable, deduped)."""
    for e in emails:
        ne = normalize_email(e)
        if ne and "@" in ne and ne not in agg["emails"]:
            agg["emails"].append(ne)
    for p in phones:
        npn = normalize_phone(p)
        if npn and npn not in agg["phones"]:
            agg["phones"].append(npn)
    if interaction_counts:
        agg["interaction_counts"] = merge_interaction_counts(
            json.dumps(agg["interaction_counts"]) if agg["interaction_counts"] else "",
            interaction_counts)
    for c in (source_channels if isinstance(source_channels, (list, set))
              else str(source_channels or "").split(",")):
        c = str(c).strip()
        if c:
            agg["source_channels"].add(c)


def _synthetic_rows_by_pub(synthetic_path: Path) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    if synthetic_path and synthetic_path.exists():
        with synthetic_path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                pub = (row.get("public_identifier") or "").strip().lower()
                if pub:
                    rows[pub] = row
    return rows


def parent_contact_union(parent: dict[str, Any], people_csv: Path,
                         synthetic_path: Path | None = None) -> dict[str, Any]:
    """UNION of every contact (emails / phones / per-channel interaction_counts /
    source_channels) across ALL of a parent's candidates — the kept option AND every
    withdrawn sibling — so a multi-option pick never loses a sibling's real email/phone.

    Sourced from people.csv / candidates.csv by person_id (the authoritative contact
    store, via the shared ``union_child_contacts`` helper the LinkedIn consolidation path
    uses), each candidate's ``match_emails`` / ``match_phones``, AND — for a synthetic
    candidate — its full synthetic-people.csv row (which carries the interaction_counts /
    source_channels the review-parent model does not surface). Deterministic + idempotent."""
    agg: dict[str, Any] = {"emails": [], "phones": [],
                           "interaction_counts": {}, "source_channels": set()}
    base = union_child_contacts(parent.get("person_ids") or [], load_people_rows(people_csv))
    _fold_contact_row(agg, base["emails"], base["phones"],
                      json.dumps(base["interaction_counts"]) if base["interaction_counts"] else "",
                      base["source_channels"])
    synth_rows = _synthetic_rows_by_pub(synthetic_path) if synthetic_path else {}
    for cand in parent.get("candidates") or []:
        _fold_contact_row(agg, cand.get("match_emails") or [],
                          cand.get("match_phones") or [], "", cand.get("sources") or [])
        # A synthetic candidate's own row carries the per-channel counts / channels the
        # parent model omits — fold them so nothing is lost when it is withdrawn.
        srow = synth_rows.get(str(cand.get("pub") or "").strip().lower())
        if srow:
            _fold_contact_row(
                agg,
                [srow.get("primary_email", ""), *parse_list(srow.get("all_emails"))],
                [srow.get("primary_phone", ""), *parse_list(srow.get("all_phones"))],
                srow.get("interaction_counts", ""), srow.get("source_channels", ""))
    agg["source_channels"] = sorted(agg["source_channels"])
    return agg


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


def _facts_canonical_name(facts_dir: Path, person_id: str) -> str:
    """The synthesis LLM's canonical_name (last record wins). Compose names the
    dossier file with it, so the row's dossier slug must be derived the same way."""
    name = ""
    for rec in read_jsonl(facts_dir / f"{person_id}.jsonl"):
        value = str((rec.get("facts") or {}).get("canonical_name") or "").strip()
        if value:
            name = value
    return name


_EMAIL_IDENTIFIER_RE = re.compile(
    r"(?i)(?<![\w.+-])([a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+)"
)


def _facts_identifier_emails(facts_dir: Path, person_id: str) -> list[str]:
    """Email aliases synthesized into the dossier's Identifiers section.

    These stay display-only: candidates.csv remains the canonical imported
    identity source, while review Details can show the same known aliases as
    the dossier beneath it.
    """
    emails: list[str] = []
    for rec in read_jsonl(facts_dir / f"{person_id}.jsonl"):
        for identifier in (rec.get("facts") or {}).get("identifiers") or []:
            for match in _EMAIL_IDENTIFIER_RE.findall(str(identifier or "")):
                email = normalize_email(match)
                if email and email not in emails:
                    emails.append(email)
    return emails


def load_candidate_parents(facts_dir: Path, overrides: dict[str, dict[str, str]],
                           shown_person_ids: set[str],
                           resolved_candidates: set[str] | None = None) -> list[dict[str, Any]]:
    """Dossier-bearing import candidates (facts exist, NO research/synthetic result yet)
    as the binary People-stage queue before any paid identity research. The review.csv
    key for a candidate is its person_id. Candidates already shown via a researched
    synthetic/LinkedIn row are deduped by person_id.

    The worth VIEW is membership-blind (owner decision 2026-07-19): every
    facts-judged candidate materializes here regardless of people.csv, and
    ``collapse_by_current_parent`` dedupes merged identities into one card.
    ``candidates_resolved_by_existing`` remains a paid-research SELECTION
    filter in reconcile_deep_research only — it must never hide a person from
    the review UI."""
    parents: list[dict[str, Any]] = []
    resolved_candidates = set() if resolved_candidates is None else resolved_candidates
    emitted: set[str] = set()
    pool = [(person.person_id, person.full_name, person.source_channels,
             person.emails, person.phones) for person in load_candidates()]
    # Facts-only sweep: the worth view is over facts/*.jsonl, so a judged person
    # must render even when no candidate pool currently lists them (dead pool
    # identities, mid-rerun imports). Pool-less rows still need a verdict to
    # appear; unjudged people only enter via a pool, exactly as before.
    pool_pids = {pid.lower() for pid, *_ in pool}
    for path in sorted(facts_dir.glob("*.jsonl")):
        pid = path.stem
        if (pid.lower() in pool_pids or pid.lower() in shown_person_ids
                or not llm_network_worth(pid, facts_dir).get("decision")):
            continue
        pool.append((pid, "", [], [], []))
    for pid, fallback_name, source_channels, emails, phones in pool:
        if (pid.lower() in emitted or pid.lower() in shown_person_ids
                or pid.lower() in resolved_candidates
                or not (facts_dir / f"{pid}.jsonl").exists()):
            continue
        emitted.add(pid.lower())
        name = _facts_canonical_name(facts_dir, pid) or fallback_name or pid
        dec = overrides.get(pid.lower(), {})
        action = str(dec.get("action") or "").strip().lower()
        approved = str(dec.get("approved") or "").strip().lower()
        new_url = str(dec.get("new_linkedin_url") or "").strip()
        proposed_pub = (str(dec.get("new_public_identifier") or "").strip().lower()
                        or extract_public_identifier(new_url).lower())
        proposed = action == "retarget" and bool(new_url and proposed_pub)
        # A pending retarget is the output of the paid lookup and belongs in the
        # LinkedIn stage. A prior explicit link-level decision is also terminal;
        # it must not fall back into the paid lookup queue on reload.
        identity_result = proposed or (action in {"verify", "detach"}
                                       and approved in {"auto", "yes", "no"})
        parents.append({
            "slug": slugify(name, pid),  # the composed CHILD dossier's slug (no parent stub needed)
            "name": name,
            "person_ids": [pid],
            "sources": _sources_of(source_channels),
            "candidates": [{
                "pub": pid,  # candidates key review.csv on their person_id
                "profile_pub": proposed_pub,
                "url": new_url if proposed else "", "full_name": name,
                "headline": "",
                "profile_pic_url": "",
                "experiences": [], "education": [], "location": "",
                "has_profile": proposed,
                "verdict": "proposed_linkedin" if proposed else "no_linkedin_candidate",
                "confidence": 0.0,
                "supporting": [], "contradicting": [],
                "reason": (str(dec.get("reason") or "deep research found this LinkedIn")
                           if proposed else
                           "unresolved import candidate — no LinkedIn attached yet"),
                "plausibly_absent": False, "recommend_dr": False,
                "match_emails": list(dict.fromkeys([
                    *emails,
                    *_facts_identifier_emails(facts_dir, pid),
                ])),
                "match_phones": phones,
                "conflict": False, "import_candidate": not identity_result,
                "candidate_origin": True,
                "action": action,
                "approved": approved,
                "new_url": new_url,
                "llm_reject": (dec.get("llm_reject") or "").strip().lower(),
                "llm_reject_confidence": dec.get("llm_reject_confidence", ""),
                "llm_reject_reason": dec.get("llm_reject_reason", ""),
            }],
        })
    return parents


def _research_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "").strip()
    return str(value or "").strip()


def _research_profile_view(profile: dict[str, Any]) -> dict[str, Any]:
    person = profile.get("person") if isinstance(profile.get("person"), dict) else {}
    location = profile.get("location") if isinstance(profile.get("location"), dict) else {}
    positions = profile.get("positions") if isinstance(profile.get("positions"), list) else []
    education_rows = profile.get("education") if isinstance(profile.get("education"), list) else []
    social = profile.get("social") if isinstance(profile.get("social"), dict) else {}
    education: list[str] = []
    for row in education_rows:
        if not isinstance(row, dict):
            continue
        school = str(row.get("school_name") or row.get("school") or "").strip()
        degree = ", ".join(
            str(row.get(key) or "").strip() for key in ("degree", "field_of_study")
            if str(row.get(key) or "").strip())
        label = f"{degree} — {school}" if degree and school else degree or school
        if label:
            education.append(label)
    raw_location = str(location.get("raw") or "").strip()
    if not raw_location:
        raw_location = ", ".join(
            str(location.get(key) or "").strip() for key in ("city", "state", "country")
            if str(location.get(key) or "").strip())
    reason = ""
    metadata = profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}
    for value in (
        metadata.get("research_notes"),
        profile.get("research_notes"),
        profile.get("reasoning"),
        profile.get("rationale"),
        profile.get("summary"),
        profile.get("headline"),
    ):
        text = _research_text(value)
        if text:
            reason = f"deep research: {text}"
            break
    return {
        "public_identifier": extract_public_identifier(str(social.get("linkedin_url") or "")).lower(),
        "linkedin_url": str(social.get("linkedin_url") or "").strip(),
        "full_name": str(person.get("full_name") or "").strip(),
        "headline": _research_text(profile.get("headline")),
        "profile_pic_url": "",
        "experiences": _fmt_experiences(json.dumps(positions, ensure_ascii=False)),
        "education": education,
        "location": raw_location,
        "reason": reason,
        "has_profile": bool(person or positions or education or raw_location),
    }


def _current_research_profiles(research_dir: Path = DEEP_RESEARCH_DIR) -> dict[str, dict[str, Any]]:
    """Current fixed-queue identity research keyed by every stable source handle."""
    queue_path = research_dir / "research_queue.csv"
    if not queue_path.exists():
        return {}
    by_key: dict[str, dict[str, Any]] = {}
    with queue_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            handle = str(row.get("handle") or "").strip()
            result_path = research_dir / handle / "01_research_parallel.json"
            if not handle or not result_path.is_file():
                continue
            try:
                raw = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            view = _research_profile_view(raw)
            keys = [
                handle,
                str(row.get("source_parent_slug") or "").strip(),
                str(row.get("source_candidate_public_identifier") or "").strip(),
                *_synthetic_source_ids(row.get("source_person_ids") or ""),
            ]
            for key in keys:
                if key:
                    by_key[key.lower()] = view
    return by_key


def hydrate_proposed_profiles(parents: list[dict[str, Any]], *,
                              profile_cache_dir: Path = PROFILE_CACHE_DIR,
                              research_dir: Path = DEEP_RESEARCH_DIR) -> None:
    """Show profile facts already on disk for proposed replacement LinkedIns.

    Prefer the existing RapidAPI cache when this LinkedIn is already known, then
    fall back to the just-completed Parallel result. This is read-only and never
    calls either provider from the review UI.
    """
    research = _current_research_profiles(research_dir)
    for parent in parents:
        parent_keys = [str(parent.get("slug") or ""), *(parent.get("person_ids") or [])]
        for candidate in parent.get("candidates") or []:
            if str(candidate.get("action") or "").strip().lower() != "retarget":
                continue
            url = str(candidate.get("url") or candidate.get("new_url") or "").strip()
            pub = (str(candidate.get("profile_pub") or "").strip().lower()
                   or extract_public_identifier(url).lower())
            if not url or not pub:
                continue
            research_view = next(
                (research[key.lower()] for key in [str(candidate.get("pub") or ""), *parent_keys]
                 if key and key.lower() in research),
                {},
            )
            cached_view = linkedin_view(
                {"public_identifier": pub, "linkedin_url": url}, profile_cache_dir)
            profile = cached_view if cached_view.get("has_profile") else research_view
            for field in ("full_name", "headline", "profile_pic_url", "experiences",
                          "education", "location"):
                if profile.get(field):
                    candidate[field] = profile[field]
            if research_view.get("reason"):
                candidate["reason"] = research_view["reason"]
            candidate["has_profile"] = bool(candidate.get("has_profile") or profile.get("has_profile"))


def annotate_worth(parents: list[dict[str, Any]], overrides: dict[str, dict[str, str]],
                   facts_dir: Path, connections: set[str] | None = None,
                   index_json: Path | None = None) -> None:
    """Attach the effective network-worth (user review.csv mark / approved exclude >
    synthesis-mirrored review.csv llm_worth > default 'maybe') to EVERY row — verdict,
    candidate, and synthetic alike — plus the machine's own view (for the secondary
    text and the unified Rejected grouping). The mark's review.csv key is the primary
    candidate's LinkedIn pub for verdict rows, else the row's person_id.

    Also stamps each parent with its worth_view row (``parent["worth_row"]``) —
    the single worth-section truth (see worth_view.py's header). Every worth
    predicate/count reads the stamp; nothing else may decide worth visibility."""
    by_pid = worth_view.rows_by_person_id(worth_view.rows_from(
        facts_dir, overrides,
        index_json if index_json is not None else worth_view.INDEX_JSON))
    for p in parents:
        p["worth_row"] = next(
            (by_pid[str(pid or "").lower()] for pid in p.get("person_ids") or []
             if str(pid or "").lower() in by_pid), None)
    for p in parents:
        cands = p["candidates"]
        if not cands:
            continue
        primary = min(cands, key=_cand_rank) if len(cands) > 1 else cands[0]
        excluded = next((cand for cand in cands
                         if str(cand.get("action") or "").strip().lower() == "exclude"
                         and str(cand.get("approved") or "").strip().lower() in APPLIED_APPROVED), None)
        key = ""
        if excluded:
            key = str(excluded.get("pub") or "").strip()
        elif not (primary.get("synthetic") or primary.get("import_candidate")):
            key = (primary.get("pub") or "").strip()
        key = key or (p["person_ids"] or [""])[0]
        if not key:
            continue
        state = effective_no_for_key(key, overrides, facts_dir, connections=connections)
        p["worth"], p["machine_worth"] = state["worth"], state["machine"]
        p["connection"] = state["connected"] or any(
            pid.lower() in (connections or set()) for pid in p["person_ids"])
        primary["worth"], primary["machine_worth"] = state["worth"], state["machine"]
        primary["worth_key"] = key


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


def effective_no_for_key(key: str, override_rows: dict[str, dict[str, str]],
                         facts_dir: Path, *, keepish: bool | None = None,
                         connections: set[str] | None = None) -> dict[str, Any]:
    """Single-row mirror of is_effective_no (the unified Rejected / merge-drop rule)
    for one review.csv key: {'worth', 'machine', 'rejected', 'connected'}. `keepish`
    overrides the rescue signal when it lives outside review.csv (the synthetic
    approved gate). A first-degree LinkedIn connection (`connections` membership by
    key or the row's person_id) is never machine-rejected — only a user no."""
    key_l = (key or "").strip().lower()
    worth = effective_network_worth(key, override_rows, facts_dir)
    row = override_rows.get(key_l) or {}
    machine = worth
    if worth["source"] == "user":  # strip the user's signals to see the machine's own view
        machine = effective_network_worth(key, {key_l: {**row, "network_worth": "", "action": ""}},
                                          facts_dir)
    user_mark = worth["decision"] if worth["source"] == "user" else ""
    if keepish is None:
        keepish = (row.get("approved") or "").strip().lower() == "yes" and \
            (row.get("action") or "").strip().lower() not in ("detach", "exclude")
    connected = bool(connections) and (
        key_l in connections
        or (row.get("person_id") or "").strip().lower() in connections)
    machine_no = machine["decision"] == "no"
    rejected = user_mark == "no" or (
        user_mark != "yes" and machine_no and not keepish and not connected)
    return {"worth": worth, "machine": machine, "rejected": rejected, "connected": connected}


# Worth mark -> synthetic approved gate (so the mint gate agrees with the mark):
# No behaves like Detach, Yes like Keep, ↺ restores pending. 'maybe' is not a gate
# decision and leaves the gate alone.
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


def synthetic_worth_key(path: Path, pub: str) -> str:
    """A synthetic row's worth key — its ORIGINAL person id (the csv row's `id`),
    matching load_synthetic_parents and the merge's id-keyed user-no lookup."""
    pub = (pub or "").strip().lower()
    if not pub or not path.exists():
        return ""
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if (row.get("public_identifier") or "").strip().lower() == pub:
                return (row.get("id") or "").strip() or pub
    return ""


def build_parents(verdicts_path: Path, review_path: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    overrides = load_override_rows(review_path)
    parents: dict[str, dict[str, Any]] = {}
    for r in read_jsonl(verdicts_path):
        slug = r.get("parent_slug") or ""
        if not slug:
            continue
        p = parents.setdefault(slug, {"slug": slug, "name": r.get("name") or slug,
                                      "person_ids": [], "candidates": []})
        for pid in r.get("person_ids") or []:
            if pid not in p["person_ids"]:
                p["person_ids"].append(pid)
        pub = (r.get("candidate_key") or "").strip().lower()
        candidate_person_ids = [
            str(pid or "").strip() for pid in (r.get("person_ids") or [])
            if is_candidate_id(str(pid or ""))
        ]
        import_candidate = bool(r.get("no_link") and candidate_person_ids)
        worth_pub = candidate_person_ids[0].lower() if import_candidate else pub
        v = r.get("verdict") or {}
        li = r.get("linkedin") or {}
        dec = overrides.get(worth_pub, {})
        action = str(dec.get("action") or "").strip().lower()
        approved = str(dec.get("approved") or "").strip().lower()
        new_url = str(dec.get("new_linkedin_url") or "").strip()
        new_pub = (str(dec.get("new_public_identifier") or "").strip().lower()
                   or extract_public_identifier(new_url).lower())
        proposed_retarget = action == "retarget" and bool(new_url and new_pub)
        p["candidates"].append({
            "pub": worth_pub,
            "profile_pub": new_pub if proposed_retarget else pub,
            "url": new_url if proposed_retarget else li.get("linkedin_url", ""),
            # Never show the old/wrong profile's biography as if it described a
            # proposed replacement. Replacement metadata appears after hydration.
            "full_name": (r.get("name") or new_pub) if proposed_retarget else li.get("full_name", ""),
            "headline": "" if proposed_retarget else li.get("headline", ""),
            "profile_pic_url": (_cached_profile_pic(new_pub) if proposed_retarget
                                    else li.get("profile_pic_url") or _cached_profile_pic(pub)),
            "experiences": [] if proposed_retarget else li.get("experiences") or [],
            "education": [] if proposed_retarget else li.get("education") or [],
            "location": "" if proposed_retarget else li.get("location", ""),
            "has_profile": bool(proposed_retarget or li.get("has_profile", False)),
            "verdict": v.get("verdict", ""),
            "confidence": float(v.get("confidence") or 0.0),
            "supporting": v.get("supporting_evidence") or [],
            "contradicting": v.get("contradicting_evidence") or [],
            "reason": str(dec.get("reason") or "") if proposed_retarget else v.get("reason", ""),
            "plausibly_absent": bool(v.get("linkedin_plausibly_absent")),
            "recommend_dr": bool(v.get("recommend_deep_research")),
            "match_emails": r.get("match_emails") or [],
            "match_phones": r.get("match_phones") or [],
            "conflict": bool(r.get("conflict")),
            "import_candidate": import_candidate,
            "candidate_origin": import_candidate,
            # current decision (from review.csv)
            "action": action,
            "approved": approved,
            "new_url": new_url,
            # machine-owned profile-proposal rejection (identity, not worth)
            "llm_reject": (dec.get("llm_reject") or "").strip().lower(),
            "llm_reject_confidence": dec.get("llm_reject_confidence", ""),
            "llm_reject_reason": dec.get("llm_reject_reason", ""),
        })
    return list(parents.values()), overrides


def is_worth_no(parent: dict[str, Any]) -> bool:
    """'Not worth adding' — the row's effective network-worth is `no` (user or LLM)."""
    return (parent.get("worth") or {}).get("decision") == "no"


def _keepish(parent: dict[str, Any]) -> bool:
    """A keep-ish user decision on any candidate (approved=yes and a keeping action) —
    the same rescue the fan-in merge honors against a machine no."""
    return any((c.get("approved") or "").strip().lower() == "yes"
               and (c.get("action") or "").strip().lower() not in ("detach", "exclude")
               for c in parent["candidates"])


def is_effective_no(parent: dict[str, Any]) -> bool:
    """The unified 'effective no' (== the Rejected tab == dropped at the next fan-in
    merge): the user said no (worth mark or approved Exclude — both surface as a
    user-sourced worth of no), or synthesis said no with no user rescue
    (worth-Yes or a keep-ish decision)."""
    if any(str(c.get("action") or "").strip().lower() == "exclude"
           and str(c.get("approved") or "").strip().lower() in APPLIED_APPROVED
           for c in parent.get("candidates") or []):
        return True
    worth = parent.get("worth") or {}
    user_mark = (worth.get("decision") or "") if worth.get("source") == "user" else ""
    if user_mark == "no":
        return True
    if user_mark == "yes":
        return False
    # a synthetic row's Detach/Exclude lives in its approved gate, not review.csv
    if any(c.get("synthetic") and (c.get("approved") or "").strip().lower() == "no"
           for c in parent["candidates"]):
        return True
    # LinkedIn connections are GROUND TRUTH — a machine no never rejects them
    if parent.get("connection"):
        return False
    machine_no = (parent.get("machine_worth") or {}).get("decision") == "no"
    return machine_no and not _keepish(parent)


def parent_in_tab(parent: dict[str, Any], tab: str) -> bool:
    if tab in ("", "all"):
        return True
    if tab == "decided":
        return is_decided(parent)
    if tab == "conflict":
        return any(c.get("conflict") for c in parent["candidates"]) or len(parent["candidates"]) > 1
    if tab == "rejected":
        return is_effective_no(parent)
    if tab == "review":
        # effective-no people (worth-no or excluded) live on the Rejected tab
        return parent_status(parent) == "review" and not is_effective_no(parent)
    return parent_status(parent) == tab


def parent_matches_query(parent: dict[str, Any], q: str) -> bool:
    if not q:
        return True
    hay = [parent["name"], parent["slug"]]
    for c in parent["candidates"]:
        hay += [c["pub"], c["url"], c["full_name"], c["headline"], c["location"], c["reason"]]
        hay += c["match_emails"] + c["match_phones"]
    return q in " ".join(hay).lower()


def parent_matches_source(parent: dict[str, Any], source: str) -> bool:
    return source not in SOURCE_FILTERS or source in (parent.get("sources") or [])


def parent_matches_worth(parent: dict[str, Any], worth: str) -> bool:
    return worth not in NETWORK_WORTH_VALUES or (parent.get("worth") or {}).get("decision") == worth


def summarize(parents: list[dict[str, Any]]) -> dict[str, int]:
    s = {k: 0 for k in ("total", "review", "verified", "detached", "conflict", "fixed", "excluded", "decided", "rejected")}
    s["total"] = len(parents)
    for p in parents:
        s[parent_status(p)] += 1
        if parent_in_tab(p, "conflict"):
            s["conflict"] += 1
        if is_decided(p):
            s["decided"] += 1
        if is_effective_no(p):
            s["rejected"] += 1
    # user-facing: a retarget ("fixed") reads as verified; effective-no rows leave the review pile
    s["verified"] += s["fixed"]
    s["review"] = sum(1 for p in parents if parent_in_tab(p, "review"))
    return s


def collapse_by_current_parent(parents: list[dict[str, Any]],
                               index_json: Path = INDEX_JSON) -> list[dict[str, Any]]:
    """Defensively fold review-parents that resolve to the SAME current deep-context
    parent into ONE card.

    A later cluster_merge can fold two former parents into one while stale, parent-scoped
    artifacts (a synthetic row, a research verdict) still carry the OLD parent slug — the
    assemble step self-heals those on rerun, but a stale artifact must never render two cards
    for one merged person in the meantime. Parents whose ``person_ids`` map to the same current
    parent (via the child->parent membership index.json already encodes) merge: candidates are
    unioned (deduped by pub) so BOTH LinkedIn/synthetic options land in the one card, and a
    parent with >1 candidate already flows into the conflict tab. Parents whose person_ids are
    not co-located under one current parent are left untouched, so genuinely-distinct people
    still get one card each.
    """
    parent_map = current_parent_by_person_id(index_json)

    def current_slug(parent: dict[str, Any]) -> str:
        for pid in parent.get("person_ids") or []:
            slug = parent_map.get(str(pid or "").strip().lower())
            if slug:
                return slug
        return ""

    order: list[str] = []
    groups: dict[str, dict[str, Any]] = {}
    passthrough: list[dict[str, Any]] = []
    for parent in parents:
        slug = current_slug(parent)
        if not slug:
            passthrough.append(parent)
            continue
        if slug not in groups:
            groups[slug] = parent
            order.append(slug)
            continue
        base = groups[slug]
        seen = {str(c.get("pub") or "").strip().lower() for c in base["candidates"]}
        for cand in parent.get("candidates") or []:
            pub = str(cand.get("pub") or "").strip().lower()
            if pub and pub in seen:
                continue
            seen.add(pub)
            base["candidates"].append(cand)
        for pid in parent.get("person_ids") or []:
            if pid not in base["person_ids"]:
                base["person_ids"].append(pid)
        base_sources = base.get("sources") or []
        for source in parent.get("sources") or []:
            if source not in base_sources:
                base_sources.append(source)
        base["sources"] = base_sources
    merged = passthrough + [groups[slug] for slug in order]
    # Preserve the original relative ordering as closely as possible (grouped parents
    # keep the position of their first occurrence).
    return merged


def extend_and_annotate(parents: list[dict[str, Any]], overrides: dict[str, dict[str, str]],
                        synthetic_path: Path, facts_dir: Path,
                        connections: set[str] | None = None, *,
                        parents_dir: Path = PARENTS_DIR,
                        dossier_dir: Path = DOSSIER_DIR,
                        profile_cache_dir: Path = PROFILE_CACHE_DIR,
                        research_dir: Path = DEEP_RESEARCH_DIR,
                        index_json: Path = INDEX_JSON) -> list[dict[str, Any]]:
    """Add the synthetic + pre-research candidate rows to the verdict parents and
    annotate everyone's worth — the full row set page_html/summarize operate on."""
    parents.extend(load_synthetic_parents(
        synthetic_path, parents_dir, dossier_dir, facts_dir))
    shown = {pid.lower() for p in parents for pid in p["person_ids"]}
    parents.extend(load_candidate_parents(facts_dir, overrides, shown))
    parents[:] = collapse_by_current_parent(parents, index_json)
    hydrate_proposed_profiles(
        parents, profile_cache_dir=profile_cache_dir, research_dir=research_dir)
    annotate_worth(parents, overrides, facts_dir, connections,
                   index_json=index_json)
    return parents


def live_counts(verdicts_path: Path, review_path: Path, synthetic_path: Path,
                facts_dir: Path, connections: set[str] | None = None) -> dict[str, int]:
    """Fresh GLOBAL tab counts after a mutation. Every POST returns these so the client
    repaints the header stats and tab pills authoritatively — recomputing counts from
    the DOM would drift on filtered views (only the visible subset is in the DOM)."""
    parents, overrides = build_parents(verdicts_path, review_path)
    return summarize(extend_and_annotate(parents, overrides, synthetic_path, facts_dir,
                                         connections))


# --- staged review model ---------------------------------------------------

def is_import_candidate_parent(parent: dict[str, Any]) -> bool:
    return any(cand.get("import_candidate") for cand in parent.get("candidates") or [])


def is_candidate_origin(parent: dict[str, Any]) -> bool:
    """A reconciled/synthetic person that came from an unresolved import."""
    return any(is_candidate_id(str(person_id or "")) for person_id in parent.get("person_ids") or [])


def is_worth_subject(parent: dict[str, Any]) -> bool:
    """A standalone imported contact whose add/no decision stays reviewable.

    Retarget and synthetic results remain in this scope because their durable
    worth key is still the candidate id. A candidate folded into an existing
    real parent does not: that person's network membership already exists.
    """
    person_ids = [str(value or "") for value in parent.get("person_ids") or []]
    return is_import_candidate_parent(parent) or (
        bool(person_ids)
        and all(is_candidate_id(person_id) for person_id in person_ids)
        and is_candidate_id(_worth_key(parent))
    )


def in_worth_view(parent: dict[str, Any]) -> bool:
    """The worth SECTION is worth_view's row set, nothing else: judged people
    (facts verdict + human override, identities grouped by index parent),
    regardless of people.csv / network membership. See worth_view.py's header
    for the entire logic — this module only renders it."""
    return parent.get("worth_row") is not None


def explicit_worth(parent: dict[str, Any]) -> str:
    """The user's terminal binary worth decision, ignoring model/default advice."""
    worth = parent.get("worth") or {}
    decision = str(worth.get("decision") or "").strip().lower()
    return decision if worth.get("source") == "user" and decision in USER_WORTH_VALUES else ""


def _effective_yes(parent: dict[str, Any]) -> bool:
    row = parent.get("worth_row")
    return bool(row) and row["effective"] == "yes"


def _effective_no_row(parent: dict[str, Any]) -> bool:
    row = parent.get("worth_row")
    return bool(row) and row["effective"] == "no"


def needs_worth_review(parent: dict[str, Any]) -> bool:
    """Only model-uncertain people need the first human decision: worth_view
    effective == maybe. A synthetic-profile row is already past the worth
    decision (it went through research/minting) and is handled in the
    LinkedIn/mint stage, so it is excluded even when its row is a maybe."""
    row = parent.get("worth_row")
    return (bool(row) and row["effective"] == "maybe"
            and not any(cand.get("synthetic") for cand in parent.get("candidates") or []))


def is_lookup_ready(parent: dict[str, Any]) -> bool:
    return is_worth_subject(parent) and _effective_yes(parent)


def pending_linkedin_candidates(parent: dict[str, Any]) -> list[dict[str, Any]]:
    """Candidates that still need the second human Yes/No.

    Existing high-confidence links may remain machine-approved. Every new
    identity originating from an import candidate must be explicitly checked;
    ``approved=auto`` on a synthetic row is profile completeness, not confidence
    that this is the right human, so it is still a pending identity decision.
    """
    if is_import_candidate_parent(parent) or is_effective_no(parent):
        return []
    from_candidate = is_candidate_origin(parent)
    pending: list[dict[str, Any]] = []
    for cand in parent.get("candidates") or []:
        approved = str(cand.get("approved") or "").strip().lower()
        if cand.get("synthetic"):
            if approved not in {"yes", "no"}:
                pending.append(cand)
        elif from_candidate:
            # A judge-ACCEPTED found profile stands (review_store's predicate):
            # the identity judge already vetted it against the dossier, so only
            # unjudged/rejected candidates still need the human Yes/No.
            if approved not in {"yes", "no"} and not judge_accepted_candidate_retarget(cand):
                pending.append(cand)
        elif candidate_state(cand) == "review":
            pending.append(cand)
    return sorted(pending, key=_cand_rank)


def identity_in_scope(parent: dict[str, Any]) -> bool:
    if is_import_candidate_parent(parent) or is_effective_no(parent):
        return False
    if is_candidate_origin(parent) or any(c.get("synthetic") for c in parent.get("candidates") or []):
        return True
    return any(candidate_state(c) == "review"
               or str(c.get("approved") or "").strip().lower() in {"yes", "no"}
               for c in parent.get("candidates") or [])


def review_progress(parents: list[dict[str, Any]]) -> dict[str, int]:
    # Worth counts come from worth_view rows, deduped: several review-parents
    # can share one PERSON row (merged identities render once, count once).
    seen: set[int] = set()
    worth_total = worth_pending = worth_yes = worth_no = 0
    for parent in parents:
        row = parent.get("worth_row")
        if row is None or id(row) in seen:
            continue
        seen.add(id(row))
        worth_total += 1
        if needs_worth_review(parent):
            worth_pending += 1
        elif row["effective"] == "yes":
            worth_yes += 1
        elif row["effective"] == "no":
            worth_no += 1
    lookup_ready = [parent for parent in parents if is_lookup_ready(parent)]
    identity_scope = [parent for parent in parents if identity_in_scope(parent)]
    identity_pending = [parent for parent in identity_scope if pending_linkedin_candidates(parent)]
    return {
        "total": len(parents),
        "worth_total": worth_total,
        "worth_pending": worth_pending,
        "worth_yes": worth_yes,
        "worth_no": worth_no,
        "lookup_ready": len(lookup_ready),
        "linkedin_total": len(identity_scope),
        "linkedin_pending": len(identity_pending),
        "linkedin_done": len(identity_scope) - len(identity_pending),
        "rejected": sum(1 for parent in parents if is_effective_no(parent)),
    }


def review_state_token(progress: dict[str, int], selection: dict[str, Any],
                       enrichment: dict[str, Any],
                       review_manifest: dict[str, Any], *,
                       job_running: bool = False) -> str:
    """Ephemeral browser refresh token derived from the fixed file state PLUS
    whether the in-process pipeline job is alive. The job bit closes a TOCTOU:
    a page rendered while the job was finishing can carry a token computed from
    the job's FINAL manifest writes — without the bit, nothing changes after the
    job exits and the observer never reloads the "working" screen."""
    payload = {
        "progress": progress,
        "selection": selection,
        "job_running": bool(job_running),
        "enrichment": {
            "status": enrichment.get("status"),
            "current": enrichment.get("current"),
            "approval_current": enrichment.get("approval_current"),
            "counts": enrichment.get("counts") or {},
            "updated_at": enrichment.get("updated_at"),
        },
        "review": {
            "stage": review_manifest.get("stage"),
            "status": review_manifest.get("status"),
            "completed_stages": review_manifest.get("completed_stages") or [],
            "updated_at": review_manifest.get("updated_at"),
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def browser_stage_for_next_action(next_action: str) -> str:
    if next_action == "review_people":
        return "worth"
    if next_action in {
            "preview_enrichment", "await_enrichment_approval",
            "run_approved_enrichment", "run_enrichment_from_cache",
            "wait_for_enrichment", "retry_enrichment", "assemble_synthetic",
            "continue_enrichment"}:
        return "enrich"
    if next_action in {"review_linkedin", "finish_linkedin"}:
        return "linkedin"
    return "done"


def worth_selection_from_parents(
    parents: list[dict[str, Any]], *, manifest_path: Path = REVIEW_MANIFEST,
) -> dict[str, Any]:
    decisions = [
        {"person_id": _worth_key(parent),
         "decision": str((parent.get("worth") or {}).get("decision") or "maybe")}
        for parent in parents if is_worth_subject(parent) and _worth_key(parent)
    ]
    decisions.sort(key=lambda row: row["person_id"])
    encoded = json.dumps(decisions, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    review_manifest = read_review_manifest(manifest_path)
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "total": len(decisions),
        "yes": sum(row["decision"] == "yes" for row in decisions),
        "maybe": sum(row["decision"] == "maybe" for row in decisions),
        "no": sum(row["decision"] == "no" for row in decisions),
        "review_revision": str(review_manifest.get("people_revision") or ""),
    }


def current_worth_selection(*, manifest_path: Path = REVIEW_MANIFEST) -> dict[str, Any]:
    """The one authoritative People-worth selection digest, built from the live review
    parents. Both the review status and the enrichment manifest must stamp THIS value so
    their sha256 can never drift: a candidate promoted to a verified LinkedIn parent (e.g.
    via a retarget/verify) leaves the worth pool here for both sides at once, instead of
    the enrichment side re-deriving the set from candidate files and disagreeing by one."""
    parents = _all_review_parents(
        VERDICTS_JSONL, LINKEDIN_OVERRIDES_CSV, SYNTHETIC_PEOPLE_CSV, FACTS_DIR,
        DEFAULT_PEOPLE_CSV, PARENTS_DIR, DOSSIER_DIR, PROFILE_CACHE_DIR)
    return worth_selection_from_parents(parents, manifest_path=manifest_path)


def approve_enrichment_manifest(path: Path = ENRICH_MANIFEST, *,
                                selection: dict[str, Any]) -> dict[str, Any]:
    """Persist the UI's exact spend approval in the fixed enrichment manifest.

    The approval is inert: the web server never starts a subprocess or provider.
    ``workflow_status`` validates this revision-bound record and exposes the one
    approved command for the agent to run.
    """
    enrichment = read_enrichment_manifest(path, selection=selection)
    if not enrichment.get("current"):
        raise ValueError("Enrichment preview is stale; refresh the preview before approving")
    # A five-second browser observer can leave a just-completed approval button
    # visible briefly after the agent has already advanced the fixed manifest.
    # Treat that stale click as an idempotent success so the client simply
    # reloads into the current progress state instead of showing a false error.
    if enrichment.get("status") in IN_FLIGHT_STATUSES:
        return enrichment
    if enrichment.get("status") != STATUS_NEEDS_APPROVAL:
        raise ValueError("Enrichment is not waiting for approval")
    if enrichment.get("approval_current"):
        return enrichment
    try:
        would_submit = int(enrichment.get("would_submit") or 0)
        estimate = round(float(enrichment.get("estimated_usd") or 0), 2)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Enrichment estimate is invalid") from exc
    if would_submit <= 0:
        raise ValueError("No paid enrichment approval is required")
    if not math.isfinite(estimate) or estimate <= 0:
        raise ValueError("Enrichment estimate must be a positive finite amount")
    recorded_selection = enrichment.get("selection") or {}
    payload = {key: value for key, value in enrichment.items()
               if key not in {"current", "approval_current"}}
    payload["approval"] = {
        "status": "approved",
        "approved_at": now_iso(),
        "approved_budget_usd": estimate,
        "estimated_usd": estimate,
        "would_submit": would_submit,
        "selection_sha256": str(recorded_selection.get("sha256") or ""),
        "review_revision": str(recorded_selection.get("review_revision") or ""),
    }
    payload["updated_at"] = now_iso()
    write_manifest(path.parent.name, payload, import_dir=path.parent.parent)
    return read_enrichment_manifest(path, selection=selection)


def phase_counts(progress: dict[str, int], stage: str) -> dict[str, int]:
    if stage == "worth":
        return {
            "total": progress["worth_total"],
            "yes": progress["worth_yes"],
            "no": progress["worth_no"],
            "pending": progress["worth_pending"],
            "ready_for_lookup": progress["lookup_ready"],
        }
    if stage == "linkedin":
        return {
            "total": progress["linkedin_total"],
            "yes_or_no": progress["linkedin_done"],
            "pending": progress["linkedin_pending"],
        }
    raise ValueError(f"unknown review stage: {stage}")


def read_review_manifest(path: Path = REVIEW_MANIFEST) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_review_manifest(stage: str, status: str, progress: dict[str, int], *,
                          path: Path = REVIEW_MANIFEST,
                          review_path: Path = LINKEDIN_OVERRIDES_CSV,
                          synthetic_path: Path = SYNTHETIC_PEOPLE_CSV,
                          launched: bool = False) -> dict[str, Any]:
    if stage not in {"worth", "enrich", "linkedin"}:
        raise ValueError(f"unknown review stage: {stage}")
    if status not in {"awaiting_user", "completed"}:
        raise ValueError(f"unknown review status: {status}")
    if path.name != "manifest.json":
        raise ValueError("review manifest path must end in manifest.json")
    if stage == "enrich":
        raise ValueError("Enrich completion must be written from the enrichment manifest")
    counts = phase_counts(progress, stage)
    if status == "completed" and counts["pending"]:
        raise ValueError(f"{counts['pending']} decisions still need an answer")
    existing = read_review_manifest(path)
    completed = {str(value) for value in existing.get("completed_stages") or []
                 if value in {"worth", "enrich", "linkedin"}}
    if (existing.get("status") == "completed"
            and existing.get("stage") in {"worth", "enrich", "linkedin"}):
        completed.add(str(existing["stage"]))
    if status == "awaiting_user":
        completed.discard(stage)
        if stage == "worth":
            completed.discard("enrich")
            completed.discard("linkedin")
    else:
        # Worth must precede LinkedIn, but enrichment does NOT block it: the
        # LinkedIn stage is reviewable (and completable) even when enrichment is
        # still running or failed, so a broken enrichment never strands the flow.
        if stage == "linkedin" and "worth" not in completed:
            raise ValueError("People decisions must be completed before LinkedIn")
        completed.add(stage)
    people_revision = str(existing.get("people_revision") or "")
    if stage == "worth" and launched:
        people_revision = str(time.time_ns())
    if not people_revision:
        people_revision = str(time.time_ns())
    payload: dict[str, Any] = {
        "stage": stage,
        "status": status,
        "counts": counts,
        "completed_stages": sorted(completed, key=("worth", "enrich", "linkedin").index),
        "people_revision": people_revision,
        "review_csv": str(review_path),
        "synthetic_people_csv": str(synthetic_path),
        "privacy": {"message_bodies_read": False, "network_called": True,
                    "paid_provider_called": False,
                    "note": "avatar cache misses may fetch an existing LinkedIn CDN image"},
    }
    if launched:
        payload["launched_at"] = now_iso()
        payload["launched_at_unix_ns"] = time.time_ns()
    elif existing.get("stage") == stage:
        for key in ("launched_at", "launched_at_unix_ns"):
            if key in existing:
                payload[key] = existing[key]
    if status == "completed":
        payload["completed_at"] = now_iso()
    return write_manifest(path.parent.name, payload, import_dir=path.parent.parent)


def write_enrichment_handoff(
    enrichment: dict[str, Any], *, path: Path = REVIEW_MANIFEST,
    review_path: Path = LINKEDIN_OVERRIDES_CSV,
    synthetic_path: Path = SYNTHETIC_PEOPLE_CSV,
) -> dict[str, Any]:
    """Record only the user's Continue handoff after current enrichment finished."""
    if enrichment.get("status") != STATUS_COMPLETED or not enrichment.get("current"):
        raise ValueError("Enrichment is not complete for the current People decisions")
    existing = read_review_manifest(path)
    completed = {str(value) for value in existing.get("completed_stages") or []
                 if value in {"worth", "enrich", "linkedin"}}
    if "worth" not in completed:
        raise ValueError("People decisions must be completed before enrichment")
    completed.add("enrich")
    completed.discard("linkedin")
    payload = {
        "stage": "enrich",
        "status": "completed",
        "counts": enrichment.get("counts") or {},
        "completed_stages": sorted(completed, key=("worth", "enrich", "linkedin").index),
        "people_revision": str(existing.get("people_revision") or ""),
        "review_csv": str(review_path),
        "synthetic_people_csv": str(synthetic_path),
        "completed_at": now_iso(),
        "privacy": {"message_bodies_read": False, "network_called": False,
                    "paid_provider_called": False},
    }
    return write_manifest(path.parent.name, payload, import_dir=path.parent.parent)


def enrichment_handoff_completed(path: Path = REVIEW_MANIFEST) -> bool:
    return "enrich" in set(read_review_manifest(path).get("completed_stages") or [])


def phase_is_completed(stage: str, progress: dict[str, int], path: Path = REVIEW_MANIFEST) -> bool:
    manifest = read_review_manifest(path)
    counts = phase_counts(progress, stage)
    completed = set(manifest.get("completed_stages") or [])
    if stage in completed and manifest.get("stage") != stage:
        return True
    return (manifest.get("stage") == stage
            and manifest.get("status") == "completed"
            and manifest.get("counts") == counts
            and counts["pending"] == 0)


# --- decision writer (the only mutation: upsert one row in review.csv) ------

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


# --- dossier rendering (show the rich CHILD dossiers, not the thin parent stub) --

_CHILDREN_RE = re.compile(r"^children:\s*\[(.*?)\]", re.MULTILINE)


def _strip_frontmatter(md: str) -> str:
    if md.startswith("---"):
        end = md.find("\n---", 3)
        if end != -1:
            return md[end + 4:].lstrip("\n")
    return md


def render_dossier(parents_dir: Path, dossier_dir: Path, slug: str) -> str:
    """The message dossier for a person = its CHILD dossiers concatenated. The parent .md is a
    thin canonical stub (for singletons just a pointer), so reading it alone looks 'chopped off' —
    the real per-person context (summary, topics, timeline, identifiers) lives in the children."""
    pmd = parents_dir / f"{Path(slug).name}.md"
    if not pmd.exists():
        # Candidate rows point straight at their composed CHILD dossier (no parent
        # stub exists before research mints a person for them).
        child = dossier_dir / f"{Path(slug).name}.md"
        if child.exists():
            return _strip_frontmatter(child.read_text(encoding="utf-8")).strip()
        return "(no dossier on file)"
    ptext = pmd.read_text(encoding="utf-8")
    m = _CHILDREN_RE.search(ptext)
    children = re.findall(r'"([^"]+)"', m.group(1)) if m else []
    chunks = []
    for cs in children:
        cd = dossier_dir / f"{Path(cs).name}.md"
        if cd.exists():
            body = _strip_frontmatter(cd.read_text(encoding="utf-8"))
            body = "\n".join(ln for ln in body.splitlines() if "<!-- parent-link -->" not in ln)
            chunks.append(body.strip())
    if not chunks:
        return _strip_frontmatter(ptext).strip()
    sep = "\n\n" + "─" * 56 + "\n\n"
    header = f"This person was merged from {len(chunks)} message cluster(s):\n\n" if len(chunks) > 1 else ""
    return header + sep.join(chunks)


# The bookkeeping cadence footer compose_dossier appends under "Relationship &
# cadence" ("_grokked N of M messages ... (stopped: ...)._") — a build stat, not
# relationship prose, so it is dropped from the preview Summary.
_GROKKED_RE = re.compile(r"^\s*_?grokked\b.*$", re.IGNORECASE)
# Bare-value dossier bullets in "Identifiers" ("- email/phone/url/address").
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")
# Markdown artifacts we never want to leak into the plain-text Summary body:
# wiki links ([[slug]] / [[slug|label]]) and inline emphasis markers.
_WIKILINK_RE = re.compile(r"\[\[[^\]|]*\|([^\]]*)\]\]|\[\[([^\]]*)\]\]")


def _dossier_section(markdown: str, heading: str) -> str:
    """The raw body of one ``## <heading>`` section, or '' when it is absent.

    Uses the same anchor pattern the rest of this module uses to slice a
    dossier section (up to the next ``##`` heading or end of file)."""
    match = re.search(
        rf"(?ms)^##\s+{re.escape(heading)}\s*\n+(.*?)(?=^##\s|\Z)", markdown)
    return match.group(1).strip() if match else ""


def _clean_text(value: str) -> str:
    """Message-derived markdown -> clean readable text (no wiki links / emphasis)."""
    text = _WIKILINK_RE.sub(lambda m: m.group(1) or m.group(2) or "", value)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)          # bold
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)   # italic (underscore)
    return text.strip()


def relationship_summary(parents_dir: Path, dossier_dir: Path, slug: str) -> str:
    """The dossier's "Relationship & cadence" prose as clean, readable text.

    Shown on cards under the "Relationship" label — NOT the dossier's own
    "## Summary" (which holds the "Network worth:" line and is intentionally
    dropped). The trailing "_grokked ..._" bookkeeping line is stripped;
    '' when the section is absent.
    """
    markdown = render_dossier(parents_dir, dossier_dir, slug)
    body = _dossier_section(markdown, "Relationship & cadence")
    lines = [line for line in body.splitlines() if not _GROKKED_RE.match(line)]
    return _clean_text("\n".join(lines).strip())


def timeline_entries(parents_dir: Path, dossier_dir: Path, slug: str) -> list[str]:
    """The dossier's "Timeline" per-date bullet lines as clean, readable text."""
    markdown = render_dossier(parents_dir, dossier_dir, slug)
    body = _dossier_section(markdown, "Timeline")
    entries: list[str] = []
    for line in body.splitlines():
        bullet = _BULLET_RE.match(line)
        text = _clean_text(bullet.group(1) if bullet else line)
        if text:
            entries.append(text)
    return entries


# Video-call / scheduling links sometimes land in dossier Identifiers, but they
# are not contact info; keep emails, phones, and social handles (GitHub etc.).
_MEETING_URL_RE = re.compile(
    r"(?i)\b(?:meet\.google\.com|zoom\.us|teams\.microsoft\.com|teams\.live\.com|"
    r"webex\.com|calendly\.com|cal\.com|whereby\.com|gotomeeting\.com)\b")


def dossier_identifiers(parents_dir: Path, dossier_dir: Path, slug: str) -> list[str]:
    """Bare identifier values (emails/phones/etc.) from the dossier's "Identifiers"
    section, so the card can bubble them up into its Contact line. Display-side
    only (dossier files are never rewritten); meeting/scheduling URLs are
    dropped because they are not contact info."""
    markdown = render_dossier(parents_dir, dossier_dir, slug)
    body = _dossier_section(markdown, "Identifiers")
    values: list[str] = []
    seen: set[str] = set()
    for line in body.splitlines():
        bullet = _BULLET_RE.match(line)
        if not bullet:
            continue
        value = _clean_text(bullet.group(1))
        if not value or value.lower() in seen or _MEETING_URL_RE.search(value):
            continue
        values.append(value)
        seen.add(value.lower())
    return values


def render_dossier_markdown(parents_dir: Path, dossier_dir: Path, slug: str) -> str:
    """The card's dossier preview: exactly two extracted sections as safe HTML.

    "Relationship" is the "Relationship & cadence" prose (never the dossier's
    own "## Summary", which carries the Network worth line). "Timeline" keeps its
    per-date bullets. Everything else (name block, wiki links, Topics,
    Identifiers, Possible same person, worth lines) is dropped; identifiers are
    bubbled up into the card's Contact section instead. Absent sections are
    omitted rather than raising, and all text is HTML-escaped.
    """
    parts: list[str] = []
    summary = relationship_summary(parents_dir, dossier_dir, slug)
    if summary:
        paragraphs = "".join(
            f"<p>{esc(block.strip())}</p>"
            for block in re.split(r"\n\s*\n", summary) if block.strip())
        parts.append(f"<div><dt>Relationship</dt><dd>{paragraphs}</dd></div>")
    timeline = timeline_entries(parents_dir, dossier_dir, slug)
    if timeline:
        items = "".join(f"<li>{esc(entry)}</li>" for entry in timeline)
        parts.append(f"<div><dt>Timeline</dt><dd><ul class='fact-list'>{items}</ul></dd></div>")
    return "".join(parts)


# --- rendering --------------------------------------------------------------

def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _initials(name: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", name or "")
    if not words:
        return "?"
    return (words[0][0] + (words[-1][0] if len(words) > 1 else "")).upper()


def _primary_candidate(parent: dict[str, Any]) -> dict[str, Any]:
    candidates = parent.get("candidates") or []
    return min(candidates, key=_cand_rank) if candidates else {}


def _worth_key(parent: dict[str, Any]) -> str:
    primary = _primary_candidate(parent)
    return str(primary.get("worth_key") or (parent.get("person_ids") or [""])[0] or "")


def _avatar(parent: dict[str, Any], candidate: dict[str, Any] | None = None, *, small: bool = False) -> str:
    cand = candidate or _primary_candidate(parent)
    name = str(cand.get("full_name") or parent.get("name") or "")
    pub = str(cand.get("profile_pub") or cand.get("pub") or "").strip().lower()
    can_load = bool(pub and not cand.get("import_candidate") and not cand.get("synthetic")
                    and (cand.get("profile_pic_url") or _cached_profile_pic(pub)))
    image = (f"<img src='/api/avatar?pub={urllib.parse.quote(pub)}' alt='' loading='eager' "
             "onerror='this.remove()'>" if can_load else "")
    return (f"<span class='avatar{' avatar-small' if small else ''}' aria-label='{esc(name)}'>"
            f"<span>{esc(_initials(name))}</span>{image}</span>")


def _machine_copy(parent: dict[str, Any]) -> tuple[str, str]:
    machine = parent.get("machine_worth") or parent.get("worth") or {}
    decision = str(machine.get("decision") or "maybe").lower()
    label = {"yes": "Suggested yes", "no": "Suggested no", "maybe": "AI is unsure"}.get(decision, "AI is unsure")
    return label, str(machine.get("reason") or "")


def _merge_contacts(contacts: list[str], identifiers: list[str]) -> list[str]:
    """Contact values plus the dossier's Identifiers, deduped case-insensitively
    against what is already shown (bubbling the dossier's aliases up into Contact)."""
    merged = list(contacts)
    seen = {value.lower() for value in merged}
    for value in identifiers:
        if value and value.lower() not in seen:
            merged.append(value)
            seen.add(value.lower())
    return merged


# Shown as the card's Summary ONLY when the card has no kept/attached LinkedIn
# and no displayable reason (the researched-and-failed case). Cards WITH a kept
# link and no displayable reason omit the Summary row instead — "Couldn't find
# profile" would be nonsense when we did find them.
NO_PROFILE_SUMMARY = "Couldn't find profile"


def _display_reason(reason: str) -> str:
    """Card display only (stored CSV reasons are never rewritten): the
    "deep research: <process notes>" text is NEVER shown. A real summary before
    a "; deep research:" tail is kept; a reason that is only the research blob
    (or empty) yields '' — the caller decides between omitting the row and the
    no-profile fallback based on whether the card has a kept link."""
    marker = reason.lower().find("deep research:")
    cleaned = reason if marker == -1 else reason[:marker]
    return cleaned.strip().rstrip(";,·—–-").strip()


# Top entries pinned in Work/Education fact lists; the rest sit behind a toggle.
FACT_LIST_PINNED = 3


def _fact_list(items: list[str], *, pinned: int = FACT_LIST_PINNED) -> str:
    """Bullet list with the first ``pinned`` entries shown; longer lists keep the
    rest behind a "+ show N more" toggle (wired in reconcile_review.js)."""
    shown = "".join(f"<li>{esc(item)}</li>" for item in items[:pinned])
    hidden = "".join(f"<li hidden data-more-item>{esc(item)}</li>" for item in items[pinned:])
    extra = len(items) - pinned
    toggle = (f"<button type='button' class='show-more' data-show-more "
              f"data-more-label='+ show {extra} more' data-less-label='show fewer'>"
              f"+ show {extra} more</button>" if extra > 0 else "")
    return f"<ul class='fact-list'>{shown}{hidden}</ul>{toggle}"


def profile_fact_rows(candidate: dict[str, Any]) -> list[str]:
    """Work / Education dt-dd rows in the same section style as Contact."""
    rows: list[str] = []
    if candidate.get("experiences"):
        rows.append(f"<div><dt>Work</dt><dd>{_fact_list(candidate['experiences'])}</dd></div>")
    if candidate.get("education"):
        rows.append(f"<div><dt>Education</dt><dd>{_fact_list(candidate['education'])}</dd></div>")
    return rows


def _details(parent: dict[str, Any], candidate: dict[str, Any], *, identity: bool,
             profile_rows: list[str] | None = None,
             identifiers: list[str] | None = None,
             profile_placeholder: str = "",
             sparse_context: bool = False) -> str:
    contacts = _merge_contacts(
        [*(candidate.get("match_emails") or []), *(candidate.get("match_phones") or [])],
        identifiers or [])
    reason = _display_reason(str(candidate.get("reason") or ""))
    # The prefetch stage's LLM-written profile summary, lifted onto the candidate
    # from the local cache at render time (display-only; the server never calls
    # an LLM). It is PREFERRED for the Summary row over the stored judge reason.
    simple_summary = str(candidate.get("simple_summary") or "").strip()
    # Section order: Contact -> Summary (simple_summary || the judge/verify reason)
    # -> the lazily loaded dossier rows (Relationship -> Timeline) -> the profile
    # facts (Work / Education) when profile data exists. (No Evidence section.)
    rows: list[str] = []
    if contacts:
        rows.append(f"<div><dt>Contact</dt><dd>{esc(' · '.join(contacts))}</dd></div>")
    if simple_summary:
        rows.append(f"<div><dt>Summary</dt><dd>{esc(simple_summary)}</dd></div>")
    elif identity:
        if reason:
            rows.append(f"<div><dt>Summary</dt><dd>{esc(reason)}</dd></div>")
        elif not str(candidate.get("url") or "").strip():
            # no kept link AND nothing displayable -> researched-and-failed
            rows.append(f"<div><dt>Summary</dt><dd>{esc(NO_PROFILE_SUMMARY)}</dd></div>")
        # kept link with no displayable reason: omit the row entirely
    extra = f"<dl>{''.join(rows)}</dl>" if rows else ""
    context_notice = (
        "<p class='context-empty'>Not enough information.</p>"
        if sparse_context else ""
    )
    profile_dl = (f"<dl>{''.join(profile_rows)}</dl>" if profile_rows else "")
    dossier_slug = parent.get("dossier_slug") or parent.get("slug")
    # No "Details"/"Context" section labels; the dossier preview renders as more
    # dt/dd rows in the SAME dl style (no inset box), lazily via /api/dossier.
    return (f"<section class='details' data-slug='{esc(dossier_slug)}'>"
            f"<div class='details-body'>{profile_placeholder}{extra}{context_notice}"
            "<dl class='dossier-text' aria-busy='true'></dl>"
            f"{profile_dl}</div></section>")


def _scroll_region(content: str) -> str:
    return ("<div class='identity-scroll-shell'>"
            f"<div class='identity-scroll'>{content}</div>"
            "<button class='scroll-cue' type='button' data-scroll-cue "
            "aria-label='Scroll down' hidden>"
            "<svg viewBox='0 0 24 24' aria-hidden='true' focusable='false'>"
            "<path d='m7 9 5 5 5-5'></path></svg></button></div>")


_STALE_MESSAGE_DAYS = 3 * 365  # matches the messages sync default (3 years)


def _recent_messages_html(parent: dict[str, Any], raw_dir: Path = RAW_DIR) -> str:
    """The person's last few raw messages, straight from the collected bundles
    the judge itself read (deep-context's scoped body-reading surface; local
    display only, nothing leaves the machine). This is what lets a human
    one-click a "maybe" like a 2021 one-way 'which number should I use' text:
    the card SHOWS the evidence instead of hiding the person. A stale callout
    flags threads older than the sync default, since a full WhatsApp backfill
    happily surfaces half-decade-old fragments."""
    person_ids = list(parent.get("person_ids") or []) or \
        list((parent.get("worth_row") or {}).get("person_ids") or [])
    messages: list[dict[str, Any]] = []
    for pid in person_ids:
        bundle_path = raw_dir / f"{pid}.json"
        try:
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for msg in bundle.get("messages") or []:
            if isinstance(msg, dict) and str(msg.get("text") or "").strip():
                messages.append(msg)
    if not messages:
        return ""
    messages.sort(key=lambda m: str(m.get("at") or ""), reverse=True)
    newest = str(messages[0].get("at") or "")
    stale = ""
    try:
        newest_dt = datetime.fromisoformat(newest.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - newest_dt).days
        if age_days > _STALE_MESSAGE_DAYS:
            # One decimal, never floored: "Aug 2022 — 3 years ago" next to a
            # 3-year window read as a contradiction when it was really 3.9.
            stale = (f"<p class='worth-messages-stale'>Last message "
                     f"{newest_dt.strftime('%b %Y')} — {age_days / 365.25:.1f} years ago, "
                     "older than your 3-year sync window</p>")
    except ValueError:
        pass
    rows = []
    for msg in messages[:3]:
        who = "you" if str(msg.get("direction") or "") == "from_me" else "them"
        when = str(msg.get("at") or "")[:10]
        text = str(msg.get("text") or "").strip()
        text = text[:200] + ("…" if len(text) > 200 else "")
        rows.append(f"<li><span class='worth-message-meta'>{esc(msg.get('channel') or 'message')}"
                    f" · {esc(who)} · {esc(when)}</span> {esc(text)}</li>")
    return ("<div class='worth-messages'>"
            f"{stale}<ul>{''.join(rows)}</ul></div>")


def render_worth_card(parent: dict[str, Any], parents_dir: Path, dossier_dir: Path,
                      profile_cache_dir: Path = PROFILE_CACHE_DIR) -> str:
    candidate = _primary_candidate(parent)
    # Cache-only: prefer the prefetch stage's profile summary in the Summary row
    # (display-only; never a provider call). Worth cards render identity=False, so
    # this is the ONLY thing that can put a Summary row on them.
    candidate["simple_summary"] = _cached_simple_summary(candidate, profile_cache_dir)
    key = _worth_key(parent)
    name = str(parent.get("name") or candidate.get("full_name") or "This person")
    slug = parent.get("dossier_slug") or parent.get("slug")
    identifiers = dossier_identifiers(parents_dir, dossier_dir, slug)
    sparse_context = (
        not candidate.get("simple_summary")
        and not relationship_summary(parents_dir, dossier_dir, slug)
        and not timeline_entries(parents_dir, dossier_dir, slug)
    )
    scroll_content = f"""
        <div class='person-top'>
          {_avatar(parent, candidate)}
          <div class='person-copy'>
            <h2>{esc(name)}</h2>
          </div>
        </div>
        {_details(
            parent,
            candidate,
            identity=False,
            identifiers=identifiers,
            sparse_context=sparse_context,
        )}
        {_recent_messages_html(parent)}"""
    return f"""
    <article class='decision-card identity-card worth-card' data-card>
      {_scroll_region(scroll_content)}
      <div class='identity-decision'>
        <div class='binary-actions'>
          <button class='button button-outline' data-worth='no' data-pub='{esc(key)}' data-parent='{esc(slug)}'>No</button>
          <button class='button button-primary' data-worth='yes' data-pub='{esc(key)}' data-parent='{esc(slug)}'>Yes</button>
        </div>
      </div>
    </article>"""


def render_candidate(idx: int, total: int, cand: dict[str, Any], **_: Any) -> str:
    """Compatibility renderer used by focused model tests; the live UI renders a parent."""
    decision = str((cand.get("worth") or {}).get("decision") or "maybe")
    key = str(cand.get("worth_key") or cand.get("pub") or "")
    return (f"<div class='candidate-compat' data-pub='{esc(cand.get('pub'))}'>"
            f"<span data-model-worth='{esc(decision)}'></span>"
            f"<button data-worth='no' data-pub='{esc(key)}'>No</button>"
            f"<button data-worth='yes' data-pub='{esc(key)}'>Yes</button></div>")


def _cached_simple_summary(candidate: dict[str, Any], profile_cache_dir: Path) -> str:
    """The prefetch stage's persisted ``simple_summary`` for this card's profile,
    read from the local cache record (display-only; never a provider call).

    Synthetic rows have no real LinkedIn cache entry, so they never carry one."""
    if candidate.get("synthetic"):
        return ""
    pub = str(candidate.get("profile_pub") or candidate.get("pub") or "").strip().lower()
    url = str(candidate.get("url") or "")
    pub = pub or extract_public_identifier(url).lower()
    if not pub or pub.startswith("candidate:"):
        return ""
    cache_path = profile_cache_dir / f"{pub}.json"
    if not cache_path.exists():
        return ""
    try:
        record = json.loads(cache_path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return ""
    return str(record.get("simple_summary") or "").strip()


def _hydrate_card_profile(candidate: dict[str, Any], profile_cache_dir: Path) -> bool:
    """Cache-ONLY render-time hydration for a card whose verdict snapshot has no
    profile facts (e.g. the cache was empty at reconcile time and the offline
    prefetch stage has since filled it). Local file read, never a provider call.
    Also lifts the prefetch stage's ``simple_summary`` onto the candidate so the
    Summary row can prefer it. Returns True when the candidate still has no
    profile facts afterwards."""
    candidate["simple_summary"] = _cached_simple_summary(candidate, profile_cache_dir)
    if candidate.get("synthetic"):
        return False
    if (candidate.get("experiences") or candidate.get("education")
            or candidate.get("headline")):
        return False
    pub = str(candidate.get("profile_pub") or candidate.get("pub") or "").strip().lower()
    url = str(candidate.get("url") or "")
    if not pub and not url:
        return False
    view = linkedin_view({"public_identifier": pub or extract_public_identifier(url).lower(),
                          "linkedin_url": url}, profile_cache_dir)
    if view.get("has_profile"):
        for field in ("full_name", "headline", "profile_pic_url", "experiences",
                      "education", "location"):
            if view.get(field):
                candidate[field] = view[field]
        candidate["has_profile"] = True
        return False
    return True  # attached link, nothing cached -> surface the prefetch note


def _skip_link(pub: Any, parent_slug: Any) -> str:
    """The inline "Skip" affordance folded into the card's question line.

    A real ``<button>`` (keyboard-activatable, proper hit area, wired by the SAME
    ``data-decide`` click delegation as before) styled as a subtle secondary link.
    Behavior is unchanged from the old standalone Skip button: detach the card,
    keyed on the parent's pub/slug."""
    return (f"<button type='button' class='skip-link' data-decide='detach' "
            f"data-toast='Skipped' data-pub='{esc(pub)}' data-parent='{esc(parent_slug)}'>"
            "Skip</button>")


def render_linkedin_card(parent: dict[str, Any],
                         candidates: dict[str, Any] | list[dict[str, Any]],
                         parents_dir: Path, dossier_dir: Path,
                         profile_cache_dir: Path = PROFILE_CACHE_DIR) -> str:
    """One review card for a parent's pending LinkedIn candidate(s).

    A single candidate (the overwhelming common case — every normal person) renders
    EXACTLY as before via ``_render_single_linkedin_card``. A merged parent with more
    than one pending candidate — e.g. two synthetic profiles for the same person after
    a later cluster_merge — renders ONE card ("show the parent, not the children"): the
    person once, then the candidate profiles as a selectable option list. Picking one
    resolves the whole parent (the /decide endpoint keeps the pick and withdraws the
    siblings)."""
    cand_list = [candidates] if isinstance(candidates, dict) else list(candidates)
    if len(cand_list) <= 1:
        candidate = cand_list[0] if cand_list else {}
        return _render_single_linkedin_card(
            parent, candidate, parents_dir, dossier_dir, profile_cache_dir)
    return _render_multi_linkedin_card(
        parent, cand_list, parents_dir, dossier_dir, profile_cache_dir)


def _render_single_linkedin_card(parent: dict[str, Any], candidate: dict[str, Any],
                                 parents_dir: Path, dossier_dir: Path,
                                 profile_cache_dir: Path = PROFILE_CACHE_DIR) -> str:
    name = str(parent.get("name") or candidate.get("full_name") or "this person")
    synthetic = bool(candidate.get("synthetic"))
    cache_miss = _hydrate_card_profile(candidate, profile_cache_dir)
    profile_name = str(candidate.get("full_name") or name)
    # The header shows the name, avatar, and — for a REAL profile — the genuine
    # LinkedIn headline + View-LinkedIn link. For a researched/synthetic row the
    # "headline" is an LLM relationship blurb ("Also known as… My relationship…"),
    # which is redundant with the Summary/Relationship/Timeline sections below, so
    # it (and the "Researched profile" / "No LinkedIn found" labels) are dropped.
    # Both a real and a synthetic row present the SAME decision UI ("Is this the
    # right profile? Or Skip?" + [No] [Use this profile] + a hidden fix form behind
    # No). The "Skip" is an inline secondary link folded into the question line (same
    # detach behavior as the old standalone button). A synthetic row simply has no
    # genuine LinkedIn header (no View-LinkedIn link, no headline); the SEMANTIC
    # difference lives only in the /decide endpoint, which routes a keep on a
    # ``synth-`` pub through the synthetic approve gate.
    question = f"Is this the right profile? Or {_skip_link(candidate.get('pub'), parent.get('slug'))}?"
    eyebrow = ""
    if synthetic:
        link = ""
        header_headline = ""
    else:
        url = str(candidate.get("url") or "")
        link = (f"<a class='linkedin-label' href='{esc(url)}' target='_blank' rel='noreferrer'>View LinkedIn"
                "<span aria-hidden='true'>↗</span></a>") if url else ""
        header_headline = str(candidate.get("headline") or "")
    fix_form = f"""<form class='linkedin-fix-form' data-fix-form
          data-pub='{esc(candidate.get('pub'))}' data-parent='{esc(parent.get('slug'))}'>
        <label class='sr-only' for='fix-{esc(candidate.get('pub'))}'>LinkedIn URL</label>
        <div><input id='fix-{esc(candidate.get('pub'))}' name='new_url' inputmode='url'
          autocomplete='url' placeholder='linkedin.com/in/…' required>
        <button class='button button-outline' type='submit'>Use this</button></div>
      </form>"""
    # Attached LinkedIn with nothing in the local profile cache: neutral copy
    # only — no operator plumbing in the card. The UI never fetches; the skill's
    # profile-prefetch stage fills the cache and logs every miss in its manifest.
    placeholder = ("<p class='profile-note'>Not enough profile information "
                   "available.</p>" if cache_miss else "")
    identifiers = dossier_identifiers(
        parents_dir, dossier_dir, parent.get("dossier_slug") or parent.get("slug"))
    scroll_content = f"""
        {f"<div class='identity-eyebrow'>{esc(eyebrow)}</div>" if eyebrow else ""}
        <div class='profile-card'>
          {_avatar(parent, candidate)}
          <div class='profile-copy'>
            <h2>{esc(profile_name)}</h2>
            {link}
            {f"<p>{esc(header_headline)}</p>" if header_headline else ""}
            {f"<span>{esc(candidate.get('location'))}</span>" if candidate.get('location') else ""}
          </div>
        </div>
        {_details(parent, candidate, identity=True, profile_rows=profile_fact_rows(candidate),
                  identifiers=identifiers, profile_placeholder=placeholder)}"""
    return f"""
    <article class='decision-card identity-card' data-card data-parent='{esc(parent.get('slug'))}'>
      {_scroll_region(scroll_content)}
      <div class='identity-decision'>
        <div class='question'>{question}</div>
        <div class='binary-actions'>
          <button class='button button-outline' data-open-fix aria-expanded='false'
                  aria-controls='fix-section-{esc(candidate.get('pub'))}'>No</button>
          <button class='button button-primary' data-decide='keep'
                  data-pub='{esc(candidate.get('pub'))}'
                  data-parent='{esc(parent.get('slug'))}'>Use this profile</button>
        </div>
        <div class='alternate' id='fix-section-{esc(candidate.get('pub'))}' hidden>
          {fix_form}
        </div>
      </div>
    </article>"""


def _linkedin_option(parent: dict[str, Any], candidate: dict[str, Any],
                     profile_cache_dir: Path) -> str:
    """One selectable profile option inside a multi-candidate parent card.

    The card header already names the person once — options never repeat the
    name, avatar, headline, or location. Each option is just a facts list: a
    "LinkedIn" row (an ↗ icon link for a real profile, "N/A" for a synthetic
    no-LinkedIn row) followed by Summary/Work/Education, plus its own
    "Use this profile" action keyed on that option's pub. Picking it resolves
    the whole parent."""
    _hydrate_card_profile(candidate, profile_cache_dir)
    pub = str(candidate.get("pub") or "")
    synthetic = bool(candidate.get("synthetic"))
    url = "" if synthetic else str(candidate.get("url") or "")
    link = (f"<a class='linkedin-label' href='{esc(url)}' target='_blank' rel='noreferrer' "
            "aria-label='View LinkedIn profile'><span aria-hidden='true'>↗</span></a>"
            if url else "<span class='linkedin-label-na'>N/A</span>")
    summary = (str(candidate.get("simple_summary") or "").strip()
               or _display_reason(str(candidate.get("reason") or "")))
    rows: list[str] = [f"<div><dt>LinkedIn</dt><dd>{link}</dd></div>"]
    if summary:
        rows.append(f"<div><dt>Summary</dt><dd>{esc(summary)}</dd></div>")
    body = f"<dl>{''.join(rows + profile_fact_rows(candidate))}</dl>"
    return f"""
      <li class='linkedin-option' data-linkedin-option>
        {body}
        <button class='button button-primary' data-decide='keep'
                data-pub='{esc(pub)}' data-parent='{esc(parent.get('slug'))}'>Use this profile</button>
      </li>"""


def _render_multi_linkedin_card(parent: dict[str, Any], candidates: list[dict[str, Any]],
                                parents_dir: Path, dossier_dir: Path,
                                profile_cache_dir: Path = PROFILE_CACHE_DIR) -> str:
    """ONE card for a merged parent offering N candidate profiles as options.

    The person is shown ONCE (name/avatar/merged Summary/Relationship/Timeline), then the
    candidate profiles as a selectable option list. A shared "None of these" opens the
    add-LinkedIn fix form and "Skip" detaches; either is keyed on the parent's primary
    candidate so the existing single-pub /decide semantics still apply. Picking any one
    option resolves the whole parent (siblings are withdrawn server-side)."""
    name = str(parent.get("name") or "this person")
    primary = candidates[0]
    identifiers = dossier_identifiers(
        parents_dir, dossier_dir, parent.get("dossier_slug") or parent.get("slug"))
    options = "".join(
        _linkedin_option(parent, cand, profile_cache_dir) for cand in candidates)
    # The person header + merged context appears once; the per-option profiles follow.
    scroll_content = f"""
        <div class='profile-card'>
          {_avatar(parent, primary)}
          <div class='profile-copy'><h2>{esc(name)}</h2></div>
        </div>
        {_details(parent, primary, identity=True, identifiers=identifiers)}
        <div class='linkedin-options-intro'>We found more than one possible profile — pick the right one.</div>
        <ul class='linkedin-options'>{options}</ul>"""
    # "None of these" / the inline Skip act on the parent via its primary candidate's
    # pub, reusing the existing fix-form + detach paths unchanged. Skip is folded into
    # the question line (secondary link), not a standalone button.
    fix_form = f"""<form class='linkedin-fix-form' data-fix-form
          data-pub='{esc(primary.get('pub'))}' data-parent='{esc(parent.get('slug'))}'>
        <label class='sr-only' for='fix-{esc(primary.get('pub'))}'>LinkedIn URL</label>
        <div><input id='fix-{esc(primary.get('pub'))}' name='new_url' inputmode='url'
          autocomplete='url' placeholder='linkedin.com/in/…' required>
        <button class='button button-outline' type='submit'>Use this</button></div>
      </form>"""
    question = f"Is this the right profile? Or {_skip_link(primary.get('pub'), parent.get('slug'))}?"
    return f"""
    <article class='decision-card identity-card identity-card-multi' data-card
             data-parent='{esc(parent.get('slug'))}' data-multi-option>
      {_scroll_region(scroll_content)}
      <div class='identity-decision'>
        <div class='question'>{question}</div>
        <div class='binary-actions'>
          <button class='button button-outline' data-open-fix aria-expanded='false'
                  aria-controls='fix-section-{esc(primary.get('pub'))}'>None of these</button>
        </div>
        <div class='alternate' id='fix-section-{esc(primary.get('pub'))}' hidden>
          {fix_form}
        </div>
      </div>
    </article>"""


def render_parent(idx: int, parent: dict[str, Any], expanded: bool = False) -> str:
    """Compact compatibility view; live pages use the staged card renderers."""
    candidate = _primary_candidate(parent)
    return (f"<article class='person-row' data-slug='{esc(parent.get('slug'))}'>"
            f"{_avatar(parent, candidate, small=True)}<strong>{esc(parent.get('name'))}</strong>"
            f"<span>{esc(parent_status(parent))}</span></article>")


def _rejection_reason(parent: dict[str, Any]) -> str:
    worth = parent.get("worth") or {}
    machine = parent.get("machine_worth") or {}
    if worth.get("source") == "user":
        return "You said no"
    if any(str(candidate.get("action") or "").strip().lower() == "exclude"
           and str(candidate.get("approved") or "").strip().lower() in APPLIED_APPROVED
           for candidate in parent.get("candidates") or []):
        return "Excluded"
    reason = str(machine.get("reason") or "").strip()
    if not reason or reason.lower() == "not yet judged":
        reason = "Not worth adding"
    return reason if len(reason) <= 140 else reason[:137].rsplit(" ", 1)[0] + "…"


def _decision_scope(parents: list[dict[str, Any]], decision: str) -> list[dict[str, Any]]:
    """The full, name-sorted row set for one decision table (yes = Added, no = Rejected)."""
    if decision not in {"yes", "no"}:
        raise ValueError(f"unknown decision table: {decision}")
    rows_in_scope = [
        parent for parent in parents
        if in_worth_view(parent)
        and ((decision == "yes" and _effective_yes(parent))
             or (decision == "no" and _effective_no_row(parent)))
    ]
    rows_in_scope.sort(key=lambda item: str(item.get("name") or "").lower())
    return rows_in_scope


def _decision_row_html(parent: dict[str, Any], decision: str,
                       parents_dir: Path | None, dossier_dir: Path | None) -> str:
    """One expandable decision row (shared by the initial table and /api/decision-rows)."""
    candidate = _primary_candidate(parent)
    flip = "no" if decision == "yes" else "yes"
    flip_label = "No" if decision == "yes" else "Yes"
    reason = (_rejection_reason(parent) if decision == "no" else
              str((parent.get("machine_worth") or {}).get("reason") or "Worth adding"))
    dossier_slug = parent.get("dossier_slug") or parent.get("slug")
    identifiers = (dossier_identifiers(parents_dir, dossier_dir, dossier_slug)
                   if parents_dir is not None and dossier_dir is not None else [])
    sparse_context = (
        parents_dir is not None
        and dossier_dir is not None
        and not candidate.get("simple_summary")
        and not relationship_summary(parents_dir, dossier_dir, dossier_slug)
        and not timeline_entries(parents_dir, dossier_dir, dossier_slug)
    )
    contacts = _merge_contacts(
        [*(candidate.get("match_emails") or []), *(candidate.get("match_phones") or [])],
        identifiers)
    why_label = "Why no" if decision == "no" else "Why yes"
    fact_rows = []
    if contacts:
        fact_rows.append(f"<div><dt>Contact</dt><dd>{esc(' · '.join(contacts))}</dd></div>")
    fact_rows.append(f"<div><dt>{why_label}</dt><dd>{esc(reason)}</dd></div>")
    dossier_preview = (
        "<p class='context-empty'>Not enough information.</p>"
        if sparse_context
        else "<dl class='row-facts dossier-text' aria-busy='true'></dl>"
    )
    # Left-edge chevron = the expand/collapse affordance; the decision button
    # stays on the far right of the summary row. data-name is the live-search
    # filter's match target (lowercased display name, matched client-side).
    return f"""
        <details class='decision-row' data-card data-slug='{esc(dossier_slug)}' data-name='{esc(str(parent.get('name') or '').lower())}'>
          <summary class='decision-row-summary'>
            <span class='decision-row-caret' aria-hidden='true'></span>
            {_avatar(parent, candidate, small=True)}
            <div class='decision-row-main'><strong>{esc(parent.get('name'))}</strong><span>{esc(reason)}</span></div>
            <div class='decision-row-actions'>
              <button class='button button-ghost' data-worth='{flip}' data-pub='{esc(_worth_key(parent))}' data-parent='{esc(dossier_slug)}' aria-label='Mark {esc(parent.get('name'))} {flip_label}'>{flip_label}</button>
            </div>
          </summary>
          <div class='decision-row-detail'>
            <dl class='row-facts'>{''.join(fact_rows)}</dl>
            {dossier_preview}
          </div>
        </details>"""


def decision_rows_payload(parents: list[dict[str, Any]], decision: str, *,
                          offset: int = 0, limit: int = DECISION_CHUNK_SIZE,
                          parents_dir: Path | None = None,
                          dossier_dir: Path | None = None) -> dict[str, Any]:
    """One chunk of decision rows for the infinite-scroll list (/api/decision-rows).

    `total` is always the FULL scope size so the client can keep counts and its
    end-of-list state authoritative regardless of how much has been fetched."""
    rows_in_scope = _decision_scope(parents, decision)
    offset = max(0, offset)
    limit = max(1, limit)
    return {
        "view": decision,
        "total": len(rows_in_scope),
        "offset": offset,
        "rows": [_decision_row_html(parent, decision, parents_dir, dossier_dir)
                 for parent in rows_in_scope[offset:offset + limit]],
    }


def render_decision_table(parents: list[dict[str, Any]], decision: str, *,
                          chunk_size: int = DECISION_CHUNK_SIZE,
                          parents_dir: Path | None = None,
                          dossier_dir: Path | None = None) -> str:
    """The decision list shell plus its first chunk of rows.

    No pagination: the client's infinite scroll fetches further chunks from
    /api/decision-rows and virtualizes the list (data-total carries the full
    scope size so tab badges / end detection never depend on loaded rows)."""
    rows_in_scope = _decision_scope(parents, decision)
    if not rows_in_scope:
        return ("<div class='empty-state decision-empty'><div class='empty-mark'>0</div>"
                f"<h2>No {esc(decision)} decisions</h2></div>")
    chunk_size = max(1, chunk_size)
    rows = [_decision_row_html(parent, decision, parents_dir, dossier_dir)
            for parent in rows_in_scope[:chunk_size]]
    return ("<div class='decision-page'>"
            + worth_search_html(decision)
            + f"<section class='decision-list' data-decision-list data-view='{esc(decision)}' "
            f"data-total='{len(rows_in_scope)}' data-chunk='{chunk_size}'>"
            + "".join(rows) + "</section></div>")


def worth_pending_entries(parents: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Name + worth key of every still-pending Review person, in queue order —
    the typeahead's dataset, embedded in the rendered page as a JSON island and
    pruned client-side as decisions settle (no separate names endpoint)."""
    queue = [parent for parent in parents if needs_worth_review(parent)]
    queue.sort(key=lambda parent: str(parent.get("name") or "").lower())
    return [{"key": str(_worth_key(parent) or ""), "name": str(parent.get("name") or "")}
            for parent in queue]


def worth_search_html(view: str, pending: list[dict[str, str]] | None = None) -> str:
    """The ONE live-search input shared by all worth views (filters as you type;
    never a Search button). The Yes/No tables hide non-matching rows client-side
    with an "N of M" count; the Review card view (``pending`` given) gets a
    typeahead dropdown over the embedded pending queue that jumps straight to a
    picked person's card via /api/worth-card?pick=."""
    extras = ""
    if pending is not None:
        payload = json.dumps(pending, ensure_ascii=False).replace("<", "\\u003c")
        extras = (f"<script type='application/json' data-worth-pending>{payload}</script>"
                  "<ul class='worth-search-list' data-search-list role='listbox' hidden></ul>")
    return (f"<div class='worth-search' data-worth-search data-search-view='{esc(view)}'>"
            "<input class='worth-search-input' type='search' placeholder='Search people…' "
            "aria-label='Search people by name' autocomplete='off' spellcheck='false'>"
            "<span class='worth-search-count' data-search-count hidden></span>"
            f"{extras}</div>")


def render_decision_tabs(progress: dict[str, int], active: str, *, preview: bool = False) -> str:
    tabs = (("review", "Review", progress["worth_pending"]),
            ("yes", "Yes", progress["worth_yes"]),
            ("no", "No", progress["worth_no"]))
    links = []
    for key, label, count in tabs:
        current = " aria-current='page'" if key == active else ""
        preview_query = "&amp;preview=1" if preview else ""
        links.append(
            f"<a class='decision-tab{' active' if key == active else ''}' data-tab='{key}' "
            f"href='/?stage=worth&amp;view={key}{preview_query}'{current}>"
            f"{label}<span>{count}</span></a>")
    return "<nav class='decision-tabs' aria-label='People decisions'>" + "".join(links) + "</nav>"


def _phase_view(params: dict[str, list[str]], progress: dict[str, int], manifest_path: Path) -> str:
    requested = str((params.get("stage") or [""])[0]).strip().lower()
    return requested if requested in {"worth", "enrich", "linkedin", "done"} else "worth"


def render_enrichment(enrichment: dict[str, Any], progress: dict[str, int]) -> str:
    """Render one derived enrichment state (see derive_enrichment_state) as HTML.
    Purely presentational — the state rules live in the derive function alone."""
    state = str(enrichment.get("state") or STATE_FREE_PENDING)
    counts = enrichment.get("counts") if isinstance(enrichment.get("counts"), dict) else {}
    total = max(0, int(counts.get("total") or progress["lookup_ready"] or 0))
    completed = min(total, max(0, int(counts.get("completed") or 0)))
    percent = round((completed / total) * 100) if total else 0
    progress_bar = f"""
      <div class='enrich-progress' role='progressbar' aria-label='Contact enrichment progress'
           aria-valuemin='0' aria-valuemax='{total}' aria-valuenow='{completed}'>
        <div class='enrich-progress-fill' style='width:{percent}%'></div>
      </div>"""
    agent_handoff_bar = """
      <div class='enrich-progress is-indeterminate' role='progressbar'
           aria-label='Preparing enrichment'>
        <div class='enrich-progress-fill'></div>
      </div>"""
    if progress["worth_pending"]:
        return ("<div class='empty-state enrich-state'><div class='empty-mark'>1</div>"
                "<h2>Review in progress</h2>"
                f"<p>{progress['worth_pending']} decisions left · {progress['lookup_ready']} currently yes</p>"
                f"{progress_bar}</div>")
    if state == STATE_RUNNING:
        if enrichment.get("phase") == "judging_retargets":
            # Commit-scoped heartbeat from the judging pass: honest counts, not a
            # static screen (see reconcile_deep_research's manifest heartbeat).
            done = max(0, int(enrichment.get("done") or 0))
            judging = max(done, int(enrichment.get("total") or 0))
            return ("<div class='empty-state enrich-state'><div class='progress-spinner' aria-hidden='true'></div>"
                    "<h2>Judging found profiles</h2>"
                    f"<p>{done} of {judging} checked</p>{agent_handoff_bar}</div>")
        return ("<div class='empty-state enrich-state'><div class='progress-spinner' aria-hidden='true'></div>"
                "<h2>Enriching contacts</h2>"
                f"<p>{completed} of {total} complete</p>{progress_bar}</div>")
    if state == STATE_NEEDS_APPROVAL:
        if enrichment.get("approval_current"):
            new_count = max(0, int(enrichment.get("would_submit") or 0))
            return ("<div class='empty-state enrich-state'>"
                    "<h2>Preparing enrichment</h2>"
                    f"<p>Approval saved · starting {new_count} new lookup"
                    f"{'s' if new_count != 1 else ''}</p>{agent_handoff_bar}</div>")
        if not enrichment.get("approvable"):
            # Net-new exists but the receipt is stale; the free job is refreshing
            # the estimate ($0) — the Approve button binds to the fresh receipt.
            return ("<div class='empty-state enrich-state'>"
                    "<h2>Preparing enrichment</h2>"
                    f"<p>Estimating {enrichment.get('net_new') or 0} new lookups</p>"
                    f"{agent_handoff_bar}</div>")
        estimate = float(enrichment.get("estimated_usd") or 0)
        new_count = max(0, int(enrichment.get("would_submit") or 0))
        reused = max(0, int(enrichment.get("reused_completed") or 0))
        details = f"{new_count} new · {reused} reused · up to ${estimate:.2f}"
        return ("<div class='empty-state enrich-state'><div class='empty-mark'>2</div>"
                "<h2>Ready to enrich</h2>"
                f"<p>{details}</p>{progress_bar}"
                f"<button class='button button-primary' data-approve-enrichment>Approve ${estimate:.2f}</button></div>")
    if state == STATE_DONE:
        return ("<div class='empty-state enrich-state'><div class='empty-mark'>✓</div>"
                "<h2>Contacts enriched</h2>"
                f"<p>{completed} profiles ready</p>{progress_bar}"
                "<button class='button button-primary' data-complete='enrich'>Continue</button></div>")
    # free_pending: the render already started-or-joined the free job; show work.
    if enrichment.get("status") in {"failed", "completed_with_errors"}:
        failed = max(0, int(counts.get("failed") or 0))
        return ("<div class='empty-state enrich-state'><div class='empty-mark'>!</div>"
                "<h2>Enrichment paused</h2>"
                f"<p>{failed} failed · {completed} complete · reload to retry</p>"
                f"{progress_bar}</div>")
    return ("<div class='empty-state enrich-state'>"
            "<h2>Preparing enrichment</h2>"
            f"<p>Preparing {progress['lookup_ready']} approved "
            f"contact{'s' if progress['lookup_ready'] != 1 else ''}</p>"
            f"{agent_handoff_bar}</div>")


def _step(number: int, label: str, active: bool, complete: bool, count: int = 0,
          href: str = "") -> str:
    state = " active" if active else (" complete" if complete else "")
    marker = "✓" if complete else str(number)
    count_html = f"<small>{count} left</small>" if count and not complete else ""
    current = " aria-current='step'" if active else ""
    content = f"<span>{marker}</span><div>{esc(label)}{count_html}</div>"
    if href:
        return f"<a class='step{state}' href='{esc(href)}'{current}>{content}</a>"
    return f"<div class='step{state}'{current}>{content}</div>"


def _carousel_nav() -> str:
    """Debug-only Prev/Next arrows flanking the queue card (browse, no decisions)."""
    return ("<button class='carousel-nav carousel-prev' type='button' "
            "data-carousel='prev' aria-label='Previous card'>&#8249;</button>"
            "<button class='carousel-nav carousel-next' type='button' "
            "data-carousel='next' aria-label='Next card'>&#8250;</button>")


def worth_review_body(parents: list[dict[str, Any]], progress: dict[str, int],
                      parents_dir: Path, dossier_dir: Path, *,
                      debug: bool = False, index: int = 0,
                      profile_cache_dir: Path = PROFILE_CACHE_DIR,
                      exclude: frozenset[str] | None = None) -> str:
    """The Review tab's current item: the next queue card, or the stage-complete
    state. Shared by page_html and /api/worth-card so a decision click can swap
    in the next card client-side without a full page reload. ``debug`` adds the
    browse-only carousel; ``index`` picks a queue position (default unchanged).
    ``exclude`` skips worth keys whose decision POST is still in flight, so the
    client can prefetch the FOLLOWING card without waiting for the save."""
    queue = [parent for parent in parents if needs_worth_review(parent)]
    if exclude:
        queue = [parent for parent in queue
                 if str(_worth_key(parent) or "").strip().lower() not in exclude]
    if queue:
        queue.sort(key=lambda parent: str(parent.get("name") or "").lower())
        index = index % len(queue)
        card = render_worth_card(queue[index], parents_dir, dossier_dir, profile_cache_dir)
        if debug:
            return (f"<div class='carousel-shell' data-queue-index='{index}' "
                    f"data-queue-total='{len(queue)}'>{_carousel_nav()}{card}</div>")
        return card
    return ("<div class='empty-state phase-finish'><div class='empty-mark'>✓</div>"
            "<h2>Decisions ready</h2>"
            f"<p>{progress['lookup_ready']} people will be enriched</p>"
            "<button class='button button-primary' data-complete='worth'>Continue</button></div>")


def _enrichment_note(enrichment: dict[str, Any] | None) -> str:
    """Passive status line for the LinkedIn stage while enrichment is incomplete.

    Purely informational — it never obscures or disables the review cards, so
    the user can keep reviewing (or debugging) even if enrichment broke."""
    status = str((enrichment or {}).get("status") or "not_started")
    copy = {
        "failed": "Enrichment failed — you can still review what's here.",
        "running": "Enrichment is still running — more people may appear here.",
    }.get(status, "Enrichment hasn't finished — more people may appear here.")
    return f"<p class='enrichment-note' role='status'>{esc(copy)}</p>"


def linkedin_review_queue(
    parents: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Stable queue of ONE entry per parent that still needs an identity decision,
    carrying ALL of that parent's pending candidates.

    A merged parent with N pending candidates (e.g. two synthetic profiles for the
    same person) is ONE card offering N options — the queue and the header/tab counts
    are per-parent, never per-candidate ("show the parent, not the children")."""
    queue: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    ordered = sorted(parents, key=lambda parent: str(parent.get("name") or "").lower())
    for parent in ordered:
        pending = pending_linkedin_candidates(parent)
        if pending:
            queue.append((parent, pending))
    return queue


# The single human -> agent handoff at the end of review: the app is done,
# the agent finishes setup (realize + index) from its blocking wait — or, if
# that session ended, the user pastes the shown phrase
# ("Review complete proceed with enrichment") into Codex.
GO_BACK_HTML = (
    "<p class='handoff-note'>Review complete — go back to Codex.</p>"
    "<div class='handoff-copy'>"
    "<code>Review complete proceed with enrichment</code>"
    "<button class='button button-outline' type='button' data-copy-continue "
    "data-phrase='Review complete proceed with enrichment' "
    "data-toast='Copied'>Copy</button></div>")


def linkedin_finished_body(progress: dict[str, int], *, linkedin_complete: bool) -> str:
    tail = (GO_BACK_HTML if linkedin_complete else
            "<button class='button button-primary' data-complete='linkedin'>Finish</button>")
    return ("<div class='empty-state phase-finish'><div class='empty-mark'>✓</div>"
            "<h2>LinkedIn profiles checked</h2>"
            f"<p>{progress['linkedin_done']} decisions saved</p>"
            f"{tail}</div>")


def linkedin_card_body(parents: list[dict[str, Any]], progress: dict[str, int], *,
                       linkedin_complete: bool, parents_dir: Path, dossier_dir: Path,
                       profile_cache_dir: Path = PROFILE_CACHE_DIR,
                       exclude: frozenset[str] | None = None) -> str:
    """The LinkedIn queue's current item: the next pending parent's card, or the
    stage-finished state — the linkedin twin of ``worth_review_body``. Shared by
    page_html and /api/linkedin-card so a decision click swaps in the next card
    client-side. ``exclude`` skips PARENT SLUGS whose decision POST is still in
    flight (the linkedin queue is parent-keyed), so the client can prefetch the
    FOLLOWING card without waiting for the save."""
    queue = linkedin_review_queue(parents)
    if exclude:
        queue = [(parent, pending) for parent, pending in queue
                 if str(parent.get("slug") or "").strip().lower() not in exclude]
    if queue:
        parent, pending = queue[0]
        return render_linkedin_card(parent, pending, parents_dir, dossier_dir,
                                    profile_cache_dir)
    return linkedin_finished_body(progress, linkedin_complete=linkedin_complete)


def linkedin_review_body(parents: list[dict[str, Any]], progress: dict[str, int], *,
                         enrichment_complete: bool, linkedin_complete: bool,
                         parents_dir: Path, dossier_dir: Path,
                         enrichment: dict[str, Any] | None = None,
                         profile_cache_dir: Path = PROFILE_CACHE_DIR,
                         debug: bool = False, index: int = 0) -> str:
    """Render the LinkedIn stage: one pending parent card inside the swap panel
    (the worth pattern), or the debug carousel."""
    note = "" if enrichment_complete else _enrichment_note(enrichment)
    queue = linkedin_review_queue(parents)

    if debug and queue:
        index %= len(queue)
        parent, pending = queue[index]
        body = render_linkedin_card(
            parent, pending, parents_dir, dossier_dir, profile_cache_dir)
        return (
            f"<div class='linkedin-stage' data-queue-index='{index}' "
            f"data-queue-total='{len(queue)}'>{note}{_carousel_nav()}{body}</div>"
        )

    card = linkedin_card_body(
        parents, progress, linkedin_complete=linkedin_complete,
        parents_dir=parents_dir, dossier_dir=dossier_dir,
        profile_cache_dir=profile_cache_dir)
    body = f"<div class='linkedin-panel' data-linkedin-panel>{card}</div>"
    return f"<div class='linkedin-stage'>{note}{body}</div>"


def page_html(parents: list[dict[str, Any]], params: dict[str, list[str]],
              review_path: Path, *, parents_dir: Path = PARENTS_DIR,
              dossier_dir: Path = DOSSIER_DIR,
              manifest_path: Path = REVIEW_MANIFEST,
              enrichment_manifest_path: Path = ENRICH_MANIFEST,
              profile_cache_dir: Path = PROFILE_CACHE_DIR,
              verdicts_path: Path = VERDICTS_JSONL,
              facts_dir: Path = FACTS_DIR,
              enrichment_state: dict[str, Any] | None = None) -> bytes:
    progress = review_progress(parents)
    selection = worth_selection_from_parents(parents, manifest_path=manifest_path)
    enrichment = read_enrichment_manifest(enrichment_manifest_path, selection=selection)
    review_manifest = read_review_manifest(manifest_path)
    state_token = review_state_token(progress, selection, enrichment, review_manifest,
                                     job_running=_job_lock.locked())
    view = _phase_view(params, progress, manifest_path)
    preview = str((params.get("preview") or [""])[0]).strip() == "1"
    debug = str((params.get("debug") or [""])[0]).strip() == "1"
    worth_complete = phase_is_completed("worth", progress, manifest_path) or not progress["worth_total"]
    enrichment_complete = (enrichment.get("status") == STATUS_COMPLETED
                           and enrichment.get("current"))
    enrichment_continued = enrichment_handoff_completed(manifest_path)
    linkedin_complete = (phase_is_completed("linkedin", progress, manifest_path)
                         or (view == "done" and not progress["linkedin_total"]))
    external_updates = (
        view in {"enrich", "done"}
        or (view == "linkedin" and not enrichment_complete)
    )
    people_active = view == "worth"
    enrich_active = view == "enrich"
    linkedin_active = view in {"linkedin", "done"}

    if view == "worth":
        decision_view = str((params.get("view") or ["review"])[0]).strip().lower()
        decision_view = decision_view if decision_view in {"review", "yes", "no"} else "review"
        tabs = render_decision_tabs(progress, decision_view, preview=preview)
        search = ""
        if decision_view == "review":
            body = worth_review_body(parents, progress, parents_dir, dossier_dir,
                                     debug=debug, profile_cache_dir=profile_cache_dir)
            # The typeahead lives OUTSIDE the swap panel so card swaps never
            # touch it; its pending queue is embedded once at page render.
            pending = worth_pending_entries(parents)
            search = worth_search_html("review", pending) if pending else ""
        else:
            # Legacy ?page= URLs still land here; the infinite-scroll list always
            # starts from the top and streams the rest via /api/decision-rows.
            body = render_decision_table(
                parents, decision_view, parents_dir=parents_dir, dossier_dir=dossier_dir)
        content = f"<div class='worth-stage'>{tabs}{search}<div class='worth-panel'>{body}</div></div>"
    elif view == "enrich":
        # The enrich page renders the DERIVED state (never a trusted persisted
        # status); the HTTP handler pre-derives it so its render matches the
        # free-job trigger decision it just made.
        if enrichment_state is None:
            enrichment_state = derive_enrichment_state(
                selection, verdicts_path=verdicts_path, review_path=review_path,
                facts_dir=facts_dir, manifest_path=enrichment_manifest_path)
        content = render_enrichment(enrichment_state, progress)
    elif view == "linkedin":
        content = linkedin_review_body(
            parents, progress, enrichment_complete=bool(enrichment_complete),
            linkedin_complete=bool(linkedin_complete),
            parents_dir=parents_dir, dossier_dir=dossier_dir, enrichment=enrichment,
            profile_cache_dir=profile_cache_dir, debug=debug)
    else:
        content = ("<div class='empty-state done'><div class='empty-mark'>✓</div><h2>All set</h2>"
                   f"<p>{progress['linkedin_done']} identities checked · {progress['rejected']} rejected</p>"
                   f"{GO_BACK_HTML}</div>")

    stepper = (_step(1, "Review Decisions", people_active, worth_complete, progress["worth_pending"],
                     "/?stage=worth&preview=1")
               + "<i class='step-line'></i>"
               + _step(2, "Enrich Contacts", enrich_active, enrichment_complete,
                       int((enrichment.get("counts") or {}).get("pending") or 0),
                       "/?stage=enrich&preview=1")
               + "<i class='step-line'></i>"
               + _step(3, "Check LinkedIn", linkedin_active,
                       enrichment_complete and enrichment_continued and linkedin_complete,
                       progress["linkedin_pending"], "/?stage=linkedin&preview=1")
               )
    title = {"worth": "Add People", "enrich": "Enrich Contacts",
             "linkedin": "Check LinkedIn", "done": "All Set"}.get(view, "Add People")
    return f"""<!doctype html>
<html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<meta name='color-scheme' content='dark'><title>{esc(title)} · Powerpacks</title>
<link rel='stylesheet' href='/assets/reconcile-review.css'></head>
<body data-stage='{esc(view)}' data-preview='{"true" if preview else "false"}' data-external-updates='{"true" if external_updates else "false"}' data-state-token='{esc(state_token)}' data-enrichment-status='{esc("approved" if enrichment.get("approval_current") else enrichment.get("status"))}'><div class='app-shell'>
  <header class='topbar'><a class='brand' href='/?stage=worth'>POWERPACKS</a><h1 class='topbar-title'>{esc(title)}</h1><span></span></header>
  <main><nav class='stepper' aria-label='Progress'>{stepper}</nav>
    <section class='stage' aria-live='polite'>{content}</section>
  </main>
  <div class='toast' role='status' aria-live='polite'></div>
</div><script src='/assets/reconcile-review.js' defer></script></body></html>""".encode("utf-8")

# --- server -----------------------------------------------------------------

def _all_review_parents(verdicts_path: Path, review_path: Path, synthetic_path: Path,
                        facts_dir: Path, people_csv: Path,
                        parents_dir: Path = PARENTS_DIR,
                        dossier_dir: Path = DOSSIER_DIR,
                        profile_cache_dir: Path = PROFILE_CACHE_DIR,
                        index_json: Path | None = None) -> list[dict[str, Any]]:
    # index.json is the sibling of the parents dir (ROOT/index.json vs ROOT/parents);
    # deriving it from parents_dir keeps test fixtures self-contained.
    index_json = index_json if index_json is not None else parents_dir.parent / "index.json"
    parents, overrides = build_parents(verdicts_path, review_path)
    extend_and_annotate(parents, overrides, synthetic_path, facts_dir,
                        load_connection_keys(people_csv), parents_dir=parents_dir,
                        dossier_dir=dossier_dir, profile_cache_dir=profile_cache_dir,
                        index_json=index_json)
    annotate_sources(parents, load_people_sources(people_csv))
    return parents


def _manifest_for_review_path(review_path: Path) -> Path:
    try:
        if review_path.resolve() == LINKEDIN_OVERRIDES_CSV.resolve():
            return REVIEW_MANIFEST
    except (OSError, RuntimeError):
        pass
    return review_path.parent / "review" / "manifest.json"


# --- in-app pipeline jobs ----------------------------------------------------
# The review app runs the mid-flow work itself (owner decision: the app is
# self-sufficient from People review through Check LinkedIn; the agent's
# `review-status --wait` returns only for retry_enrichment/realize). One
# daemon thread at a time; the primitives keep owning their manifests, so the
# UI's existing /api/status polling renders progress with no new state store.
ENRICH_FLAGS = ["--include-candidates", "--include-plausibly-absent"]
_job_lock = threading.Lock()


def _mark_enrichment_failed(error: str) -> None:
    """Best-effort: surface a job crash in the fixed enrichment manifest so
    workflow_status turns it into retry_enrichment instead of a silent stall."""
    try:
        existing = json.loads(ENRICH_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    if existing.get("status") == "failed" and existing.get("error") == error[:500]:
        return  # already surfaced; a repeat write would churn the UI poll per retry
    existing.update({"stage": "enrich", "status": "failed", "error": error[:500],
                     "updated_at": now_iso()})
    try:
        ENRICH_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        ENRICH_MANIFEST.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except OSError:
        pass


def _run_pipeline_job(name: str, steps: Callable[[], None]) -> None:
    if not _job_lock.acquire(blocking=False):
        return  # one job at a time; the durable manifests re-trigger any rerun

    def runner() -> None:
        try:
            steps()
        # BaseException on purpose: the primitives raise SystemExit on their
        # guard paths, which `except Exception` misses — the thread then died
        # silently and the manifest stranded mid-state with no failure marker.
        except BaseException as exc:  # the manifest is the UI's/agent's error surface
            _mark_enrichment_failed(f"{name}: {type(exc).__name__}: {exc}")
        finally:
            _job_lock.release()

    threading.Thread(target=runner, name=f"pipeline-job-{name}", daemon=True).start()


def _post_enrichment_chain() -> None:
    """Free follow-ups once research is done: no-LinkedIn cards + profile cache."""
    # Local imports avoid a module cycle (these primitives import this module).
    from packs.ingestion.primitives.deep_context import assemble_synthetic_profile
    from packs.ingestion.primitives.deep_context import prefetch_profiles
    assemble_synthetic_profile.main([])
    prefetch_profiles.main(["--fetch"])


def _free_enrichment_steps() -> None:
    """The ONE free-work pass: run the enrichment continue with a $0 ceiling.
    Zero net-new does ALL the free work (reuse + fingerprint-cached retarget
    judging) and the follow-up chain; any real spend hits the primitive's budget
    gate, which stamps a current needs_approval receipt WITHOUT spending a cent
    (the Approve button owns money). No convergence loop: the chain may re-drift
    the selection, and the next enrich-page render re-derives and re-triggers."""
    from packs.ingestion.primitives.deep_context import reconcile_deep_research
    reconcile_deep_research.main([*ENRICH_FLAGS, "--approve", "--budget", "0.00"])
    enrichment = read_enrichment_manifest(selection=current_worth_selection())
    if enrichment.get("status") == STATUS_RESEARCH_COMPLETE:
        _post_enrichment_chain()


def start_free_enrichment_job() -> None:
    """Start-or-join THE single free-work job (one module-level mutex; idempotent).
    Rendering the enrich page is the only trigger — a stranded manifest state
    cannot survive a reload because every render re-derives and re-kicks this."""
    _run_pipeline_job("free-enrichment", _free_enrichment_steps)


def start_approved_enrichment_job(budget: float) -> None:
    """The Approve $X click IS the user's spend approval: run exactly that."""
    def steps() -> None:
        from packs.ingestion.primitives.deep_context import reconcile_deep_research
        reconcile_deep_research.main(
            [*ENRICH_FLAGS, "--approve", "--budget", f"{budget:.2f}"])
        _post_enrichment_chain()

    _run_pipeline_job("approved-enrichment", steps)


def make_handler(review_path: Path, verdicts_path: Path, parents_dir: Path, dossier_dir: Path,
                 confirm_threshold: float, detach_threshold: float,
                 synthetic_path: Path = SYNTHETIC_PEOPLE_CSV,
                 facts_dir: Path = FACTS_DIR, people_csv: Path = DEFAULT_PEOPLE_CSV,
                 manifest_path: Path | None = None,
                 enrichment_manifest_path: Path = ENRICH_MANIFEST,
                 profile_cache_dir: Path = PROFILE_CACHE_DIR,
                 avatar_dir: Path | None = None,
                 initial_parents: list[dict[str, Any]] | None = None,
                 agent_notifier: Callable[[], object] | None = None,
                 run_jobs: bool | None = None):
    manifest_path = manifest_path or _manifest_for_review_path(review_path)
    # In-app jobs call the primitives on their CANONICAL default paths, so they
    # only auto-enable for the canonical server (tests use temp paths -> off).
    if run_jobs is None:
        try:
            run_jobs = review_path.resolve() == LINKEDIN_OVERRIDES_CSV.resolve()
        except OSError:
            run_jobs = False
    avatar_dir = avatar_dir or manifest_path.parent / "avatars"
    mutation_lock = threading.Lock()

    def input_signature() -> tuple[tuple[str, int, int], ...]:
        """Cheap invalidation key for files that can change the review queue.

        Facts/dossiers are fixed before review. Provider work changes the durable
        review/synthetic CSVs, so those files are sufficient to notice external
        agent progress without recursively scanning thousands of artifacts.
        """
        values = []
        # ENRICH_MANIFEST included so an enrichment state change (in-app or an
        # external CLI completion) invalidates the cached model — without it
        # the enrich page served a stale phase until a manual server restart.
        for path in (review_path, verdicts_path, synthetic_path, people_csv,
                     ENRICH_MANIFEST):
            try:
                stat = path.stat()
                values.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                values.append((str(path), 0, 0))
        return tuple(values)

    def notify_agent() -> None:
        """Best-effort wake after durable UI mutations; file state stays authoritative."""
        if agent_notifier is None:
            return
        try:
            agent_notifier()
        except Exception:
            # Review decisions must never fail because an optional observer
            # hook (tests use it to count mutations) raised.
            pass

    cached_parents = (
        initial_parents if initial_parents is not None else
        _all_review_parents(
            verdicts_path, review_path, synthetic_path, facts_dir, people_csv,
            parents_dir, dossier_dir, profile_cache_dir)
    )
    cached_signature = input_signature()
    connection_keys = load_connection_keys(people_csv)

    def parents_now() -> list[dict[str, Any]]:
        """Return the in-memory SPA model, reloading only after an external write."""
        nonlocal cached_parents, cached_signature, connection_keys
        signature = input_signature()
        if signature != cached_signature:
            cached_parents = _all_review_parents(
                verdicts_path, review_path, synthetic_path, facts_dir, people_csv,
                parents_dir, dossier_dir, profile_cache_dir)
            connection_keys = load_connection_keys(people_csv)
            cached_signature = signature
        return cached_parents

    def accept_local_write() -> None:
        """The caller already updated ``cached_parents`` to mirror its durable write."""
        nonlocal cached_signature
        cached_signature = input_signature()

    def refresh_parents_from_disk() -> list[dict[str, Any]]:
        """Rebuild the model FRESH from files, discarding optimistic patches.

        Used at stage-completion boundaries: the agent's `review-status` CLI
        always rebuilds fresh, so "completed" must only ever be written when a
        fresh derivation agrees — otherwise the UI shows "waiting on the agent"
        while the agent's own read says N people are still pending (the
        off-by-N handoff split)."""
        nonlocal cached_parents, cached_signature, connection_keys
        cached_parents = _all_review_parents(
            verdicts_path, review_path, synthetic_path, facts_dir, people_csv,
            parents_dir, dossier_dir, profile_cache_dir)
        connection_keys = load_connection_keys(people_csv)
        cached_signature = input_signature()
        return cached_parents

    # Parsed review.csv rows, cached so a decision click does not re-read a
    # potentially large CSV per POST. Invalidation mirrors input_signature:
    # any external write changes the file stat and forces a reload; our own
    # writes refresh the stat via accept_rows_write (the dict itself was
    # mutated in place by apply_worth_decision, so it is already current).
    cached_rows: dict[str, dict[str, str]] | None = None
    cached_rows_sig: tuple[int, int] | None = None

    def _review_rows_sig() -> tuple[int, int]:
        try:
            stat = review_path.stat()
            return (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return (0, 0)

    def review_rows_now() -> dict[str, dict[str, str]]:
        nonlocal cached_rows, cached_rows_sig
        sig = _review_rows_sig()
        if cached_rows is None or sig != cached_rows_sig:
            cached_rows = load_override_rows(review_path)
            cached_rows_sig = sig
        return cached_rows

    def accept_rows_write() -> None:
        nonlocal cached_rows_sig
        cached_rows_sig = _review_rows_sig()

    def candidate_in_snapshot(pub: str, prefer_slug: str = "",
                              ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Resolve a candidate pub to (parent, candidate). The same pub can be
        owned by SEVERAL parents (one confirmed LinkedIn attached to two split
        parents), so when the client says which card it decided (prefer_slug),
        honor that parent — resolving globally would hit the other owner first
        and 409 every click as 'stale or mismatched person card'."""
        pub_lower = pub.strip().lower()
        hits: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for parent in cached_parents:
            for candidate in parent.get("candidates") or []:
                if str(candidate.get("pub") or "").strip().lower() == pub_lower:
                    hits.append((parent, candidate))
        if prefer_slug:
            for parent, candidate in hits:
                if str(parent.get("slug") or "") == prefer_slug:
                    return parent, candidate
        return hits[0] if hits else None

    def worth_parent_in_snapshot(key: str, parent_slug: str = "") -> dict[str, Any] | None:
        """The cached parent this decision was rendered from. The card/row
        sends its parent slug (unique), because a worth KEY is not unique:
        two split parents can share one pub with DISTINCT worth_row dicts,
        and first-hit-by-key patched the wrong twin — leaving an unkillable
        pending zombie in the live model (the disk write was always right;
        only a restart cleared it). Slug match first, key fallback."""
        slug_lower = parent_slug.strip().lower()
        if slug_lower:
            for parent in cached_parents:
                candidate_slug = str(parent.get("dossier_slug")
                                     or parent.get("slug") or "").strip().lower()
                if candidate_slug == slug_lower:
                    return parent
        key_lower = key.strip().lower()
        return next(
            (parent for parent in cached_parents
             if str(_worth_key(parent) or "").strip().lower() == key_lower),
            None,
        )

    def state_token_for(parents: list[dict[str, Any]], progress: dict[str, int]) -> str:
        selection = worth_selection_from_parents(parents, manifest_path=manifest_path)
        enrichment = read_enrichment_manifest(
            enrichment_manifest_path, selection=selection)
        return review_state_token(
            progress, selection, enrichment, read_review_manifest(manifest_path),
            job_running=_job_lock.locked())

    def invalidate_manifest(stage: str, progress: dict[str, int], *, launched: bool = False) -> None:
        write_review_manifest(stage, "awaiting_user", progress, path=manifest_path,
                              review_path=review_path, synthetic_path=synthetic_path,
                              launched=launched)

    class Handler(BaseHTTPRequestHandler):
        def send_bytes(self, body: bytes, content_type: str = "text/html; charset=utf-8",
                       status: int = 200, *, cache: str = "no-store") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            self.send_bytes(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8",
                            status=status)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/healthz":
                self.send_bytes(b"ok", "text/plain")
                return
            if parsed.path == "/api/status":
                with mutation_lock:
                    status = workflow_status_from_parents(
                        parents_now(), manifest_path=manifest_path,
                        enrichment_manifest_path=enrichment_manifest_path)
                self.send_json({
                    "primitive": "reconcile_review_web",
                    "ok": True,
                    "manifest": str(manifest_path),
                    "stage": browser_stage_for_next_action(status["next_action"]),
                    "next_action": status["next_action"],
                    "state_token": review_state_token(
                        status["progress"], status["selection"],
                        status["enrichment"], status["review_manifest"],
                        job_running=_job_lock.locked()),
                })
                return
            if parsed.path == "/api/enrichment":
                with mutation_lock:
                    parents = parents_now()
                selection = worth_selection_from_parents(
                    parents, manifest_path=manifest_path)
                self.send_json(read_enrichment_manifest(
                    enrichment_manifest_path, selection=selection))
                return
            if parsed.path == "/assets/reconcile-review.css":
                if not REVIEW_CSS.exists():
                    self.send_bytes(b"not found", "text/plain", status=404)
                else:
                    self.send_bytes(REVIEW_CSS.read_bytes(), "text/css; charset=utf-8",
                                    cache="no-cache")
                return
            if parsed.path == "/assets/reconcile-review.js":
                if not REVIEW_JS.exists():
                    self.send_bytes(b"not found", "text/plain", status=404)
                else:
                    self.send_bytes(REVIEW_JS.read_bytes(), "text/javascript; charset=utf-8",
                                    cache="no-cache")
                return
            if parsed.path == "/api/dossier":
                slug = (params.get("slug") or [""])[0]
                body = render_dossier_markdown(parents_dir, dossier_dir, slug)
                self.send_bytes(body.encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/decision-rows":
                view = str((params.get("view") or [""])[0]).strip().lower()
                if view not in {"yes", "no"}:
                    self.send_json({"error": f"unknown view: {view}"}, status=400)
                    return
                try:
                    offset = int(str((params.get("offset") or ["0"])[0]))
                    limit = int(str((params.get("limit") or [str(DECISION_CHUNK_SIZE)])[0]))
                except ValueError:
                    offset, limit = 0, DECISION_CHUNK_SIZE
                with mutation_lock:
                    parents = parents_now()
                self.send_json(decision_rows_payload(
                    parents, view, offset=offset, limit=min(max(1, limit), 200),
                    parents_dir=parents_dir, dossier_dir=dossier_dir))
                return
            if parsed.path in {"/api/worth-card", "/api/linkedin-card"}:
                # The next queue card (or its stage-complete state), so a decision
                # click swaps content in place instead of reloading. Optional
                # debug/index params drive the browse-only carousel; defaults are
                # exactly the pre-carousel behavior.
                debug = str((params.get("debug") or [""])[0]).strip() == "1"
                try:
                    index = max(0, int(str((params.get("index") or ["0"])[0])))
                except ValueError:
                    index = 0
                exclude = frozenset(
                    key.strip().lower()
                    for key in str((params.get("exclude") or [""])[0]).split(",")
                    if key.strip())
                pick = str((params.get("pick") or [""])[0]).strip().lower()
                if parsed.path == "/api/worth-card" and pick:
                    # Typeahead jump: ONE specific pending person's card, served
                    # from the same lock-free snapshot as the exclude prefetch
                    # (never takes the mutation lock, never rebuilds the model).
                    # A key that is no longer pending — decided elsewhere or
                    # stale — answers 404 so the client prunes it locally and
                    # keeps the current card.
                    picked = next(
                        (parent for parent in cached_parents
                         if needs_worth_review(parent)
                         and str(_worth_key(parent) or "").strip().lower() == pick),
                        None)
                    if picked is None:
                        self.send_bytes(b"gone", "text/plain; charset=utf-8", status=404)
                        return
                    card = render_worth_card(picked, parents_dir, dossier_dir,
                                             profile_cache_dir)
                    self.send_bytes(card.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if exclude:
                    # Prefetch of the FOLLOWING card while a decision POST holds
                    # the mutation lock: render from the current snapshot without
                    # blocking. The excluded keys make the pick race-free, and the
                    # POST's own response re-syncs counts when it lands.
                    parents = cached_parents
                else:
                    with mutation_lock:
                        parents = parents_now()
                progress = review_progress(parents)
                if parsed.path == "/api/worth-card":
                    body = worth_review_body(parents, progress, parents_dir, dossier_dir,
                                             debug=debug, index=index,
                                             profile_cache_dir=profile_cache_dir,
                                             exclude=exclude or None)
                elif debug:
                    selection = worth_selection_from_parents(
                        parents, manifest_path=manifest_path)
                    enrichment = read_enrichment_manifest(
                        enrichment_manifest_path, selection=selection)
                    body = linkedin_review_body(
                        parents, progress,
                        enrichment_complete=bool(enrichment.get("status") == STATUS_COMPLETED
                                                 and enrichment.get("current")),
                        linkedin_complete=phase_is_completed("linkedin", progress, manifest_path),
                        parents_dir=parents_dir, dossier_dir=dossier_dir,
                        enrichment=enrichment, profile_cache_dir=profile_cache_dir,
                        debug=debug, index=index)
                else:
                    body = linkedin_card_body(
                        parents, progress,
                        linkedin_complete=phase_is_completed("linkedin", progress, manifest_path),
                        parents_dir=parents_dir, dossier_dir=dossier_dir,
                        profile_cache_dir=profile_cache_dir,
                        exclude=exclude or None)
                self.send_bytes(body.encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/avatar":
                pub = (params.get("pub") or [""])[0]
                avatar = load_avatar(pub, profile_cache_dir=profile_cache_dir,
                                     avatar_dir=avatar_dir)
                if not avatar:
                    self.send_bytes(b"not found", "text/plain", status=404)
                else:
                    body, content_type = avatar
                    self.send_bytes(body, content_type, cache="private, max-age=86400")
                return
            if parsed.path != "/":
                self.send_bytes(b"not found", "text/plain", status=404)
                return

            # Serialize the snapshot with decision writes. GET stays read-only for
            # the durable decision files; rendering the ENRICH page derives its
            # state from disk and starts-or-joins the one free-work job — so a
            # stranded manifest (external CLI write, restart, crash) never
            # survives a reload. Money is the only stop: a needs_approval state
            # renders the Approve button and starts nothing.
            with mutation_lock:
                parents = parents_now()
            enrichment_state = None
            if _phase_view(params, {}, manifest_path) == "enrich":
                selection = worth_selection_from_parents(
                    parents, manifest_path=manifest_path)
                enrichment_state = derive_enrichment_state(
                    selection, verdicts_path=verdicts_path, review_path=review_path,
                    facts_dir=facts_dir, manifest_path=enrichment_manifest_path,
                    job_running=_job_lock.locked())
                free_work = (enrichment_state["state"] == STATE_FREE_PENDING
                             or (enrichment_state["state"] == STATE_NEEDS_APPROVAL
                                 and not enrichment_state.get("approvable")
                                 and not enrichment_state.get("approval_current")))
                if run_jobs and free_work and not review_progress(parents)["worth_pending"]:
                    # Render keeps the derived free_pending/needs_approval screen
                    # ("Preparing…"); the next poll derives running + heartbeat.
                    start_free_enrichment_job()
            self.send_bytes(page_html(parents, params, review_path, parents_dir=parents_dir,
                                      dossier_dir=dossier_dir, manifest_path=manifest_path,
                                      enrichment_manifest_path=enrichment_manifest_path,
                                      profile_cache_dir=profile_cache_dir,
                                      verdicts_path=verdicts_path, facts_dir=facts_dir,
                                      enrichment_state=enrichment_state))

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path not in {"/decide", "/worth", "/complete", "/approve-enrichment"}:
                self.send_bytes(b"not found", "text/plain", status=404)
                return
            origin = (self.headers.get("Origin") or "").strip()
            if origin and (urllib.parse.urlparse(origin).hostname or "").lower() not in {
                    "127.0.0.1", "localhost", "::1"}:
                self.send_bytes(b"cross-origin request rejected", "text/plain", status=403)
                return
            length = min(int(self.headers.get("Content-Length", "0")), 32_768)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            pub = (form.get("pub") or [""])[0]

            if parsed.path == "/approve-enrichment":
                try:
                    with mutation_lock:
                        current_parents = parents_now()
                        selection = worth_selection_from_parents(
                            current_parents, manifest_path=manifest_path)
                        enrichment = approve_enrichment_manifest(
                            enrichment_manifest_path, selection=selection)
                except ValueError as exc:
                    self.send_bytes(str(exc).encode("utf-8"), "text/plain; charset=utf-8",
                                    status=409)
                    return
                if run_jobs:
                    # The click IS the approval: run exactly the approved budget.
                    approved = float(
                        (enrichment.get("approval") or {}).get("approved_budget_usd") or 0)
                    start_approved_enrichment_job(approved)
                notify_agent()
                self.send_json({"ok": True, "enrichment": enrichment})
                return

            if parsed.path == "/complete":
                stage = (form.get("stage") or [""])[0].strip().lower()
                try:
                    with mutation_lock:
                        # Stage completion is a durable handoff to the agent —
                        # decide it from a FRESH rebuild, never the patched
                        # cache, so `review-status` can never disagree.
                        current_parents = refresh_parents_from_disk()
                        progress = review_progress(current_parents)
                        pending_key = {"worth": "worth_pending",
                                       "linkedin": "linkedin_pending"}.get(stage)
                        if pending_key and progress[pending_key]:
                            self.send_bytes(
                                (f"{progress[pending_key]} people still need review — "
                                 "the page refreshed with the current queue").encode("utf-8"),
                                "text/plain; charset=utf-8", status=409)
                            return
                        if stage == "enrich":
                            selection = worth_selection_from_parents(
                                current_parents, manifest_path=manifest_path)
                            enrichment = read_enrichment_manifest(
                                enrichment_manifest_path, selection=selection)
                            manifest = write_enrichment_handoff(
                                enrichment, path=manifest_path,
                                review_path=review_path, synthetic_path=synthetic_path)
                        else:
                            # No enrichment kick here: the next enrich-page render
                            # derives the state and triggers the free job itself.
                            manifest = write_review_manifest(
                                stage, "completed", progress, path=manifest_path,
                                review_path=review_path, synthetic_path=synthetic_path)
                except ValueError as exc:
                    self.send_bytes(str(exc).encode("utf-8"), "text/plain; charset=utf-8",
                                    status=409)
                    return
                notify_agent()
                self.send_json({"ok": True, "manifest": manifest, "progress": progress})
                return

            if parsed.path == "/worth":
                worth_val = (form.get("worth") or [""])[0].strip().lower()
                if worth_val not in {*USER_WORTH_VALUES, "restore"}:
                    self.send_bytes(b"worth must be yes, no, or restore", "text/plain", status=400)
                    return
                stored_worth = "" if worth_val == "restore" else worth_val
                try:
                    with mutation_lock:
                        parents_now()
                        target_parent = worth_parent_in_snapshot(
                            pub, (form.get("parent_slug") or [""])[0])
                        rows_now = review_rows_now()
                        # The posted pub is the QUEUE key; the durable mark must
                        # land on a row worth_view ATTACHES to the parent the
                        # user decided (row key or row.person_id inside that
                        # parent's identities). Split twins can share a queue
                        # key naming a row whose person_id belongs to the OTHER
                        # twin — writing there decides the wrong person, and the
                        # served twin re-derives pending on every rebuild: an
                        # undecidable zombie no click could ever clear.
                        write_key = pub.strip().lower()
                        target_ids = {str(value or "").strip().lower()
                                      for value in (target_parent or {}).get("person_ids") or []}
                        target_ids.discard("")
                        if target_parent and target_ids:
                            posted_row = rows_now.get(write_key) or {}
                            posted_pid = str(posted_row.get("person_id") or "").strip().lower()
                            if write_key not in target_ids and posted_pid not in target_ids:
                                write_key = sorted(target_ids)[0]
                        result = apply_worth_decision(review_path, write_key, stored_worth,
                                                      rows=rows_now)
                        accept_rows_write()
                        gate = sync_synthetic_gate(synthetic_path, write_key, stored_worth)
                        state = effective_no_for_key(
                            write_key, rows_now, facts_dir,
                            keepish=(gate["approved"] == "yes") if gate else None,
                            connections=connection_keys)
                        row_now = rows_now.get(write_key) or {}
                        decided = gate or {
                            "action": (row_now.get("action") or "").strip().lower(),
                            "approved": (row_now.get("approved") or "").strip().lower(),
                        }
                        # worth_row is the SOLE worth truth for queue, tabs,
                        # and counts — patch it too, or the click lands on
                        # disk while the live model keeps serving the old
                        # decision until the next full rebuild.
                        def patch_worth_state(model_parent: dict[str, Any]) -> None:
                            model_parent["worth"] = state["worth"]
                            model_parent["machine_worth"] = state["machine"]
                            model_primary = _primary_candidate(model_parent)
                            model_primary["worth"] = state["worth"]
                            model_primary["machine_worth"] = state["machine"]
                            model_row = model_parent.get("worth_row")
                            if model_row is None:
                                return
                            machine_dec = (model_row.get("machine") or {}).get("decision") or ""
                            if stored_worth:
                                model_row["human"] = {"decision": stored_worth,
                                                      "updated_at": now_iso()}
                                model_row["effective"] = stored_worth
                                model_row["source"] = "user"
                            else:  # restore: back to the machine's verdict
                                model_row["human"] = None
                                model_row["effective"] = machine_dec or "maybe"
                                model_row["source"] = ("llm" if machine_dec
                                                       else "default")

                        if target_parent:
                            patch_worth_state(target_parent)
                            primary = _primary_candidate(target_parent)
                            durable_candidate = next(
                                (candidate for candidate in target_parent.get("candidates") or []
                                 if str(candidate.get("pub") or "").strip().lower()
                                 == pub.strip().lower()),
                                None,
                            )
                            if durable_candidate:
                                durable_candidate["action"] = decided["action"]
                                durable_candidate["approved"] = decided["approved"]
                                durable_candidate["new_url"] = row_now.get(
                                    "new_linkedin_url", "")
                            if gate and primary.get("synthetic"):
                                primary["action"] = gate["action"]
                                primary["approved"] = gate["approved"]
                            # A FRESH rebuild derives every parent the written
                            # row ATTACHES to (worth_view rule 3: row key or
                            # row.person_id inside the person's identities) —
                            # so the cache must patch exactly that same set, no
                            # more (an unrelated twin sharing only the queue
                            # key stays independently decidable) and no less
                            # (a merged parent sharing the identity must flip,
                            # or it keeps the queue serving a decided person).
                            written_pid = str((rows_now.get(write_key) or {})
                                              .get("person_id") or "").strip().lower()
                            attach_keys = {write_key, written_pid} - {""}
                            for sibling in cached_parents:
                                if sibling is target_parent:
                                    continue
                                sibling_ids = {str(value or "").strip().lower()
                                               for value in sibling.get("person_ids") or []}
                                if sibling_ids & attach_keys:
                                    patch_worth_state(sibling)
                        accept_local_write()
                        current_parents = cached_parents
                        progress = review_progress(current_parents)
                        if progress["worth_pending"] == 0:
                            # The patched cache says done — confirm against a
                            # FRESH rebuild before declaring completion, so the
                            # agent's own fresh read can never disagree.
                            current_parents = refresh_parents_from_disk()
                            progress = review_progress(current_parents)
                        if progress["worth_pending"] == 0:
                            review_manifest = write_review_manifest(
                                "worth", "completed", progress, path=manifest_path,
                                review_path=review_path, synthetic_path=synthetic_path)
                            next_stage = "enrich"
                        else:
                            review_manifest = write_review_manifest(
                                "worth", "awaiting_user", progress, path=manifest_path,
                                review_path=review_path, synthetic_path=synthetic_path)
                            next_stage = "worth"
                        counts = summarize(current_parents)
                        state_token = state_token_for(current_parents, progress)
                except ValueError as exc:
                    self.send_bytes(str(exc).encode("utf-8"), "text/plain; charset=utf-8",
                                    status=400)
                    return
                notify_agent()
                self.send_json({
                    "ok": True, "pub": pub, **result,
                    "action": decided["action"], "approved": decided["approved"],
                    "new_url": row_now.get("new_linkedin_url", ""),
                    "effective": state["worth"]["decision"],
                    "source": state["worth"]["source"],
                    "reason": state["worth"]["reason"],
                    "rejected": state["rejected"],
                    "counts": counts,
                    "progress": progress,
                    "review_manifest": review_manifest,
                    "next_stage": next_stage,
                    "state_token": state_token,
                })
                return

            decision = (form.get("decision") or [""])[0]
            new_url = (form.get("new_url") or [""])[0]
            parent_slug = (form.get("parent_slug") or [""])[0]
            if not pub or decision not in {"keep", "detach", "fix", "reset", "exclude"}:
                self.send_bytes(b"bad request", "text/plain", status=400)
                return
            try:
                with mutation_lock:
                    parents_now()
                    pub_lower = pub.strip().lower()
                    target = candidate_in_snapshot(pub, prefer_slug=parent_slug)
                    if not target:
                        raise ValueError(f"review row not found: {pub}")
                    target_parent, target_candidate = target
                    actual_slug = str(target_parent.get("slug") or "")
                    if parent_slug and parent_slug != actual_slug:
                        raise ValueError("stale or mismatched person card")
                    synthetic_target = pub_lower.startswith("synth-")
                    if synthetic_target:
                        worth_key = synthetic_worth_key(synthetic_path, pub)
                        if decision == "fix":
                            if not worth_key:
                                raise ValueError(f"synthetic worth key not found: {pub}")
                            result = apply_decision(
                                review_path, verdicts_path, worth_key, decision, new_url,
                                confirm_threshold, detach_threshold)
                            rows = load_override_rows(review_path)
                            rows[worth_key.lower()]["person_id"] = (
                                rows[worth_key.lower()].get("person_id") or worth_key)
                            _write_override_rows(review_path, rows)
                            apply_synthetic_decision(synthetic_path, pub, "detach")
                            keepish = True
                            target_candidate["action"] = "verify"
                            target_candidate["approved"] = "no"
                            target_candidate["new_url"] = ""
                        else:
                            result = apply_synthetic_decision(synthetic_path, pub, decision)
                            keepish = result["approved"] == "yes"
                            target_candidate["action"] = result["action"]
                            target_candidate["approved"] = result["approved"]
                            target_candidate["new_url"] = result.get("new_url", "")
                    else:
                        result = apply_decision(
                            review_path, verdicts_path, pub, decision, new_url,
                            confirm_threshold, detach_threshold)
                        worth_key, keepish = pub, None
                        target_candidate["action"] = result["action"]
                        target_candidate["approved"] = result["approved"]
                        target_candidate["new_url"] = result.get("new_url", "")

                    # One affirmative answer resolves a multi-match person: every OTHER
                    # still-pending option on this parent is withdrawn as a link-level No
                    # decision (never a person reject), so picking one option resolves the
                    # whole parent and it does not reappear. A synthetic sibling's gate lives
                    # in synthetic-people.csv, so it is withdrawn through its approve gate; a
                    # real-LinkedIn sibling is detached in review.csv exactly as before.
                    resolved_pubs = [pub_lower]
                    if decision in {"keep", "fix"}:
                        for sibling in target_parent.get("candidates") or []:
                            sibling_pub = str(sibling.get("pub") or "").strip().lower()
                            if not sibling_pub or sibling_pub == pub_lower:
                                continue
                            sibling_approved = str(sibling.get("approved") or "").strip().lower()
                            if sibling.get("synthetic"):
                                # A synthetic option is pending unless the user already gated it
                                # (auto == still pending, matching pending_linkedin_candidates).
                                if sibling_approved in {"yes", "no"}:
                                    continue
                                apply_synthetic_decision(synthetic_path, sibling_pub, "detach")
                                sibling["action"] = "verify"
                                sibling["approved"] = "no"
                                sibling["new_url"] = ""
                            else:
                                if candidate_state(sibling) != "review":
                                    continue
                                apply_decision(
                                    review_path, verdicts_path, sibling_pub, "detach", "",
                                    confirm_threshold, detach_threshold)
                                sibling["action"] = "detach"
                                sibling["approved"] = "yes"
                                sibling["new_url"] = ""
                            resolved_pubs.append(sibling_pub)
                        # Carry the UNION of every candidate's contacts (kept + withdrawn
                        # siblings) onto the KEPT identity, so a withdrawn sibling's real
                        # email/phone is never lost. No-op for a single-candidate parent.
                        carry_forward_multi_option_contacts(
                            target_parent, target_candidate,
                            synthetic_path=synthetic_path, people_csv=people_csv)

                    accept_local_write()
                    current_parents = cached_parents
                    progress = review_progress(current_parents)
                    invalidate_manifest("linkedin", progress)
                    payload: dict[str, Any] = {
                        "ok": True, "pub": pub, **result,
                        "counts": summarize(current_parents),
                        "progress": progress,
                        "resolved_pubs": resolved_pubs,
                        "state_token": state_token_for(current_parents, progress),
                    }
                    if worth_key:
                        state = effective_no_for_key(
                            worth_key, load_override_rows(review_path), facts_dir,
                            keepish=keepish, connections=connection_keys)
                        payload.update({
                            "rejected": state["rejected"],
                            "effective": state["worth"]["decision"],
                            "source": state["worth"]["source"],
                        })
            except ValueError as exc:
                self.send_bytes(str(exc).encode("utf-8"), "text/plain; charset=utf-8",
                                status=400)
                return
            notify_agent()
            self.send_json(payload)

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    return Handler


def cmd_serve(args: argparse.Namespace) -> None:
    review_path = Path(args.review)
    verdicts_path = Path(args.verdicts)
    parents_dir = Path(args.parents_dir)
    synthetic_path = Path(args.synthetic_people)
    manifest_path = Path(args.manifest)
    parents = _all_review_parents(
        verdicts_path, review_path, synthetic_path,
        Path(args.facts_dir), Path(args.people_csv),
        Path(args.parents_dir), Path(args.dossier_dir), Path(args.profile_cache_dir))
    progress = review_progress(parents)
    requested_stage = args.stage or "worth"
    query = f"?stage={urllib.parse.quote(requested_stage)}"
    requested_url = f"http://{args.host}:{args.port}/{query}"

    def begin_people_review() -> None:
        write_review_manifest("worth", "awaiting_user", progress, path=manifest_path,
                              review_path=review_path, synthetic_path=synthetic_path,
                              launched=True)
        if progress["worth_pending"] == 0:
            write_review_manifest("worth", "completed", progress, path=manifest_path,
                                  review_path=review_path, synthetic_path=synthetic_path)

    # Reopening a live UI is read-only. Starting a new server begins one fresh
    # People-review revision; later stages are merely direct views into files.
    status_payload: dict[str, Any] = {}
    try:
        with urllib.request.urlopen(
                f"http://{args.host}:{args.port}/api/status", timeout=1) as response:
            status_payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, urllib.error.HTTPError,
            json.JSONDecodeError, TimeoutError):
        status_payload = {}
    if status_payload.get("primitive") == "reconcile_review_web":
        live_manifest = str(status_payload.get("manifest") or "").strip()
        try:
            wrong_server = bool(
                live_manifest
                and Path(live_manifest).resolve() != manifest_path.resolve())
        except (OSError, RuntimeError):
            wrong_server = live_manifest != str(manifest_path)
        if wrong_server:
            raise SystemExit(
                f"Port {args.port} belongs to a review server for {live_manifest}; "
                f"this review uses {manifest_path}"
            )
        if args.fresh and requested_stage == "worth":
            begin_people_review()
        print(json.dumps({"primitive": "reconcile_review_web", "status": "reused",
                          "url": requested_url, "manifest": str(manifest_path),
                          "stage": requested_stage}, indent=2))
        if args.open:
            webbrowser.open(requested_url)
        return

    if requested_stage == "worth":
        begin_people_review()
    # No launch self-heal kick: enrichment state is DERIVED at every enrich-page
    # render (derive_enrichment_state), and the render starts-or-joins the one
    # free-work job — so a stranded persisted state cannot survive a reload.
    # No push notifier: the agent watches state with `review-status --wait`,
    # which stats the same durable files this server writes. Simplicity wins.
    server = ThreadingHTTPServer((args.host, args.port),
                                 make_handler(review_path, verdicts_path, parents_dir, Path(args.dossier_dir),
                                              args.confirm_threshold, args.detach_threshold,
                                              synthetic_path=synthetic_path,
                                              facts_dir=Path(args.facts_dir),
                                              people_csv=Path(args.people_csv),
                                              manifest_path=manifest_path,
                                              enrichment_manifest_path=Path(args.enrichment_manifest),
                                              profile_cache_dir=Path(args.profile_cache_dir),
                                              avatar_dir=Path(args.avatar_dir),
                                              initial_parents=parents))
    host, port = server.server_address
    url = f"http://{host}:{port}/{query}"
    print(json.dumps({"primitive": "reconcile_review_web", "status": "serving", "url": url,
                      "manifest": str(manifest_path), "parents": len(parents),
                      "progress": progress}, indent=2))
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)


def workflow_status_from_parents(
    parents: list[dict[str, Any]], *,
    manifest_path: Path = REVIEW_MANIFEST,
    enrichment_manifest_path: Path = ENRICH_MANIFEST,
) -> dict[str, Any]:
    """Read-only next-action contract from an already-loaded server snapshot."""
    progress = review_progress(parents)
    selection = worth_selection_from_parents(parents, manifest_path=manifest_path)
    enrichment = read_enrichment_manifest(
        enrichment_manifest_path, selection=selection)
    worth_complete = phase_is_completed("worth", progress, manifest_path)
    enrich_continued = enrichment_handoff_completed(manifest_path)
    linkedin_complete = phase_is_completed("linkedin", progress, manifest_path)
    enrich_status = str(enrichment.get("status") or "not_started")
    approval_current = bool(enrichment.get("approval_current"))
    approved_budget = (float((enrichment.get("approval") or {}).get("approved_budget_usd") or 0)
                       if approval_current else 0.0)

    if progress["worth_pending"] or not worth_complete:
        next_action = "review_people"
    elif enrich_status in {"not_started", "stale"}:
        next_action = "preview_enrichment"
    elif enrich_status == "needs_approval" and int(enrichment.get("would_submit") or 0) == 0:
        next_action = "run_enrichment_from_cache"
    elif enrich_status == "needs_approval" and approval_current:
        next_action = "run_approved_enrichment"
    elif enrich_status == "needs_approval":
        next_action = "await_enrichment_approval"
    elif enrich_status in {"running", "submitted"}:
        next_action = "wait_for_enrichment"
    elif enrich_status in {"failed", "completed_with_errors"}:
        next_action = "retry_enrichment"
    elif enrich_status == "research_complete":
        next_action = "assemble_synthetic"
    elif enrich_status != "completed":
        next_action = "wait_for_enrichment"
    elif not enrich_continued:
        next_action = "continue_enrichment"
    elif progress["linkedin_pending"]:
        next_action = "review_linkedin"
    elif not linkedin_complete:
        next_action = "finish_linkedin"
    else:
        next_action = "realize"

    commands = {
        "review_people": "bin/deep-context review",
        "preview_enrichment": (
            "bin/deep-context reconcile-deep-research --dry-run "
            "--include-candidates --include-plausibly-absent"
        ),
        "await_enrichment_approval": "wait for the user to click Approve in Enrich Contacts",
        "run_approved_enrichment": (
            "bin/deep-context reconcile-deep-research "
            "--include-candidates --include-plausibly-absent --approve "
            f"--budget {approved_budget:.2f}"
        ),
        "run_enrichment_from_cache": (
            "bin/deep-context reconcile-deep-research "
            "--include-candidates --include-plausibly-absent"
        ),
        "wait_for_enrichment": "bin/deep-context review-status",
        "retry_enrichment": "inspect the fixed enrichment manifest error",
        "assemble_synthetic": "bin/deep-context assemble-synthetic",
        "continue_enrichment": "wait for the user to click Continue in Enrich Contacts",
        "review_linkedin": "wait for LinkedIn Yes/No decisions in the review UI",
        "finish_linkedin": "wait for the user to click Finish in Check LinkedIn",
        "realize": "bin/deep-context realize",
    }
    return {
        "primitive": "deep_context_review_status",
        "status": "ok",
        "next_action": next_action,
        "command": commands[next_action],
        "poll_after_seconds": 60,
        "progress": progress,
        "selection": selection,
        "review_manifest": read_review_manifest(manifest_path),
        "enrichment": enrichment,
    }


def workflow_status(
    *, review_path: Path = LINKEDIN_OVERRIDES_CSV,
    verdicts_path: Path = VERDICTS_JSONL,
    synthetic_path: Path = SYNTHETIC_PEOPLE_CSV,
    facts_dir: Path = FACTS_DIR,
    people_csv: Path = DEFAULT_PEOPLE_CSV,
    manifest_path: Path = REVIEW_MANIFEST,
    enrichment_manifest_path: Path = ENRICH_MANIFEST,
    parents_dir: Path = PARENTS_DIR,
    dossier_dir: Path = DOSSIER_DIR,
    profile_cache_dir: Path = PROFILE_CACHE_DIR,
) -> dict[str, Any]:
    """Read-only next-action contract for the agent's one-minute CLI poll."""
    parents = _all_review_parents(
        verdicts_path, review_path, synthetic_path, facts_dir, people_csv,
        parents_dir, dossier_dir, profile_cache_dir)
    return workflow_status_from_parents(
        parents, manifest_path=manifest_path,
        enrichment_manifest_path=enrichment_manifest_path)


# next_action values the AGENT acts on. The review app runs the mid-flow work
# itself (preview, approved enrichment, from-cache continue, assemble,
# prefetch — see the in-app pipeline jobs), so the agent's wait returns only
# when something went wrong or when everything is done.
AGENT_ACTIONS = {
    "retry_enrichment",
    "realize",
}


def cmd_status(args: argparse.Namespace) -> None:
    """Print the next-action contract; ``--wait`` blocks until it is an AGENT
    action (or the timeout passes), then prints and exits.

    This is the whole agent-handoff mechanism — deliberately primitive so it
    always works: stat six local files once a second, recompute the contract
    only when one changed. No sockets, no daemons, no thread ids, no coupling
    to any harness. On timeout the payload carries ``status: waiting`` and the
    caller simply runs the command again."""
    paths = dict(
        review_path=Path(args.review), verdicts_path=Path(args.verdicts),
        synthetic_path=Path(args.synthetic_people), facts_dir=Path(args.facts_dir),
        people_csv=Path(args.people_csv), manifest_path=Path(args.manifest),
        enrichment_manifest_path=Path(args.enrichment_manifest),
    )
    watched = (paths["review_path"], paths["verdicts_path"], paths["synthetic_path"],
               paths["people_csv"], paths["manifest_path"],
               paths["enrichment_manifest_path"])

    def file_signature() -> tuple[tuple[int, int], ...]:
        values = []
        for path in watched:
            try:
                stat = path.stat()
                values.append((stat.st_mtime_ns, stat.st_size))
            except OSError:
                values.append((0, 0))
        return tuple(values)

    status = workflow_status(**paths)
    if getattr(args, "wait", False):
        started = time.monotonic()
        deadline = started + max(1, int(args.timeout))
        signature = file_signature()
        while (status["next_action"] not in AGENT_ACTIONS
               and time.monotonic() < deadline):
            time.sleep(1)
            current = file_signature()
            if current == signature:
                continue
            signature = current
            status = workflow_status(**paths)
        status["waited_seconds"] = int(time.monotonic() - started)
        if status["next_action"] not in AGENT_ACTIONS:
            status["status"] = "waiting"  # still the human's move — run me again
    print(json.dumps(status, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the staged deep-context people review UI.")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve")
    serve.add_argument("--review", default=str(LINKEDIN_OVERRIDES_CSV))
    serve.add_argument("--verdicts", default=str(VERDICTS_JSONL))
    serve.add_argument("--parents-dir", default=str(PARENTS_DIR))
    serve.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    serve.add_argument("--facts-dir", default=str(FACTS_DIR))
    serve.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    serve.add_argument("--synthetic-people", default=str(SYNTHETIC_PEOPLE_CSV))
    serve.add_argument("--manifest", default=str(REVIEW_MANIFEST))
    serve.add_argument("--enrichment-manifest", default=str(ENRICH_MANIFEST))
    serve.add_argument("--profile-cache-dir", default=str(PROFILE_CACHE_DIR))
    serve.add_argument("--avatar-dir", default=str(AVATAR_DIR))
    serve.add_argument("--confirm-threshold", type=float, default=DEFAULT_CONFIRM)
    serve.add_argument("--detach-threshold", type=float, default=DEFAULT_DETACH)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--stage", choices=("worth", "enrich", "linkedin", "done"))
    serve.add_argument("--fresh", action="store_true",
                       help="Begin a new People-review revision even when reusing a live server")
    serve.add_argument("--open", action="store_true")
    serve.set_defaults(func=cmd_serve)
    status = sub.add_parser("status", help="Read files and print the exact next workflow action")
    status.add_argument("--review", default=str(LINKEDIN_OVERRIDES_CSV))
    status.add_argument("--verdicts", default=str(VERDICTS_JSONL))
    status.add_argument("--facts-dir", default=str(FACTS_DIR))
    status.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    status.add_argument("--synthetic-people", default=str(SYNTHETIC_PEOPLE_CSV))
    status.add_argument("--manifest", default=str(REVIEW_MANIFEST))
    status.add_argument("--enrichment-manifest", default=str(ENRICH_MANIFEST))
    status.add_argument("--wait", action="store_true",
                        help="block until next_action is an AGENT action "
                             "(or --timeout passes), then print and exit")
    status.add_argument("--timeout", type=int, default=900,
                        help="max seconds to --wait before returning "
                             "status=waiting (default 900)")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        args = build_parser().parse_args(["serve", *(argv or [])])
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
