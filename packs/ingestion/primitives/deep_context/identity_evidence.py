"""One typed evidence judge for attached and researched LinkedIn identities."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Callable

from packs.indexing.lib.openai_responses import (
    is_retryable,
    make_async_client,
    parse_json_response,
    responses_kwargs,
    usage_tokens,
)
from packs.ingestion.primitives.deep_context.common import load_env
from packs.ingestion.primitives.deep_context.db.models import IdentityOrigin
from packs.ingestion.primitives.deep_context.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.identity_reconcile import judgment_policy
from packs.ingestion.primitives.deep_context.judge_models import (
    IdentityTask,
    IdentityJudgeResult,
    IdentityUsage,
    IdentityVerdict,
    JudgeProfile,
)
from packs.ingestion.primitives.deep_context.prompts.loader import load_prompt

DEFAULT_IDENTITY_CONCURRENCY = 64
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
    """Render the sole identity-judge user prompt."""
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
) -> str:
    """Hash exactly the identity judge input, candidate payload, and policy origin.

    This fingerprint is the paid-judge cache key: changing its serialization
    invalidates every matching judgment and re-bills the identity judge.
    """
    judge_profile = {key: value for key, value in profile.as_judge_dict().items() if not key.startswith("_")}
    payload = json.dumps(
        {
            "origin": origin.value,
            "system": SYSTEM_PROMPT,
            "input": identity_judge_prompt(evidence, profile, origin, owner_block),
            "profile": judge_profile,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class IdentityJudge:
    """Configured judge sharing one client, event loop, and semaphore per batch."""

    client: Any | None
    owner_block: str
    model: str
    effort: str
    semaphore: asyncio.Semaphore
    max_retries: int

    async def judge_identity(
        self,
        evidence: DossierEvidence,
        profile: JudgeProfile,
        origin: IdentityOrigin,
    ) -> IdentityJudgeResult:
        """Evaluate one typed identity packet without owning client lifecycle."""
        fingerprint = judgment_fingerprint(evidence, profile, origin, self.owner_block)
        if self.client is None:
            return IdentityJudgeResult(
                verdict=judgment_policy.deterministic_identity(evidence, profile, origin),
                usage=IdentityUsage(),
                error="",
                fingerprint=fingerprint,
            )
        kwargs = responses_kwargs(
            self.model,
            effort=self.effort,
            schema=RECONCILE_SCHEMA,
            schema_name="reconcile",
        )
        async with self.semaphore:
            for attempt in range(self.max_retries + 1):
                try:
                    response = await self.client.responses.create(
                        model=self.model,
                        input=[
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {
                                "role": "user",
                                "content": identity_judge_prompt(evidence, profile, origin, self.owner_block),
                            },
                        ],
                        **kwargs,
                    )
                    return IdentityJudgeResult(
                        verdict=IdentityVerdict.from_payload(parse_json_response(response, "reconcile")),
                        usage=IdentityUsage.from_payload(usage_tokens(response)),
                        error="",
                        fingerprint=fingerprint,
                    )
                except Exception as exc:  # noqa: BLE001
                    if attempt < self.max_retries and is_retryable(exc):
                        await asyncio.sleep(min(2 ** (attempt + 1), 30))
                        continue
                    return IdentityJudgeResult(
                        verdict=None,
                        usage=IdentityUsage(),
                        error=f"{type(exc).__name__}: {exc}"[:200],
                        fingerprint=fingerprint,
                    )
        raise AssertionError("unreachable")


def task_fingerprint(task: IdentityTask, owner_block: str) -> str:
    """Fingerprint one parsed orchestration task from its actual judge input."""
    return judgment_fingerprint(*task.packet(), owner_block)


async def judge_task(
    client: Any,
    task: IdentityTask,
    owner_block: str,
    *,
    model: str,
    effort: str,
    semaphore: asyncio.Semaphore,
    max_retries: int,
) -> IdentityJudgeResult:
    """Compatibility boundary; all evaluation delegates to ``IdentityJudge``."""
    evidence, profile, origin = task.packet()
    return await IdentityJudge(client, owner_block, model, effort, semaphore, max_retries).judge_identity(
        evidence, profile, origin
    )


def judge_batch(
    tasks: list[IdentityTask],
    *,
    use_llm: bool,
    owner_block: str,
    model: str,
    effort: str,
    concurrency: int,
    timeout: int,
    max_retries: int,
    on_done: Callable[[int, int], None] | None = None,
) -> list[IdentityJudgeResult]:
    """Evaluate a bounded batch through one async client and event loop."""
    if use_llm:
        load_env()

    async def run() -> list[IdentityJudgeResult]:
        client = make_async_client(timeout=timeout) if use_llm else None
        semaphore = asyncio.Semaphore(max(1, concurrency))
        judge = IdentityJudge(client, owner_block, model, effort, semaphore, max_retries)
        done = 0

        async def one(task: IdentityTask) -> IdentityJudgeResult:
            nonlocal done
            if client is None:
                result = await judge.judge_identity(*task.packet())
            else:
                result = await judge_task(
                    client,
                    task,
                    owner_block,
                    model=model,
                    effort=effort,
                    semaphore=semaphore,
                    max_retries=max_retries,
                )
            if not result.fingerprint:
                result = replace(
                    result,
                    fingerprint=task_fingerprint(task, owner_block),
                )
            done += 1
            if on_done:
                on_done(done, len(tasks))
            return result

        try:
            return list(await asyncio.gather(*(one(task) for task in tasks)))
        finally:
            if client is not None:
                await client.close()

    return asyncio.run(run())


def research_proposal_task(
    evidence: DossierEvidence,
    profile: JudgeProfile,
    *,
    name: str,
    confidence: float = 0.0,
    unverified: bool = False,
) -> IdentityTask:
    return IdentityTask(
        name=name,
        evidence=evidence,
        linkedin=profile,
        origin=IdentityOrigin.RESEARCH,
        research_confidence=confidence,
        research_unverified=unverified,
    )
