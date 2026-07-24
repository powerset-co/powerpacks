"""Domain model construction and local profile/media loading for review."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import threading
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.candidates import (
    NETWORK_WORTH_VALUES,
    current_parent_by_person_id,
    effective_network_worth,
    is_candidate_id,
    llm_network_worth,
    load_candidates,
)
from packs.ingestion.primitives.deep_context import worth_view
from packs.ingestion.primitives.deep_context.common import (
    DEEP_RESEARCH_DIR,
    DOSSIER_DIR,
    FACTS_DIR,
    GMAIL_CHANNEL,
    IMESSAGE_CHANNEL,
    INDEX_JSON,
    LINKEDIN_OVERRIDES_CSV,
    PARENTS_DIR,
    PROFILE_CACHE_DIR,
    REVIEW_DIR,
    WHATSAPP_CHANNEL,
    normalize_email,
    normalize_phone,
    parse_list,
    read_jsonl,
    slugify,
)
from packs.ingestion.primitives.deep_context.reconcile_linkedin import (
    USER_APPROVED,
    linkedin_view,
    load_override_rows,
    load_people_rows,
    union_child_contacts,
)
from packs.ingestion.schemas.people_schema import (
    extract_public_identifier,
    merge_interaction_counts,
)


APPLIED_APPROVED = {"auto", "yes"}


VALID_TABS = {"all", "review", "verified", "detached", "conflict", "fixed", "excluded", "decided", "rejected"}


VALID_STAGES = {"worth", "enrich", "linkedin", "done"}


USER_WORTH_VALUES = {"yes", "no"}


AVATAR_DIR = REVIEW_DIR / "avatars"


CHANNEL_TO_SOURCE = {GMAIL_CHANNEL: "gmail", IMESSAGE_CHANNEL: "imessage", WHATSAPP_CHANNEL: "whatsapp"}


SOURCE_FILTERS = ("gmail", "imessage", "whatsapp")


def _primary_candidate(parent: dict[str, Any]) -> dict[str, Any]:
    candidates = parent.get("candidates") or []
    return min(candidates, key=_cand_rank) if candidates else {}



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


def _worth_key(parent: dict[str, Any]) -> str:
    primary = _primary_candidate(parent)
    return str(primary.get("worth_key") or (parent.get("person_ids") or [""])[0] or "")


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
