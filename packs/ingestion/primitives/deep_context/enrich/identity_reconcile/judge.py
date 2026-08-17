"""One typed evidence judge for attached and researched LinkedIn identities.

Changelog:
- 2026-08-08: judgment_fingerprint now hashes model + reasoning effort. It
  used to hash neither, so a stored verdict cache-hit across a model swap or
  a reasoning-effort change — identity_reconcile/healing.py's rejudge()
  deliberately asks for effort="high" to get a more careful judgment than
  the first pass, but was silently served back the medium-effort verdict it
  existed to replace. Every fingerprint stored before this date is now a
  guaranteed cache miss (real re-judge cost, once).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Callable

from packs.ingestion.primitives.deep_context.db.models import IdentityOrigin
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import (
    IdentityTask,
    IdentityJudgeResult,
    IdentityUsage,
    IdentityVerdict,
    JudgeProfile,
)
from packs.ingestion.primitives.deep_context.prompts.loader import load_prompt
from packs.ingestion.primitives.deep_context.shared.openai_responses import (
    OpenAIResponsesCaller,
    OpenAIResponsesConfig,
)

SYSTEM_PROMPT = load_prompt("linkedin_reconcile_system")
RECONCILE_SCHEMA: dict[str, Any] = json.loads(load_prompt("linkedin_reconcile_schema"))


def prefer_cached_profile(
    research_profile: JudgeProfile,
    cached_profile: JudgeProfile,
) -> JudgeProfile:
    """Use the hydrated LinkedIn profile while retaining research rationale."""
    if cached_profile.experiences or cached_profile.education:
        return replace(
            cached_profile,
            reason=research_profile.reason,
            _present=cached_profile._present | {"reason"},
        )
    return research_profile


def _bullets(items: tuple[str, ...] | list[str], empty: str) -> str:
    return "\n".join(f"  - {item}" for item in items) if items else f"  {empty}"


def identity_judge_prompt(
    evidence: DossierEvidence,
    profile: JudgeProfile,
    origin: IdentityOrigin,
    owner_block: str,
) -> str:
    """Render the sole identity-judge user prompt.

    Example (attached origin, synthetic identity)::

        CONTACT: Jordan Bravo
          relationship: college friend
          work: engineer @ Acme Robotics
          me to them:
            - "congrats on the new role!"
          them to me:
            - "thanks! starting next month"

        LINKEDIN: https://linkedin.com/in/jordan-bravo-eng
          name: Jordan Bravo
          headline: Senior Engineer at Acme Robotics
          experience:
            - Acme Robotics, Senior Engineer, 2022-present
          education:
            (none)

        Is this the same human?
    """
    fields = [
        f"relationship: {evidence.relationship}",
        f"work: {evidence.title} @ {', '.join(evidence.employers)}",
        f"school: {evidence.school}",
        f"location: {evidence.location}",
        f"topics: {', '.join(evidence.topics)}",
        f"shared context: {'; '.join(evidence.shared_context)}",
    ]
    fields = [line for line in fields if line.split(":", 1)[1].strip(" @")]
    contact = (f"{owner_block}\n" if owner_block else "") + (
        f"CONTACT: {evidence.name or '(unknown)'}\n"
        + "\n".join(f"  {line}" for line in fields)
        + f"\n  me to them:\n{_bullets(evidence.from_me, '(none)')}"
        + f"\n  them to me:\n{_bullets(evidence.from_them, '(none)')}"
    )
    linked = (
        f"\n\nLINKEDIN: {profile.linkedin_url or '(none)'}"
        f"\n  name: {profile.full_name or '(unknown)'}"
        f"\n  headline: {profile.headline or '(none)'}"
        f"\n  location: {profile.location or '(unknown)'}"
        f"\n  experience:\n{_bullets(profile.experiences, '(none)')}"
        f"\n  education:\n{_bullets(profile.education, '(none)')}"
    )
    speculative = ""
    if origin == IdentityOrigin.RESEARCH:
        speculative = (
            "\n\nThis is a speculative web-research proposal. A shared name alone is not "
            "corroboration; require employer, school, location, topic, domain, or equivalent evidence."
        )
    return contact + linked + speculative + "\n\nIs this the same human?"


def judgment_fingerprint(
    evidence: DossierEvidence,
    profile: JudgeProfile,
    origin: IdentityOrigin,
    owner_block: str,
    *,
    model: str,
    effort: str,
) -> str:
    """Hash the identity judge input, candidate payload, origin, and model/effort.

    This fingerprint is the paid-judge cache key: changing its serialization
    invalidates every matching judgment and re-bills the identity judge.

    In: origin, SYSTEM_PROMPT text, the rendered identity_judge_prompt (which
    embeds evidence + profile + owner_block), profile.as_judge_dict(), and the
    model/reasoning-effort pair that will answer this exact input — a verdict
    from one model/effort is not evidence about what a different model/effort
    would answer, so a model or effort change must miss cache, not silently
    hit it.
    Out, deliberately: timeout/retry config and any timestamp — those don't
    change what is asked or how carefully, so a bare retry under the same
    model/effort still cache-hits.
    """
    judge_profile = profile.as_judge_dict()
    payload = json.dumps(
        {
            "origin": origin.value,
            "system": SYSTEM_PROMPT,
            "input": identity_judge_prompt(evidence, profile, origin, owner_block),
            "profile": judge_profile,
            "model": model,
            "effort": effort,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IdentityJudge:
    """Configured judge sharing one client, event loop, and semaphore per batch."""

    caller: OpenAIResponsesCaller
    owner_block: str
    model: str
    effort: str

    async def judge_identity(
        self,
        evidence: DossierEvidence,
        profile: JudgeProfile,
        origin: IdentityOrigin,
    ) -> IdentityJudgeResult:
        """Evaluate one typed identity packet without owning client lifecycle."""
        fingerprint = judgment_fingerprint(
            evidence,
            profile,
            origin,
            self.owner_block,
            model=self.model,
            effort=self.effort,
        )
        try:
            response = await self.caller.call(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=identity_judge_prompt(
                    evidence,
                    profile,
                    origin,
                    self.owner_block,
                ),
                schema=RECONCILE_SCHEMA,
                schema_name="reconcile",
                context="reconcile",
            )
            return IdentityJudgeResult(
                verdict=IdentityVerdict.from_payload(response.payload),
                usage=IdentityUsage.from_payload(response.usage.as_dict()),
                error="",
                fingerprint=fingerprint,
            )
        except Exception as exc:  # noqa: BLE001 - SDK retries before result recording
            # Degrade to a per-task error string instead of raising: one row's
            # exhausted-retry failure must not abort the whole judge_batch
            # gather, and the fingerprint above still lets a rerun retarget
            # just this row.
            return IdentityJudgeResult(
                verdict=None,
                usage=IdentityUsage(),
                error=f"{type(exc).__name__}: {exc}"[:200],
                fingerprint=fingerprint,
            )


def task_fingerprint(task: IdentityTask, owner_block: str, *, model: str, effort: str) -> str:
    """Fingerprint one parsed orchestration task from its actual judge input."""
    return judgment_fingerprint(*task.packet(), owner_block, model=model, effort=effort)


def judge_batch(
    tasks: list[IdentityTask],
    *,
    owner_block: str,
    model: str,
    effort: str,
    concurrency: int | None,
    timeout: int,
    max_retries: int,
    on_done: Callable[[int, int], None] | None = None,
) -> list[IdentityJudgeResult]:
    """Evaluate a bounded batch through one async client and event loop."""
    config = OpenAIResponsesConfig.resolve(
        model=model,
        effort=effort,
        concurrency=concurrency,
        timeout=timeout,
        max_retries=max_retries,
    )

    async def run() -> list[IdentityJudgeResult]:
        caller = OpenAIResponsesCaller(config)
        judge = IdentityJudge(caller, owner_block, config.model, config.effort)
        done = 0

        async def one(task: IdentityTask) -> IdentityJudgeResult:
            nonlocal done
            result = await judge.judge_identity(*task.packet())
            done += 1
            if on_done:
                on_done(done, len(tasks))
            return result

        try:
            # gather schedules all tasks at once; caller.semaphore (not this
            # call) is what actually bounds concurrent in-flight OpenAI requests.
            return list(await asyncio.gather(*(one(task) for task in tasks)))
        finally:
            await caller.close()

    return asyncio.run(run())


def research_proposal_task(
    evidence: DossierEvidence,
    profile: JudgeProfile,
    *,
    name: str,
) -> IdentityTask:
    return IdentityTask(
        name=name,
        evidence=evidence,
        linkedin=profile,
        origin=IdentityOrigin.RESEARCH,
    )
