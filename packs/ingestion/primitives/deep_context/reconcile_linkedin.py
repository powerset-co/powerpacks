"""Judge attached LinkedIn identities against canonical Deep Context evidence.

SQLite selects identity candidates.  The shared RapidAPI cache supplies profile
evidence and the shared judge compares it with the message-derived dossier.  One
fixed JSONL artifact is written before its verdicts are projected back into
SQLite.  The review UI reads SQLite only; CSV reports and parent-Markdown
mutation are deliberately outside this stage.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.indexing.lib.openai_responses import (
    estimate_cost_usd,
    is_retryable,
    make_async_client,
    parse_json_response,
    reasoning_effort,
    responses_kwargs,
    usage_tokens,
)
from packs.indexing.lib.openai_stream import drain_pool
from packs.indexing.lib.openai_usage_tiers import env_or_profile_int
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context import compose_dossier as compose
from packs.ingestion.primitives.deep_context.common import (
    CONSOLIDATE_PEOPLE_CSV,
    DEFAULT_PEOPLE_CSV,
    FACTS_DIR,
    FACTS_TEMPLATE,
    INDEX_JSON,
    LINKEDIN_OVERRIDES_CSV,
    OWNER_JSON,
    PARENTS_DIR,
    PROFILE_CACHE_DIR,
    PROFILE_CACHE_TEMPLATE,
    RAW_BUNDLE_TEMPLATE,
    RAW_DIR,
    RECONCILE_DIR,
    ROOT,
    VERDICTS_CSV,
    VERDICTS_JSONL,
    emit,
    load_env,
    load_owner,
    owner_background_block,
    read_jsonl,
)
from packs.ingestion.primitives.deep_context.db.models import (
    ApprovedState,
    DECISIVE_CONFIRM_THRESHOLD,
    IdentityMachineProjection,
    JUDGE_CONFIRM_THRESHOLD,
    JUDGE_DETACH_THRESHOLD,
    ReviewSource,
    RowKind,
)
from packs.ingestion.primitives.deep_context.db import views
from packs.ingestion.primitives.deep_context.db.snapshots import (
    canonical_snapshot,
    identity_snapshot,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.prompts.loader import load_prompt
from packs.ingestion.primitives.enrich.profile_cache import (
    profile_cache_path,
    read_usable_cached_profile,
)
from packs.ingestion.primitives.enrich.rapidapi_client import RapidApiClient, hydrate_profiles
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, StageManifest
from packs.ingestion.schemas.people_schema import (
    extract_public_identifier,
    normalize_linkedin_url,
    parse_jsonish,
)


DEFAULT_CONFIRM, DEFAULT_DETACH = JUDGE_CONFIRM_THRESHOLD, JUDGE_DETACH_THRESHOLD
DECISIVE_CONFIRM = DECISIVE_CONFIRM_THRESHOLD
USER_APPROVED = {ApprovedState.YES.value, ApprovedState.NO.value}
CANONICAL_DB = ROOT / "deep-context.sqlite"
NO_PROFILE_REASON = "no usable LinkedIn profile"
VERDICTS = ("confirmed", "wrong_person", "needs_review")
SYSTEM_PROMPT = load_prompt("linkedin_reconcile_system")

RECONCILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "confidence": {"type": "number"},
        "supporting_evidence": {"type": "array", "items": {"type": "string"}},
        "contradicting_evidence": {"type": "array", "items": {"type": "string"}},
        "linkedin_plausibly_absent": {"type": "boolean"},
        "recommend_deep_research": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "confidence", "supporting_evidence", "contradicting_evidence",
                 "linkedin_plausibly_absent", "recommend_deep_research", "reason"],
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def linkedin_key(row: dict[str, Any]) -> str:
    return (
        str(row.get("public_identifier") or "").strip().lower()
        or extract_public_identifier(str(row.get("linkedin_url") or "")).lower()
    )


def _span(entry: dict[str, Any]) -> str:
    def year(value: object) -> str:
        return str(value.get("year") or "") if isinstance(value, dict) else ""

    start, end = year(entry.get("starts_at")), year(entry.get("ends_at"))
    return f"{start}–{end}" if start and end else f"{start}–present" if start else end


def linkedin_view(row: dict[str, Any], cache_dir: Path) -> dict[str, Any]:
    """Normalize cached/fallback profile fields into the shared judge view."""
    pub = linkedin_key(row)
    cached = read_usable_cached_profile(profile_cache_path(cache_dir, pub)) if pub else None
    profile = (cached or {}).get("normalized_profile") if cached else None
    if isinstance(profile, dict):
        experiences = profile.get("experiences") or []
        education = profile.get("education") or []
        location = profile.get("location_str") or ", ".join(
            str(profile.get(key) or "") for key in ("city", "state", "country")
            if profile.get(key)
        )
        full_name = profile.get("full_name") or ""
        headline = profile.get("headline") or ""
        picture = profile.get("profile_pic_url") or ""
        source = "cache"
    else:
        experiences = parse_jsonish(row.get("work_experiences"), []) or []
        education = parse_jsonish(row.get("education"), []) or []
        location = ", ".join(
            str(row.get(key) or "") for key in ("city", "state", "country") if row.get(key)
        )
        full_name = row.get("full_name") or row.get("display_name") or ""
        headline = row.get("headline") or ""
        picture = row.get("profile_picture_url") or ""
        source = "fallback"
    work = []
    for item in experiences:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "")
        company = str(item.get("company_name") or item.get("company") or "")
        text = " @ ".join(value for value in (title, company) if value)
        span = _span(item)
        if text:
            work.append(f"{text}{f' ({span})' if span else ''}")
    schools = []
    for item in education:
        if not isinstance(item, dict):
            continue
        school = str(item.get("school") or item.get("school_name") or "")
        degree = ", ".join(
            str(item.get(key) or "") for key in ("degree", "field") if item.get(key)
        )
        text = f"{degree} — {school}" if degree and school else degree or school
        if text:
            schools.append(text)
    url = str(row.get("linkedin_url") or "")
    return {
        "public_identifier": pub,
        "linkedin_url": url,
        "full_name": str(full_name),
        "headline": str(headline),
        "profile_pic_url": str(picture),
        "experiences": work,
        "education": schools,
        "location": location,
        "source": source,
        "has_profile": bool(profile or work or schools or headline),
    }


def _sample(messages: list[dict[str, Any]], direction: str) -> list[str]:
    selected = [
        str(message.get("text") or "").strip()[:200]
        for message in sorted(messages, key=lambda item: item.get("at") or "", reverse=True)
        if message.get("direction") == direction and str(message.get("text") or "").strip()
    ]
    return selected[:4]


def dossier_view(person_ids: list[str], facts_dir: Path, raw_dir: Path) -> dict[str, Any]:
    """Hydrate the one evidence packet shared by attached and researched links."""
    records: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    for person_id in person_ids:
        records.extend(read_jsonl(facts_dir / f"{person_id}.jsonl"))
        messages.extend(_read_json(raw_dir / f"{person_id}.json").get("messages") or [])
    merged = compose.merge_facts(records) if records else {}
    return {
        "relationship": str(merged.get("relationship_to_owner") or ""),
        "title": str(merged.get("title") or ""),
        "employers": [
            str(item.get("name") or "")
            for item in merged.get("employers") or []
            if isinstance(item, dict) and item.get("name")
        ],
        "school": str(merged.get("school") or ""),
        "location": str(merged.get("location") or ""),
        "topics": list(merged.get("topics") or [])[:10],
        "shared_context": [
            f"{item.get('overlap', 'other')}: {item.get('detail', '')}"
            for item in merged.get("shared_context") or []
            if isinstance(item, dict) and item.get("detail")
        ],
        "from_me": _sample(messages, "from_me"),
        "from_them": _sample(messages, "from_them"),
        "has_messages": bool(messages),
    }


def _profile_row(link: Any) -> dict[str, Any]:
    return {
        "public_identifier": str(link.public_identifier or "").lower(),
        "linkedin_url": link.linkedin_url or "",
        "display_name": link.display_name or "",
    }


def build_tasks(
    db: Db,
    facts_dir: Path,
    raw_dir: Path,
    cache_dir: Path,
) -> list[dict[str, Any]]:
    """Build one attached-link task per SQLite candidate row."""
    graph, identity = canonical_snapshot(db), identity_snapshot(db)
    parents = {row.parent_id: row for row in graph.parents}
    parent_people: dict[str, list[str]] = {}
    for row in graph.people:
        if not row.is_owner and not row.is_ghost:
            parent_people.setdefault(row.parent_id, []).append(row.person_id)
    members: dict[str, list[str]] = {}
    for row in identity.memberships:
        members.setdefault(row.row_key, []).append(row.person_id)
    sources: dict[str, set[str]] = {}
    for row in graph.sources:
        sources.setdefault(row.person_id, set()).add(row.source)

    tasks: list[dict[str, Any]] = []
    for link in identity.links:
        if not link.linkedin_url or link.kind in {RowKind.SYNTHETIC.value, RowKind.RESEARCH.value}:
            continue
        parent = parents.get(link.parent_id)
        if parent is None:
            continue
        all_people = sorted(parent_people.get(link.parent_id, []))
        person_ids = sorted(members.get(link.row_key) or all_people)
        row = _profile_row(link)
        tasks.append({
            "parent_slug": parent.display_slug or parent.public_identifier,
            "parent_id": parent.parent_id,
            "name": parent.display_name or link.display_name or parent.public_identifier,
            "candidate_key": link.row_key,
            "person_ids": person_ids,
            "conflict": False,
            "no_link": False,
            "dossier": dossier_view(all_people, facts_dir, raw_dir),
            "linkedin": linkedin_view(row, cache_dir),
            "from_connections": any("linkedin_csv" in sources.get(pid, set()) for pid in person_ids),
            "_profile_row": row,
        })
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task["parent_id"]] = counts.get(task["parent_id"], 0) + 1
    for task in tasks:
        task["conflict"] = counts[task["parent_id"]] > 1
    return tasks


def connection_verdict() -> dict[str, Any]:
    return {
        "verdict": "confirmed",
        "confidence": 1.0,
        "supporting_evidence": ["LinkedIn Connections import"],
        "contradicting_evidence": [],
        "linkedin_plausibly_absent": False,
        "recommend_deep_research": False,
        "reason": "Ground truth: this profile came from your LinkedIn Connections import.",
    }


def _bullets(items: list[str], empty: str) -> str:
    return "\n".join(f"  - {item}" for item in items) if items else f"  {empty}"


def judge_prompt(task: dict[str, Any], owner_block: str) -> str:
    dossier, profile = task["dossier"], task["linkedin"]
    evidence = [
        f"relationship: {dossier.get('relationship')}",
        f"work: {dossier.get('title')} @ {', '.join(dossier.get('employers') or [])}",
        f"school: {dossier.get('school')}",
        f"location: {dossier.get('location')}",
        f"topics: {', '.join(dossier.get('topics') or [])}",
        f"shared context: {'; '.join(dossier.get('shared_context') or [])}",
    ]
    evidence = [line for line in evidence if line.split(":", 1)[1].strip(" @")]
    contact = (
        f"{owner_block}\n" if owner_block else ""
    ) + (
        f"CONTACT: {task.get('name') or '(unknown)'}\n"
        + "\n".join(f"  {line}" for line in evidence)
        + f"\n  me to them:\n{_bullets(dossier.get('from_me') or [], '(none)')}"
        + f"\n  them to me:\n{_bullets(dossier.get('from_them') or [], '(none)')}"
    )
    linked = (
        f"\n\nLINKEDIN: {profile.get('linkedin_url') or '(none)'}"
        f"\n  name: {profile.get('full_name') or '(unknown)'}"
        f"\n  headline: {profile.get('headline') or '(none)'}"
        f"\n  location: {profile.get('location') or '(unknown)'}"
        f"\n  experience:\n{_bullets(profile.get('experiences') or [], '(none)')}"
        f"\n  education:\n{_bullets(profile.get('education') or [], '(none)')}"
    )
    speculative = ""
    if task.get("research_proposal"):
        speculative = (
            "\n\nThis is a speculative web-research proposal. A shared name alone is not "
            "corroboration; require employer, school, location, topic, domain, or equivalent evidence."
        )
    return contact + linked + speculative + "\n\nIs this the same human?"


async def judge_task(
    client: Any,
    task: dict[str, Any],
    owner_block: str,
    *,
    model: str,
    effort: str,
    semaphore: asyncio.Semaphore,
    max_retries: int,
) -> dict[str, Any]:
    kwargs = responses_kwargs(model, effort=effort, schema=RECONCILE_SCHEMA, schema_name="reconcile")
    async with semaphore:
        for attempt in range(max_retries + 1):
            try:
                response = await client.responses.create(
                    model=model,
                    input=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": judge_prompt(task, owner_block)},
                    ],
                    **kwargs,
                )
                return {
                    "verdict": parse_json_response(response, "reconcile"),
                    "usage": usage_tokens(response),
                    "error": "",
                }
            except Exception as exc:  # noqa: BLE001
                if attempt < max_retries and is_retryable(exc):
                    await asyncio.sleep(min(2 ** (attempt + 1), 30))
                    continue
                return {
                    "verdict": {},
                    "usage": {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0},
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                }
    raise AssertionError("unreachable")


def deterministic_verdict(task: dict[str, Any]) -> dict[str, Any]:
    profile = task.get("linkedin") or {}
    if task.get("research_proposal"):
        confidence = float(task.get("research_confidence") or 0)
        if task.get("research_unverified") or confidence < 0.5:
            return {
                "verdict": "wrong_person",
                "confidence": 0.0,
                "supporting_evidence": [],
                "contradicting_evidence": ["unverified deep-research proposal"],
                "linkedin_plausibly_absent": False,
                "recommend_deep_research": False,
                "reason": "deep-research guess is unverified",
            }
        return {
            "verdict": "needs_review",
            "confidence": 0.0,
            "supporting_evidence": [],
            "contradicting_evidence": [],
            "linkedin_plausibly_absent": False,
            "recommend_deep_research": False,
            "reason": "speculative deep-research proposal needs the evidence judge",
        }
    if not profile.get("has_profile"):
        return {
            "verdict": "needs_review",
            "confidence": 0.0,
            "supporting_evidence": [],
            "contradicting_evidence": [],
            "linkedin_plausibly_absent": True,
            "recommend_deep_research": False,
            "reason": NO_PROFILE_REASON,
        }
    return {
        "verdict": "confirmed",
        "confidence": 0.9,
        "supporting_evidence": ["attached profile (offline stub)"],
        "contradicting_evidence": [],
        "linkedin_plausibly_absent": False,
        "recommend_deep_research": False,
        "reason": "offline stub trusts the attached profile",
    }


def research_proposal_task(
    dossier: dict[str, Any],
    profile: dict[str, Any],
    *,
    name: str,
    match_emails: list[str] | None = None,
    match_phones: list[str] | None = None,
    confidence: float = 0.0,
    unverified: bool = False,
) -> dict[str, Any]:
    return {
        "research_proposal": True,
        "name": name,
        "dossier": dossier,
        "linkedin": profile,
        "match_emails": match_emails or [],
        "match_phones": match_phones or [],
        "research_confidence": confidence,
        "research_unverified": unverified,
    }


def judge_research_proposal(
    task: dict[str, Any],
    *,
    use_llm: bool,
    owner_block: str = "",
    model: str = DEFAULT_MODEL,
    effort: str = "high",
    timeout: int = 120,
    max_retries: int = 6,
) -> dict[str, Any]:
    if not use_llm:
        return deterministic_verdict(task)

    async def run() -> dict[str, Any]:
        client = make_async_client(timeout=timeout)
        try:
            result = await judge_task(
                client,
                task,
                owner_block,
                model=model,
                effort=effort,
                semaphore=asyncio.Semaphore(1),
                max_retries=max_retries,
            )
            return result.get("verdict") or {}
        finally:
            await client.close()

    load_env()
    return asyncio.run(run())


def research_reject_fields(verdict: dict[str, Any], confirm_threshold: float) -> dict[str, str]:
    value = str(verdict.get("verdict") or "").lower()
    confidence = float(verdict.get("confidence") or 0)
    if value == "confirmed" and confidence >= confirm_threshold:
        return {
            "llm_reject": "",
            "llm_reject_confidence": "",
            "llm_reject_reason": "",
            "confidence": f"{confidence:.3f}",
        }
    return {
        "llm_reject": "yes",
        "llm_reject_confidence": f"{confidence:.3f}",
        "llm_reject_reason": str(
            verdict.get("reason") or "deep-research proposal not corroborated by the dossier"
        ),
    }


class ConfidenceBars:
    def __init__(self, confirm: float, detach: float | None) -> None:
        self.confirm = confirm
        self.detach = confirm if detach is None else detach

    def clears(self, task: dict[str, Any], verdict: str) -> bool:
        result = task.get("verdict") or {}
        threshold = self.detach if verdict == "wrong_person" else self.confirm
        return result.get("verdict") == verdict and float(result.get("confidence") or 0) >= threshold


def decide_actions(tasks: list[dict[str, Any]], confirm: float, detach: float | None = None) -> None:
    """Apply the keep-biased deterministic thresholds, including conflicts."""
    bars = ConfidenceBars(confirm, detach)
    groups: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        task["action"], task["via"] = "review", ""
        groups.setdefault(str(task.get("parent_id") or task.get("parent_slug") or ""), []).append(task)
    for group in groups.values():
        if len(group) == 1:
            task = group[0]
            if bars.clears(task, "confirmed"):
                task["action"], task["via"] = "confirm", "normal"
            elif bars.clears(task, "wrong_person"):
                task["action"], task["via"] = "detach", "normal"
            continue
        confirmed = [task for task in group if bars.clears(task, "confirmed")]
        wrong = [task for task in group if bars.clears(task, "wrong_person")]
        decisive = confirmed and float(confirmed[0]["verdict"].get("confidence") or 0) >= DECISIVE_CONFIRM
        if len(confirmed) == 1 and (decisive or len(wrong) == len(group) - 1):
            winner = confirmed[0]
            for task in group:
                task["action"] = "confirm" if task is winner else "detach"
                task["via"] = "conflict_resolved"


def _identity_projection(db: Db, key: str, **updates: Any) -> IdentityMachineProjection:
    rows = [
        row for row in identity_snapshot(db).links
        if row.row_key == key or row.public_identifier.lower() == key.lower()
    ]
    if not rows:
        raise StoreError(f"unknown identity candidate: {key}")
    row = sorted(rows, key=lambda item: item.row_key != key)[0]
    values = {
        field: getattr(row, field)
        for field in IdentityMachineProjection.__dataclass_fields__
        if field not in {"row_key", "updated_at"}
    }
    values.update(updates)
    return IdentityMachineProjection(row.row_key, **values, updated_at=now_iso())


def _review_rows(db: Db) -> dict[str, dict[str, str]]:
    return {
        row.key: {key: value for key, value in asdict(row).items() if key != "key"}
        for row in identity_snapshot(db).review_rows
    }


def write_overrides(
    db: Db,
    tasks: list[dict[str, Any]],
    *,
    artifact_path: Path | None = None,
) -> dict[str, Any]:
    """Project machine identity judgments without touching human decisions."""
    existing = _review_rows(db)
    projections = []
    detached = verified = pending = preserved = 0
    for task in tasks:
        pub = str(task.get("candidate_key") or "").lower()
        if not pub:
            continue
        if str(existing.get(pub, {}).get("approved") or "").lower() in USER_APPROVED:
            preserved += 1
            continue
        verdict = task.get("verdict") or {}
        action = task.get("action")
        if action == "confirm":
            machine_action, approved = "verify", "auto"
            verified += 1
        elif action == "detach":
            machine_action, approved = "detach", "auto"
            detached += 1
        else:
            machine_action = "detach" if verdict.get("verdict") == "wrong_person" else "verify"
            approved = None
            pending += 1
        payload = json.dumps(verdict, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        projections.append(_identity_projection(
            db,
            pub,
            machine_action=machine_action,
            machine_approved=approved,
            machine_confidence=float(verdict.get("confidence") or 0),
            machine_reason=str(verdict.get("reason") or ""),
            machine_judgment=str(verdict.get("verdict") or "") or None,
            authoritative_detach=int(machine_action == "detach" and approved == "auto"),
            judgment_fingerprint=hashlib.sha256(payload.encode()).hexdigest(),
            judgment_artifact_path=str(artifact_path) if artifact_path else None,
            judgment_payload_json=payload,
            source=ReviewSource.RECONCILE.value,
        ))
    db.project_identity(tuple(projections))
    return {
        "path": str(db.db_path),
        "detached": detached,
        "verified": verified,
        "pending": pending,
        "preserved_user_rows": preserved,
        "total_rows": len(existing),
    }


def count_pending_identity_reviews(db: Db) -> int:
    return int(views.linkedin_review(db, "progress")["pending"])


# Stable public service names.  The older names remain aliases for callers whose
# CLI/API contracts predate the SQLite rewrite.
judge_identity_candidate = judge_research_proposal
project_identity_judgments = write_overrides
count_pending = count_pending_identity_reviews


def upsert_retargets(db: Db, proposals: list[dict[str, Any]]) -> dict[str, Any]:
    existing = _review_rows(db)
    projections = []
    proposed = preserved = 0
    for proposal in proposals:
        old_pub = str(proposal.get("old_public_identifier") or "").lower()
        new_url = normalize_linkedin_url(str(proposal.get("new_linkedin_url") or ""))
        if not old_pub or not new_url:
            continue
        if str(existing.get(old_pub, {}).get("approved") or "").lower() in USER_APPROVED:
            preserved += 1
            continue
        updates: dict[str, Any] = {
            "machine_action": "retarget",
            "machine_approved": str(proposal.get("approved") or "").lower() or None,
            "machine_confidence": float(proposal.get("confidence") or 0),
            "machine_reason": str(proposal.get("reason") or ""),
            "machine_proposed_url": new_url,
            "machine_proposed_public_identifier": str(
                proposal.get("new_public_identifier") or extract_public_identifier(new_url)
            ).lower(),
            "paid_profile": 1,
            "source": str(proposal.get("source") or ReviewSource.DEEP_RESEARCH.value),
        }
        if "llm_reject" in proposal:
            updates.update({
                "machine_reject": proposal.get("llm_reject") or None,
                "machine_reject_confidence": float(proposal.get("llm_reject_confidence") or 0),
                "machine_reject_reason": proposal.get("llm_reject_reason") or None,
            })
        if "judge_fingerprint" in proposal:
            updates["judgment_fingerprint"] = str(proposal.get("judge_fingerprint") or "")
        projections.append(_identity_projection(db, old_pub, **updates))
        proposed += 1
    db.project_identity(tuple(projections))
    return {
        "path": str(db.db_path),
        "proposed": proposed,
        "preserved_user_rows": preserved,
        "total_rows": len(existing),
    }


_ARTIFACT_FIELDS = (
    "parent_slug",
    "parent_id",
    "name",
    "candidate_key",
    "person_ids",
    "conflict",
    "linkedin",
    "verdict",
    "error",
)


def write_verdicts(path: Path, tasks: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for task in tasks:
            stream.write(json.dumps(
                {key: task[key] for key in _ARTIFACT_FIELDS if key in task},
                ensure_ascii=False,
                separators=(",", ":"),
            ) + "\n")


def load_tasks_from_verdicts(path: Path) -> list[dict[str, Any]]:
    return [
        {**record, "verdict": record.get("verdict") or {}, "linkedin": record.get("linkedin") or {}}
        for record in read_jsonl(path)
    ]


def merge_subset_tasks(path: Path, fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
    replaced = {str(task.get("parent_id") or task.get("parent_slug") or "") for task in fresh}
    prior = [
        task for task in load_tasks_from_verdicts(path)
        if str(task.get("parent_id") or task.get("parent_slug") or "") not in replaced
    ]
    return prior + fresh


def profile_fetch_candidates(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        task for task in tasks
        if not task.get("from_connections")
        and (task.get("linkedin") or {}).get("linkedin_url")
        and not (task.get("linkedin") or {}).get("has_profile")
    ]


def fetch_missing_profiles(
    tasks: list[dict[str, Any]],
    people: dict[str, dict[str, str]],
    cache_dir: Path,
    *,
    max_workers: int = 8,
) -> dict[str, int]:
    del people
    wanted = profile_fetch_candidates(tasks)
    counts = {
        "fetch_wanted": len(wanted),
        "fetch_ok": 0,
        "fetch_failed": 0,
        "fetch_skipped_no_key": 0,
    }
    if not wanted:
        return counts
    if not RapidApiClient.resolve_key():
        counts["fetch_skipped_no_key"] = len(wanted)
        return counts
    pairs = [
        (
            str((task.get("linkedin") or {}).get("public_identifier") or ""),
            str((task.get("linkedin") or {}).get("linkedin_url") or ""),
        )
        for task in wanted
    ]
    hydrated = hydrate_profiles(pairs, cache_dir, max_workers=max_workers)
    counts["fetch_ok"], counts["fetch_failed"] = hydrated["ok"], hydrated["failed"]
    for task in wanted:
        task["linkedin"] = linkedin_view(task.get("_profile_row") or task["linkedin"], cache_dir)
    return counts


def _select_tasks(
    db: Db,
    people_csv: Path,
    facts_dir: Path,
    raw_dir: Path,
    cache_dir: Path,
    slugs: list[str] | None,
    limit: int,
) -> list[dict[str, Any]]:
    del people_csv
    tasks = build_tasks(db, facts_dir, raw_dir, cache_dir)
    if slugs:
        wanted = {value.lower() for value in slugs}
        tasks = [task for task in tasks if str(task.get("parent_slug") or "").lower() in wanted]
    return tasks[:limit] if limit else tasks


def dry_run_estimate(
    *,
    index_json: Path,
    people_csv: Path,
    profile_cache_dir: Path,
    facts_dir: Path,
    raw_dir: Path,
    model: str,
    effort: str,
    slug: list[str] | None = None,
    limit: int = 0,
    db: Db | None = None,
) -> dict[str, Any]:
    del index_json
    started = time.monotonic()
    tasks = _select_tasks(
        db or Db(CANONICAL_DB), people_csv, facts_dir, raw_dir, profile_cache_dir, slug, limit
    )
    judgeable = [task for task in tasks if not task.get("from_connections") and task["linkedin"].get("has_profile")]
    misses = len(profile_fetch_candidates(tasks))
    return {
        "source": "reconcile_linkedin",
        "status": "dry_run",
        "profile_fetch_misses": misses,
        "estimated_rapidapi_credits": misses,
        "parents": len({task["parent_id"] for task in tasks}),
        "tasks": len(tasks),
        "judgeable": len(judgeable),
        "no_link": 0,
        "identity_judgeable": len(judgeable),
        "ground_truth_connections": sum(bool(task.get("from_connections")) for task in tasks),
        "conflicts": sum(bool(task.get("conflict")) for task in tasks),
        "estimated_cost_usd_low": round(len(judgeable) * 0.004, 2),
        "estimated_cost_usd_high": round(len(judgeable) * 0.02, 2),
        "model": model,
        "reasoning_effort": reasoning_effort(effort),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "updated_at": now_iso(),
    }


class ReconcileLinkedinManifest(StageManifest):
    source: str = "reconcile_linkedin"
    judge: str = ""
    parents: int = 0
    tasks: int = 0
    judged: int = 0
    ground_truth_connections: int = 0
    self_reported_retargets: int = 0
    name_match_reviews: int = 0
    verdicts: dict[str, int] = {}
    conflicts: int = 0
    conflicts_auto_resolved: int = 0
    conflicts_to_review: int = 0
    profile_fetch: dict[str, int] | None = None
    no_link: int = 0
    errors: int = 0
    overrides: dict[str, Any] = {}
    consolidation: dict[str, Any] = {}
    summary_md: str = ""
    applied_csv: str = ""
    needs_review: int = 0
    deep_research_eligible: int = 0
    deep_research_est_usd: float = 0.0
    tokens: dict[str, int] = {}
    estimated_cost_usd: float = 0.0
    elapsed_ms: int = 0


class ReconcileLinkedin(Node):
    """SQLite-selected attached-link judge with one file-first artifact."""

    name = "deep_reconcile"
    inputs = (
        Artifact(path=FACTS_TEMPLATE, required=False),
        Artifact(path=RAW_BUNDLE_TEMPLATE, required=False),
        Artifact(path=PROFILE_CACHE_TEMPLATE, external=True, required=False),
        Artifact(path=str(OWNER_JSON), required=False),
    )
    outputs = (Artifact(path=str(VERDICTS_JSONL), writes="full_rewrite"),)
    payload = ReconcileLinkedinManifest
    manifest = str(RECONCILE_DIR / "manifest.json")

    def __init__(
        self,
        *,
        db: Db,
        index_json: Path | None = None,
        people_csv: Path | None = None,
        profile_cache_dir: Path | None = None,
        facts_dir: Path | None = None,
        raw_dir: Path | None = None,
        parents_dir: Path | None = None,
        verdicts_jsonl: Path | None = None,
        verdicts_csv: Path | None = None,
        confirm_threshold: float = DEFAULT_CONFIRM,
        detach_threshold: float = DEFAULT_DETACH,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = "high",
        concurrency: int = 0,
        timeout: int = 120,
        max_retries: int = 6,
        overrides_csv: Path | None = None,
        consolidate_people_csv: Path | None = None,
        slug: list[str] | None = None,
        limit: int = 0,
        no_overrides: bool = False,
        no_llm: bool = False,
        reapply: bool = False,
    ) -> None:
        self.db = db
        self.index_json = Path(index_json or INDEX_JSON)
        self.people_csv = Path(people_csv or DEFAULT_PEOPLE_CSV)
        self.profile_cache_dir = Path(profile_cache_dir or PROFILE_CACHE_DIR)
        self.facts_dir = Path(facts_dir or FACTS_DIR)
        self.raw_dir = Path(raw_dir or RAW_DIR)
        self.parents_dir = Path(parents_dir or PARENTS_DIR)
        self.verdicts_jsonl = Path(verdicts_jsonl or VERDICTS_JSONL)
        self.verdicts_csv = Path(verdicts_csv or VERDICTS_CSV)
        self.confirm_threshold = confirm_threshold
        self.detach_threshold = detach_threshold
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.concurrency = concurrency
        self.timeout = timeout
        self.max_retries = max_retries
        self.overrides_csv = Path(overrides_csv or LINKEDIN_OVERRIDES_CSV)
        self.consolidate_people_csv = Path(consolidate_people_csv or CONSOLIDATE_PEOPLE_CSV)
        self.slug = list(slug or [])
        self.limit = limit
        self.no_overrides = no_overrides
        self.no_llm = no_llm
        self.reapply = reapply

    def bindings(self) -> dict[str, str]:
        return {
            FACTS_TEMPLATE: str(self.facts_dir / "{person_id}.jsonl"),
            RAW_BUNDLE_TEMPLATE: str(self.raw_dir / "{person_id}.json"),
            PROFILE_CACHE_TEMPLATE: str(self.profile_cache_dir / "{public_identifier}.json"),
            str(VERDICTS_JSONL): str(self.verdicts_jsonl),
            self.manifest: str(self.verdicts_jsonl.parent / "manifest.json"),
        }

    def execute(self) -> ReconcileLinkedinManifest:
        started = time.monotonic()
        usage = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
        use_llm = not self.no_llm and not self.reapply
        fetch_counts: dict[str, int] = {}
        if self.reapply:
            tasks = load_tasks_from_verdicts(self.verdicts_jsonl)
        else:
            tasks = _select_tasks(
                self.db,
                self.people_csv,
                self.facts_dir,
                self.raw_dir,
                self.profile_cache_dir,
                self.slug,
                self.limit,
            )
            for task in tasks:
                if task.get("from_connections"):
                    task["verdict"], task["error"] = connection_verdict(), ""
            if use_llm:
                fetch_counts = fetch_missing_profiles(tasks, {}, self.profile_cache_dir)
            judgeable = [
                task for task in tasks
                if not task.get("from_connections") and task["linkedin"].get("has_profile")
            ]
            if use_llm and judgeable:
                load_env()
                concurrency = self.concurrency or env_or_profile_int(
                    "POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency", fallback=64
                )

                async def run() -> None:
                    client = make_async_client(timeout=self.timeout)
                    results: dict[int, dict[str, Any]] = {}
                    semaphore = asyncio.Semaphore(max(1, concurrency))
                    try:
                        async def one(index: int, task: dict[str, Any]) -> tuple[int, dict[str, Any]]:
                            return index, await judge_task(
                                client,
                                task,
                                owner_background_block(load_owner()) if load_owner() else "",
                                model=self.model,
                                effort=reasoning_effort(self.reasoning_effort),
                                semaphore=semaphore,
                                max_retries=self.max_retries,
                            )

                        await drain_pool(
                            [one(index, task) for index, task in enumerate(judgeable)],
                            lambda result: results.__setitem__(result[0], result[1]),
                        )
                    finally:
                        await client.close()
                    for index, task in enumerate(judgeable):
                        result = results.get(index, {"verdict": {}, "usage": {}, "error": "no result"})
                        task["verdict"], task["error"] = result.get("verdict") or {}, result.get("error") or ""
                        for key in usage:
                            usage[key] += int((result.get("usage") or {}).get(key) or 0)

                asyncio.run(run())
            for task in tasks:
                if "verdict" not in task:
                    task["verdict"], task["error"] = deterministic_verdict(task), ""
            if self.slug or self.limit:
                tasks = merge_subset_tasks(self.verdicts_jsonl, tasks)

        decide_actions(tasks, self.confirm_threshold, self.detach_threshold)
        write_verdicts(self.verdicts_jsonl, tasks)
        overrides = {
            "path": str(self.db.db_path),
            "detached": 0,
            "verified": 0,
            "pending": 0,
            "preserved_user_rows": 0,
            "total_rows": len(_review_rows(self.db)),
        }
        if not self.no_overrides:
            overrides = write_overrides(self.db, tasks, artifact_path=self.verdicts_jsonl)

        counts = {value: 0 for value in VERDICTS}
        for task in tasks:
            value = str((task.get("verdict") or {}).get("verdict") or "")
            if value in counts:
                counts[value] += 1
        conflicts = [task for task in tasks if task.get("conflict")]
        research = [
            task for task in tasks
            if (task.get("verdict") or {}).get("verdict") == "wrong_person"
            and float((task.get("verdict") or {}).get("confidence") or 0) >= self.detach_threshold
            and (task.get("verdict") or {}).get("recommend_deep_research")
            and not (task.get("verdict") or {}).get("linkedin_plausibly_absent")
        ]
        output = usage["output_tokens"] + usage["reasoning_tokens"]
        return ReconcileLinkedinManifest(
            status="completed",
            judge="llm" if use_llm else "deterministic",
            parents=len({task.get("parent_id") or task.get("parent_slug") for task in tasks}),
            tasks=len(tasks),
            judged=sum(not task.get("from_connections") and task["linkedin"].get("has_profile") for task in tasks),
            ground_truth_connections=sum(bool(task.get("from_connections")) for task in tasks),
            verdicts=counts,
            conflicts=len(conflicts),
            conflicts_auto_resolved=sum(task.get("via") == "conflict_resolved" for task in conflicts),
            conflicts_to_review=sum(task.get("action") == "review" for task in conflicts),
            profile_fetch=fetch_counts or None,
            errors=sum(bool(task.get("error")) for task in tasks),
            overrides=overrides,
            consolidation={"consolidated_parents": 0},
            needs_review=int(overrides.get("pending", 0)),
            deep_research_eligible=len(research),
            deep_research_est_usd=round(len(research) * 0.05, 2),
            tokens=usage,
            estimated_cost_usd=estimate_cost_usd(usage["input_tokens"], output, self.model),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Judge attached LinkedIn profiles against Deep Context evidence")
    paths = {
        "index-json": INDEX_JSON, "people-csv": DEFAULT_PEOPLE_CSV,
        "profile-cache-dir": PROFILE_CACHE_DIR, "facts-dir": FACTS_DIR,
        "raw-dir": RAW_DIR, "parents-dir": PARENTS_DIR,
        "verdicts-jsonl": VERDICTS_JSONL, "verdicts-csv": VERDICTS_CSV,
        "overrides-csv": LINKEDIN_OVERRIDES_CSV, "db": CANONICAL_DB,
        "consolidate-people-csv": CONSOLIDATE_PEOPLE_CSV,
    }
    for flag, default in paths.items():
        parser.add_argument(f"--{flag}", default=str(default))
    parser.add_argument("--confirm-threshold", type=float, default=DEFAULT_CONFIRM)
    parser.add_argument("--detach-threshold", type=float, default=DEFAULT_DETACH)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning-effort", default="high", choices=["minimal", "low", "medium", "high"])
    for flag, default in (("concurrency", 0), ("timeout", 120), ("max-retries", 6)):
        parser.add_argument(f"--{flag}", type=int, default=default)
    parser.add_argument("--slug", action="append", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-overrides", action="store_true")
    parser.add_argument("--reapply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = Db(Path(args.db))
    paths = {
        name: Path(getattr(args, name))
        for name in ("index_json", "people_csv", "profile_cache_dir", "facts_dir", "raw_dir")
    }
    if args.dry_run and not args.reapply:
        emit(dry_run_estimate(
            **paths, model=args.model, effort=args.reasoning_effort,
            slug=args.slug, limit=args.limit, db=db,
        ))
        return 0
    payload = ReconcileLinkedin(
        db=db,
        **paths,
        parents_dir=Path(args.parents_dir),
        verdicts_jsonl=Path(args.verdicts_jsonl),
        verdicts_csv=Path(args.verdicts_csv),
        confirm_threshold=args.confirm_threshold,
        detach_threshold=args.detach_threshold,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        concurrency=args.concurrency,
        timeout=args.timeout,
        max_retries=args.max_retries,
        overrides_csv=Path(args.overrides_csv),
        consolidate_people_csv=Path(args.consolidate_people_csv),
        slug=args.slug,
        limit=args.limit,
        no_overrides=args.no_overrides,
        reapply=args.reapply,
    ).run()
    emit(payload.to_payload())
    return 0


if __name__ == "__main__":
    sys.exit(main())
