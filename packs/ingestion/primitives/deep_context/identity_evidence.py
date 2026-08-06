"""Shared, SQL-free identity evidence parsing and judgment.

Existing LinkedIn links and Parallel research proposals use the same prompt,
model call, deterministic fallback, and confidence thresholds.  Callers own
profile hydration and SQLite projection; this module only evaluates evidence.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from packs.indexing.lib.openai_responses import (
    is_retryable,
    make_async_client,
    parse_json_response,
    responses_kwargs,
    usage_tokens,
)
from packs.ingestion.primitives.deep_context.common import load_env
from packs.ingestion.primitives.deep_context.db.models import DECISIVE_CONFIRM_THRESHOLD
from packs.ingestion.primitives.deep_context.prompts.loader import load_prompt
NO_PROFILE_REASON = "no usable LinkedIn profile"
VERDICTS = ("confirmed", "wrong_person", "needs_review")
DECISIVE_CONFIRM = DECISIVE_CONFIRM_THRESHOLD
SYSTEM_PROMPT = load_prompt("linkedin_reconcile_system")
RECONCILE_SCHEMA: dict[str, Any] = json.loads(load_prompt("linkedin_reconcile_schema"))

def prefer_cached_profile(
    research_profile: dict[str, Any], cached_profile: dict[str, Any]
) -> dict[str, Any]:
    """Use the actual LinkedIn profile when hydrated; retain research rationale."""
    if cached_profile.get("experiences") or cached_profile.get("education"):
        return {**cached_profile, "reason": research_profile.get("reason", "")}
    return research_profile


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
    contact = (f"{owner_block}\n" if owner_block else "") + (
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


def _verdict(
    value: str, confidence: float, reason: str, *, supporting: tuple[str, ...] = (),
    contradicting: tuple[str, ...] = (), plausibly_absent: bool = False,
) -> dict[str, Any]:
    return {
        "verdict": value, "confidence": confidence,
        "supporting_evidence": list(supporting),
        "contradicting_evidence": list(contradicting),
        "linkedin_plausibly_absent": plausibly_absent,
        "recommend_deep_research": False, "reason": reason,
    }


def deterministic_verdict(task: dict[str, Any]) -> dict[str, Any]:
    if task.get("research_proposal"):
        confidence = float(task.get("research_confidence") or 0)
        if task.get("research_unverified") or confidence < 0.5:
            return _verdict(
                "wrong_person", 0.0, "deep-research guess is unverified",
                contradicting=("unverified deep-research proposal",),
            )
        return _verdict(
            "needs_review", 0.0,
            "speculative deep-research proposal needs the evidence judge",
        )
    if not (task.get("linkedin") or {}).get("has_profile"):
        return _verdict("needs_review", 0.0, NO_PROFILE_REASON, plausibly_absent=True)
    return _verdict(
        "confirmed", 0.9, "offline stub trusts the attached profile",
        supporting=("attached profile (offline stub)",),
    )


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


def judge_batch(
    tasks: list[dict[str, Any]],
    *,
    use_llm: bool,
    owner_block: str,
    model: str,
    effort: str,
    concurrency: int,
    timeout: int,
    max_retries: int,
    on_done: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate a bounded batch through one async client and event loop."""
    if not use_llm:
        results = [
            {"verdict": deterministic_verdict(task), "usage": {}, "error": ""}
            for task in tasks
        ]
        for done in range(1, len(results) + 1):
            if on_done:
                on_done(done, len(results))
        return results
    load_env()

    async def run() -> list[dict[str, Any]]:
        client = make_async_client(timeout=timeout)
        semaphore = asyncio.Semaphore(max(1, concurrency))
        done = 0

        async def one(task: dict[str, Any]) -> dict[str, Any]:
            nonlocal done
            result = await judge_task(
                client, task, owner_block, model=model, effort=effort,
                semaphore=semaphore, max_retries=max_retries,
            )
            done += 1
            if on_done:
                on_done(done, len(tasks))
            return result

        try:
            return list(await asyncio.gather(*(one(task) for task in tasks)))
        finally:
            await client.close()

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


def decide_actions(tasks: list[dict[str, Any]], confirm: float, detach: float | None = None) -> None:
    """Apply the keep-biased deterministic thresholds, including conflicts."""
    thresholds = {"confirmed": confirm, "wrong_person": confirm if detach is None else detach}

    def clears(task: dict[str, Any], verdict: str) -> bool:
        result = task.get("verdict") or {}
        return result.get("verdict") == verdict and float(
            result.get("confidence") or 0
        ) >= thresholds[verdict]

    groups: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        task["action"], task["via"] = "review", ""
        groups.setdefault(str(task.get("parent_id") or task.get("parent_slug") or ""), []).append(task)
    for group in groups.values():
        if len(group) == 1:
            task = group[0]
            if clears(task, "confirmed"):
                task["action"], task["via"] = "confirm", "normal"
            elif clears(task, "wrong_person"):
                task["action"], task["via"] = "detach", "normal"
            continue
        confirmed = [task for task in group if clears(task, "confirmed")]
        wrong = [task for task in group if clears(task, "wrong_person")]
        decisive = confirmed and float(confirmed[0]["verdict"].get("confidence") or 0) >= DECISIVE_CONFIRM
        if len(confirmed) == 1 and (decisive or len(wrong) == len(group) - 1):
            winner = confirmed[0]
            for task in group:
                task["action"] = "confirm" if task is winner else "detach"
                task["via"] = "conflict_resolved"
