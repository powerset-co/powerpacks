"""[Phase 3] Reconcile each canonical parent against its attached LinkedIn profile.

Every deep-context PARENT is a person built from MESSAGE evidence. Separately,
people.csv already staples a `linkedin_url` to each person — often resolved on thin
same-name evidence during ingestion (so a "John Smith CEO" profile can be wrongly
attached to a "John Smith" who is actually the user's plumber).

This step is the SELF-HEAL pass: a high-reasoning LLM judges, for each
(parent dossier ↔ attached LinkedIn profile), whether they are the SAME HUMAN —
using corroboration (employer/school/location/role/behavior) and especially
CONTRADICTIONS, never name alone. Then:

  - confirmed  : the profile lines up with the message-derived dossier.
  - wrong_person: the profile contradicts it, or only the name links them.
  - needs_review: too little either way.

High-confidence verdicts AUTO-APPLY to people.csv (confirmed -> mark verified;
wrong_person -> detach the bad link, preserving it in `linkedin_url_rejected`), after
backing up people.csv. Low-confidence verdicts + link conflicts drop into a review
queue for the user. A wrong_person verdict NEVER forces a replacement — some people
legitimately have no LinkedIn (the judge flags `linkedin_plausibly_absent`).

Mirrors `verify_gmail_resolution` (verdict semantics) and `cluster_merge_candidates`
(Responses-API + drain_pool mechanics). ``--no-llm`` is a deterministic offline stub
for tests.

Outputs:
  reconcile/summary.md   the ONE report to read (what changed + what needs review)
  reconcile/verdicts.*   full per-candidate audit (jsonl + flat csv)
  reconcile/applied.csv  what auto-applied (drill-down)
  reconcile/manifest.json
  overrides/review.csv  the ONE file to EDIT (approved column; every judged row)
  (a "## LinkedIn identity" section injected into each parent markdown)
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.indexing.lib.openai_stream import drain_pool
from packs.indexing.lib.openai_usage_tiers import env_or_profile_int
from packs.indexing.lib.openai_responses import (
    estimate_cost_usd,
    is_retryable,
    make_async_client,
    parse_json_response,
    reasoning_effort,
    responses_kwargs,
    usage_tokens,
)
from packs.ingestion.primitives.deep_context import compose_dossier as compose
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    DOSSIER_DIR,
    FACTS_DIR,
    CONSOLIDATE_PEOPLE_CSV,
    INDEX_JSON,
    LINKEDIN_OVERRIDES_CSV,
    PARENTS_DIR,
    PROFILE_CACHE_DIR,
    RAW_DIR,
    VERDICTS_CSV,
    VERDICTS_JSONL,
    emit,
    load_env,
    load_owner,
    normalize_email,
    normalize_phone,
    now_iso,
    owner_background_block,
    parse_list,
    write_json,
)
from packs.ingestion.primitives.enrich_people.enrich_people import (
    profile_cache_path,
    read_usable_cached_profile,
)
from packs.ingestion.schemas.people_schema import (
    PEOPLE_SCHEMA_COLUMNS,
    extract_public_identifier,
    merge_interaction_counts,
    normalize_linkedin_url,
    parse_jsonish,
)

DEFAULT_CONFIRM = 0.70         # auto-VERIFY a `confirmed` link at/above this (keep-biased — the user fixes the rare mismatch)
DEFAULT_DETACH = 0.85          # auto-DETACH a `wrong_person` link only at/above this (dropping a real person is the costly error)
SECTION_ANCHOR = "## LinkedIn identity"
SAMPLE_PER_DIRECTION = 4
SAMPLE_CHARS = 200
DR_COST_PER_PERSON = 0.05      # Parallel.ai core2x $/person (matches reconcile_deep_research)
DEFAULT_DR_BUDGET = 25.0

VERDICTS = ["confirmed", "wrong_person", "needs_review"]

SYSTEM_PROMPT = (
    "You verify whether a LinkedIn profile is the SAME PERSON as a contact I know from my "
    "own messages. You are given (A) a dossier of that contact synthesized from how we "
    "actually interact — my relationship to them, what we discuss, their employer/school/"
    "location as it shows up in our messages, and sample messages — and (B) the LinkedIn "
    "profile currently attached to them (name, headline, company, education, location).\n\n"
    "DEFAULT TO CONFIRMING. This link was attached because the names already matched — your "
    "job is to catch the GENUINE mismatches (a different human who happens to share the name), "
    "NOT to demand extra proof. A matching name PLUS any ONE corroborating signal — employer "
    "(current OR past), school, city/region, era/timeline, or shared social context — and NO "
    "hard contradiction means it is the same person: confirmed.\n\n"
    "MOST of my contacts are PERSONAL / SOCIAL, where we almost never discuss work, titles, or "
    "employers. Do NOT lower confidence or withhold a confirm just because the messages don't "
    "name their company/role or some 'unique identifier' — that absence is EXPECTED and is not "
    "evidence against a match. For these, geography, school, mutual-friend / social context, or "
    "a plausible timeline IS sufficient corroboration.\n\n"
    "For WORK contacts: a contact EMAIL whose DOMAIN matches the LinkedIn employer — current OR "
    "past — (e.g. mmasse@riotgames.com against a Riot Games profile) is NEAR-DECISIVE identity "
    "proof: confirmed, high confidence (0.9+). A work email at a company means they work/worked "
    "there. And do NOT withhold a confirm or lower confidence because a GRANULAR sub-detail from "
    "my messages (a specific internal team, project, product line, or exact title) isn't on the "
    "profile — people don't list every team and titles roll up; a missing sub-detail is NOT a "
    "contradiction. Only an actual conflict (different company/city/era/career that can't be the "
    "same human) is.\n\n"
    "REASON FROM BASE RATES. Two DIFFERENT people who share an EXACT full name AND the same "
    "employer (or the same school, or the same small region + era) is RARE — on the order of "
    "1-in-100. So a name + one such anchor, with no hard contradiction, is already STRONG "
    "evidence of the same person — confirmed at 0.85+. Start from 'this is them' and only back "
    "off for a genuine contradiction, not for small mismatches. The cost of losing a real match "
    "(recall) is high; do NOT nickel-and-dime confidence for trivia.\n\n"
    "These do NOT count as contradictions and must NOT lower confidence:\n"
    "  • a different/missing internal TEAM, project, or product line (people don't list every team)\n"
    "  • a TITLE that differs in wording or seniority at the same org (Founder vs CEO vs Exec "
    "Chairman vs Manager — same person, different hat)\n"
    "  • imprecise, rounded, or non-overlapping LinkedIn DATE RANGES (LinkedIn dates are routinely "
    "wrong/missing; a date gap at a MATCHING employer is not a contradiction)\n"
    "  • the messages not naming their employer/role (expected, esp. for personal contacts)\n"
    "  • extra impressive CREDENTIALS on the profile (awards, prior roles, prof/PhD/fellowships) "
    "not visible in casual messages — accomplished people simply have more on LinkedIn than shows "
    "up in logistics texts; this is NOT grounds for doubt.\n\n"
    "- confirmed: the name matches and at least one of {employer, school, location, era, shared "
    "context} lines up, with no real contradiction. THIS IS THE COMMON CASE. (people change "
    "jobs — a PAST employer or school still counts.)\n"
    "- wrong_person: there is an ACTIVE, HARD CONTRADICTION making them a different human — e.g. "
    "the dossier is a local friend / tradesperson but the profile is a big-company exec of the "
    "same name, or a clearly different city + industry + era that cannot reconcile. This is "
    "name-shared-WITH-a-contradicting-profile, not merely name-without-extra-proof, and NOT the "
    "small-stuff list above.\n"
    "- needs_review: ONLY when the name matches but there is genuinely ZERO corroboration AND "
    "something is mildly off, so you truly cannot tell. Use this SPARINGLY — if there is any "
    "reasonable corroboration and no contradiction, choose confirmed.\n\n"
    "CONFIDENCE CALIBRATION: name + a strong anchor (same employer, matching email domain, same "
    "school, or same city + plausible role) → 0.85–0.95, even if small details differ. "
    "Softer-but-consistent signals (location + social context, no contradiction) → 0.75–0.85. Go "
    "below 0.70 only when you are ACTUALLY unsure (zero corroboration) — never deflate a real "
    "match for lack of a 'unique identifier' or for the small-stuff above.\n\n"
    "Some people legitimately have NO LinkedIn. If the dossier suggests this person plausibly "
    "would not have a (matching) profile, set linkedin_plausibly_absent=true rather than "
    "forcing a verdict. Set recommend_deep_research=true only when EXTERNAL research could "
    "realistically resolve the identity (i.e. not when they plausibly have no profile at all). "
    "Cite concrete supporting and contradicting evidence.\n\n"
    "SPAM / COLD-OUTREACH SCREEN (separate from identity): also assess whether this CONTACT "
    "is a spammy relationship not worth indexing — unsolicited cold outreach ('hey, would you "
    "be interested in X', sales/SEO/agency/recruiting pitches, automated sequences) where I "
    "never engaged, replied 'not interested', or asked them to stop. Set spam_contact=true with "
    "spam_confidence and a one-line spam_reason. A real relationship — a colleague, friend, "
    "warm intro, anyone I initiated with or had a genuine back-and-forth with — is NEVER spam, "
    "no matter how pitchy a single message reads. When in doubt, spam_contact=false."
)

RECONCILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": VERDICTS},
        "confidence": {"type": "number"},
        "supporting_evidence": {"type": "array", "items": {"type": "string"}},
        "contradicting_evidence": {"type": "array", "items": {"type": "string"}},
        "linkedin_plausibly_absent": {"type": "boolean"},
        "recommend_deep_research": {"type": "boolean"},
        "reason": {"type": "string", "description": "One-line rationale."},
        "spam_contact": {"type": "boolean", "description": "Unsolicited cold-outreach contact I never engaged with."},
        "spam_confidence": {"type": "number"},
        "spam_reason": {"type": "string", "description": "One line; empty when spam_contact=false."},
    },
    "required": ["verdict", "confidence", "supporting_evidence", "contradicting_evidence",
                 "linkedin_plausibly_absent", "recommend_deep_research", "reason",
                 "spam_contact", "spam_confidence", "spam_reason"],
}


# --- IO helpers -------------------------------------------------------------

def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def load_people_rows(people_csv: Path) -> dict[str, dict[str, str]]:
    """person_id -> raw people.csv row (we only need a handful of columns)."""
    rows: dict[str, dict[str, str]] = {}
    if not people_csv.exists():
        return rows
    with people_csv.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pid = str(row.get("id") or "").strip()
            if pid:
                rows[pid] = row
    return rows


def linkedin_key(row: dict[str, str]) -> str:
    """Comparable public_identifier (lowercased) for the row's attached LinkedIn."""
    pub = (row.get("public_identifier") or "").strip().lower()
    if not pub:
        pub = extract_public_identifier(row.get("linkedin_url") or "").lower()
    return pub


def _fmt_span(entry: dict[str, Any]) -> str:
    def yr(v: Any) -> str:
        return str((v or {}).get("year") or "") if isinstance(v, dict) else ""
    start, end = yr(entry.get("starts_at")), yr(entry.get("ends_at"))
    if start and end:
        return f"{start}–{end}"
    if start:
        return f"{start}–present"
    return end or ""


def linkedin_view(row: dict[str, str], cache_dir: Path) -> dict[str, Any]:
    """Build the LinkedIn side for the judge — prefer the rich cached profile, fall
    back to the work_experiences/education columns already on the people.csv row."""
    pub = linkedin_key(row)
    cached = read_usable_cached_profile(profile_cache_path(cache_dir, pub)) if pub else None
    np = (cached or {}).get("normalized_profile") if cached else None
    if np:
        exps = np.get("experiences") or []
        edus = np.get("education") or []
        location = np.get("location_str") or ", ".join(
            x for x in [np.get("city"), np.get("state"), np.get("country")] if x)
        full_name = np.get("full_name") or ""
        headline = np.get("headline") or ""
        profile_pic_url = np.get("profile_pic_url") or ""
        source = "cache"
    else:  # fall back to people.csv columns (same RapidAPI fetch, fewer descriptions)
        exps = parse_jsonish(row.get("work_experiences"), []) or []
        edus = parse_jsonish(row.get("education"), []) or []
        location = ", ".join(x for x in [row.get("city"), row.get("state"), row.get("country")] if x)
        full_name = row.get("full_name") or ""
        headline = row.get("headline") or ""
        profile_pic_url = row.get("profile_picture_url") or ""
        source = "people_csv"
    experiences = []
    # Feed the judge the FULL work history — a PAST employer is often the anchor that confirms
    # identity (e.g. an old AngelList role matching a help@alist.co contact). Any truncation
    # silently hides those and manufactures false misses, so we cap nothing.
    for e in exps:
        title = e.get("title") or ""
        company = e.get("company_name") or e.get("company") or ""
        span = _fmt_span(e)
        line = " @ ".join(x for x in [title, company] if x) or company or title
        experiences.append(f"{line}{f' ({span})' if span else ''}".strip())
    education = []
    for ed in edus:
        school = ed.get("school") or ed.get("school_name") or ""
        degree = ", ".join(x for x in [ed.get("degree"), ed.get("field")] if x)
        education.append(f"{degree + ' — ' if degree else ''}{school}".strip(" —"))
    return {
        "public_identifier": pub,
        "linkedin_url": row.get("linkedin_url") or "",
        "full_name": full_name,
        "headline": headline,
        "profile_pic_url": profile_pic_url,
        "experiences": [x for x in experiences if x],
        "education": [x for x in education if x],
        "location": location,
        "source": source,
        "has_profile": bool(np or experiences or education or headline),
    }


# --- dossier (message-derived) side -----------------------------------------

def _sample(messages: list[dict[str, Any]], direction: str) -> list[str]:
    out: list[str] = []
    for m in sorted(messages, key=lambda m: m.get("at") or "", reverse=True):
        if m.get("direction") != direction:
            continue
        text = (m.get("text") or "").strip()
        if text:
            out.append(text[:SAMPLE_CHARS])
        if len(out) >= SAMPLE_PER_DIRECTION:
            break
    return out


def _self_linkedin(identifiers: list[Any] | None) -> tuple[str, str]:
    """The LinkedIn URL the contact shared THEMSELVES in messages (recruiters, intros, sig lines),
    captured by synthesis in facts `identifiers`. Near-ground-truth for who they are. Returns
    (normalized_url, public_identifier) or ('', '')."""
    for ident in identifiers or []:
        if "linkedin.com/in/" in str(ident).lower():
            pub = extract_public_identifier(str(ident)).lower()
            if pub:
                return normalize_linkedin_url(str(ident)), pub
    return "", ""


def self_linkedin_from_facts(person_ids: list[str], facts_dir: Path) -> tuple[str, str]:
    """Self-reported LinkedIn for a candidate, recomputed from facts (used by --reapply, no LLM)."""
    records: list[dict[str, Any]] = []
    for pid in person_ids:
        records.extend(_read_jsonl(facts_dir / f"{pid}.jsonl"))
    return _self_linkedin((compose.merge_facts(records) if records else {}).get("identifiers"))


def dossier_view(child_pids: list[str], facts_dir: Path, raw_dir: Path) -> dict[str, Any]:
    """Merge the confirmed children's facts + a few message samples for the judge."""
    records: list[dict[str, Any]] = []
    msgs: list[dict[str, Any]] = []
    for pid in child_pids:
        records.extend(_read_jsonl(facts_dir / f"{pid}.jsonl"))
        msgs.extend(_read_json(raw_dir / f"{pid}.json").get("messages") or [])
    merged = compose.merge_facts(records) if records else {}
    self_url, self_url_pub = _self_linkedin(merged.get("identifiers"))
    return {
        "relationship": str(merged.get("relationship_to_owner") or ""),
        "title": str(merged.get("title") or ""),
        "employers": [e.get("name", "") for e in (merged.get("employers") or []) if e.get("name")],
        "school": str(merged.get("school") or ""),
        "location": str(merged.get("location") or ""),
        "topics": list(merged.get("topics") or [])[:10],
        "shared_context": [f"{s.get('overlap', 'other')}: {s.get('detail', '')}"
                           for s in (merged.get("shared_context") or []) if s.get("detail")],
        "self_linkedin_url": self_url,
        "self_linkedin_pub": self_url_pub,
        "from_me": _sample(msgs, "from_me"),
        "from_them": _sample(msgs, "from_them"),
        "has_messages": bool(msgs),
    }


# --- candidate pairing ------------------------------------------------------

def build_tasks(index: dict[str, Any], people: dict[str, dict[str, str]],
                facts_dir: Path, raw_dir: Path, cache_dir: Path) -> list[dict[str, Any]]:
    """One judge task per (parent, distinct attached LinkedIn). Parents whose children
    carry different LinkedIn profiles produce multiple tasks flagged as a conflict."""
    slugs_info = index.get("slugs", {})
    tasks: list[dict[str, Any]] = []
    for pslug, pinfo in index.get("parents", {}).items():
        child_slugs = [s for s in (pinfo.get("children") or []) if s in slugs_info]
        if not child_slugs:
            continue
        child_pids = [slugs_info[s]["person_id"] for s in child_slugs]
        # Group child pids by their attached LinkedIn key.
        by_key: dict[str, list[str]] = {}
        for pid in child_pids:
            row = people.get(pid)
            if not row:
                continue
            key = linkedin_key(row)
            if key:
                by_key.setdefault(key, []).append(pid)
        dossier = dossier_view(child_pids, facts_dir, raw_dir)
        conflict = len(by_key) > 1
        if not by_key:  # no LinkedIn attached to any child
            tasks.append({"parent_slug": pslug, "name": pinfo.get("name", pslug),
                          "candidate_key": "", "person_ids": child_pids, "conflict": False,
                          "no_link": True, "dossier": dossier, "linkedin": {}})
            continue
        for key, pids in by_key.items():
            row = people[pids[0]]
            emails, phones = _contact_keys(pids, people)
            tasks.append({
                "parent_slug": pslug, "name": pinfo.get("name", pslug),
                "candidate_key": key, "person_ids": pids, "conflict": conflict,
                "no_link": False, "dossier": dossier, "linkedin": linkedin_view(row, cache_dir),
                "match_emails": emails, "match_phones": phones,
                # Ground truth: this LinkedIn came from your own Connections export — you're
                # connected, so it IS them. No LLM needed (see CONNECTION_VERDICT).
                "from_connections": _from_connections(pids, people),
            })
    return tasks


CONNECTION_CHANNEL = "linkedin_csv"  # source_channels marker for a row imported from LinkedIn Connections.csv


def _from_connections(pids: list[str], people: dict[str, dict[str, str]]) -> bool:
    """True if any of the candidate's rows came from your LinkedIn Connections import."""
    return any(CONNECTION_CHANNEL in (people.get(pid, {}).get("source_channels") or "") for pid in pids)


def connection_verdict() -> dict[str, Any]:
    """Deterministic ground-truth verdict for a contact who is one of your LinkedIn connections."""
    return {
        "verdict": "confirmed", "confidence": 1.0,
        "supporting_evidence": ["This LinkedIn is one of your own connections (from your LinkedIn "
                                "Connections import) — you are connected, so it is the same person."],
        "contradicting_evidence": [], "linkedin_plausibly_absent": False, "recommend_deep_research": False,
        "reason": "Ground truth: you're connected to this person on LinkedIn (linkedin_csv import).",
        "spam_contact": False, "spam_confidence": 0.0, "spam_reason": "",
    }


def _name_compatible(name: str, pub: str) -> bool:
    """True if a LinkedIn slug shares a real name token with the contact — guards against a
    THIRD party's URL the contact merely mentioned (e.g. an intro: 'meet Brandon, /brandonmoak')."""
    name_tokens = {t for t in re.findall(r"[a-z]+", (name or "").lower()) if len(t) >= 3}
    pub_tokens = {t for t in re.findall(r"[a-z]+", (pub or "").lower()) if len(t) >= 3}
    return bool(name_tokens & pub_tokens)


def self_reported_retargets(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recover the CORRECT LinkedIn for free: when the contact shared their own profile in our
    messages (dossier `self_linkedin`) and it DIFFERS from the attached link, propose a retarget
    to the URL they gave — no Parallel deep-research needed. Auto-apply only when the URL's slug
    is NAME-COMPATIBLE with the contact (their own); otherwise propose it PENDING, since a shared
    URL can occasionally be a third party they mentioned (re-attaching a wrong identity is worse
    than leaving it detached)."""
    proposals = []
    for t in tasks:
        if t.get("no_link"):
            continue
        # Only recover a WRONG attachment. If the attached link is already right — a ground-truth
        # connection, or a confirmed verdict — keep it; a LinkedIn merely mentioned in the messages
        # must not override it (that's how Ben Taft, a real connection, got a third party's URL).
        if t.get("from_connections") or (t.get("verdict") or {}).get("verdict") == "confirmed":
            continue
        d = t.get("dossier") or {}
        self_pub = (d.get("self_linkedin_pub") or "").lower()
        self_url = d.get("self_linkedin_url") or ""
        attached = (t.get("candidate_key") or "").lower()
        if not self_pub or not self_url or self_pub == attached:
            continue  # nothing to recover (no self-URL, or the attached link already matches it)
        own = _name_compatible(t.get("name", ""), self_pub)
        proposals.append({
            "old_public_identifier": attached, "new_linkedin_url": self_url,
            "new_public_identifier": self_pub, "linkedin_url": (t.get("linkedin") or {}).get("linkedin_url", ""),
            "match_emails": t.get("match_emails") or [], "match_phones": t.get("match_phones") or [],
            "confidence": 0.95 if own else 0.5, "person_id": (t.get("person_ids") or [""])[0],
            "reason": ("The contact shared this LinkedIn themselves in your messages — retargeting to their own URL."
                       if own else "A LinkedIn URL appeared in this contact's messages but the name doesn't match — "
                       "possibly a third party they mentioned; approve if it's really them."),
            "source": "dossier-self-reported", "approved": "auto" if own else "",
        })
    return proposals


def _contact_keys(pids: list[str], people: dict[str, dict[str, str]]) -> tuple[list[str], list[str]]:
    """Normalized emails/phones across a candidate's person rows — used to scope the
    override to the right person group at merge time."""
    emails: list[str] = []
    phones: list[str] = []
    for pid in pids:
        row = people.get(pid, {})
        for e in [row.get("primary_email", ""), *parse_list(row.get("all_emails"))]:
            ne = normalize_email(e)
            if ne and "@" in ne and ne not in emails:
                emails.append(ne)
        for p in [row.get("primary_phone", ""), *parse_list(row.get("all_phones"))]:
            npn = normalize_phone(p)
            if npn and npn not in phones:
                phones.append(npn)
    return emails, phones


# --- LLM judge --------------------------------------------------------------

def _bullets(items: list[str], empty: str) -> str:
    return "\n".join(f"  - {x}" for x in items) if items else f"  {empty}"


def judge_prompt(task: dict[str, Any], owner_block: str) -> str:
    d, li = task["dossier"], task["linkedin"]
    dossier_lines = []
    if d["relationship"]:
        dossier_lines.append(f"  relationship to me: {d['relationship']}")
    if d["title"] or d["employers"]:
        dossier_lines.append(f"  work (from messages): {d['title']} {('@ ' + ', '.join(d['employers'])) if d['employers'] else ''}".strip())
    if d["school"]:
        dossier_lines.append(f"  school (from messages): {d['school']}")
    if d["location"]:
        dossier_lines.append(f"  location (from messages): {d['location']}")
    if d["topics"]:
        dossier_lines.append(f"  we discuss: {', '.join(d['topics'])}")
    if d["shared_context"]:
        dossier_lines.append(f"  shared context with me: {'; '.join(d['shared_context'])}")
    if d.get("self_linkedin_url"):
        same = d.get("self_linkedin_pub") == (task.get("candidate_key") or "").lower()
        dossier_lines.append(
            f"  *** a LinkedIn URL appears in this contact's own messages: {d['self_linkedin_url']} — "
            + ("it MATCHES the attached profile below → strong confirmation, very high confidence."
               if same else
               f"it DIFFERS from the attached profile (/{task.get('candidate_key')}). If this shared URL is "
               "THEIRS (name lines up), the attached profile is the wrong namesake → wrong_person. (It could "
               "occasionally be a third party they mentioned, so weigh the name.)") + " ***")
    contact_ids = ", ".join((task.get("match_emails") or []) + (task.get("match_phones") or []))
    if contact_ids:
        dossier_lines.append(f"  my address-book contact handles for them: {contact_ids}")
        dossier_lines.append("    (a work-email DOMAIN matching the profile's employer is strong identity proof)")
    dossier_block = "\n".join(dossier_lines) or "  (sparse dossier)"
    me = _bullets(d["from_me"], "(no messages from me)")
    them = _bullets(d["from_them"], "(no messages from them)")
    li_block = "\n".join([
        f"  name: {li.get('full_name') or '(unknown)'}",
        f"  headline: {li.get('headline') or '(none)'}",
        f"  location: {li.get('location') or '(unknown)'}",
        "  experience:",
        _bullets(li.get("experiences") or [], "(none listed)"),
        "  education:",
        _bullets(li.get("education") or [], "(none listed)"),
    ])
    owner = f"\n{owner_block}\n" if owner_block else ""
    return (
        f"{owner}"
        f"CONTACT (from my messages) — {task['name']}\n{dossier_block}\n"
        f"  messages me→them:\n{me}\n  messages them→me:\n{them}\n\n"
        f"ATTACHED LINKEDIN PROFILE ({li.get('linkedin_url') or 'n/a'})\n{li_block}\n\n"
        f"Is this LinkedIn profile the same human as the contact I know from my messages?"
    )


async def judge_task(client: Any, task: dict[str, Any], owner_block: str, *, model: str,
                     effort: str, semaphore: asyncio.Semaphore, max_retries: int) -> dict[str, Any]:
    kwargs = responses_kwargs(model, effort=effort, schema=RECONCILE_SCHEMA, schema_name="reconcile")
    async with semaphore:
        attempt = 0
        while True:
            try:
                response = await client.responses.create(
                    model=model,
                    input=[{"role": "system", "content": SYSTEM_PROMPT},
                           {"role": "user", "content": judge_prompt(task, owner_block)}],
                    **kwargs,
                )
                return {"verdict": parse_json_response(response, "reconcile"), "usage": usage_tokens(response), "error": ""}
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                if is_retryable(exc) and attempt <= max_retries:
                    await asyncio.sleep(min(2 ** attempt, 30))
                    continue
                return {"verdict": {}, "usage": {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0},
                        "error": f"{type(exc).__name__}: {exc}"[:200]}


def deterministic_verdict(task: dict[str, Any]) -> dict[str, Any]:
    """Offline/tests fallback (--no-llm): trusts the attached link unless it's missing."""
    li = task["linkedin"]
    if not li or not li.get("has_profile"):
        return {"verdict": "needs_review", "confidence": 0.0, "supporting_evidence": [],
                "contradicting_evidence": [], "linkedin_plausibly_absent": True,
                "recommend_deep_research": False, "reason": "no usable LinkedIn profile",
                "spam_contact": False, "spam_confidence": 0.0, "spam_reason": ""}
    return {"verdict": "confirmed", "confidence": 0.9, "supporting_evidence": ["attached link (offline stub)"],
            "contradicting_evidence": [], "linkedin_plausibly_absent": False,
            "recommend_deep_research": False, "reason": "offline stub: trusts attached link",
            "spam_contact": False, "spam_confidence": 0.0, "spam_reason": ""}


# --- parent markdown injection ----------------------------------------------

_BADGE = {"confirmed": "✅ confirmed", "wrong_person": "⚠️ wrong person", "needs_review": "❓ needs review"}


def render_section(verdict: dict[str, Any], li: dict[str, Any]) -> str:
    v = verdict.get("verdict", "needs_review")
    conf = float(verdict.get("confidence") or 0)
    lines = [f"**{_BADGE.get(v, v)}** ({conf:.2f}) — _{verdict.get('reason', '')}_", ""]
    url = li.get("linkedin_url") or ""
    if url:
        lines.append(f"- Profile: {url}  ({li.get('headline') or 'no headline'})")
    if verdict.get("supporting_evidence"):
        lines.append("- Supporting:")
        lines += [f"  - {x}" for x in verdict["supporting_evidence"]]
    if verdict.get("contradicting_evidence"):
        lines.append("- Contradicting:")
        lines += [f"  - {x}" for x in verdict["contradicting_evidence"]]
    if verdict.get("linkedin_plausibly_absent"):
        lines.append("- _Person may legitimately have no LinkedIn._")
    return "\n".join(lines)


def inject_section(path: Path, body: str) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    head = text.split(SECTION_ANCHOR)[0].rstrip()
    path.write_text(f"{head}\n\n{SECTION_ANCHOR}\n\n{body}\n", encoding="utf-8")


# --- decide what auto-applies (incl. conflict auto-resolution) --------------

def decide_actions(tasks: list[dict[str, Any]], confirm_threshold: float,
                   detach_threshold: float | None = None) -> None:
    """Annotate each task with `action` ∈ {confirm, detach, review} and `via`.

    ASYMMETRIC, keep-biased thresholds: a `confirmed` link auto-VERIFIES at the (low)
    confirm_threshold — keeping a slightly-wrong link is cheap because the user fixes it
    in review — while a `wrong_person` link auto-DETACHES only at the (higher)
    detach_threshold, since wrongly dropping a real person removes them from people.csv.

    Non-conflict parent: confirmed≥confirm_threshold → confirm, wrong_person≥detach_threshold
    → detach; anything else → review.

    Conflict parent (one canonical person, MULTIPLE different attached LinkedIns):
    auto-RESOLVE only the unambiguous shape — exactly ONE confirmed (≥confirm_threshold)
    and EVERY other candidate a wrong_person (≥detach_threshold). Keep the confirmed,
    detach the wrong (via=conflict_resolved). Any other conflict shape stays → review."""
    detach_threshold = confirm_threshold if detach_threshold is None else detach_threshold

    def hi(task: dict[str, Any], verdict: str) -> bool:
        v = task.get("verdict") or {}
        bar = detach_threshold if verdict == "wrong_person" else confirm_threshold
        return v.get("verdict") == verdict and float(v.get("confidence") or 0) >= bar

    by_parent: dict[str, list[dict[str, Any]]] = {}
    for t in tasks:
        t["action"], t["via"] = "review", ""
        by_parent.setdefault(t["parent_slug"], []).append(t)

    for group in by_parent.values():
        judged = [t for t in group if not t.get("no_link")]
        if any(t.get("conflict") for t in group):
            confirmed_hi = [t for t in judged if hi(t, "confirmed")]
            wrong_hi = [t for t in judged if hi(t, "wrong_person")]
            if len(confirmed_hi) == 1 and len(wrong_hi) == len(judged) - 1 and len(judged) >= 2:
                confirmed_hi[0]["action"], confirmed_hi[0]["via"] = "confirm", "conflict_resolved"
                for t in wrong_hi:
                    t["action"], t["via"] = "detach", "conflict_resolved"
            continue  # ambiguous conflicts stay as review
        for t in judged:
            if hi(t, "confirmed"):
                t["action"], t["via"] = "confirm", "normal"
            elif hi(t, "wrong_person"):
                t["action"], t["via"] = "detach", "normal"


# --- durable override (consumed by the fan-in merge) ------------------------

OVERRIDE_COLUMNS = ["public_identifier", "action", "approved", "new_linkedin_url",
                    "new_public_identifier", "linkedin_url", "match_emails", "match_phones",
                    "confidence", "reason", "person_id", "source", "updated_at",
                    # Machine-owned spam screen (backwards compatible: older files simply lack
                    # them). The LLM may ALWAYS refresh these three — and ONLY these three — on
                    # any row, including user-decided ones; action/approved stay user-owned.
                    "llm_reject", "llm_reject_confidence", "llm_reject_reason",
                    # USER-owned network-worth mark (yes|maybe|no; blank = defer to the
                    # synthesis LLM's network_worth in facts). Sticky like approved —
                    # the machine never writes it.
                    "network_worth"]
# A user-touched approval is sticky — re-runs never overwrite these rows.
USER_APPROVED = {"yes", "no"}


def load_override_rows(path: Path) -> dict[str, dict[str, str]]:
    """Existing decisions keyed by public_identifier (tolerates a pre-approval-column file)."""
    rows: dict[str, dict[str, str]] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                pub = (row.get("public_identifier") or "").strip().lower()
                if pub:
                    rows[pub] = row
    return rows


def _write_override_rows(path: Path, rows: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=OVERRIDE_COLUMNS)
        w.writeheader()
        for pub in sorted(rows):
            w.writerow({k: rows[pub].get(k, "") for k in OVERRIDE_COLUMNS})


# A low-confidence verdict still suggests an action, written PENDING for the user to approve.
_VERDICT_TO_ACTION = {"wrong_person": "detach", "confirmed": "verify", "needs_review": "verify"}


def _llm_reject_fields(v: dict[str, Any]) -> dict[str, str]:
    """The machine-owned spam columns. Always refreshable — including on user-decided rows —
    because they never carry a decision, only the model's latest read of the relationship."""
    if v.get("spam_contact"):
        return {"llm_reject": "spam",
                "llm_reject_confidence": f"{float(v.get('spam_confidence') or 0):.3f}",
                "llm_reject_reason": v.get("spam_reason", "")}
    return {"llm_reject": "", "llm_reject_confidence": "", "llm_reject_reason": ""}


def write_overrides(path: Path, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """Upsert EVERY judged row into the single durable, approval-aware decisions table — the
    one file the user edits and the fan-in merge re-applies.

    High-confidence (action confirm/detach) -> `approved=auto` (applied at merge).
    Everything else (low-confidence / needs_review / ambiguous conflict) -> `approved=` PENDING,
    with a suggested action mapped from the verdict (wrong_person->detach, confirmed/needs_review
    ->verify). The merge applies only approved ∈ {auto,yes}; pending rows wait for the user to set
    `yes` (or flip the action). Idempotent + INCREMENTAL: keyed by public_identifier, a row the
    USER has touched (approved ∈ {yes,no}) is NEVER overwritten — sticky across re-runs. people.csv
    is NOT touched. no-LinkedIn people have no row (nothing to act on)."""
    existing = load_override_rows(path)
    detach = verify = pending = preserved = 0
    for t in tasks:
        if t.get("no_link"):
            continue
        pub = (t.get("candidate_key") or "").strip().lower()
        if not pub:
            continue
        if (existing.get(pub, {}).get("approved") or "").strip().lower() in USER_APPROVED:
            # sticky: never overwrite a user decision — but the machine-owned llm_* columns
            # are always refreshed, so a re-review can flag spam without touching the decision.
            existing[pub].update(_llm_reject_fields(t.get("verdict") or {}))
            preserved += 1
            continue
        v = t.get("verdict") or {}
        action = t.get("action")
        if action == "confirm":
            ov_action, approved = "verify", "auto"
        elif action == "detach":
            ov_action, approved = "detach", "auto"
        else:  # review -> pending, suggest an action from the verdict
            ov_action, approved = _VERDICT_TO_ACTION.get(v.get("verdict", ""), "verify"), ""
        existing[pub] = {
            "public_identifier": pub, "action": ov_action, "approved": approved,
            "new_linkedin_url": "", "new_public_identifier": "",
            "linkedin_url": (t.get("linkedin") or {}).get("linkedin_url", ""),
            "match_emails": "|".join(t.get("match_emails") or []),
            "match_phones": "|".join(t.get("match_phones") or []),
            "confidence": f"{float(v.get('confidence') or 0):.3f}",
            "reason": v.get("reason", ""), "person_id": (t.get("person_ids") or [""])[0],
            "source": "deep-context-reconcile", "updated_at": now_iso(),
            **_llm_reject_fields(v),
            # user-owned; survives machine rebuilds even on non-user-approved rows
            "network_worth": existing.get(pub, {}).get("network_worth", ""),
        }
        if approved == "auto":
            detach += ov_action == "detach"
            verify += ov_action == "verify"
        else:
            pending += 1

    _write_override_rows(path, existing)
    return {"path": str(path), "detached": detach, "verified": verify, "pending": pending,
            "preserved_user_rows": preserved, "total_rows": len(existing)}


def count_pending(path: Path) -> int:
    """Rows awaiting the user's decision (pending or rejected-but-revisitable)."""
    return sum(1 for r in load_override_rows(path).values()
               if (r.get("approved") or "").strip().lower() not in ("auto", "yes", "no"))


def upsert_retargets(path: Path, proposals: list[dict[str, Any]]) -> dict[str, Any]:
    """Add/refresh `retarget` rows (the CORRECT LinkedIn for a detached person) into the same
    decisions table. Default `approved=` pending (re-attaching a wrong identity is worse than
    dropping, so it needs a `yes`) unless a proposal sets it. Same sticky upsert: a row the user
    already decided (approved in {yes,no}) is preserved. Used by deep research + manual edits."""
    existing = load_override_rows(path)
    proposed = preserved = 0
    for p in proposals:
        old_pub = (p.get("old_public_identifier") or "").strip().lower()
        new_url = normalize_linkedin_url(p.get("new_linkedin_url") or "")
        if not old_pub or not new_url:
            continue
        if (existing.get(old_pub, {}).get("approved") or "").strip().lower() in USER_APPROVED:
            preserved += 1
            continue
        prior = existing.get(old_pub, {})
        existing[old_pub] = {
            "public_identifier": old_pub, "action": "retarget",
            "approved": (p.get("approved") or "").strip().lower(),
            "new_linkedin_url": new_url,
            "new_public_identifier": (p.get("new_public_identifier") or extract_public_identifier(new_url)).lower(),
            "linkedin_url": p.get("linkedin_url") or prior.get("linkedin_url", ""),
            "match_emails": "|".join(p.get("match_emails") or []) or prior.get("match_emails", ""),
            "match_phones": "|".join(p.get("match_phones") or []) or prior.get("match_phones", ""),
            "confidence": f"{float(p.get('confidence') or 0):.3f}",
            "reason": p.get("reason", ""), "person_id": p.get("person_id", prior.get("person_id", "")),
            "source": p.get("source", "deep-research"), "updated_at": now_iso(),
            # user-owned; survives machine rebuilds even on non-user-approved rows
            "network_worth": prior.get("network_worth", ""),
        }
        proposed += 1
    _write_override_rows(path, existing)
    return {"path": str(path), "proposed": proposed, "preserved_user_rows": preserved, "total_rows": len(existing)}


def write_consolidations(path: Path, tasks: list[dict[str, Any]], people_csv: Path) -> dict[str, Any]:
    """Fold a parent's children onto its KEPT LinkedIn (trust Phase 2's grouping).

    For each parent with a kept (`confirm`) link AND ≥1 detached sibling, emit ONE contact-only
    people row keyed by the kept `public_identifier` carrying the UNION of every child's emails /
    phones / per-channel interaction_counts / source_channels. The fan-in merge auto-ingests it;
    because it shares the kept LinkedIn key it unions onto the real row (which supplies the
    profile), so the surviving person keeps the correct profile AND all the contacts of its
    siblings — while the sibling rows still detach/drop. Per-channel counts are preserved
    (merge_interaction_counts is channel-wise, never summed)."""
    people = load_people_rows(people_csv)
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for t in tasks:
        by_parent.setdefault(t["parent_slug"], []).append(t)

    rows: list[dict[str, str]] = []
    for group in by_parent.values():
        kept = next((t for t in group if t.get("action") == "confirm"), None)
        detached = [t for t in group if t.get("action") == "detach"]
        if not kept or not detached:
            continue
        pub = (kept.get("candidate_key") or "").strip().lower()
        if not pub:
            continue
        pids: list[str] = []
        for t in group:
            pids.extend(t.get("person_ids") or [])
        emails: list[str] = []
        phones: list[str] = []
        ic_values: list[str] = []
        channels: set[str] = set()
        for pid in dict.fromkeys(pids):
            r = people.get(pid)
            if not r:
                continue
            for e in [r.get("primary_email", ""), *parse_list(r.get("all_emails"))]:
                ne = normalize_email(e)
                if ne and "@" in ne and ne not in emails:
                    emails.append(ne)
            for ph in [r.get("primary_phone", ""), *parse_list(r.get("all_phones"))]:
                npn = normalize_phone(ph)
                if npn and npn not in phones:
                    phones.append(npn)
            if r.get("interaction_counts"):
                ic_values.append(r["interaction_counts"])
            for c in (r.get("source_channels") or "").split(","):
                if c.strip():
                    channels.add(c.strip())
        ic = merge_interaction_counts(*ic_values)
        row = {c: "" for c in PEOPLE_SCHEMA_COLUMNS}
        row["public_identifier"] = pub
        row["linkedin_url"] = (kept.get("linkedin") or {}).get("linkedin_url", "")
        row["primary_email"] = emails[0] if emails else ""
        row["all_emails"] = json.dumps(emails) if emails else ""
        row["primary_phone"] = phones[0] if phones else ""
        row["all_phones"] = json.dumps(phones) if phones else ""
        row["interaction_counts"] = json.dumps(ic) if ic else ""
        row["source_channels"] = ",".join(sorted(channels))
        rows.append(row)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=PEOPLE_SCHEMA_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return {"path": str(path), "consolidated_parents": len(rows)}


def decided_report(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-row preview of what the override will do at the next merge (for applied.csv)."""
    rows: list[dict[str, Any]] = []
    for t in tasks:
        action = t.get("action")
        if action not in ("confirm", "detach"):
            continue
        v = t.get("verdict") or {}
        rows.append({
            "parent_slug": t.get("parent_slug", ""), "name": t.get("name", ""),
            "person_id": (t.get("person_ids") or [""])[0],
            "action": "verified_kept" if action == "confirm" else "detached",
            "via": t.get("via", ""), "confidence": round(float(v.get("confidence") or 0), 3),
            "linkedin_url": (t.get("linkedin") or {}).get("linkedin_url", ""),
            "reason": v.get("reason", ""),
        })
    return rows


def write_summary(path: Path, tasks: list[dict[str, Any]], override_path: Path,
                  consolidation: dict[str, Any]) -> None:
    """ONE human-readable report — what changed + what needs review. The user reads this and
    edits ONE file (the decisions table) to approve/reject."""
    detached = [t for t in tasks if t.get("action") == "detach"]
    verified = sum(1 for t in tasks if t.get("action") == "confirm")
    no_link = sum(1 for t in tasks if t.get("no_link"))
    ov = load_override_rows(override_path)

    def _is_pending(r: dict[str, Any]) -> bool:
        return (r.get("approved") or "").strip().lower() not in ("auto", "yes", "no")

    pending_retargets = [r for r in ov.values() if (r.get("action") or "") == "retarget" and _is_pending(r)]
    pending_other = [r for r in ov.values() if (r.get("action") or "") != "retarget" and _is_pending(r)]

    def _line(t: dict[str, Any]) -> str:
        v = t.get("verdict") or {}
        url = (t.get("linkedin") or {}).get("linkedin_url", "")
        return f"- **{t.get('name', '?')}** ({float(v.get('confidence') or 0):.2f}) — _{v.get('reason', '')}_  ·  {url}"

    lines = ["# Deep-context self-heal — what changed", "", f"_Generated {now_iso()}._", "",
             "Applied automatically (lands on your next fan-in merge + index rebuild):", ""]
    lines.append(f"- 🔧 **Detached {len(detached)}** wrong LinkedIn link(s)")
    lines.append(f"- 🔁 **Consolidated {consolidation.get('consolidated_parents', 0)}** "
                 "people — folded siblings' emails/phones onto the kept LinkedIn")
    lines.append(f"- ✅ **Verified {verified}** link(s) (not listed)")
    if detached:
        lines += ["", "## 🔧 Detached (wrong link removed)", ""]
        lines += [_line(t) for t in sorted(detached, key=lambda t: -(float((t.get('verdict') or {}).get('confidence') or 0)))]

    total_review = len(pending_retargets) + len(pending_other) + no_link
    lines += ["", f"## ❓ Needs your review ({total_review})",
              "_Edit the `approved` column in the decisions table to act — set `yes` to apply, "
              "`no` to reject (your edit is sticky). The merge applies only `auto`/`yes`._", ""]
    if pending_retargets:
        lines.append(f"- **{len(pending_retargets)} retarget(s)** — a correct LinkedIn was found; "
                     "set `approved=yes` then run apply-retargets to re-attach.")
    if pending_other:
        lines.append(f"- **{len(pending_other)} low-confidence row(s)** to confirm/reject:")
        for r in sorted(pending_other, key=lambda r: -(float(r.get("confidence") or 0)))[:15]:
            lines.append(f"  - **{r.get('person_id', '')[:8]}** {r.get('action', '')} "
                         f"({float(r.get('confidence') or 0):.2f}) — _{r.get('reason', '')}_  ·  {r.get('linkedin_url', '')}")
        if len(pending_other) > 15:
            lines.append(f"  - …and {len(pending_other) - 15} more (in the decisions table)")
    if no_link:
        lines.append(f"- **{no_link} person(s) with no LinkedIn** — nothing to act on (left as-is).")
    if not total_review:
        lines.append("_Nothing — every decision was high-confidence._")

    lines += ["", "---", "_The one file to edit: "
              "`.powerpacks/network-import/overrides/review.csv` (`approved` column, sticky). "
              "Drill-down: `reconcile/applied.csv`._"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_applied(path: Path, rows: list[dict[str, Any]]) -> None:
    """Audit report of what the override will apply — so the user can review what was done."""
    fields = ["parent_slug", "name", "person_id", "action", "via", "confidence", "linkedin_url", "reason"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in sorted(rows, key=lambda r: (r.get("via") != "conflict_resolved", r.get("action", ""))):
            w.writerow({k: r.get(k, "") for k in fields})


# --- output writers ---------------------------------------------------------

def _flat(r: dict[str, Any]) -> dict[str, Any]:
    v = r.get("verdict") or {}
    return {
        "parent_slug": r["parent_slug"], "name": r["name"],
        "linkedin_url": r["linkedin"].get("linkedin_url", "") if r.get("linkedin") else "",
        "verdict": v.get("verdict", "no_link" if r.get("no_link") else ""),
        "confidence": round(float(v.get("confidence") or 0), 3),
        "conflict": r.get("conflict", False),
        "linkedin_plausibly_absent": v.get("linkedin_plausibly_absent", ""),
        "recommend_deep_research": v.get("recommend_deep_research", ""),
        "supporting": " | ".join(v.get("supporting_evidence") or []),
        "contradicting": " | ".join(v.get("contradicting_evidence") or []),
        "reason": v.get("reason", ""),
    }


def write_verdicts(jsonl_path: Path, csv_path: Path, results: list[dict[str, Any]]) -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps({k: r[k] for k in ("parent_slug", "name", "candidate_key",
                     "person_ids", "conflict", "no_link", "linkedin", "match_emails",
                     "match_phones", "verdict", "error")
                     if k in r}, ensure_ascii=False) + "\n")
    fields = ["parent_slug", "name", "linkedin_url", "verdict", "confidence", "conflict",
              "linkedin_plausibly_absent", "recommend_deep_research", "supporting", "contradicting", "reason"]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in sorted(results, key=lambda r: float((r.get("verdict") or {}).get("confidence") or 0), reverse=True):
            w.writerow(_flat(r))


# --- driver -----------------------------------------------------------------

def load_tasks_from_verdicts(path: Path) -> list[dict[str, Any]]:
    """Reload already-judged tasks from verdicts.jsonl (for --reapply, no LLM spend)."""
    tasks = []
    for rec in _read_jsonl(path):
        rec.setdefault("verdict", {})
        rec.setdefault("linkedin", {})
        tasks.append(rec)
    return tasks


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    index = _read_json(Path(args.index_json))

    # --reapply: re-decide/apply from the existing verdicts (e.g. after changing the
    # auto-resolution rule) without re-judging — no OpenAI spend. Still overlays the
    # deterministic connection ground-truth, so it's free to fold in your LinkedIn connections.
    if getattr(args, "reapply", False):
        tasks = load_tasks_from_verdicts(Path(args.verdicts_jsonl))
        # Drop verdicts for parents that no longer exist (e.g. an owner-alias parent that
        # build_parents now excludes) so they fall out of the review table/UI for free.
        valid_parents = set(index.get("parents", {}))
        if valid_parents:
            tasks = [t for t in tasks if t.get("parent_slug") in valid_parents]
        people = load_people_rows(Path(args.people_csv))
        facts_dir = Path(args.facts_dir)
        for t in tasks:
            if t.get("no_link"):
                continue
            if _from_connections(t.get("person_ids") or [], people):
                t["from_connections"], t["verdict"], t["error"] = True, connection_verdict(), ""
            # Recompute the self-reported LinkedIn from facts so the free recovery also runs here.
            url, pub = self_linkedin_from_facts(t.get("person_ids") or [], facts_dir)
            t["dossier"] = {**(t.get("dossier") or {}), "self_linkedin_url": url, "self_linkedin_pub": pub}
        return _finalize(args, tasks, index, usage_total={"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0},
                         use_llm=False, judged=sum(1 for t in tasks if not t.get("no_link")), started=started)

    people = load_people_rows(Path(args.people_csv))
    tasks = build_tasks(index, people, Path(args.facts_dir), Path(args.raw_dir), Path(args.profile_cache_dir))
    # Subset targeting (--slug/--limit): cheap spot re-reviews (e.g. testing the spam screen)
    # without re-judging everyone. Results MERGE into verdicts.jsonl (see _finalize).
    if getattr(args, "slug", None):
        wanted = {s.strip().lower() for s in args.slug}
        tasks = [t for t in tasks if (t.get("parent_slug") or "").lower() in wanted]
    if getattr(args, "limit", 0):
        tasks = tasks[: args.limit]
    # Ground truth first: contacts who ARE your LinkedIn connections are confirmed without the LLM.
    connections = [t for t in tasks if t.get("from_connections") and not t.get("no_link")]
    for t in connections:
        t["verdict"], t["error"] = connection_verdict(), ""
    judgeable = [t for t in tasks if not t.get("no_link") and t["linkedin"].get("has_profile")
                 and not t.get("from_connections")]

    if args.dry_run:
        # ~ cost bracket: judgeable tasks * (rich-context floor/ceiling) — no spend.
        per_lo, per_hi = 0.004, 0.02
        manifest = {
            "source": "reconcile_linkedin", "status": "dry_run",
            "parents": len(index.get("parents", {})), "tasks": len(tasks),
            "judgeable": len(judgeable), "no_link": sum(1 for t in tasks if t.get("no_link")),
            "ground_truth_connections": len(connections),
            "conflicts": sum(1 for t in tasks if t.get("conflict")),
            "estimated_cost_usd_low": round(len(judgeable) * per_lo, 2),
            "estimated_cost_usd_high": round(len(judgeable) * per_hi, 2),
            "model": args.model, "reasoning_effort": reasoning_effort(args.reasoning_effort),
            "elapsed_ms": int((time.monotonic() - started) * 1000), "updated_at": now_iso(),
        }
        return manifest

    usage_total = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    use_llm = not args.no_llm
    owner_block = owner_background_block(load_owner()) if load_owner() else ""

    if use_llm and judgeable:
        load_env()
        # Wall-time is bound by per-call high-reasoning latency, not local CPU — so parallelize hard.
        concurrency = args.concurrency or env_or_profile_int("POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency", fallback=64)
        effort = reasoning_effort(args.reasoning_effort)

        async def driver() -> None:
            client = make_async_client(timeout=args.timeout)
            semaphore = asyncio.Semaphore(max(1, concurrency))
            collected: dict[int, dict[str, Any]] = {}

            async def one(i: int, task: dict[str, Any]) -> tuple[int, dict[str, Any]]:
                return i, await judge_task(client, task, owner_block, model=args.model,
                                           effort=effort, semaphore=semaphore, max_retries=args.max_retries)
            try:
                await drain_pool([one(i, t) for i, t in enumerate(judgeable)], lambda r: collected.__setitem__(r[0], r[1]))
            finally:
                await client.close()
            for i, task in enumerate(judgeable):
                res = collected.get(i, {"verdict": {}, "usage": {}, "error": "no result"})
                for k in usage_total:
                    usage_total[k] += res.get("usage", {}).get(k, 0)
                task["verdict"] = res.get("verdict") or {}
                task["error"] = res.get("error", "")
        asyncio.run(driver())
    elif judgeable:
        for task in judgeable:
            task["verdict"] = deterministic_verdict(task)
            task["error"] = ""

    # Tasks without a usable profile still get a (no-spend) verdict so they route to review.
    for task in tasks:
        if "verdict" not in task:
            task["verdict"] = deterministic_verdict(task)
            task["error"] = ""

    # A subset run must not clobber the full verdicts file: overlay the fresh rows onto the
    # existing verdicts so the review UI keeps seeing everyone.
    if getattr(args, "slug", None) or getattr(args, "limit", 0):
        tasks = merge_subset_tasks(Path(args.verdicts_jsonl), tasks)

    return _finalize(args, tasks, index, usage_total=usage_total, use_llm=use_llm,
                     judged=len(judgeable), started=started)


def merge_subset_tasks(verdicts_path: Path, fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Overlay freshly judged tasks onto the existing verdicts file by (parent_slug,
    candidate_key). Existing rows keep their old verdicts; downstream decide/override
    passes are idempotent and sticky, so re-running them over the merged set is safe."""
    existing = load_tasks_from_verdicts(verdicts_path)
    merged: dict[tuple[str, str], dict[str, Any]] = {
        ((t.get("parent_slug") or ""), (t.get("candidate_key") or "")): t for t in existing
    }
    for t in fresh:
        merged[((t.get("parent_slug") or ""), (t.get("candidate_key") or ""))] = t
    return list(merged.values())


def _finalize(args: argparse.Namespace, tasks: list[dict[str, Any]], index: dict[str, Any], *,
              usage_total: dict[str, int], use_llm: bool, judged: int, started: float) -> dict[str, Any]:
    """Shared tail: decide -> verdicts/review/applied outputs -> parent injection -> manifest."""
    out_dir = Path(args.verdicts_jsonl).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    write_verdicts(Path(args.verdicts_jsonl), Path(args.verdicts_csv), tasks)

    decide_actions(tasks, args.confirm_threshold, getattr(args, "detach_threshold", DEFAULT_DETACH))   # one authoritative decision pass
    parents_dir = Path(args.parents_dir)
    for task in tasks:
        if task.get("verdict") and not task.get("no_link"):
            inject_section(parents_dir / f"{task['parent_slug']}.md", render_section(task["verdict"], task["linkedin"]))

    override_stats = {"path": str(args.overrides_csv), "detached": 0, "verified": 0, "pending": 0, "total_rows": 0}
    consolidation = {"consolidated_parents": 0}
    self_retargets = {"proposed": 0}
    if not args.no_overrides:
        override_stats = write_overrides(Path(args.overrides_csv), tasks)
        # Free recovery: retarget to a LinkedIn the contact shared themselves (overrides any
        # detach/verify on the wrong attached link). Sticky — won't clobber a user decision.
        self_retargets = upsert_retargets(Path(args.overrides_csv), self_reported_retargets(tasks))
        # Fold each parent's children's contacts onto its kept LinkedIn (trust Phase 2).
        consolidation = write_consolidations(Path(args.consolidate_people_csv), tasks, Path(args.people_csv))
    write_applied(out_dir / "applied.csv", decided_report(tasks))
    write_summary(out_dir / "summary.md", tasks, Path(args.overrides_csv), consolidation)

    counts = {v: 0 for v in VERDICTS}
    for task in tasks:
        v = (task.get("verdict") or {}).get("verdict")
        if v in counts:
            counts[v] += 1
    conflict_tasks = [t for t in tasks if t.get("conflict")]
    dr_subset = [t for t in tasks
                 if (t.get("verdict") or {}).get("verdict") == "wrong_person"
                 and float((t.get("verdict") or {}).get("confidence") or 0) >= getattr(args, "detach_threshold", DEFAULT_DETACH)
                 and (t.get("verdict") or {}).get("recommend_deep_research")
                 and not (t.get("verdict") or {}).get("linkedin_plausibly_absent")]

    billed_output = usage_total["output_tokens"] + usage_total["reasoning_tokens"]
    manifest = {
        "source": "reconcile_linkedin", "status": "completed",
        "judge": "llm" if use_llm else "deterministic",
        "parents": len(index.get("parents", {})), "tasks": len(tasks), "judged": judged,
        "ground_truth_connections": sum(1 for t in tasks if t.get("from_connections") and not t.get("no_link")),
        "self_reported_retargets": self_retargets.get("proposed", 0),
        "verdicts": counts, "conflicts": len(conflict_tasks),
        "conflicts_auto_resolved": sum(1 for t in conflict_tasks if t.get("via") == "conflict_resolved"),
        "conflicts_to_review": sum(1 for t in conflict_tasks if t.get("action") == "review"),
        "no_link": sum(1 for t in tasks if t.get("no_link")),
        "errors": sum(1 for t in tasks if t.get("error")),
        "overrides": override_stats, "consolidation": consolidation,
        "summary_md": str(out_dir / "summary.md"),
        "applied_csv": str(out_dir / "applied.csv"),
        "needs_review": override_stats.get("pending", 0) + sum(1 for t in tasks if t.get("no_link")),
        "deep_research_eligible": len(dr_subset),
        "deep_research_est_usd": round(len(dr_subset) * DR_COST_PER_PERSON, 2),
        "tokens": usage_total,
        "estimated_cost_usd": estimate_cost_usd(usage_total["input_tokens"], billed_output, args.model),
        "elapsed_ms": int((time.monotonic() - started) * 1000), "updated_at": now_iso(),
    }
    write_json(out_dir / "manifest.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Reconcile parents against their attached LinkedIn profile (self-heal).")
    p.add_argument("--index-json", default=str(INDEX_JSON))
    p.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    p.add_argument("--profile-cache-dir", default=str(PROFILE_CACHE_DIR))
    p.add_argument("--facts-dir", default=str(FACTS_DIR))
    p.add_argument("--raw-dir", default=str(RAW_DIR))
    p.add_argument("--parents-dir", default=str(PARENTS_DIR))
    p.add_argument("--verdicts-jsonl", default=str(VERDICTS_JSONL))
    p.add_argument("--verdicts-csv", default=str(VERDICTS_CSV))
    p.add_argument("--confirm-threshold", type=float, default=DEFAULT_CONFIRM,
                   help="Min judge confidence to auto-VERIFY a confirmed link (else PENDING). Keep-biased (low).")
    p.add_argument("--detach-threshold", type=float, default=DEFAULT_DETACH,
                   help="Min judge confidence to auto-DETACH a wrong_person link (else PENDING). Strict (high).")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--reasoning-effort", default="high", choices=["minimal", "low", "medium", "high"])
    p.add_argument("--concurrency", type=int, default=0)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--max-retries", type=int, default=6)
    p.add_argument("--overrides-csv", default=str(LINKEDIN_OVERRIDES_CSV),
                   help="Durable override the fan-in merge re-applies (detach/verify per public_identifier)")
    p.add_argument("--consolidate-people-csv", default=str(CONSOLIDATE_PEOPLE_CSV),
                   help="Contact-only rows folding each parent's children onto its kept LinkedIn")
    p.add_argument("--slug", action="append", default=None,
                   help="Only re-judge these parent slugs (repeatable). Results merge into verdicts.jsonl.")
    p.add_argument("--limit", type=int, default=0,
                   help="Only re-judge the first N tasks (0 = all). Results merge into verdicts.jsonl.")
    p.add_argument("--dry-run", action="store_true", help="Estimate cost only; no spend, no writes")
    p.add_argument("--no-overrides", action="store_true", help="Write verdicts but do NOT update the override table")
    p.add_argument("--no-llm", action="store_true", help="Deterministic fallback (offline/tests only)")
    p.add_argument("--reapply", action="store_true",
                   help="Re-decide/write overrides from existing verdicts.jsonl (no re-judging, no OpenAI spend)")
    return p


def main(argv: list[str] | None = None) -> int:
    emit(run(build_parser().parse_args(argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
