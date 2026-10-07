"""The OpenAI Responses client for paid stages: one configured client, strict-schema calls, cost estimate.

Created: 2026-10-06
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI, RateLimitError

from packs.indexing.lib.llm_config import (
    CHAT_MODEL_PRICES_PER_1K_USD,
    FLEX_SERVICE_TIER,
    STANDARD_SERVICE_TIER,
    is_reasoning_model,
    openai_price_multiplier,
    openai_service_tier,
)
from packs.indexing.lib.openai_usage_tiers import env_or_profile_int

_REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_OPENAI_CONCURRENCY = 64
DEFAULT_MAX_OUTPUT_TOKENS = 8000
DEFAULT_REASONING_EFFORT = "medium"
VALID_EFFORTS = ("minimal", "low", "medium", "high")


def load_env() -> None:
    """Load the nearest .env (cwd, its parents, then the repo root) without overriding set variables."""
    bases = [Path.cwd()]
    bases.extend(Path.cwd().parents)
    bases.append(_REPO_ROOT)
    for base in bases:
        env_path = base / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
            return


def normalize_reasoning_effort(default: str) -> str:
    """POWERPACKS_DEEP_CONTEXT_REASONING_EFFORT overrides `default`; an unknown value falls back to medium."""
    effort = os.getenv("POWERPACKS_DEEP_CONTEXT_REASONING_EFFORT", default).strip().lower()
    if effort in VALID_EFFORTS:
        return effort
    return DEFAULT_REASONING_EFFORT


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
        slots = env_or_profile_int(
            "POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency", fallback=DEFAULT_OPENAI_CONCURRENCY
        )
        return cls(
            model=model,
            effort=normalize_reasoning_effort(effort),
            concurrency=max(1, slots),
            timeout=max(1, timeout),
            max_retries=max(0, max_retries),
        )


class OpenAIResponsesCaller:
    """One SDK client and a semaphore bounding in-flight calls."""

    def __init__(self, config: OpenAIResponsesConfig) -> None:
        self.config = config
        load_env()
        self.client = AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL") or None,
            timeout=config.timeout,
            max_retries=config.max_retries,  # the SDK retries transient statuses and honors Retry-After
        )
        self.semaphore = asyncio.Semaphore(config.concurrency)

    async def __aenter__(self) -> OpenAIResponsesCaller:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.client.close()

    async def call(
        self, *, system_prompt: str, user_prompt: str, schema: dict[str, Any], schema_name: str, context: str
    ) -> dict[str, Any]:
        """One strict-schema response, parsed. The only network call in this module."""
        request = self._request(system_prompt, user_prompt, schema, schema_name)
        async with self.semaphore:
            try:
                response = await self.client.responses.create(**request)
            except RateLimitError:
                # Flex is spare capacity; when it has none, send once more on the standard tier.
                if request.get("service_tier") != FLEX_SERVICE_TIER:
                    raise
                request["service_tier"] = STANDARD_SERVICE_TIER
                response = await self.client.responses.create(**request)
        return _payload(response, context)

    def _request(self, system_prompt: str, user_prompt: str, schema: dict[str, Any], schema_name: str) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.config.model,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "store": False,  # requests carry message text; OpenAI keeps none of them
            "max_output_tokens": int(
                os.getenv("POWERPACKS_DEEP_CONTEXT_MAX_OUTPUT_TOKENS", str(DEFAULT_MAX_OUTPUT_TOKENS))
            ),
            "text": {"format": {"type": "json_schema", "name": schema_name, "strict": True, "schema": schema}},
        }
        if is_reasoning_model(self.config.model):
            # The API rejects reasoning and service_tier on non-reasoning models.
            request["reasoning"] = {"effort": self.config.effort}
            request["service_tier"] = openai_service_tier()
        return request


def _payload(response: Any, context: str) -> dict[str, Any]:
    """The answer's JSON; a truncated answer is warned about on stderr, an empty one raises."""
    if getattr(response, "status", None) == "incomplete":
        reason = getattr(getattr(response, "incomplete_details", None), "reason", "unknown")
        print(
            f"⚠️  {context}: LLM output was TRUNCATED (incomplete: {reason}) — "
            "raise POWERPACKS_DEEP_CONTEXT_MAX_OUTPUT_TOKENS",
            file=sys.stderr,
        )
    raw = str(getattr(response, "output_text", "") or "").strip()
    if not raw:
        # output_text is the SDK's join of these same chunks; walk them when it comes back empty.
        parts: list[str] = []
        for item in getattr(response, "output", None) or ():
            for chunk in getattr(item, "content", None) or ():
                value = getattr(chunk, "text", None)
                if value:
                    parts.append(value)
        raw = "".join(parts).strip()
    if not raw:
        raise ValueError(f"{context}: empty response")
    return json.loads(raw)


def estimate_cost_usd(input_tokens: int, output_tokens: int, model: str) -> float:
    """Estimated cost from the shared price table; an unpriced model estimates 0.0."""
    prices = CHAT_MODEL_PRICES_PER_1K_USD.get(model)
    if not prices:
        return 0.0
    raw = (input_tokens / 1000.0) * prices["input"] + (output_tokens / 1000.0) * prices["output"]
    return round(raw * openai_price_multiplier(), 6)
