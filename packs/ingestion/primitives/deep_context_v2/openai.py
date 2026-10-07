"""Strict-schema calls to the OpenAI Responses API, on top of the shared helpers in
`packs.indexing.lib.openai_responses` (effort, request kwargs, parsing, pricing).

What is specific to deep context lives here: the one `.env` load, the resolved config, a
semaphore bounding in-flight calls, and the flex-to-standard retry when flex has no capacity.

Created: 2026-10-06
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI, RateLimitError

from packs.indexing.lib.llm_config import FLEX_SERVICE_TIER, STANDARD_SERVICE_TIER
from packs.indexing.lib.openai_responses import parse_json_response, reasoning_effort, responses_kwargs
from packs.indexing.lib.openai_usage_tiers import env_or_profile_int

_REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_OPENAI_CONCURRENCY = 64


def load_env() -> None:
    """Load the nearest .env (cwd, its parents, then the repo root) without overriding set variables."""
    bases: list[Path] = [Path.cwd()]
    bases.extend(Path.cwd().parents)
    bases.append(_REPO_ROOT)
    for base in bases:
        env_path = base / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
            return


@dataclass(frozen=True)
class OpenAIResponsesConfig:
    model: str
    effort: str
    concurrency: int
    timeout: int
    max_retries: int

    @classmethod
    def resolve(cls, *, model: str, effort: str, timeout: int, max_retries: int) -> OpenAIResponsesConfig:
        """Resolve effort and concurrency from the environment once, before any call."""
        load_env()
        slots: int = env_or_profile_int(
            "POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency", fallback=DEFAULT_OPENAI_CONCURRENCY
        )
        return cls(model=model, effort=reasoning_effort(effort), concurrency=slots, timeout=timeout, max_retries=max_retries)


class OpenAIResponsesCaller:
    """One SDK client and a semaphore bounding in-flight calls."""

    def __init__(self, config: OpenAIResponsesConfig) -> None:
        self.config = config
        # The SDK retries transient statuses and honors Retry-After.
        self.client = AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL") or None,
            timeout=config.timeout,
            max_retries=config.max_retries,
        )
        self.semaphore = asyncio.Semaphore(config.concurrency)

    async def __aenter__(self) -> OpenAIResponsesCaller:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.client.close()

    async def call(
        self, *, system_prompt: str, user_prompt: str, schema: dict[str, Any], schema_name: str, context: str
    ) -> dict[str, Any]:
        """One strict-schema response, parsed to a dict. The only network call in deep context v2."""
        request: dict[str, Any] = responses_kwargs(
            self.config.model, effort=self.config.effort, schema=schema, schema_name=schema_name
        )
        request["model"] = self.config.model
        request["input"] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        async with self.semaphore:
            try:
                response = await self.client.responses.create(**request)
            except RateLimitError:
                # Flex is spare capacity; when it has none, send once more on the standard tier.
                if request.get("service_tier") != FLEX_SERVICE_TIER:
                    raise
                request["service_tier"] = STANDARD_SERVICE_TIER
                response = await self.client.responses.create(**request)
        return parse_json_response(response, context)
