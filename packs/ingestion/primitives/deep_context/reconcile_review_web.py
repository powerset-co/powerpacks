#!/usr/bin/env python3
"""Local, file-backed review UI for the staged deep-context workflow.

The browser reviews uncertain People decisions, observes Enrich Contacts progress,
then confirms new identities. Human choices remain the existing durable
``review.csv`` / synthetic gates; two fixed manifests tell the agent when to advance.
No provider call, subprocess, or paid work happens in this server.
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt

from packs.ingestion.primitives.deep_context.candidates import (
    NETWORK_WORTH_VALUES,
    candidates_resolved_by_existing,
    effective_network_worth,
    is_candidate_id,
    load_candidates,
)
from packs.ingestion.primitives.deep_context.common import (
    DEEP_RESEARCH_DIR,
    DEFAULT_PEOPLE_CSV,
    DOSSIER_DIR,
    ENRICH_MANIFEST,
    FACTS_DIR,
    GMAIL_CHANNEL,
    IMESSAGE_CHANNEL,
    LINKEDIN_OVERRIDES_CSV,
    PARENTS_DIR,
    PROFILE_CACHE_DIR,
    REVIEW_DIR,
    REVIEW_MANIFEST,
    ROOT,
    VERDICTS_JSONL,
    WHATSAPP_CHANNEL,
    normalize_email,
    now_iso,
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
)
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url

APPLIED_APPROVED = {"auto", "yes"}
VALID_TABS = {"all", "review", "verified", "detached", "conflict", "fixed", "excluded", "decided", "rejected"}
VALID_STAGES = {"worth", "enrich", "linkedin", "done"}
USER_WORTH_VALUES = {"yes", "no"}
DECISION_PAGE_SIZE = 10
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
    MACHINE no (worth judgment / spam flag) never rejects or drops them; only
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
    synthetic/LinkedIn row are deduped by person_id."""
    parents: list[dict[str, Any]] = []
    resolved_candidates = (candidates_resolved_by_existing()
                           if resolved_candidates is None else resolved_candidates)
    for person in load_candidates():
        pid = person.person_id
        if (pid.lower() in shown_person_ids or pid.lower() in resolved_candidates
                or not (facts_dir / f"{pid}.jsonl").exists()):
            continue
        name = _facts_canonical_name(facts_dir, pid) or person.full_name or pid
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
            "sources": _sources_of(person.source_channels),
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
                    *person.emails,
                    *_facts_identifier_emails(facts_dir, pid),
                ])),
                "match_phones": person.phones,
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
                   facts_dir: Path, connections: set[str] | None = None) -> None:
    """Attach the effective network-worth (user review.csv mark / approved exclude >
    fresh review.csv llm_worth > synthesis LLM fallback > default 'maybe') to EVERY row — verdict,
    candidate, and synthetic alike — plus the machine's own view (for the secondary
    text and the unified Rejected grouping). The mark's review.csv key is the primary
    candidate's LinkedIn pub for verdict rows, else the row's person_id."""
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


def apply_worth_decision(review_path: Path, pub: str, worth: str) -> dict[str, str]:
    """Upsert the USER-owned `network_worth` mark for one review.csv row (keyed by the
    row's key — a verdict row's pub, a candidate/synthetic row's person_id). '' clears
    the mark (back to the LLM's judgment). Never touches action/approved — with ONE
    exception: a worth-Yes on an excluded row clears the exclude (an approved exclude
    IS a user no, so the rescue must clear both stores)."""
    pub = (pub or "").strip().lower()
    worth = (worth or "").strip().lower()
    if not pub:
        raise ValueError("worth mark needs a row key")
    if worth not in ("", *NETWORK_WORTH_VALUES):
        raise ValueError(f"unknown worth mark: {worth}")
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
    machine_no = machine["decision"] == "no" or (row.get("llm_reject") or "").strip().lower() == "spam"
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
            # machine-owned spam screen (review.csv llm_* columns)
            "llm_reject": (dec.get("llm_reject") or "").strip().lower(),
            "llm_reject_confidence": dec.get("llm_reject_confidence", ""),
            "llm_reject_reason": dec.get("llm_reject_reason", ""),
        })
    return list(parents.values()), overrides


def is_llm_rejected(parent: dict[str, Any]) -> bool:
    return any(c.get("llm_reject") == "spam" for c in parent["candidates"])


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
    user-sourced worth of no), or the machine said no (worth judgment or spam flag)
    with no user rescue (worth-Yes or a keep-ish decision)."""
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
    machine_no = (parent.get("machine_worth") or {}).get("decision") == "no" or is_llm_rejected(parent)
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
        # effective-no people (spam, worth-no, excluded) live on the Rejected tab
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


def extend_and_annotate(parents: list[dict[str, Any]], overrides: dict[str, dict[str, str]],
                        synthetic_path: Path, facts_dir: Path,
                        connections: set[str] | None = None, *,
                        parents_dir: Path = PARENTS_DIR,
                        dossier_dir: Path = DOSSIER_DIR,
                        profile_cache_dir: Path = PROFILE_CACHE_DIR,
                        research_dir: Path = DEEP_RESEARCH_DIR) -> list[dict[str, Any]]:
    """Add the synthetic + pre-research candidate rows to the verdict parents and
    annotate everyone's worth — the full row set page_html/summarize operate on."""
    parents.extend(load_synthetic_parents(
        synthetic_path, parents_dir, dossier_dir, facts_dir))
    shown = {pid.lower() for p in parents for pid in p["person_ids"]}
    parents.extend(load_candidate_parents(facts_dir, overrides, shown))
    hydrate_proposed_profiles(
        parents, profile_cache_dir=profile_cache_dir, research_dir=research_dir)
    annotate_worth(parents, overrides, facts_dir, connections)
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


def explicit_worth(parent: dict[str, Any]) -> str:
    """The user's terminal binary worth decision, ignoring model/default advice."""
    worth = parent.get("worth") or {}
    decision = str(worth.get("decision") or "").strip().lower()
    return decision if worth.get("source") == "user" and decision in USER_WORTH_VALUES else ""


def needs_worth_review(parent: dict[str, Any]) -> bool:
    """Only model-uncertain unresolved imports need the first human decision.

    Model Yes starts in Added, model No/spam starts in Rejected, and both piles
    remain editable. A model Maybe is the only item highlighted in the main
    binary queue until the user places it in one of those piles.
    """
    machine = str((parent.get("machine_worth") or {}).get("decision") or "maybe").lower()
    return (is_worth_subject(parent)
            and is_import_candidate_parent(parent)
            and not is_effective_no(parent)
            and machine == "maybe"
            and explicit_worth(parent) not in USER_WORTH_VALUES)


def is_lookup_ready(parent: dict[str, Any]) -> bool:
    machine = str((parent.get("machine_worth") or {}).get("decision") or "maybe").lower()
    user = explicit_worth(parent)
    return (is_worth_subject(parent)
            and not is_effective_no(parent)
            and (user == "yes" or (not user and machine == "yes")))


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
            if approved not in {"yes", "no"}:
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
    worth_scope = [parent for parent in parents if is_worth_subject(parent)]
    worth_pending = [parent for parent in worth_scope if needs_worth_review(parent)]
    lookup_ready = [parent for parent in worth_scope if is_lookup_ready(parent)]
    identity_scope = [parent for parent in parents if identity_in_scope(parent)]
    identity_pending = [parent for parent in identity_scope if pending_linkedin_candidates(parent)]
    return {
        "total": len(parents),
        "worth_total": len(worth_scope),
        "worth_pending": len(worth_pending),
        "worth_yes": len(lookup_ready),
        "worth_no": sum(1 for parent in worth_scope if is_effective_no(parent)),
        "lookup_ready": len(lookup_ready),
        "linkedin_total": len(identity_scope),
        "linkedin_pending": len(identity_pending),
        "linkedin_done": len(identity_scope) - len(identity_pending),
        "rejected": sum(1 for parent in parents if is_effective_no(parent)),
    }


def review_state_token(progress: dict[str, int], selection: dict[str, Any],
                       enrichment: dict[str, Any],
                       review_manifest: dict[str, Any]) -> str:
    """Ephemeral browser refresh token derived only from the fixed file state."""
    payload = {
        "progress": progress,
        "selection": selection,
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


def read_enrichment_manifest(path: Path = ENRICH_MANIFEST, *,
                             selection: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"stage": "enrich", "status": "not_started",
                "counts": {"total": 0, "completed": 0, "pending": 0, "failed": 0},
                "current": False, "approval_current": False}
    if not isinstance(value, dict):
        return {"stage": "enrich", "status": "not_started", "counts": {},
                "current": False, "approval_current": False}
    recorded_selection = value.get("selection") if isinstance(value.get("selection"), dict) else {}
    current = bool(
        selection
        and recorded_selection.get("sha256") == selection.get("sha256")
        and recorded_selection.get("review_revision") == selection.get("review_revision")
        and bool(selection.get("review_revision"))
    )
    approval = value.get("approval") if isinstance(value.get("approval"), dict) else {}
    try:
        estimated = round(float(value.get("estimated_usd") or 0), 2)
        approved_estimate = round(float(approval.get("estimated_usd") or -1), 2)
        approved_budget = round(float(approval.get("approved_budget_usd") or -1), 2)
        approved_count = int(approval.get("would_submit") or -1)
        manifest_count = int(value.get("would_submit") or 0)
    except (TypeError, ValueError, OverflowError):
        estimated = approved_estimate = approved_budget = -1
        approved_count = manifest_count = -1
    approval_current = bool(
        current
        and value.get("status") == "needs_approval"
        and approval.get("status") == "approved"
        and approval.get("selection_sha256") == recorded_selection.get("sha256")
        and approval.get("review_revision") == recorded_selection.get("review_revision")
        and approved_count == manifest_count
        and math.isfinite(estimated)
        and math.isfinite(approved_estimate)
        and math.isfinite(approved_budget)
        and approved_estimate == estimated
        and approved_budget >= estimated
    )
    result = {**value, "current": current, "approval_current": approval_current}
    if not current:
        result["status"] = "stale"
    return result


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
    if enrichment.get("status") != "needs_approval":
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
        if stage == "linkedin" and ("worth" not in completed or "enrich" not in completed):
            raise ValueError("People decisions and enrichment must be completed before LinkedIn")
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
    if enrichment.get("status") != "completed" or not enrichment.get("current"):
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
_MARKDOWN = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})


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


def render_dossier_markdown(parents_dir: Path, dossier_dir: Path, slug: str) -> str:
    """Render a dossier as safe, dependency-local HTML for the Details drawer.

    Raw HTML is disabled so message-derived text cannot become executable markup.
    Headings are demoted beneath the page/card headings, and the visual separator
    used between merged child dossiers becomes a real horizontal rule.
    """
    markdown = render_dossier(parents_dir, dossier_dir, slug)
    # ``compose_dossier.headline`` intentionally caps relationship-only index
    # headlines at 80 characters. That compact line is useful in an index, but
    # looks like missing prose inside an expanded review dossier. When the full
    # relationship follows, omit only that redundant truncated preview.
    relationship = re.search(
        r"(?ms)^## Relationship & cadence\s*\n+(.*?)(?=^##\s|\Z)", markdown
    )
    summary = re.search(r"(?ms)^## Summary\s*\n+(.*?)(?=^##\s|\Z)", markdown)
    if relationship and summary:
        full_relationship = next(
            (line.strip() for line in relationship.group(1).splitlines() if line.strip()), ""
        )
        summary_lines = summary.group(1).splitlines()
        preview_index = next(
            (index for index, line in enumerate(summary_lines) if line.strip()), None
        )
        if preview_index is not None:
            preview = summary_lines[preview_index].strip()
            prefix = preview[:-1].rstrip() if preview.endswith("…") else ""
            if prefix and full_relationship.startswith(prefix):
                del summary_lines[preview_index]
                replacement = "\n".join(summary_lines).lstrip("\n")
                markdown = markdown[:summary.start(1)] + replacement + markdown[summary.end(1):]
    markdown = re.sub(r"(?m)^─{3,}\s*$", "---", markdown)
    markdown = re.sub(
        r"(?m)^(#{1,4})(\s+)",
        lambda match: "#" * min(len(match.group(1)) + 2, 6) + match.group(2),
        markdown,
    )
    return _MARKDOWN.render(markdown)


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


def _source_badges(parent: dict[str, Any]) -> str:
    labels = {"gmail": "Gmail", "imessage": "iMessage", "whatsapp": "WhatsApp"}
    return "".join(
        f"<span class='source source-{esc(source)}'><i aria-hidden='true'></i>{esc(labels.get(source, source))}</span>"
        for source in parent.get("sources") or []
    )


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


def _details(parent: dict[str, Any], candidate: dict[str, Any], *, identity: bool,
             profile_facts: str = "") -> str:
    contacts = [*(candidate.get("match_emails") or []), *(candidate.get("match_phones") or [])]
    evidence = [*(candidate.get("supporting") or []), *(candidate.get("contradicting") or [])]
    reason = str(candidate.get("reason") or "")
    rows: list[str] = []
    if contacts:
        rows.append(f"<div><dt>Contact</dt><dd>{esc(' · '.join(contacts))}</dd></div>")
    if identity and reason:
        rows.append(f"<div><dt>Match signal</dt><dd>{esc(reason)}</dd></div>")
    if evidence:
        rows.append(f"<div><dt>Evidence</dt><dd>{esc(' · '.join(evidence[:5]))}</dd></div>")
    extra = f"<dl>{''.join(rows)}</dl>" if rows else ""
    dossier_slug = parent.get("dossier_slug") or parent.get("slug")
    return (f"<section class='details' data-slug='{esc(dossier_slug)}'>"
            f"<h3 class='details-heading'>Details</h3>"
            f"<div class='details-body'>{profile_facts}{extra}"
            "<h4 class='dossier-heading'>Context</h4>"
            "<div class='dossier-text' aria-busy='true'>Loading…</div></div></section>")


def _scroll_region(content: str) -> str:
    return ("<div class='identity-scroll-shell'>"
            f"<div class='identity-scroll'>{content}</div>"
            "<button class='scroll-cue' type='button' data-scroll-cue "
            "aria-label='Scroll down' hidden>"
            "<svg viewBox='0 0 24 24' aria-hidden='true' focusable='false'>"
            "<path d='m7 9 5 5 5-5'></path></svg></button></div>")


def render_worth_card(parent: dict[str, Any], parents_dir: Path, dossier_dir: Path) -> str:
    candidate = _primary_candidate(parent)
    key = _worth_key(parent)
    name = str(parent.get("name") or candidate.get("full_name") or "This person")
    scroll_content = f"""
        <div class='person-top'>
          {_avatar(parent, candidate)}
          <div class='person-copy'>
            <div class='eyebrow-row'>{_source_badges(parent)}</div>
            <h2>{esc(name)}</h2>
          </div>
        </div>
        {_details(parent, candidate, identity=False)}"""
    return f"""
    <article class='decision-card identity-card worth-card' data-card>
      {_scroll_region(scroll_content)}
      <div class='identity-decision'>
        <div class='binary-actions'>
          <button class='button button-outline' data-worth='no' data-pub='{esc(key)}'>No</button>
          <button class='button button-primary' data-worth='yes' data-pub='{esc(key)}'>Yes</button>
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


def render_linkedin_card(parent: dict[str, Any], candidate: dict[str, Any],
                         parents_dir: Path, dossier_dir: Path) -> str:
    name = str(parent.get("name") or candidate.get("full_name") or "this person")
    synthetic = bool(candidate.get("synthetic"))
    profile_name = str(candidate.get("full_name") or name)
    roles = "".join(f"<li>{esc(role)}</li>" for role in (candidate.get("experiences") or [])[:3])
    education = " · ".join(candidate.get("education") or [])
    if synthetic:
        eyebrow = "No LinkedIn found"
        link = "<span class='linkedin-label'>Researched profile</span>"
    else:
        question = "Is this the right LinkedIn?"
        eyebrow = ""
        url = str(candidate.get("url") or "")
        link = (f"<a class='linkedin-label' href='{esc(url)}' target='_blank' rel='noreferrer'>View LinkedIn"
                "<span aria-hidden='true'>↗</span></a>") if url else ""
    fix_form = f"""<form class='linkedin-fix-form' data-fix-form
          data-pub='{esc(candidate.get('pub'))}' data-parent='{esc(parent.get('slug'))}'>
        <label class='sr-only' for='fix-{esc(candidate.get('pub'))}'>LinkedIn URL</label>
        <div><input id='fix-{esc(candidate.get('pub'))}' name='new_url' inputmode='url'
          autocomplete='url' placeholder='linkedin.com/in/…' required>
        <button class='button {'button-primary' if synthetic else 'button-outline'}'
          type='submit'>Use this</button></div>
      </form>
      <button class='button button-ghost alternate-skip' data-decide='detach'
              data-toast='Skipped' data-pub='{esc(candidate.get('pub'))}'
              data-parent='{esc(parent.get('slug'))}'>Skip</button>"""
    profile_facts = (
        (f"<ul class='roles'>{roles}</ul>" if roles else "")
        + (f"<p class='education'>{esc(education)}</p>" if education else "")
    )
    scroll_content = f"""
        {f"<div class='identity-eyebrow'>{esc(eyebrow)}</div>" if eyebrow else ""}
        <div class='profile-card'>
          {_avatar(parent, candidate)}
          <div class='profile-copy'>
            <h2>{esc(profile_name)}</h2>
            {link}
            {f"<p>{esc(candidate.get('headline'))}</p>" if candidate.get('headline') else ""}
            {f"<span>{esc(candidate.get('location'))}</span>" if candidate.get('location') else ""}
          </div>
        </div>
        {_details(parent, candidate, identity=True, profile_facts=profile_facts)}"""
    return f"""
    <article class='decision-card identity-card' data-card data-parent='{esc(parent.get('slug'))}'>
      {_scroll_region(scroll_content)}
      <div class='identity-decision'>
        {f"""<div class='synthetic-correction'>
          <div class='question'>Add their LinkedIn</div>
          {fix_form}
        </div>""" if synthetic else f"""<div class='question'>{question}</div>
        <div class='binary-actions'>
          <button class='button button-outline' data-open-fix aria-expanded='false'
                  aria-controls='fix-section-{esc(candidate.get('pub'))}'>No</button>
          <button class='button button-primary' data-decide='keep'
                  data-pub='{esc(candidate.get('pub'))}'
                  data-parent='{esc(parent.get('slug'))}'>Yes</button>
        </div>
        <div class='alternate' id='fix-section-{esc(candidate.get('pub'))}' hidden>
          {fix_form}
        </div>"""}
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
    spam_reason = next((str(candidate.get("llm_reject_reason") or "").strip()
                        for candidate in parent.get("candidates") or []
                        if candidate.get("llm_reject") == "spam"), "")
    if spam_reason:
        reason = spam_reason
    else:
        reason = str(machine.get("reason") or "").strip()
        if not reason or reason.lower() == "not yet judged":
            reason = "Not worth adding"
    return reason if len(reason) <= 140 else reason[:137].rsplit(" ", 1)[0] + "…"


def _pagination(page: int, total_pages: int, *, decision: str, preview: bool = False) -> str:
    if total_pages <= 1:
        return ""
    numbers = sorted({1, total_pages, page - 2, page - 1, page, page + 1, page + 2}
                     & set(range(1, total_pages + 1)))
    def href(number: int) -> str:
        preview_query = "&amp;preview=1" if preview else ""
        return f"/?stage=worth&amp;view={esc(decision)}&amp;page={number}{preview_query}"
    items: list[str] = [
        (f"<a class='page-link page-direction' href='{href(page - 1)}' rel='prev' "
         f"aria-label='Previous {esc(decision)} decisions page'>Previous</a>")
        if page > 1 else
        "<span class='page-link page-direction disabled' aria-disabled='true'>Previous</span>"
    ]
    previous = 0
    for number in numbers:
        if previous and number - previous > 1:
            items.append("<span class='page-ellipsis' aria-hidden='true'>…</span>")
        classes = ["page-link"]
        if number in {1, total_pages}:
            classes.append("page-boundary")
        if abs(number - page) == 2:
            classes.append("page-far")
        if number == page:
            classes.append("active")
            items.append(f"<span class='{' '.join(classes)}' aria-current='page'>{number}</span>")
        else:
            items.append(f"<a class='{' '.join(classes)}' href='{href(number)}' "
                         f"aria-label='{esc(decision.title())} decisions page {number}'>{number}</a>")
        previous = number
    items.append(
        (f"<a class='page-link page-direction' href='{href(page + 1)}' rel='next' "
         f"aria-label='Next {esc(decision)} decisions page'>Next</a>")
        if page < total_pages else
        "<span class='page-link page-direction disabled' aria-disabled='true'>Next</span>"
    )
    return f"<nav class='pagination' aria-label='{esc(decision.title())} decision pages'>" + "".join(items) + "</nav>"


def render_decision_table(parents: list[dict[str, Any]], decision: str, *, page: int = 1,
                          page_size: int = DECISION_PAGE_SIZE,
                          preview: bool = False) -> str:
    if decision not in {"yes", "no"}:
        raise ValueError(f"unknown decision table: {decision}")
    rows_in_scope = [
        parent for parent in parents
        if is_worth_subject(parent)
        and ((decision == "yes" and is_lookup_ready(parent))
             or (decision == "no" and is_effective_no(parent)))
    ]
    rows_in_scope.sort(key=lambda item: str(item.get("name") or "").lower())
    if not rows_in_scope:
        return ("<div class='empty-state decision-empty'><div class='empty-mark'>0</div>"
                f"<h2>No {esc(decision)} decisions</h2></div>")
    page_size = max(1, page_size)
    total_pages = max(1, (len(rows_in_scope) + page_size - 1) // page_size)
    page = min(max(1, page), total_pages)
    start = (page - 1) * page_size
    rows = []
    for parent in rows_in_scope[start:start + page_size]:
        candidate = _primary_candidate(parent)
        flip = "no" if decision == "yes" else "yes"
        flip_label = "No" if decision == "yes" else "Yes"
        reason = (_rejection_reason(parent) if decision == "no" else
                  str((parent.get("machine_worth") or {}).get("reason") or "Worth adding"))
        contacts = [*(candidate.get("match_emails") or []), *(candidate.get("match_phones") or [])]
        badges = _source_badges(parent)
        dossier_slug = parent.get("dossier_slug") or parent.get("slug")
        why_label = "Why no" if decision == "no" else "Why yes"
        fact_rows = []
        if contacts:
            fact_rows.append(f"<div><dt>Contact</dt><dd>{esc(' · '.join(contacts))}</dd></div>")
        fact_rows.append(f"<div><dt>{why_label}</dt><dd>{esc(reason)}</dd></div>")
        rows.append(f"""
        <details class='decision-row' data-card data-slug='{esc(dossier_slug)}'>
          <summary class='decision-row-summary'>
            {_avatar(parent, candidate, small=True)}
            <div class='decision-row-main'><strong>{esc(parent.get('name'))}</strong><span>{esc(reason)}</span></div>
            <div class='decision-row-actions'>
              <button class='button button-ghost' data-worth='{flip}' data-pub='{esc(_worth_key(parent))}' aria-label='Mark {esc(parent.get('name'))} {flip_label}'>{flip_label}</button>
              <span class='decision-row-caret' aria-hidden='true'></span>
            </div>
          </summary>
          <div class='decision-row-detail'>
            {f"<div class='row-badges'>{badges}</div>" if badges else ""}
            <dl class='row-facts'>{''.join(fact_rows)}</dl>
            <h4 class='dossier-heading'>Who they are</h4>
            <div class='dossier-text' aria-busy='true'>Loading…</div>
          </div>
        </details>""")
    return ("<div class='decision-page'><section class='decision-list'>" + "".join(rows)
            + "</section>" + _pagination(
                page, total_pages, decision=decision, preview=preview) + "</div>")


def render_decision_tabs(progress: dict[str, int], active: str, *, preview: bool = False) -> str:
    tabs = (("review", "Review", progress["worth_pending"]),
            ("yes", "Yes", progress["worth_yes"]),
            ("no", "No", progress["worth_no"]))
    links = []
    for key, label, count in tabs:
        current = " aria-current='page'" if key == active else ""
        preview_query = "&amp;preview=1" if preview else ""
        links.append(
            f"<a class='decision-tab{' active' if key == active else ''}' "
            f"href='/?stage=worth&amp;view={key}{preview_query}'{current}>"
            f"{label}<span>{count}</span></a>")
    return "<nav class='decision-tabs' aria-label='People decisions'>" + "".join(links) + "</nav>"


def _phase_view(params: dict[str, list[str]], progress: dict[str, int], manifest_path: Path) -> str:
    requested = str((params.get("stage") or [""])[0]).strip().lower()
    return requested if requested in {"worth", "enrich", "linkedin", "done"} else "worth"


def render_enrichment(enrichment: dict[str, Any], progress: dict[str, int]) -> str:
    status = str(enrichment.get("status") or "not_started")
    counts = enrichment.get("counts") if isinstance(enrichment.get("counts"), dict) else {}
    total = max(0, int(counts.get("total") or progress["lookup_ready"] or 0))
    completed = min(total, max(0, int(counts.get("completed") or 0)))
    failed = max(0, int(counts.get("failed") or 0))
    percent = round((completed / total) * 100) if total else 0
    progress_bar = f"""
      <div class='enrich-progress' role='progressbar' aria-label='Contact enrichment progress'
           aria-valuemin='0' aria-valuemax='{total}' aria-valuenow='{completed}'>
        <div class='enrich-progress-fill' style='width:{percent}%'></div>
      </div>"""
    if progress["worth_pending"]:
        return ("<div class='empty-state enrich-state'><div class='empty-mark'>1</div>"
                "<h2>Review in progress</h2>"
                f"<p>{progress['worth_pending']} decisions left · {progress['lookup_ready']} currently yes</p>"
                f"{progress_bar}</div>")
    if status in {"not_started", "stale"}:
        return ("<div class='empty-state enrich-state'><div class='empty-mark'>2</div>"
                "<h2>Preparing enrichment</h2>"
                f"<p>{progress['lookup_ready']} approved contact{'s' if progress['lookup_ready'] != 1 else ''}</p>"
                f"{progress_bar}</div>")
    if status == "needs_approval":
        estimate = float(enrichment.get("estimated_usd") or 0)
        new_count = max(0, int(enrichment.get("would_submit") or 0))
        reused = max(0, int(enrichment.get("reused_completed") or 0))
        if enrichment.get("approval_current"):
            return ("<div class='empty-state enrich-state'><div class='progress-spinner' aria-hidden='true'></div>"
                    "<h2>Approved</h2>"
                    f"<p>${estimate:.2f} approved · waiting to start</p>{progress_bar}</div>")
        details = f"{new_count} new · {reused} reused · up to ${estimate:.2f}"
        return ("<div class='empty-state enrich-state'><div class='empty-mark'>2</div>"
                "<h2>Ready to enrich</h2>"
                f"<p>{details}</p>{progress_bar}"
                f"<button class='button button-primary' data-approve-enrichment>Approve ${estimate:.2f}</button></div>")
    if status in {"running", "submitted"}:
        return ("<div class='empty-state enrich-state'><div class='progress-spinner' aria-hidden='true'></div>"
                "<h2>Enriching contacts</h2>"
                f"<p>{completed} of {total} complete</p>{progress_bar}</div>")
    if status == "research_complete":
        return ("<div class='empty-state enrich-state'><div class='progress-spinner' aria-hidden='true'></div>"
                "<h2>Building profiles</h2>"
                f"<p>{completed} of {total} researched</p>{progress_bar}</div>")
    if status in {"failed", "completed_with_errors"}:
        return ("<div class='empty-state enrich-state'><div class='empty-mark'>!</div>"
                "<h2>Enrichment paused</h2>"
                f"<p>{failed} failed · {completed} complete</p>{progress_bar}</div>")
    return ("<div class='empty-state enrich-state'><div class='empty-mark'>✓</div>"
            "<h2>Contacts enriched</h2>"
            f"<p>{completed} profiles ready</p>{progress_bar}"
            "<button class='button button-primary' data-complete='enrich'>Continue</button></div>")


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


def page_html(parents: list[dict[str, Any]], params: dict[str, list[str]],
              review_path: Path, *, parents_dir: Path = PARENTS_DIR,
              dossier_dir: Path = DOSSIER_DIR,
              manifest_path: Path = REVIEW_MANIFEST,
              enrichment_manifest_path: Path = ENRICH_MANIFEST) -> bytes:
    progress = review_progress(parents)
    selection = worth_selection_from_parents(parents, manifest_path=manifest_path)
    enrichment = read_enrichment_manifest(enrichment_manifest_path, selection=selection)
    review_manifest = read_review_manifest(manifest_path)
    state_token = review_state_token(progress, selection, enrichment, review_manifest)
    view = _phase_view(params, progress, manifest_path)
    preview = str((params.get("preview") or [""])[0]).strip() == "1"
    worth_complete = phase_is_completed("worth", progress, manifest_path) or not progress["worth_total"]
    enrichment_complete = enrichment.get("status") == "completed" and enrichment.get("current")
    enrichment_continued = enrichment_handoff_completed(manifest_path)
    linkedin_complete = (phase_is_completed("linkedin", progress, manifest_path)
                         or (view == "done" and not progress["linkedin_total"]))
    people_active = view == "worth"
    enrich_active = view == "enrich"
    linkedin_active = view in {"linkedin", "done"}

    if view == "worth":
        decision_view = str((params.get("view") or ["review"])[0]).strip().lower()
        decision_view = decision_view if decision_view in {"review", "yes", "no"} else "review"
        tabs = render_decision_tabs(progress, decision_view, preview=preview)
        if decision_view == "review":
            queue = [parent for parent in parents if needs_worth_review(parent)]
            if queue:
                queue.sort(key=lambda parent: str(parent.get("name") or "").lower())
                body = render_worth_card(queue[0], parents_dir, dossier_dir)
            else:
                body = ("<div class='empty-state phase-finish'><div class='empty-mark'>✓</div>"
                        "<h2>Decisions ready</h2>"
                        f"<p>{progress['lookup_ready']} people will be enriched</p>"
                        "<button class='button button-primary' data-complete='worth'>Continue</button></div>")
        else:
            try:
                decision_page = int(str((params.get("page") or ["1"])[0]))
            except ValueError:
                decision_page = 1
            body = render_decision_table(
                parents, decision_view, page=decision_page, preview=preview)
        content = f"<div class='worth-stage'>{tabs}<div class='worth-panel'>{body}</div></div>"
    elif view == "enrich":
        content = render_enrichment(enrichment, progress)
    elif view == "linkedin":
        if not enrichment_complete:
            content = ("<div class='empty-state enrich-state'><div class='empty-mark'>2</div>"
                       "<h2>Enrichment not finished</h2><p>Results will appear here when ready</p></div>")
        else:
            queue = [parent for parent in parents if pending_linkedin_candidates(parent)]
            if queue:
                queue.sort(key=lambda parent: str(parent.get("name") or "").lower())
                content = render_linkedin_card(
                    queue[0], pending_linkedin_candidates(queue[0])[0], parents_dir, dossier_dir)
            else:
                finish_button = ("" if linkedin_complete else
                                 "<button class='button button-primary' data-complete='linkedin'>Finish</button>")
                content = ("<div class='empty-state phase-finish'><div class='empty-mark'>✓</div>"
                           "<h2>LinkedIn profiles checked</h2>"
                           f"<p>{progress['linkedin_done']} decisions saved</p>"
                           f"{finish_button}</div>")
    else:
        content = ("<div class='empty-state done'><div class='empty-mark'>✓</div><h2>All set</h2>"
                   f"<p>{progress['linkedin_done']} identities checked · {progress['rejected']} rejected</p></div>")

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
<body data-stage='{esc(view)}' data-preview='{"true" if preview else "false"}' data-state-token='{esc(state_token)}' data-enrichment-status='{esc("approved" if enrichment.get("approval_current") else enrichment.get("status"))}'><div class='app-shell'>
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
                        profile_cache_dir: Path = PROFILE_CACHE_DIR) -> list[dict[str, Any]]:
    parents, overrides = build_parents(verdicts_path, review_path)
    extend_and_annotate(parents, overrides, synthetic_path, facts_dir,
                        load_connection_keys(people_csv), parents_dir=parents_dir,
                        dossier_dir=dossier_dir, profile_cache_dir=profile_cache_dir)
    annotate_sources(parents, load_people_sources(people_csv))
    return parents


def _manifest_for_review_path(review_path: Path) -> Path:
    try:
        if review_path.resolve() == LINKEDIN_OVERRIDES_CSV.resolve():
            return REVIEW_MANIFEST
    except (OSError, RuntimeError):
        pass
    return review_path.parent / "review" / "manifest.json"


def make_handler(review_path: Path, verdicts_path: Path, parents_dir: Path, dossier_dir: Path,
                 confirm_threshold: float, detach_threshold: float,
                 synthetic_path: Path = SYNTHETIC_PEOPLE_CSV,
                 facts_dir: Path = FACTS_DIR, people_csv: Path = DEFAULT_PEOPLE_CSV,
                 manifest_path: Path | None = None,
                 enrichment_manifest_path: Path = ENRICH_MANIFEST,
                 profile_cache_dir: Path = PROFILE_CACHE_DIR,
                 avatar_dir: Path | None = None):
    manifest_path = manifest_path or _manifest_for_review_path(review_path)
    avatar_dir = avatar_dir or manifest_path.parent / "avatars"
    mutation_lock = threading.Lock()

    def parents_now() -> list[dict[str, Any]]:
        return _all_review_parents(
            verdicts_path, review_path, synthetic_path, facts_dir, people_csv,
            parents_dir, dossier_dir, profile_cache_dir)

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
                status = workflow_status(
                    review_path=review_path, verdicts_path=verdicts_path,
                    synthetic_path=synthetic_path, facts_dir=facts_dir,
                    people_csv=people_csv, manifest_path=manifest_path,
                    enrichment_manifest_path=enrichment_manifest_path,
                    parents_dir=parents_dir, dossier_dir=dossier_dir,
                    profile_cache_dir=profile_cache_dir)
                self.send_json({
                    "primitive": "reconcile_review_web",
                    "ok": True,
                    "manifest": str(manifest_path),
                    "stage": browser_stage_for_next_action(status["next_action"]),
                    "next_action": status["next_action"],
                    "state_token": review_state_token(
                        status["progress"], status["selection"],
                        status["enrichment"], status["review_manifest"]),
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

            # Serialize the snapshot with decision writes. GET remains read-only:
            # stage activation and completion are explicit POST/CLI operations.
            with mutation_lock:
                parents = parents_now()
            self.send_bytes(page_html(parents, params, review_path, parents_dir=parents_dir,
                                      dossier_dir=dossier_dir, manifest_path=manifest_path,
                                      enrichment_manifest_path=enrichment_manifest_path))

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
                self.send_json({"ok": True, "enrichment": enrichment})
                return

            if parsed.path == "/complete":
                stage = (form.get("stage") or [""])[0].strip().lower()
                try:
                    with mutation_lock:
                        current_parents = parents_now()
                        progress = review_progress(current_parents)
                        if stage == "enrich":
                            selection = worth_selection_from_parents(
                                current_parents, manifest_path=manifest_path)
                            enrichment = read_enrichment_manifest(
                                enrichment_manifest_path, selection=selection)
                            manifest = write_enrichment_handoff(
                                enrichment, path=manifest_path,
                                review_path=review_path, synthetic_path=synthetic_path)
                        else:
                            manifest = write_review_manifest(
                                stage, "completed", progress, path=manifest_path,
                                review_path=review_path, synthetic_path=synthetic_path)
                except ValueError as exc:
                    self.send_bytes(str(exc).encode("utf-8"), "text/plain; charset=utf-8",
                                    status=409)
                    return
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
                        result = apply_worth_decision(review_path, pub, stored_worth)
                        gate = sync_synthetic_gate(synthetic_path, pub, stored_worth)
                        rows_now = load_override_rows(review_path)
                        conns = load_connection_keys(people_csv)
                        state = effective_no_for_key(
                            pub, rows_now, facts_dir,
                            keepish=(gate["approved"] == "yes") if gate else None,
                            connections=conns)
                        row_now = rows_now.get(pub.strip().lower()) or {}
                        decided = gate or {
                            "action": (row_now.get("action") or "").strip().lower(),
                            "approved": (row_now.get("approved") or "").strip().lower(),
                        }
                        current_parents = parents_now()
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
                except ValueError as exc:
                    self.send_bytes(str(exc).encode("utf-8"), "text/plain; charset=utf-8",
                                    status=400)
                    return
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
                    before = parents_now()
                    pub_lower = pub.strip().lower()
                    target_parent = next((parent for parent in before
                                          if any(str(cand.get("pub") or "").strip().lower() == pub_lower
                                                 for cand in parent.get("candidates") or [])), None)
                    if not target_parent:
                        raise ValueError(f"review row not found: {pub}")
                    actual_slug = str(target_parent.get("slug") or "")
                    if parent_slug and parent_slug != actual_slug:
                        raise ValueError("stale or mismatched person card")
                    if pub.strip().lower().startswith("synth-"):
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
                        else:
                            result = apply_synthetic_decision(synthetic_path, pub, decision)
                            keepish = result["approved"] == "yes"
                    else:
                        result = apply_decision(
                            review_path, verdicts_path, pub, decision, new_url,
                            confirm_threshold, detach_threshold)
                        worth_key, keepish = pub, None

                    # One affirmative answer resolves a multi-match person: other
                    # still-pending LinkedIns are link-level No decisions, never person rejects.
                    if decision in {"keep", "fix"}:
                        for sibling in target_parent.get("candidates") or []:
                            sibling_pub = str(sibling.get("pub") or "").strip().lower()
                            if (sibling_pub and sibling_pub != pub_lower
                                    and not sibling.get("synthetic")
                                    and candidate_state(sibling) == "review"):
                                apply_decision(
                                    review_path, verdicts_path, sibling_pub, "detach", "",
                                    confirm_threshold, detach_threshold)

                    conns = load_connection_keys(people_csv)
                    current_parents = parents_now()
                    progress = review_progress(current_parents)
                    invalidate_manifest("linkedin", progress)
                    payload: dict[str, Any] = {
                        "ok": True, "pub": pub, **result,
                        "counts": summarize(current_parents),
                        "progress": progress,
                    }
                    if worth_key:
                        state = effective_no_for_key(
                            worth_key, load_override_rows(review_path), facts_dir,
                            keepish=keepish, connections=conns)
                        payload.update({
                            "rejected": state["rejected"],
                            "effective": state["worth"]["decision"],
                            "source": state["worth"]["source"],
                        })
            except ValueError as exc:
                self.send_bytes(str(exc).encode("utf-8"), "text/plain; charset=utf-8",
                                status=400)
                return
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
    parents = _all_review_parents(verdicts_path, review_path, synthetic_path,
                                  Path(args.facts_dir), Path(args.people_csv))
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
    server = ThreadingHTTPServer((args.host, args.port),
                                 make_handler(review_path, verdicts_path, parents_dir, Path(args.dossier_dir),
                                              args.confirm_threshold, args.detach_threshold,
                                              synthetic_path=synthetic_path,
                                              facts_dir=Path(args.facts_dir),
                                              people_csv=Path(args.people_csv),
                                              manifest_path=manifest_path,
                                              enrichment_manifest_path=Path(args.enrichment_manifest),
                                              profile_cache_dir=Path(args.profile_cache_dir),
                                              avatar_dir=Path(args.avatar_dir)))
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
    """Read-only next-action contract for the agent's one-minute poll loop."""
    parents = _all_review_parents(
        verdicts_path, review_path, synthetic_path, facts_dir, people_csv,
        parents_dir, dossier_dir, profile_cache_dir)
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


def cmd_status(args: argparse.Namespace) -> None:
    print(json.dumps(workflow_status(
        review_path=Path(args.review), verdicts_path=Path(args.verdicts),
        synthetic_path=Path(args.synthetic_people), facts_dir=Path(args.facts_dir),
        people_csv=Path(args.people_csv), manifest_path=Path(args.manifest),
        enrichment_manifest_path=Path(args.enrichment_manifest),
    ), indent=2))


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
