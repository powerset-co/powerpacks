#!/usr/bin/env python3
"""[Phase 3, review] Local web UI to review the LinkedIn self-heal at scale.

`review.csv` is keyed only on the candidate LinkedIn (`public_identifier`), so on
its own it's un-reviewable — you can't see which canonical PERSON ("parent") a link
belongs to, the other links considered, or why the model picked one. The parent
structure + profile + the judge's reasoning live in `reconcile/verdicts.jsonl`.

This primitive JOINS the two (plus the per-parent dossier in `parents/*.md`) and
serves a parent-grouped table:

  * one row per PARENT (canonical person), expandable to its candidate LinkedIn(s),
  * each candidate shows the profile we matched, the picked link, confidence, and the
    model's supporting / contradicting reasoning,
  * per-candidate actions autosave straight into `review.csv`:
        Keep   -> action=verify   approved=yes
        Detach -> action=detach   approved=yes
        Fix…   -> action=retarget approved=yes  (paste the correct LinkedIn URL)
  * quick filters (needs-review / verified / detached / conflicts / fixed / my
    decisions), search, and a risk sort that floats the lowest-confidence parents up,
  * dossier-bearing import candidates (no LinkedIn at all yet) surface as review rows
    BEFORE any paid research, plus source (gmail/imessage/whatsapp) and worth filter
    chips,
  * EVERY row (verdict, candidate, synthetic) carries a Yes/Maybe/No "worth adding?"
    mark that writes the user-owned `network_worth` column in review.csv. The Rejected
    tab is the ONE unified effective-no grouping — a user no (worth mark or approved
    Exclude), or a machine no (reconcile's `llm_worth`, incl. the spam screen) with no
    user rescue (worth-Yes or a keep-ish Keep) — and mirrors what the fan-in merge
    drops from the searchable network.

It only reads the deep-context artifacts and writes `review.csv` (the same durable
table the fan-in merge re-applies). A `Fix…` decision is enriched + re-attached later
by `apply-retargets` + `realize`. No spend, local only.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.candidates import (
    NETWORK_WORTH_VALUES,
    effective_network_worth,
    load_candidates,
)
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    DOSSIER_DIR,
    FACTS_DIR,
    GMAIL_CHANNEL,
    IMESSAGE_CHANNEL,
    LINKEDIN_OVERRIDES_CSV,
    PARENTS_DIR,
    PROFILE_CACHE_DIR,
    VERDICTS_JSONL,
    WHATSAPP_CHANNEL,
    now_iso,
    read_jsonl,
    slugify,
)
from packs.ingestion.primitives.deep_context.reconcile_linkedin import (
    DEFAULT_CONFIRM,
    DEFAULT_DETACH,
    OVERRIDE_COLUMNS,
    USER_APPROVED,
    _VERDICT_TO_ACTION,
    _write_override_rows,
    load_override_rows,
)
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url

APPLIED_APPROVED = {"auto", "yes"}
VALID_TABS = {"all", "review", "verified", "detached", "conflict", "fixed", "excluded", "decided", "rejected"}

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


def _cached_profile_pic(pub: str) -> str:
    """Best-effort LinkedIn avatar URL from the local RapidAPI profile cache.

    Lets older verdicts.jsonl (written before linkedin_view captured the avatar)
    still render pictures without a re-judge. Cache files are named <pub>.json.
    """
    pub = (pub or "").strip().lower()
    if not pub:
        return ""
    path = PROFILE_CACHE_DIR / f"{pub}.json"
    if not path.exists():
        return ""
    try:
        np = (json.loads(path.read_text(encoding="utf-8")) or {}).get("normalized_profile") or {}
    except (json.JSONDecodeError, OSError):
        return ""
    return str(np.get("profile_pic_url") or "")


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


def load_synthetic_parents(path: Path) -> list[dict[str, Any]]:
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
            parents.append({
                "slug": f"synthetic-{pub}", "name": name, "person_ids": [row.get("id") or pub],
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


def load_candidate_parents(facts_dir: Path, overrides: dict[str, dict[str, str]],
                           shown_person_ids: set[str]) -> list[dict[str, Any]]:
    """Dossier-bearing import candidates (facts exist, NO research/synthetic result yet)
    as review rows — they ARE the needs-review pile of the candidate flow: mark worth
    (Yes/Maybe/No) BEFORE any paid research. The review.csv key for a candidate is its
    person_id; Fix… retargets it, Exclude keeps it out of research. Candidates already
    shown via a synthetic/retarget row are deduped by person_id."""
    parents: list[dict[str, Any]] = []
    for person in load_candidates():
        pid = person.person_id
        if pid.lower() in shown_person_ids or not (facts_dir / f"{pid}.jsonl").exists():
            continue
        name = _facts_canonical_name(facts_dir, pid) or person.full_name or pid
        dec = overrides.get(pid.lower(), {})
        parents.append({
            "slug": slugify(name, pid),  # the composed CHILD dossier's slug (no parent stub needed)
            "name": name,
            "person_ids": [pid],
            "sources": _sources_of(person.source_channels),
            "candidates": [{
                "pub": pid,  # candidates key review.csv on their person_id
                "url": "", "full_name": name,
                "headline": "",
                "profile_pic_url": "",
                "experiences": [], "education": [], "location": "",
                "has_profile": False,
                "verdict": "no_linkedin_candidate",
                "confidence": 0.0,
                "supporting": [], "contradicting": [],
                "reason": "unresolved import candidate — no LinkedIn attached yet",
                "plausibly_absent": False, "recommend_dr": False,
                "match_emails": person.emails, "match_phones": person.phones,
                "conflict": False, "import_candidate": True,
                "action": dec.get("action", ""),
                "approved": (dec.get("approved") or "").strip().lower(),
                "new_url": dec.get("new_linkedin_url", ""),
                "llm_reject": (dec.get("llm_reject") or "").strip().lower(),
                "llm_reject_confidence": dec.get("llm_reject_confidence", ""),
                "llm_reject_reason": dec.get("llm_reject_reason", ""),
            }],
        })
    return parents


def annotate_worth(parents: list[dict[str, Any]], overrides: dict[str, dict[str, str]],
                   facts_dir: Path) -> None:
    """Attach the effective network-worth (user review.csv mark / approved exclude >
    synthesis LLM > review.csv llm_worth > default 'maybe') to EVERY row — verdict,
    candidate, and synthetic alike — plus the machine's own view (for the secondary
    text and the unified Rejected grouping). The mark's review.csv key is the primary
    candidate's LinkedIn pub for verdict rows, else the row's person_id."""
    for p in parents:
        cands = p["candidates"]
        if not cands:
            continue
        primary = min(cands, key=_cand_rank) if len(cands) > 1 else cands[0]
        key = ""
        if not (primary.get("synthetic") or primary.get("import_candidate")):
            key = (primary.get("pub") or "").strip()
        key = key or (p["person_ids"] or [""])[0]
        if not key:
            continue
        state = effective_no_for_key(key, overrides, facts_dir)
        p["worth"], p["machine_worth"] = state["worth"], state["machine"]
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
                         facts_dir: Path, *, keepish: bool | None = None) -> dict[str, Any]:
    """Single-row mirror of is_effective_no (the unified Rejected / merge-drop rule)
    for one review.csv key: {'worth', 'machine', 'rejected'}. `keepish` overrides the
    rescue signal when it lives outside review.csv (the synthetic approved gate)."""
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
    machine_no = machine["decision"] == "no" or (row.get("llm_reject") or "").strip().lower() == "spam"
    rejected = user_mark == "no" or (user_mark != "yes" and machine_no and not keepish)
    return {"worth": worth, "machine": machine, "rejected": rejected}


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
        v = r.get("verdict") or {}
        li = r.get("linkedin") or {}
        dec = overrides.get(pub, {})
        p["candidates"].append({
            "pub": pub,
            "url": li.get("linkedin_url", ""),
            "full_name": li.get("full_name", ""),
            "headline": li.get("headline", ""),
            "profile_pic_url": li.get("profile_pic_url") or _cached_profile_pic(pub),
            "experiences": li.get("experiences") or [],
            "education": li.get("education") or [],
            "location": li.get("location", ""),
            "has_profile": li.get("has_profile", False),
            "verdict": v.get("verdict", ""),
            "confidence": float(v.get("confidence") or 0.0),
            "supporting": v.get("supporting_evidence") or [],
            "contradicting": v.get("contradicting_evidence") or [],
            "reason": v.get("reason", ""),
            "plausibly_absent": bool(v.get("linkedin_plausibly_absent")),
            "recommend_dr": bool(v.get("recommend_deep_research")),
            "match_emails": r.get("match_emails") or [],
            "match_phones": r.get("match_phones") or [],
            "conflict": bool(r.get("conflict")),
            # current decision (from review.csv)
            "action": dec.get("action", ""),
            "approved": (dec.get("approved") or "").strip().lower(),
            "new_url": dec.get("new_linkedin_url", ""),
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
                        synthetic_path: Path, facts_dir: Path) -> list[dict[str, Any]]:
    """Add the synthetic + pre-research candidate rows to the verdict parents and
    annotate everyone's worth — the full row set page_html/summarize operate on."""
    parents.extend(load_synthetic_parents(synthetic_path))
    shown = {pid.lower() for p in parents for pid in p["person_ids"]}
    parents.extend(load_candidate_parents(facts_dir, overrides, shown))
    annotate_worth(parents, overrides, facts_dir)
    return parents


def live_counts(verdicts_path: Path, review_path: Path, synthetic_path: Path,
                facts_dir: Path) -> dict[str, int]:
    """Fresh GLOBAL tab counts after a mutation. Every POST returns these so the client
    repaints the header stats and tab pills authoritatively — recomputing counts from
    the DOM would drift on filtered views (only the visible subset is in the DOM)."""
    parents, overrides = build_parents(verdicts_path, review_path)
    return summarize(extend_and_annotate(parents, overrides, synthetic_path, facts_dir))


# --- decision writer (the only mutation: upsert one row in review.csv) ------

def apply_decision(review_path: Path, verdicts_path: Path, pub: str, decision: str,
                   new_url: str, confirm_threshold: float, detach_threshold: float | None = None) -> dict[str, str]:
    """Upsert a single decision into review.csv (keyed by public_identifier)."""
    pub = (pub or "").strip().lower()
    rows = load_override_rows(review_path)
    row = rows.get(pub) or {k: "" for k in OVERRIDE_COLUMNS}
    row["public_identifier"] = pub
    if decision == "keep":
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


# --- rendering --------------------------------------------------------------

def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


STATUS_LABEL = {"review": "needs review", "verified": "verified", "detached": "detached",
                "fixed": "verified", "excluded": "excluded"}


# Direction-aware judgment line: the confidence is confidence IN THE VERDICT, so a
# high-% wrong_person is a STRONG MISMATCH, not a strong match. Spell that out.
def judgment_line(verdict: str, confidence: float) -> str:
    pct = f"{confidence * 100:.0f}%"
    if verdict == "confirmed":
        return f"<div class='judgment j-confirmed'>✓ Looks like the same person · <b>{pct} sure</b></div>"
    if verdict == "wrong_person":
        return (f"<div class='judgment j-wrong'>✗ Looks like a <b>different</b> person · "
                f"<b>{pct} sure it's the wrong one</b></div>")
    return f"<div class='judgment j-review'>? Not enough to tell · {pct} sure either way</div>"


# For merged (multi-candidate) people: rank the kept / most-confident LinkedIn first so it
# floats to the top as the "parent", and collapse the high-confidence wrong matches.
WRONG_COLLAPSE_CONF = 0.8  # judge ≥80% sure it's the wrong person → hide behind the expander


def _cand_rank(cand: dict[str, Any]) -> tuple[int, float]:
    st = candidate_state(cand)
    if st in ("verified", "fixed"):
        return (0, -cand["confidence"])
    if st in ("detached", "excluded", "rejected"):
        return (4, -cand["confidence"])
    verdict = cand["verdict"]
    if verdict == "confirmed":
        return (1, -cand["confidence"])
    if verdict == "wrong_person":
        return (3, -cand["confidence"])
    return (2, -cand["confidence"])  # needs_review / unknown


def _is_collapsible_wrong(cand: dict[str, Any]) -> bool:
    """A candidate we can hide by default: a keeper we never collapse; otherwise collapse it if
    it's already resolved-wrong (detached/rejected/excluded) or the judge is ≥80% sure it's the
    wrong person. The user keeps the option to expand and act on it."""
    st = candidate_state(cand)
    if st in ("verified", "fixed"):
        return False
    if st in ("detached", "rejected", "excluded"):
        return True
    return cand["verdict"] == "wrong_person" and cand["confidence"] >= WRONG_COLLAPSE_CONF


def render_candidate(idx: int, total: int, cand: dict[str, Any], *,
                     show_exclude: bool = True, parent_label: bool = False,
                     using_parent: bool = False) -> str:
    st = candidate_state(cand)
    option = f"<span class='opt'>Option {idx + 1} of {total}</span>" if total > 1 else ""
    badge = "<span class='parentbadge' title='the LinkedIn we keep for this person'>parent</span>" if parent_label else ""
    if using_parent:
        badge += ("<span class='usingparent' title=\"Detaching this LinkedIn does NOT lose the email/phone "
                  "— it stays on this person, under the parent LinkedIn above\">✓ email kept on this person</span>")
    if cand.get("synthetic"):
        judgment = (f"<div class='judgment j-review'>Researched profile · "
                    f"{cand['confidence'] * 100:.0f}% complete — keep to make them searchable</div>")
    elif cand.get("import_candidate"):
        judgment = ("<div class='judgment j-review'>Unresolved contact — no LinkedIn attached yet. "
                    "Mark whether they're worth adding; Yes/Maybe stay eligible for research.</div>")
    else:
        judgment = judgment_line(cand["verdict"], cand["confidence"])
    profile = []
    if cand["headline"]:
        profile.append(f"<div class='hl'>{esc(cand['headline'])}</div>")
    if cand["location"]:
        profile.append(f"<div class='loc'>{esc(cand['location'])}</div>")
    if cand["experiences"]:
        exp = "".join(f"<li>{esc(e)}</li>" for e in cand["experiences"])
        profile.append(f"<ul class='exp'>{exp}</ul>")
    if cand["education"]:
        profile.append(f"<div class='edu'>🎓 {esc('; '.join(cand['education']))}</div>")
    if not cand["has_profile"]:
        profile.append("<div class='loc'>no enriched profile on file</div>")
    evid = []
    if cand["supporting"]:
        evid.append("<div class='ev good'><span>supports</span><ul>"
                    + "".join(f"<li>{esc(x)}</li>" for x in cand["supporting"]) + "</ul></div>")
    if cand["contradicting"]:
        evid.append("<div class='ev bad'><span>contradicts</span><ul>"
                    + "".join(f"<li>{esc(x)}</li>" for x in cand["contradicting"]) + "</ul></div>")
    contacts = " · ".join(filter(None, [", ".join(cand["match_emails"]), ", ".join(cand["match_phones"])]))
    flags = []
    if cand["plausibly_absent"]:
        flags.append("<span class='flag'>may have no LinkedIn</span>")
    if cand["recommend_dr"]:
        flags.append("<span class='flag'>deep-research suggested</span>")
    if cand.get("llm_reject") == "spam":
        conf = cand.get("llm_reject_confidence") or ""
        pct = f" {float(conf) * 100:.0f}%" if conf else ""
        flags.append(f"<span class='flag flag-spam' title='{esc(cand.get('llm_reject_reason') or '')}'>"
                     f"🚫 spam-flagged{pct} — won't be indexed unless you keep them</span>")
    if cand.get("synthetic"):
        flags.append("<span class='flag flag-synth' title='Deep-researched profile — this person has no "
                     "real LinkedIn. Keep = merge into your searchable network; Detach = discard.'>"
                     "synthetic — no LinkedIn</span>")
    if cand.get("import_candidate"):
        flags.append("<span class='flag flag-synth' title='An imported contact we could not resolve to a "
                     "LinkedIn yet. Mark their worth, paste the right LinkedIn if you know it, or exclude.'>"
                     "candidate — no LinkedIn</span>")
    fixed_note = f"<div class='fixednote'>✓ verified — corrected LinkedIn: <a href='{esc(cand['new_url'])}' target='_blank' rel='noreferrer'>{esc(cand['new_url'])}</a></div>" if st == "fixed" and cand["new_url"] else ""
    pic = cand.get("profile_pic_url") or ""
    avatar = (f"<img class='avatar' src='{esc(pic)}' alt='' loading='lazy' referrerpolicy='no-referrer' "
              f"onerror='this.style.display=&quot;none&quot;'>" if pic else "<span class='avatar avatar-empty'></span>")
    if cand.get("synthetic"):
        ident = f"<span class='nopick'>{esc(cand['pub'])} (researched — no LinkedIn)</span>"
    elif cand.get("import_candidate"):
        ident = "<span class='nopick'>no LinkedIn attached yet</span>"
    else:
        ident = f"<a href='{esc(cand['url'])}' target='_blank' rel='noreferrer'>{esc(cand['url'] or cand['pub'])}</a>"
    # No LinkedIn is attached to an import candidate, so Keep/Detach have nothing to act
    # on — worth marks, Fix… (paste the right LinkedIn) and Exclude are the actions.
    keep_detach = "" if cand.get("import_candidate") else (
        f"<button class='btn keep{' suggested' if parent_label and st == 'review' else ''}' data-act='keep'>Keep this LinkedIn</button>"
        f"<button class='btn detach{' suggested' if using_parent and st == 'review' else ''}' data-act='detach'>Detach (wrong person)</button>")
    return f"""
    <div class='cand cand-{st}{" cand-parent" if parent_label else ""}' data-pub='{esc(cand['pub'])}'>
      <div class='cand-top'>
        {avatar}
        <div class='cand-id'>
          {badge}{option}
          {ident}
          {''.join(flags)}
          {f"<div class='contacts'><strong>from your messages</strong> <b class='cval'>{esc(contacts)}</b></div>" if contacts else ""}
        </div>
        <div class='cand-state state-{st}'>{esc(STATUS_LABEL.get(st, st))}</div>
      </div>
      {judgment}
      <div class='cand-body'>
        <div class='profile'>{''.join(profile) or "<div class='loc'>—</div>"}</div>
        <div class='col-evidence'>
          <div class='reason'>{esc(cand['reason'])}</div>
          {f"<div class='evidence'>{''.join(evid)}</div>" if evid else ""}
        </div>
      </div>
      {fixed_note}
      {render_worth_row(cand)}
      <div class='actions'>
        {keep_detach}
        <span class='fixwrap'><input class='fixurl' placeholder='paste correct LinkedIn URL'>
          <button class='btn fix' data-act='fix'>Fix</button></span>
        {"<button class='btn exclude' data-act='exclude-person' title=\"Don't index this person at all — drops them from people.csv (whole person, all LinkedIns)\">✕ Exclude person</button>" if show_exclude else ""}
        <button class='btn reset' data-act='reset' title='revert to the model decision'>↺</button>
      </div>
    </div>"""


def render_worth_row(cand: dict[str, Any]) -> str:
    """Yes/Maybe/No network-worth marks — on EVERY row type (verdict, candidate,
    synthetic). Highlights the EFFECTIVE value; the machine's own decision + reason
    (the synthesis judgment, or the spam screen's reason) shows alongside. ↺ clears
    the user's mark (back to the machine's judgment)."""
    worth = cand.get("worth")
    if not worth or not cand.get("worth_key"):
        return ""
    dec = (worth.get("decision") or "").lower()
    buttons = "".join(
        f"<button class='btn worth worth-{v}{' on' if v == dec else ''}' data-worth='{v}'>{v.capitalize()}</button>"
        for v in NETWORK_WORTH_VALUES)
    machine = cand.get("machine_worth") or {}
    hint = (f"<span class='worth-llm'>LLM: {esc(machine.get('decision'))} — {esc(machine.get('reason') or '')}</span>"
            if machine.get("source") == "llm" else "")
    return (f"<div class='actions worthrow' data-worthkey='{esc(cand['worth_key'])}'>"
            f"<span class='worth-label'>Worth adding?</span>{buttons}"
            f"<button class='btn reset worth-clear' data-worth='' title='clear your mark — back to the LLM call'>↺</button>"
            f"{hint}</div>")


def auto_resolve_merged(parents: list[dict[str, Any]], review_path: Path,
                        confirm_threshold: float) -> int:
    """Minimize clicks on merged (multi-LinkedIn) people: machine-apply the obvious decisions
    as approved='auto' so a human only overturns mistakes instead of confirming the obvious.
    - Every non-parent LinkedIn still undecided -> detach (its email stays on the person).
    - The ranked-first 'parent' -> verify, but ONLY when the judge is confident it's the same
      person (verdict confirmed at/above the confirm bar). Unclear parents stay needs-review.
    Never touches user decisions (approved yes/no). Idempotent: decided rows are skipped."""
    changed = 0
    rows = load_override_rows(review_path)
    for parent in parents:
        cands = parent["candidates"]
        if len(cands) < 2:
            continue
        ordered = sorted(cands, key=_cand_rank)
        for i, cand in enumerate(ordered):
            if candidate_state(cand) != "review":
                continue
            pub = (cand.get("pub") or "").strip().lower()
            if not pub:
                continue
            if i == 0:
                if not (cand.get("verdict") == "confirmed" and cand.get("confidence", 0.0) >= confirm_threshold):
                    continue
                action = "verify"
            else:
                action = "detach"
            row = rows.get(pub) or {k: "" for k in OVERRIDE_COLUMNS}
            row["public_identifier"] = pub
            row["action"], row["approved"] = action, "auto"
            row["new_linkedin_url"], row["new_public_identifier"] = "", ""
            rows[pub] = row
            cand["action"], cand["approved"] = action, "auto"
            changed += 1
    if changed:
        _write_override_rows(review_path, rows)
    return changed


def render_parent(idx: int, parent: dict[str, Any], expanded: bool) -> str:
    status = parent_status(parent)
    picked = picked_link(parent)
    cands_list = parent["candidates"]
    n_cand = len(cands_list)
    n_people = len(parent["person_ids"])
    conf = f"{min_confidence(parent) * 100:.0f}%"
    decided = " decided" if is_decided(parent) else ""
    multi = n_cand > 1
    picked_html = (f"<a href='{esc(picked)}' target='_blank' rel='noreferrer'>{esc(picked)}</a>"
                   if picked else "<span class='nopick'>no link chosen</span>")
    # Collapse by default; only honor an explicit expand request. A multi-candidate
    # (Merged) row no longer force-opens — the user wants those collapsed first.
    open_attr = " open" if expanded else ""
    # Merged people: float the kept / most-confident LinkedIn to the top (label it
    # "parent"), and tuck the high-confidence wrong matches behind an expander so the
    # default view shows just the one we keep.
    ordered = sorted(cands_list, key=_cand_rank) if multi else list(cands_list)
    shown = [c for c in ordered if not _is_collapsible_wrong(c)]
    collapsed = [c for c in ordered if _is_collapsible_wrong(c)]
    if not shown:  # nothing confident to keep — show them all rather than hide everything
        shown, collapsed = ordered, []
    if multi:
        cand_html = "".join(
            render_candidate(i, n_cand, c, show_exclude=False, parent_label=(i == 0),
                             using_parent=(i != 0))
            for i, c in enumerate(shown))
        if collapsed:
            wrong = "".join(render_candidate(0, 1, c, show_exclude=False, using_parent=True)
                            for c in collapsed)
            n_wrong = len(collapsed)
            cand_html += (f"<details class='wrongones'><summary>show {n_wrong} likely-wrong "
                          f"match{'es' if n_wrong != 1 else ''}</summary>{wrong}</details>")
        exclude_top = ("<div class='parent-actions'><button class='btn exclude exclude-top' "
                       "data-act='exclude-person' title=\"Drop this whole person from people.csv — "
                       f"all {n_cand} LinkedIns, won't be indexed\">✕ Exclude this whole person</button></div>")
    else:
        cand_html = "".join(render_candidate(i, n_cand, c) for i, c in enumerate(shown))
        exclude_top = ""
    cands = cand_html
    banner = (f"<div class='conflictbanner'>⚠ Multiple emails — {n_cand} different LinkedIns matched "
              f"this one person. We keep the parent and detach the rest automatically (every email "
              f"stays on this person) — overturn below if we picked wrong.</div>" if multi else "")
    # Clarify which dossier this is: it's the whole person's combined message history, not
    # tied to any single LinkedIn candidate (that confusion is what the label fixes).
    doss_label = "show message dossier — all messages for this person"
    if n_people > 1:
        doss_label += f" ({n_people} clusters)"
    parent_cls = "parent multi" if multi else "parent"
    if is_effective_no(parent):
        parent_cls += " worthno"
    summary_pic = next((c.get("profile_pic_url") for c in cands_list if c.get("profile_pic_url")), "")
    summary_avatar = (f"<img class='avatar avatar-sm' src='{esc(summary_pic)}' alt='' loading='lazy' "
                      f"referrerpolicy='no-referrer' onerror='this.style.display=&quot;none&quot;'>"
                      if summary_pic else "")
    return f"""
    <details class='{parent_cls} p-{status}{decided}' data-slug='{esc(parent['slug'])}' data-idx='{idx}'{open_attr}>
      <summary>
        <span class='chip chip-{status}'>{esc(STATUS_LABEL.get(status, status))}</span>
        {summary_avatar}
        <span class='pname'>{esc(parent['name'])}</span>
        {"<span class='multibadge'>" + str(n_cand) + " LinkedIns</span><span class='usingparent usingparent-sm' title='All this person&#39;s emails/phones stay together — one LinkedIn is kept, the rest get detached'>✓ " + str(n_cand) + " accounts linked, keeps 1</span>" if multi else f"<span class='picked'>{picked_html}</span>"}
        <span class='pmeta'>{n_people} message cluster{'s' if n_people != 1 else ''} · conf {conf}</span>
      </summary>
      <div class='pbody'>
        {exclude_top}
        {banner}
        {cands}
        <details class='dossier' data-slug='{esc(parent['slug'])}'>
          <summary>{doss_label}</summary>
          <pre class='dosstext'>loading…</pre>
        </details>
      </div>
    </details>"""


def page_html(parents: list[dict[str, Any]], params: dict[str, list[str]],
              review_path: Path) -> bytes:
    tab = (params.get("tab") or ["review"])[0].strip().lower()  # default to the Needs-review pile
    if tab not in VALID_TABS:
        tab = "review"
    q = (params.get("q") or [""])[0].strip()
    sort = (params.get("sort") or ["risk"])[0].strip().lower()
    source = (params.get("source") or ["all"])[0].strip().lower()
    worth = (params.get("worth") or ["all"])[0].strip().lower()
    summary = summarize(parents)

    visible = [p for p in parents if parent_in_tab(p, tab) and parent_matches_query(p, q.lower())
               and parent_matches_source(p, source) and parent_matches_worth(p, worth)]
    if sort == "name":
        visible.sort(key=lambda p: p["name"].lower())
    elif sort == "conf":
        visible.sort(key=min_confidence, reverse=True)
    else:  # risk: pending first, then lowest confidence
        visible.sort(key=lambda p: (parent_status(p) != "review", min_confidence(p)))

    def tab_href(name: str) -> str:
        # Always set tab explicitly — "/" with no param now defaults to the review pile.
        qp = {k: v[0] for k, v in params.items() if k not in {"tab"} and v and v[0]}
        qp["tab"] = name
        return "/?" + urllib.parse.urlencode(qp)

    def tab_link(name: str, label: str, key: str) -> str:
        klass = "tab active" if tab == name else "tab"
        return (f"<a class='{klass}' href='{esc(tab_href(name))}'><span>{esc(label)}</span>"
                f"<strong data-tabcount='{esc(key)}'>{summary[key]}</strong></a>")

    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>Deep Context · LinkedIn Self-Heal Review</title>",
        "<style>", CSS, "</style></head><body><div class='wrap'>",
        "<header><div>",
        "<h1>LinkedIn self-heal review</h1>",
        f"<div class='meta'>{esc(review_path)} · {len(visible)} of {len(parents)} people · "
        "each row is a person; expand to see the LinkedIn(s) we matched, the reasoning, and to keep / detach / fix. "
        "Every change autosaves.</div>",
        "</div><div class='stats'>",
        f"<div class='stat'><span>needs review</span><strong data-count='review'>{summary['review']}</strong></div>",
        f"<div class='stat'><span>verified</span><strong data-count='verified'>{summary['verified']}</strong></div>",
        f"<div class='stat'><span>excluded</span><strong data-count='excluded'>{summary['excluded']}</strong></div>",
        f"<div class='stat'><span>you decided</span><strong data-count='decided'>{summary['decided']}</strong></div>",
        "</div></header>",
        "<nav class='tabs'>",
        tab_link("conflict", "Multiple emails", "conflict"),
        tab_link("review", "Needs review", "review"),
        tab_link("rejected", "Rejected", "rejected"),
        tab_link("excluded", "Excluded", "excluded"),
        tab_link("all", "All", "total"),
        "</nav>",
        "<form class='filters' method='get' action='/'>",
        f"<input type='hidden' name='tab' value='{esc(tab)}'>",
        f"<input type='hidden' name='source' value='{esc(source)}'>" if source in SOURCE_FILTERS else "",
        f"<input type='hidden' name='worth' value='{esc(worth)}'>" if worth in NETWORK_WORTH_VALUES else "",
        f"<input name='q' placeholder='Search name, company, email, LinkedIn' value='{esc(q)}'>",
        "<select name='sort'>",
    ]
    for value, label in [("risk", "riskiest first"), ("conf", "most confident first"), ("name", "name A–Z")]:
        sel = " selected" if sort == value else ""
        parts.append(f"<option value='{value}'{sel}>{esc(label)}</option>")
    parts.append("</select><button type='submit'>Apply</button><a class='clear' href='/'>clear</a></form>")

    def chip_bar(label: str, param: str, options: list[str], current: str) -> str:
        def href(value: str) -> str:
            qp = {k: v[0] for k, v in params.items() if k != param and v and v[0]}
            if value != "all":
                qp[param] = value
            return "/?" + urllib.parse.urlencode(qp)
        chips = "".join(
            f"<a class='fchip{' active' if current == o or (o == 'all' and current not in options) else ''}'"
            f" href='{esc(href(o))}'>{esc(o)}</a>"
            for o in ["all", *options])
        return f"<div class='chips'><span class='chips-label'>{esc(label)}</span>{chips}</div>"

    # Source chips only when the rendered rows actually span >1 message source;
    # worth chips only when there are candidate/synthetic rows to mark.
    sources_present = sorted({s for p in parents for s in (p.get("sources") or [])})
    if len(sources_present) > 1:
        parts.append(chip_bar("source", "source", sources_present, source))
    if any(p.get("worth") for p in parents):
        parts.append(chip_bar("worth adding", "worth", list(NETWORK_WORTH_VALUES), worth))

    if not visible:
        parts.append("<div class='empty'>Nothing matches this view.</div>")
    else:
        # Expand automatically only on the already-resolved "fixed" pile. The Merged
        # (conflict) tab stays collapsed first so the user scans the list, then opens rows.
        expand = tab == "fixed" and len(visible) <= 40
        parts.append("<section class='list'>")
        idx_by_slug = {p["slug"]: i for i, p in enumerate(parents)}
        for p in visible[:600]:
            parts.append(render_parent(idx_by_slug[p["slug"]], p, expand))
        parts.append("</section>")
        if len(visible) > 600:
            parts.append("<p class='muted'>Showing first 600. Narrow the filter to see more.</p>")

    parts.append("<div id='toast' class='toast'>Saved</div>")
    parts.append("<script>" + JS + "</script>")
    parts.append("</div></body></html>")
    return "".join(parts).encode("utf-8")


CSS = """
:root{color-scheme:light;--bg:#f6f7f9;--panel:#fff;--line:#d7dde5;--text:#17202a;--muted:#5b6876;--soft:#eef1f5;
--ok:#0f766e;--okbg:#e6f7f3;--bad:#b42318;--badbg:#fde7e3;--warn:#b25e00;--warnbg:#fff2dc;--fix:#5b3da8;--fixbg:#f0eafb}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;margin:0;color:var(--text);background:var(--bg)}
.wrap{max-width:1180px;margin:0 auto;padding:26px 22px 60px}
header{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:16px}
h1{font-size:23px;margin:0 0 6px;font-weight:700}
.meta{color:var(--muted);font-size:13px;line-height:1.45;max-width:760px;overflow-wrap:anywhere}
.stats{display:grid;grid-template-columns:repeat(4,minmax(86px,1fr));gap:8px;min-width:380px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.stat span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.stat strong{display:block;font-size:20px;margin-top:2px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 12px;border-bottom:1px solid var(--line)}
.tab{display:flex;align-items:center;gap:7px;text-decoration:none;color:var(--muted);padding:8px 12px;border:1px solid transparent;border-bottom:0;border-radius:8px 8px 0 0;font-size:13px}
.tab strong{font-size:12px;color:var(--text);background:var(--soft);border-radius:999px;padding:2px 7px}
.tab.active{color:var(--text);background:var(--panel);border-color:var(--line);margin-bottom:-1px}
form.filters{display:flex;gap:8px;flex-wrap:wrap;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:11px;margin-bottom:14px}
input,select{font:inherit;border:1px solid #b8c1cc;border-radius:6px;padding:7px 8px;background:#fff;color:var(--text)}
.filters input[name=q]{min-width:300px;flex:1}
button{font:inherit;border:1px solid #17202a;background:#17202a;color:#fff;border-radius:6px;padding:7px 12px;cursor:pointer}
button:hover{background:#2a3642}
.clear{display:inline-flex;align-items:center;color:var(--muted);text-decoration:none;padding:0 6px}
.empty{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:24px;color:var(--muted)}
.list{display:flex;flex-direction:column;gap:8px}
.parent{background:var(--panel);border:1px solid var(--line);border-radius:9px;overflow:hidden}
.parent[open]{box-shadow:0 2px 10px rgba(15,23,42,.06)}
.parent.decided{border-color:#9bd3c8}
summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:11px;padding:11px 14px}
summary::-webkit-details-marker{display:none}
.parent>summary:hover{background:#fafbfc}
.chip{font-size:11px;font-weight:700;border-radius:999px;padding:3px 9px;white-space:nowrap;text-transform:uppercase;letter-spacing:.03em;background:#fff;border:1px solid var(--line);color:var(--muted)}
.chip-review{color:var(--warn);border-color:#e7d3ad}
.chip-verified{color:var(--ok);border-color:#a8d5cd}
.chip-detached{color:var(--bad);border-color:#e6b8b0}
.chip-fixed{color:var(--ok);border-color:#a8d5cd}
.pname{font-weight:700;font-size:15px;min-width:150px}
.picked{font-size:12.5px;color:var(--muted);flex:1;overflow-wrap:anywhere}
.picked a{color:var(--ok);text-decoration:none}.picked a:hover{text-decoration:underline}
.nopick{color:var(--bad)}
.pmeta{font-size:11.5px;color:var(--muted);white-space:nowrap}
.multibadge{font-size:11.5px;font-weight:700;color:var(--muted);background:#fff;border:1px solid var(--line);border-radius:999px;padding:2px 9px;flex:1;max-width:max-content}
.parent.multi{border-color:var(--line)}
.parent.multi[open]{box-shadow:0 2px 10px rgba(15,23,42,.06)}
.pbody{border-top:1px solid var(--line);padding:12px 14px;display:flex;flex-direction:column;gap:11px}
.conflictbanner{font-size:12.5px;font-weight:600;color:var(--muted);background:#fff;border:1px solid var(--line);border-left:3px solid var(--warn);border-radius:6px;padding:8px 11px}
.opt{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);background:#fff;border:1px solid var(--line);border-radius:999px;padding:2px 8px}
/* judgment: a plain line, color on the text only — no filled block */
.judgment{font-size:13px;padding:1px 0;margin-bottom:6px}
.judgment b{font-weight:700}
.j-confirmed{color:var(--ok)}
.j-wrong{color:var(--bad)}
.j-review{color:var(--warn)}
/* cards are neutral white; the only emphasis goes to the one we think is right */
.cand{border:1px solid var(--line);border-radius:8px;padding:11px 12px;background:#fff}
.cand-parent{border-color:#a8d5cd;background:#f6fbf9;box-shadow:inset 3px 0 0 var(--ok)}
.cand-verified{border-color:#a8d5cd;background:#f6fbf9}
.cand-detached{opacity:.66}
.cand-rejected{opacity:.66}
.cand-fixed{border-color:#c9bce8}
.cand-top{display:flex;justify-content:space-between;gap:10px;align-items:flex-start;margin-bottom:7px}
.avatar{width:44px;height:44px;border-radius:50%;object-fit:cover;background:var(--soft);border:1px solid var(--line);flex:0 0 auto}
.avatar-empty{display:inline-block;background:repeating-linear-gradient(45deg,#eef1f5,#eef1f5 6px,#e7ebf0 6px,#e7ebf0 12px)}
.avatar-sm{width:26px;height:26px;border-radius:50%;object-fit:cover;border:1px solid var(--line);flex:0 0 auto}
.cand-id{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:13px;flex:1}
.cand-id a{color:#0a58ca;text-decoration:none;font-weight:600}.cand-id a:hover{text-decoration:underline}
.verdict{font-size:11px;border-radius:999px;padding:2px 7px;background:var(--soft);color:#334155}
.v-confirmed{background:var(--okbg);color:var(--ok)}.v-wrong_person{background:var(--badbg);color:var(--bad)}.v-needs_review{background:var(--warnbg);color:var(--warn)}
.conf{font-size:12px;font-weight:700;color:#334155}
.flag{font-size:10.5px;border-radius:999px;padding:2px 6px;background:#eef1f5;color:#5b6876}
.flag.conflict{background:#fff;border:1px solid var(--line);color:var(--warn)}
.cand-state{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.03em;color:var(--muted)}
.state-verified{color:var(--ok)}.state-detached{color:var(--bad)}.state-fixed{color:var(--ok)}.state-review{color:var(--warn)}.state-rejected{color:var(--bad)}
.profile .hl{font-size:13px;color:#334155;font-weight:600}
.profile .loc{font-size:12px;color:var(--muted)}
.exp{margin:5px 0;padding-left:18px;font-size:12.5px;color:#334155}
.edu{font-size:12px;color:var(--muted);margin-top:3px}
.contacts{font-size:12.5px;color:#334155;margin-top:4px;flex-basis:100%;overflow-wrap:anywhere}
.contacts strong{color:var(--muted);font-weight:700;font-size:10.5px;text-transform:uppercase;letter-spacing:.04em}
.contacts .cval{font-size:14px;font-weight:700;color:#0f172a;margin-left:3px}
/* compact two-column card: who (profile) on the left, why (reason + evidence) on the right */
.profile{min-width:0}
.cand-body{display:grid;grid-template-columns:1fr;gap:6px 18px;margin:6px 0}
.col-evidence{display:flex;flex-direction:column;gap:6px;min-width:0}
.reason{font-size:13px;color:#1f2937;margin:0 0 2px;line-height:1.45}
.evidence{display:flex;flex-direction:column;gap:6px}
@media(min-width:760px){.cand-body{grid-template-columns:minmax(0,1.05fr) minmax(0,1fr);align-items:start}}
.ev{font-size:12px;border-radius:6px;padding:6px 9px;background:#fff;border:1px solid var(--line);color:#334155}
.ev span{font-weight:700;text-transform:uppercase;font-size:10px;letter-spacing:.04em;display:block;margin-bottom:3px}
.ev ul{margin:0;padding-left:16px;line-height:1.4}
.ev.good{border-left:3px solid var(--ok)}.ev.good span{color:var(--ok)}
.ev.bad{border-left:3px solid var(--bad)}.ev.bad span{color:var(--bad)}
.fixednote{font-size:12px;color:var(--fix);margin-bottom:6px;overflow-wrap:anywhere}
.fixednote a{color:var(--fix)}
.actions{display:flex;gap:7px;flex-wrap:wrap;align-items:center;border-top:1px dashed var(--line);padding-top:9px}
.btn{border-color:#cdd5df;background:#fff;color:#334155;padding:6px 11px;font-size:12.5px}
.btn:hover{background:#f1f5f9}
.btn.keep.on{background:var(--ok);border-color:var(--ok);color:#fff}
.btn.detach.on{background:var(--bad);border-color:var(--bad);color:#fff}
.btn.fix.on{background:var(--fix);border-color:var(--fix);color:#fff}
.fixwrap{display:inline-flex;gap:4px;align-items:center}
.fixurl{min-width:230px;font-size:12px;padding:6px 7px}
.btn.reset{padding:6px 9px;color:var(--muted)}
/* expander toggles look like clickable pills with a caret that rotates when open */
.dossier>summary,.wrongones>summary{display:inline-flex;align-items:center;gap:7px;cursor:pointer;
  font-size:12px;font-weight:600;list-style:none;user-select:none;border-radius:7px;padding:5px 11px;margin:2px 0}
.dossier>summary::-webkit-details-marker,.wrongones>summary::-webkit-details-marker{display:none}
.dossier>summary::before,.wrongones>summary::before{content:'▸';font-size:9px;transition:transform .12s;display:inline-block}
.dossier[open]>summary::before,.wrongones[open]>summary::before{transform:rotate(90deg)}
.dossier>summary{color:var(--muted);background:#fff;border:1px solid var(--line)}
.dossier>summary:hover{background:var(--soft)}
.dosstext{white-space:pre-wrap;font-size:12px;line-height:1.5;background:#f8fafc;border:1px solid var(--line);border-radius:6px;padding:10px;max-height:340px;overflow:auto;color:#1f2937;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.toast{position:fixed;right:16px;bottom:16px;background:#17202a;color:#fff;border-radius:8px;padding:9px 13px;font-size:13px;opacity:0;transform:translateY(8px);transition:.15s;pointer-events:none}
.toast.show{opacity:1;transform:translateY(0)}
.muted{color:var(--muted)}
.chip-excluded{color:#5b6876;border-color:var(--line)}
.state-excluded{color:#5b6876}
.cand-excluded{border-color:var(--line);background:#fff;opacity:.55}
.parent.p-excluded{opacity:.62}
.parent.p-excluded .pname{text-decoration:line-through}
.btn.exclude{color:var(--bad)}
.btn.exclude:hover{background:var(--badbg);border-color:#e6b0a6}
.btn.exclude.on{background:var(--bad);border-color:var(--bad);color:#fff}
.parent-actions{display:flex;justify-content:flex-end;margin-bottom:2px}
.exclude-top{font-size:12px}
.parentbadge{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:#fff;background:var(--ok);border-radius:999px;padding:2px 9px}
.usingparent{font-size:11px;font-weight:700;color:var(--ok);background:var(--okbg);border:1px solid var(--ok);border-radius:999px;padding:2px 9px;white-space:nowrap}
.usingparent-sm{font-size:10.5px;padding:1px 8px;flex:0 0 auto}
.btn.keep.suggested{background:var(--okbg);border-color:var(--ok);color:var(--ok);font-weight:700}
.btn.detach.suggested{background:var(--badbg);border-color:var(--bad);color:var(--bad);font-weight:700}
.btn.suggested::after{content:" · suggested";font-size:10.5px;font-weight:600;opacity:.75}
.flag-spam{background:var(--badbg);color:var(--bad);border:1px solid var(--bad)}
.flag-synth{background:#f0ebfa;color:var(--fix);border:1px solid var(--fix)}
/* source / worth filter chips + the Yes/Maybe/No worth marks */
.chips{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:0 0 10px}
.chips-label{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.fchip{font-size:12px;color:var(--muted);text-decoration:none;background:#fff;border:1px solid var(--line);border-radius:999px;padding:3px 11px}
.fchip:hover{background:var(--soft)}
.fchip.active{background:#17202a;border-color:#17202a;color:#fff}
.worth-label{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.btn.worth-yes.on{background:var(--ok);border-color:var(--ok);color:#fff}
.btn.worth-maybe.on{background:var(--warn);border-color:var(--warn);color:#fff}
.btn.worth-no.on{background:var(--bad);border-color:var(--bad);color:#fff}
.worth-llm{font-size:12px;color:var(--muted);overflow-wrap:anywhere}
.parent.worthno>summary .pname{opacity:.75}
.wrongones{margin-top:4px;border-top:1px dashed var(--line);padding-top:6px}
.wrongones>summary{color:var(--bad);background:#fff;border:1px solid var(--line)}
.wrongones>summary:hover{background:var(--soft)}
.wrongones[open]>summary{margin-bottom:6px}
.wrongones .cand{margin-top:6px}
@media(max-width:820px){header{display:block}.stats{grid-template-columns:repeat(2,1fr);min-width:0;margin-top:12px}summary{flex-wrap:wrap}.picked{flex-basis:100%}}
"""

JS = r"""
const toast=document.getElementById('toast');let tt=null;
function showToast(t){toast.textContent=t;toast.classList.add('show');clearTimeout(tt);tt=setTimeout(()=>toast.classList.remove('show'),1300)}
const LBL={review:'needs review',verified:'verified',detached:'detached',fixed:'verified',excluded:'excluded',rejected:'rejected'};
function candStateOf(c){return ([...c.classList].find(x=>x.startsWith('cand-')&&x!=='cand')||'cand-review').slice(5)}
function setCandState(cand,action,approved,newUrl){
  cand.classList.remove('cand-verified','cand-detached','cand-fixed','cand-review','cand-excluded','cand-rejected');
  const ok=(approved==='yes'||approved==='auto');let st='review';
  if(action==='exclude'&&ok)st='excluded';
  else if(action==='retarget'&&ok)st='fixed';
  else if(ok)st=(action==='detach')?'detached':'verified';
  else if(approved==='no')st='rejected';
  cand.classList.add('cand-'+st);
  const se=cand.querySelector('.cand-state');se.className='cand-state state-'+st;se.textContent=LBL[st];
  cand.querySelectorAll('.btn:not(.worth)').forEach(b=>b.classList.remove('on'));
  // a real decision replaces the pre-highlighted suggestion
  if(st!=='review')cand.querySelectorAll('.btn.suggested').forEach(b=>b.classList.remove('suggested'));
  if(st==='verified')cand.querySelector('.btn.keep')?.classList.add('on');
  if(st==='detached'||st==='rejected')cand.querySelector('.btn.detach')?.classList.add('on');
  if(st==='fixed')cand.querySelector('.btn.fix')?.classList.add('on');
  if(st==='excluded')cand.querySelector('.btn.exclude')?.classList.add('on');
}
function parentStatusFromCands(parent){
  const states=[...parent.querySelectorAll('.cand')].map(candStateOf);
  if(states.includes('excluded'))return 'excluded';
  if(states.includes('fixed'))return 'fixed';
  if(states.includes('review'))return 'review';
  if(states.includes('verified'))return 'verified';
  if(states.length&&states.every(s=>s==='detached'||s==='rejected'))return 'detached';
  return 'review';
}
// Every POST response carries fresh GLOBAL counts from the server (`counts`) — the
// only correct source on filtered views, where the DOM holds just the visible subset.
// Repaints both the header stat cards and the nav tab pills.
function applyCounts(c){
  if(!c)return;
  document.querySelectorAll('.stat strong[data-count]').forEach(el=>{const k=el.dataset.count;if(k in c)el.textContent=c[k]});
  document.querySelectorAll('.tab strong[data-tabcount]').forEach(el=>{const k=el.dataset.tabcount;if(k in c)el.textContent=c[k]});
}
// The active worth filter chip value ('' when unfiltered) — rows that stop matching disappear.
function worthFilter(){const v=new URLSearchParams(location.search).get('worth');return ['yes','maybe','no'].includes(v)?v:''}
// Which status-filtered tab are we on? (the Merged/decided/all tabs aren't pure-status, so a
// decision never evicts a row from them — only the status tabs below do.)
const STATUS_TABS=new Set(['review','verified','detached','fixed','excluded']);
function activeTab(){
  const a=document.querySelector('.tab.active');
  const m=a&&(a.getAttribute('href')||'').match(/[?&]tab=([a-z]+)/);
  return m?m[1]:'all';
}
// Slide a row off the current list (fired when it no longer belongs on this tab).
function evictParent(parent){
  if(parent.dataset.evicting)return;parent.dataset.evicting='1';
  parent.style.transition='opacity .28s ease';parent.style.opacity='0';
  setTimeout(()=>{parent.remove();
    const list=document.querySelector('.list');
    if(list&&!list.querySelector('.parent'))list.innerHTML="<div class='empty'>All clear on this tab — nothing left to review here.</div>";
  },280);
}
// Once a row's new status no longer matches the status tab you're on (e.g. you Fix/Keep/Detach
// someone while on "needs review"), slide it off the list — it lives on its own tab now.
function reconcileTabMembership(parent){
  const t=activeTab();
  if(!STATUS_TABS.has(t)||parentStatusFromCands(parent)===t)return;
  evictParent(parent);
}
// The fix for "looks like nothing happened": after any decision, repaint the PARENT row's
// chip + the header counts, not just the inner card.
function refreshParent(parent){
  const st=parentStatusFromCands(parent);
  ['p-review','p-verified','p-detached','p-fixed','p-excluded'].forEach(x=>parent.classList.remove(x));
  parent.classList.add('p-'+st,'decided');
  const chip=parent.querySelector(':scope > summary .chip');
  if(chip){['chip-review','chip-verified','chip-detached','chip-fixed','chip-excluded'].forEach(x=>chip.classList.remove(x));
    chip.classList.add('chip-'+st);chip.textContent=LBL[st];}
  const top=parent.querySelector(':scope > .pbody > .parent-actions .btn.exclude');
  if(top)top.classList.toggle('on',st==='excluded');
  reconcileTabMembership(parent);
}
// Shared post-response repaint: Rejected membership (both directions), worth-button
// highlight, worth-filter membership, and the authoritative header/tab counts.
function applyLiveState(parent,cand,j){
  applyCounts(j.counts);
  if(typeof j.rejected==='undefined')return;
  parent.classList.toggle('worthno',j.rejected);
  const wr=cand&&cand.querySelector('.worthrow');
  if(wr&&typeof j.effective!=='undefined')
    wr.querySelectorAll('.btn.worth').forEach(x=>x.classList.toggle('on',x.dataset.worth===j.effective));
  if(j.rejected&&activeTab()==='review')evictParent(parent);
  if(!j.rejected&&activeTab()==='rejected')evictParent(parent);
  const wf=worthFilter();
  if(wf&&typeof j.effective!=='undefined'&&j.effective!==wf)evictParent(parent);
}
async function postDecide(cand,act,newUrl){
  const body=new URLSearchParams({pub:cand.dataset.pub,decision:act});
  if(newUrl)body.set('new_url',newUrl);
  const r=await fetch('/decide',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body});
  if(!r.ok)throw new Error(await r.text());return r.json();
}
async function decide(cand,act){
  let url='';
  if(act==='fix'){url=cand.querySelector('.fixurl').value.trim();if(!url){showToast('paste a LinkedIn URL first');return}}
  try{const j=await postDecide(cand,act,url);setCandState(cand,j.action,j.approved,j.new_url);
    const parent=cand.closest('.parent');
    // Keeping/fixing one LinkedIn on a merged person IS the decision for the whole person:
    // auto-detach the other still-undecided LinkedIns (incl. ones tucked in the likely-wrong
    // expander) so one click resolves the row. Explicit user decisions are never overridden.
    let auto=0,counts=j.counts;
    if((act==='keep'||act==='fix')&&parent.classList.contains('multi')){
      for(const sib of parent.querySelectorAll('.cand')){
        if(sib!==cand&&candStateOf(sib)==='review'){
          const sj=await postDecide(sib,'detach','');setCandState(sib,sj.action,sj.approved,sj.new_url);
          counts=sj.counts;auto++;}
      }
    }
    refreshParent(parent);
    // unified Rejected state (a Keep/Fix rescues a machine no; an Exclude IS a user no)
    // + authoritative counts, all from the response — no reload needed.
    applyLiveState(parent,cand,{...j,counts});
    const base={keep:'Kept',detach:'Detached',fix:'Re-targeted',reset:'Reset to model'}[act]||'Saved';
    showToast(auto?base+' — detached the other '+auto+' LinkedIn'+(auto===1?'':'s')+' for you':base);
  }catch(e){showToast('Save failed: '+e.message)}
}
document.querySelectorAll('.cand .btn[data-act]:not(.exclude)').forEach(b=>b.addEventListener('click',e=>{e.preventDefault();decide(b.closest('.cand'),b.dataset.act)}));
// Yes / Maybe / No network-worth marks on candidate + synthetic rows: writes the user-owned
// network_worth column in review.csv ('' clears the mark — back to the LLM's judgment).
document.querySelectorAll('.worthrow .btn').forEach(b=>b.addEventListener('click',async e=>{
  e.preventDefault();
  const row=b.closest('.worthrow'),cand=b.closest('.cand'),parent=b.closest('.parent');
  const body=new URLSearchParams({pub:row.dataset.worthkey,worth:b.dataset.worth||''});
  try{
    const r=await fetch('/worth',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body});
    if(!r.ok)throw new Error(await r.text());
    const j=await r.json();
    // a mark can change the row's decision state too (worth-Yes clears an exclude; a
    // synthetic mark flips its mint gate) — repaint chip + status from the response.
    setCandState(cand,j.action,j.approved,j.new_url||'');
    refreshParent(parent);
    // unified Rejected membership live-updates BOTH ways: an effective-no leaves the
    // review pile for Rejected; a rescue (Yes / cleared machine call) leaves Rejected.
    applyLiveState(parent,cand,j);
    showToast(b.dataset.worth?'Marked '+j.effective:'Cleared — '+j.source+' says '+j.effective);
  }catch(err){showToast('Save failed: '+err.message)}
}));
// "Exclude person" sits with the per-candidate buttons but acts on the WHOLE person:
// excludes — or restores — every candidate at once. Click again to undo.
document.querySelectorAll('.btn.exclude').forEach(x=>x.addEventListener('click',async e=>{
  e.preventDefault();
  const parent=x.closest('.parent');const cands=[...parent.querySelectorAll('.cand')];
  const excluding=parentStatusFromCands(parent)!=='excluded';const act=excluding?'exclude':'reset';
  try{let j=null;
    for(const c of cands){j=await postDecide(c,act,'');setCandState(c,j.action,j.approved,j.new_url);}
    refreshParent(parent);
    if(j)applyLiveState(parent,cands[0],j);   // exclude == unified no: Rejected + counts live-update
    showToast(excluding?"Excluded — won’t be indexed":'Restored');
  }catch(err){showToast('Save failed: '+err.message)}
}));
// initialize button highlight from current state
document.querySelectorAll('.cand').forEach(c=>{
  if(c.classList.contains('cand-verified'))c.querySelector('.btn.keep')?.classList.add('on');
  if(c.classList.contains('cand-detached')||c.classList.contains('cand-rejected'))c.querySelector('.btn.detach')?.classList.add('on');
  if(c.classList.contains('cand-fixed'))c.querySelector('.btn.fix')?.classList.add('on');
  if(c.classList.contains('cand-excluded'))c.querySelector('.btn.exclude')?.classList.add('on');});
// merged people carry one "exclude whole person" button at the row top — light it if excluded
document.querySelectorAll('.parent').forEach(p=>{
  const t=p.querySelector(':scope > .pbody > .parent-actions .btn.exclude');
  if(t)t.classList.toggle('on',parentStatusFromCands(p)==='excluded');});
// lazy-load dossier on first expand
document.querySelectorAll('details.dossier').forEach(d=>d.addEventListener('toggle',async()=>{
  if(!d.open||d.dataset.loaded)return;d.dataset.loaded='1';const pre=d.querySelector('.dosstext');
  try{const r=await fetch('/api/dossier?slug='+encodeURIComponent(d.dataset.slug));pre.textContent=r.ok?await r.text():'(no dossier found)';}
  catch(e){pre.textContent='(failed to load dossier)'}}));
"""


# --- server -----------------------------------------------------------------

def make_handler(review_path: Path, verdicts_path: Path, parents_dir: Path, dossier_dir: Path,
                 confirm_threshold: float, detach_threshold: float,
                 synthetic_path: Path = SYNTHETIC_PEOPLE_CSV,
                 facts_dir: Path = FACTS_DIR, people_csv: Path = DEFAULT_PEOPLE_CSV):
    class Handler(BaseHTTPRequestHandler):
        def send_bytes(self, body: bytes, content_type: str = "text/html; charset=utf-8", status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/healthz":
                self.send_bytes(b"ok", "text/plain")
                return
            if parsed.path == "/api/dossier":
                slug = (urllib.parse.parse_qs(parsed.query).get("slug") or [""])[0]
                text = render_dossier(parents_dir, dossier_dir, slug)
                self.send_bytes(text.encode("utf-8"), "text/plain; charset=utf-8")
                return
            if parsed.path != "/":
                self.send_bytes(b"not found", "text/plain", status=404)
                return
            parents, overrides = build_parents(verdicts_path, review_path)
            auto_resolve_merged(parents, review_path, confirm_threshold)
            extend_and_annotate(parents, overrides, synthetic_path, facts_dir)
            annotate_sources(parents, load_people_sources(people_csv))
            params = urllib.parse.parse_qs(parsed.query)
            self.send_bytes(page_html(parents, params, review_path))

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path not in {"/decide", "/worth"}:
                self.send_bytes(b"not found", "text/plain", status=404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            pub = (form.get("pub") or [""])[0]
            if parsed.path == "/worth":
                # Yes/Maybe/No mark (or '' to clear) — user-owned network_worth column.
                worth_val = (form.get("worth") or [""])[0]
                try:
                    result = apply_worth_decision(review_path, pub, worth_val)
                except ValueError as exc:
                    self.send_bytes(str(exc).encode(), "text/plain", status=400)
                    return
                # a synthetic row's mint gate must agree: No = Detach, Yes = Keep, ↺ = pending
                gate = sync_synthetic_gate(synthetic_path, pub, worth_val)
                rows_now = load_override_rows(review_path)
                state = effective_no_for_key(pub, rows_now, facts_dir,
                                             keepish=(gate["approved"] == "yes") if gate else None)
                # the row's decision state (a worth-Yes may have cleared an exclude; a
                # synthetic mark flips its gate) so the client repaints chips in place
                row_now = rows_now.get(pub.strip().lower()) or {}
                decided = gate or {"action": (row_now.get("action") or "").strip().lower(),
                                   "approved": (row_now.get("approved") or "").strip().lower()}
                self.send_bytes(json.dumps({"ok": True, "pub": pub, **result,
                                            "action": decided["action"],
                                            "approved": decided["approved"],
                                            "new_url": row_now.get("new_linkedin_url", ""),
                                            "effective": state["worth"]["decision"],
                                            "source": state["worth"]["source"],
                                            "reason": state["worth"]["reason"],
                                            "rejected": state["rejected"],
                                            "counts": live_counts(verdicts_path, review_path,
                                                                  synthetic_path, facts_dir)}).encode(),
                                "application/json")
                return
            decision = (form.get("decision") or [""])[0]
            new_url = (form.get("new_url") or [""])[0]
            if not pub or decision not in {"keep", "detach", "fix", "reset", "exclude"}:
                self.send_bytes(b"bad request", "text/plain", status=400)
                return
            try:
                if pub.strip().lower().startswith("synth-"):
                    # synthetic rows live in synthetic-people.csv, gated by `approved` only
                    result = apply_synthetic_decision(synthetic_path, pub, decision)
                    worth_key = synthetic_worth_key(synthetic_path, pub)
                    keepish = result["approved"] == "yes"
                else:
                    result = apply_decision(review_path, verdicts_path, pub, decision, new_url,
                                            confirm_threshold, detach_threshold)
                    worth_key, keepish = pub, None
            except ValueError as exc:
                self.send_bytes(str(exc).encode(), "text/plain", status=400)
                return
            # the unified Rejected state after this decision (a Keep rescues a machine no;
            # an Exclude IS a user no) + fresh global counts for the header/tab pills
            payload: dict[str, Any] = {"ok": True, "pub": pub, **result,
                                       "counts": live_counts(verdicts_path, review_path,
                                                             synthetic_path, facts_dir)}
            if worth_key:
                state = effective_no_for_key(worth_key, load_override_rows(review_path),
                                             facts_dir, keepish=keepish)
                payload.update({"rejected": state["rejected"],
                                "effective": state["worth"]["decision"],
                                "source": state["worth"]["source"]})
            self.send_bytes(json.dumps(payload).encode(), "application/json")

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    return Handler


def cmd_serve(args: argparse.Namespace) -> None:
    review_path = Path(args.review)
    verdicts_path = Path(args.verdicts)
    parents_dir = Path(args.parents_dir)
    if not verdicts_path.exists():
        print(json.dumps({"primitive": "reconcile_review_web", "status": "error",
                          "error": f"no verdicts at {verdicts_path} — run `bin/deep-context reconcile` first"}))
        return
    parents, _ = build_parents(verdicts_path, review_path)
    server = ThreadingHTTPServer((args.host, args.port),
                                 make_handler(review_path, verdicts_path, parents_dir, Path(args.dossier_dir),
                                              args.confirm_threshold, args.detach_threshold,
                                              facts_dir=Path(args.facts_dir), people_csv=Path(args.people_csv)))
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    print(json.dumps({"primitive": "reconcile_review_web", "status": "serving", "url": url,
                      "review": str(review_path), "parents": len(parents),
                      "needs_review": summarize(parents)["review"]}, indent=2))
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the deep-context LinkedIn self-heal review UI.")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve")
    serve.add_argument("--review", default=str(LINKEDIN_OVERRIDES_CSV))
    serve.add_argument("--verdicts", default=str(VERDICTS_JSONL))
    serve.add_argument("--parents-dir", default=str(PARENTS_DIR))
    serve.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    serve.add_argument("--facts-dir", default=str(FACTS_DIR))
    serve.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    serve.add_argument("--confirm-threshold", type=float, default=DEFAULT_CONFIRM)
    serve.add_argument("--detach-threshold", type=float, default=DEFAULT_DETACH)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--open", action="store_true")
    serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        args = build_parser().parse_args(["serve", *(argv or [])])
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
