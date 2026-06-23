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

Outputs (under .powerpacks/deep-context/reconcile/):
  verdicts.jsonl     full per-candidate record (verdict + evidence + usage)
  verdicts.csv       flat review table
  review-queue.csv   low-confidence rows + conflicts (blank user_decision column)
  manifest.json      counts per verdict + conflicts + tokens + cost
  (a "## LinkedIn identity" section injected into each parent markdown)
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import shutil
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
    INDEX_JSON,
    PARENTS_DIR,
    PROFILE_CACHE_DIR,
    RAW_DIR,
    REVIEW_QUEUE_CSV,
    VERDICTS_CSV,
    VERDICTS_JSONL,
    emit,
    load_env,
    load_owner,
    now_iso,
    owner_background_block,
    write_json,
)
from packs.ingestion.primitives.enrich_people.enrich_people import (
    profile_cache_path,
    read_usable_cached_profile,
)
from packs.ingestion.schemas.people_schema import (
    extract_public_identifier,
    parse_jsonish,
)

DEFAULT_CONFIRM = 0.85         # auto-apply only at/above this judge confidence
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
    "Reason HOLISTICALLY. A shared NAME ALONE is never enough. Decide using corroboration "
    "and contradiction:\n"
    "- confirmed: the profile's employer / school / location / role / seniority clearly "
    "lines up with the dossier (people change jobs, so a PAST employer or school match still "
    "counts as corroboration).\n"
    "- wrong_person: the profile CONTRADICTS the dossier — a different industry, city, era, "
    "or career stage that cannot be the same human (e.g. the dossier shows a local friend / "
    "tradesperson but the profile is a big-company CEO with the same name), OR the ONLY thing "
    "linking them is the name with no corroborating evidence.\n"
    "- needs_review: there is too little evidence either way to be confident.\n\n"
    "Some people legitimately have NO LinkedIn. If the dossier suggests this person plausibly "
    "would not have a (matching) profile, set linkedin_plausibly_absent=true rather than "
    "forcing a verdict. Set recommend_deep_research=true only when EXTERNAL research could "
    "realistically resolve the identity (i.e. not when they plausibly have no profile at all). "
    "Cite concrete supporting and contradicting evidence."
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
    },
    "required": ["verdict", "confidence", "supporting_evidence", "contradicting_evidence",
                 "linkedin_plausibly_absent", "recommend_deep_research", "reason"],
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
        source = "cache"
    else:  # fall back to people.csv columns (same RapidAPI fetch, fewer descriptions)
        exps = parse_jsonish(row.get("work_experiences"), []) or []
        edus = parse_jsonish(row.get("education"), []) or []
        location = ", ".join(x for x in [row.get("city"), row.get("state"), row.get("country")] if x)
        full_name = row.get("full_name") or ""
        headline = row.get("headline") or ""
        source = "people_csv"
    experiences = []
    for e in exps[:6]:
        title = e.get("title") or ""
        company = e.get("company_name") or e.get("company") or ""
        span = _fmt_span(e)
        line = " @ ".join(x for x in [title, company] if x) or company or title
        experiences.append(f"{line}{f' ({span})' if span else ''}".strip())
    education = []
    for ed in edus[:4]:
        school = ed.get("school") or ed.get("school_name") or ""
        degree = ", ".join(x for x in [ed.get("degree"), ed.get("field")] if x)
        education.append(f"{degree + ' — ' if degree else ''}{school}".strip(" —"))
    return {
        "public_identifier": pub,
        "linkedin_url": row.get("linkedin_url") or "",
        "full_name": full_name,
        "headline": headline,
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


def dossier_view(child_pids: list[str], facts_dir: Path, raw_dir: Path) -> dict[str, Any]:
    """Merge the confirmed children's facts + a few message samples for the judge."""
    records: list[dict[str, Any]] = []
    msgs: list[dict[str, Any]] = []
    for pid in child_pids:
        records.extend(_read_jsonl(facts_dir / f"{pid}.jsonl"))
        msgs.extend(_read_json(raw_dir / f"{pid}.json").get("messages") or [])
    merged = compose.merge_facts(records) if records else {}
    return {
        "relationship": str(merged.get("relationship_to_owner") or ""),
        "title": str(merged.get("title") or ""),
        "employers": [e.get("name", "") for e in (merged.get("employers") or []) if e.get("name")],
        "school": str(merged.get("school") or ""),
        "location": str(merged.get("location") or ""),
        "topics": list(merged.get("topics") or [])[:10],
        "shared_context": [f"{s.get('overlap', 'other')}: {s.get('detail', '')}"
                           for s in (merged.get("shared_context") or []) if s.get("detail")],
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
            tasks.append({
                "parent_slug": pslug, "name": pinfo.get("name", pslug),
                "candidate_key": key, "person_ids": pids, "conflict": conflict,
                "no_link": False, "dossier": dossier, "linkedin": linkedin_view(row, cache_dir),
            })
    return tasks


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
                "recommend_deep_research": False, "reason": "no usable LinkedIn profile"}
    return {"verdict": "confirmed", "confidence": 0.9, "supporting_evidence": ["attached link (offline stub)"],
            "contradicting_evidence": [], "linkedin_plausibly_absent": False,
            "recommend_deep_research": False, "reason": "offline stub: trusts attached link"}


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

def decide_actions(tasks: list[dict[str, Any]], threshold: float) -> None:
    """Annotate each task with `action` ∈ {confirm, detach, review} and `via`.

    Non-conflict parent: high-confidence confirmed → confirm, wrong_person → detach;
    anything else → review.

    Conflict parent (one canonical person, MULTIPLE different attached LinkedIns):
    auto-RESOLVE only the unambiguous shape — exactly ONE high-confidence `confirmed`
    and EVERY other candidate a high-confidence `wrong_person` (one right link, the rest
    wrong). Keep the confirmed, detach the wrong (via=conflict_resolved). Any other
    conflict shape (two confirmed, a needs_review, a low-confidence one) stays → review,
    so a human decides."""
    def hi(task: dict[str, Any], verdict: str) -> bool:
        v = task.get("verdict") or {}
        return v.get("verdict") == verdict and float(v.get("confidence") or 0) >= threshold

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


# --- auto-apply to people.csv -----------------------------------------------

APPLY_COLUMNS = ["linkedin_verified", "linkedin_verified_confidence",
                 "linkedin_verified_reason", "linkedin_url_rejected"]


def apply_verdicts(people_csv: Path, tasks: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    """Apply the decided actions to people.csv in place (after a .bkup backup).

    confirm -> annotate linkedin_verified=confirmed (non-destructive).
    detach  -> stash linkedin_url into linkedin_url_rejected, clear linkedin_url +
    public_identifier. Reversible + auditable. Idempotent: re-applying a detach keeps the
    already-stashed rejected url, and the pristine .bkup (pre-Phase-3) is never clobbered.
    Returns counts + a per-row report of exactly what was done."""
    decide_actions(tasks, threshold)
    decisions: dict[str, dict[str, Any]] = {}  # person_id -> action to apply
    for t in tasks:
        if t.get("action") not in ("confirm", "detach"):
            continue
        v = t.get("verdict") or {}
        for pid in t["person_ids"]:
            decisions[pid] = {"action": t["action"], "via": t.get("via", ""),
                              "confidence": float(v.get("confidence") or 0), "reason": v.get("reason", ""),
                              "parent_slug": t.get("parent_slug", ""), "name": t.get("name", "")}
    empty = {"applied": 0, "confirmed": 0, "detached": 0, "conflict_resolved": 0, "backup": "", "rows": []}
    if not people_csv.exists() or not decisions:
        return empty

    with people_csv.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    for col in APPLY_COLUMNS:
        if col not in fieldnames:
            fieldnames.append(col)

    confirmed = detached = resolved = 0
    report: list[dict[str, Any]] = []
    for row in rows:
        d = decisions.get(str(row.get("id") or "").strip())
        if not d:
            continue
        if d["via"] == "conflict_resolved":
            resolved += 1
        entry = {"parent_slug": d["parent_slug"], "name": d["name"], "person_id": row.get("id", ""),
                 "via": d["via"], "confidence": round(d["confidence"], 3), "reason": d["reason"]}
        if d["action"] == "confirm":
            row["linkedin_verified"] = "confirmed"
            row["linkedin_verified_confidence"] = f"{d['confidence']:.3f}"
            row["linkedin_verified_reason"] = d["reason"]
            confirmed += 1
            report.append({**entry, "action": "verified_kept", "linkedin_url": row.get("linkedin_url", "")})
        else:  # detach (idempotent: only stash a real current url)
            current = (row.get("linkedin_url") or "").strip()
            rejected = current or row.get("linkedin_url_rejected", "")
            if current:
                row["linkedin_url_rejected"] = current
                row["linkedin_url"] = ""
                row["public_identifier"] = ""
            row["linkedin_verified"] = "wrong_person"
            row["linkedin_verified_confidence"] = f"{d['confidence']:.3f}"
            row["linkedin_verified_reason"] = d["reason"]
            detached += 1
            report.append({**entry, "action": "detached", "linkedin_url": rejected})

    backup = str(people_csv) + ".bkup"
    if not Path(backup).exists():           # preserve the FIRST (pristine) copy only
        shutil.copy2(people_csv, backup)
    with people_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
    return {"applied": confirmed + detached, "confirmed": confirmed, "detached": detached,
            "conflict_resolved": resolved, "backup": backup, "rows": report}


def write_applied(path: Path, rows: list[dict[str, Any]]) -> None:
    """Audit report of what auto-applied — so the user can review what was done."""
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
                     "person_ids", "conflict", "no_link", "linkedin", "verdict", "error")
                     if k in r}, ensure_ascii=False) + "\n")
    fields = ["parent_slug", "name", "linkedin_url", "verdict", "confidence", "conflict",
              "linkedin_plausibly_absent", "recommend_deep_research", "supporting", "contradicting", "reason"]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in sorted(results, key=lambda r: float((r.get("verdict") or {}).get("confidence") or 0), reverse=True):
            w.writerow(_flat(r))


def write_review_queue(path: Path, tasks: list[dict[str, Any]], threshold: float) -> int:
    """Everything NOT auto-applied (action == review): med/low-confidence verdicts,
    needs_review, no-link, and ambiguous conflicts — for the user to decide on."""
    decide_actions(tasks, threshold)
    fields = ["parent_slug", "name", "linkedin_url", "verdict", "confidence", "conflict",
              "reason", "user_decision"]
    queued = []
    for t in tasks:
        if t.get("action") != "review":
            continue
        row = _flat(t)
        queued.append({k: row.get(k, "") for k in fields[:-1]} | {"user_decision": ""})
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(queued)
    return len(queued)


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
    # auto-resolution rule) without re-judging — no OpenAI spend.
    if getattr(args, "reapply", False):
        tasks = load_tasks_from_verdicts(Path(args.verdicts_jsonl))
        return _finalize(args, tasks, index, usage_total={"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0},
                         use_llm=False, judged=sum(1 for t in tasks if not t.get("no_link")), started=started)

    people = load_people_rows(Path(args.people_csv))
    tasks = build_tasks(index, people, Path(args.facts_dir), Path(args.raw_dir), Path(args.profile_cache_dir))
    judgeable = [t for t in tasks if not t.get("no_link") and t["linkedin"].get("has_profile")]

    if args.dry_run:
        # ~ cost bracket: judgeable tasks * (rich-context floor/ceiling) — no spend.
        per_lo, per_hi = 0.004, 0.02
        manifest = {
            "source": "reconcile_linkedin", "status": "dry_run",
            "parents": len(index.get("parents", {})), "tasks": len(tasks),
            "judgeable": len(judgeable), "no_link": sum(1 for t in tasks if t.get("no_link")),
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
        concurrency = args.concurrency or env_or_profile_int("POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency", fallback=16)
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

    return _finalize(args, tasks, index, usage_total=usage_total, use_llm=use_llm,
                     judged=len(judgeable), started=started)


def _finalize(args: argparse.Namespace, tasks: list[dict[str, Any]], index: dict[str, Any], *,
              usage_total: dict[str, int], use_llm: bool, judged: int, started: float) -> dict[str, Any]:
    """Shared tail: decide -> verdicts/review/applied outputs -> parent injection -> manifest."""
    out_dir = Path(args.verdicts_jsonl).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    write_verdicts(Path(args.verdicts_jsonl), Path(args.verdicts_csv), tasks)

    decide_actions(tasks, args.confirm_threshold)   # one authoritative decision pass
    queued = write_review_queue(Path(args.review_queue), tasks, args.confirm_threshold)
    parents_dir = Path(args.parents_dir)
    for task in tasks:
        if task.get("verdict") and not task.get("no_link"):
            inject_section(parents_dir / f"{task['parent_slug']}.md", render_section(task["verdict"], task["linkedin"]))

    apply_stats = {"applied": 0, "confirmed": 0, "detached": 0, "conflict_resolved": 0, "backup": "", "rows": []}
    if not args.no_apply:
        apply_stats = apply_verdicts(Path(args.people_csv), tasks, args.confirm_threshold)
    write_applied(out_dir / "applied.csv", apply_stats.get("rows", []))

    counts = {v: 0 for v in VERDICTS}
    for task in tasks:
        v = (task.get("verdict") or {}).get("verdict")
        if v in counts:
            counts[v] += 1
    conflict_tasks = [t for t in tasks if t.get("conflict")]
    dr_subset = [t for t in tasks
                 if (t.get("verdict") or {}).get("verdict") == "wrong_person"
                 and float((t.get("verdict") or {}).get("confidence") or 0) >= args.confirm_threshold
                 and (t.get("verdict") or {}).get("recommend_deep_research")
                 and not (t.get("verdict") or {}).get("linkedin_plausibly_absent")]

    billed_output = usage_total["output_tokens"] + usage_total["reasoning_tokens"]
    manifest = {
        "source": "reconcile_linkedin", "status": "completed",
        "judge": "llm" if use_llm else "deterministic",
        "parents": len(index.get("parents", {})), "tasks": len(tasks), "judged": judged,
        "verdicts": counts, "conflicts": len(conflict_tasks),
        "conflicts_auto_resolved": sum(1 for t in conflict_tasks if t.get("via") == "conflict_resolved"),
        "conflicts_to_review": sum(1 for t in conflict_tasks if t.get("action") == "review"),
        "no_link": sum(1 for t in tasks if t.get("no_link")),
        "errors": sum(1 for t in tasks if t.get("error")),
        "applied": {k: apply_stats[k] for k in ("applied", "confirmed", "detached", "conflict_resolved", "backup")},
        "applied_csv": str(out_dir / "applied.csv"), "review_queued": queued,
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
    p.add_argument("--review-queue", default=str(REVIEW_QUEUE_CSV))
    p.add_argument("--confirm-threshold", type=float, default=DEFAULT_CONFIRM,
                   help="Min judge confidence to auto-apply a verdict (else -> review queue)")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--reasoning-effort", default="high", choices=["minimal", "low", "medium", "high"])
    p.add_argument("--concurrency", type=int, default=0)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--max-retries", type=int, default=6)
    p.add_argument("--dry-run", action="store_true", help="Estimate cost only; no spend, no writes")
    p.add_argument("--no-apply", action="store_true", help="Write verdicts but do NOT touch people.csv")
    p.add_argument("--no-llm", action="store_true", help="Deterministic fallback (offline/tests only)")
    p.add_argument("--reapply", action="store_true",
                   help="Re-decide/apply from existing verdicts.jsonl (no re-judging, no OpenAI spend)")
    return p


def main(argv: list[str] | None = None) -> int:
    emit(run(build_parser().parse_args(argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
