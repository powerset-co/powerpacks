"""One configured OpenAI Responses caller for Deep Context paid stages."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from packs.indexing.lib.llm_config import (
    CHAT_MODEL_PRICES_PER_1K_USD,
    is_reasoning_model,
    openai_price_multiplier,
    openai_service_tier,
)
from packs.indexing.lib.openai_usage_tiers import env_or_profile_int
from packs.ingestion.primitives.deep_context.shared.common import load_env

DEFAULT_OPENAI_CONCURRENCY = 64
DEFAULT_MAX_OUTPUT_TOKENS = 8000
DEFAULT_REASONING_EFFORT = "medium"
VALID_EFFORTS = ("minimal", "low", "medium", "high")


def normalize_reasoning_effort(default: str = DEFAULT_REASONING_EFFORT) -> str:
    """Resolve the one Deep Context reasoning-effort override."""
    effort = os.getenv(
        "POWERPACKS_DEEP_CONTEXT_REASONING_EFFORT",
        default,
    ).strip().lower()
    return effort if effort in VALID_EFFORTS else DEFAULT_REASONING_EFFORT


@dataclass(frozen=True)
class OpenAIResponsesConfig:
    model: str
    effort: str
    concurrency: int
    timeout: int
    max_retries: int

    @classmethod
    def resolve(
        cls,
        *,
        model: str,
        effort: str,
        concurrency: int | None,
        timeout: int,
        max_retries: int,
    ) -> OpenAIResponsesConfig:
        """Resolve shared paid-call configuration once before work starts."""
        load_env()
        slots = concurrency or env_or_profile_int(
            "POWERPACKS_OPENAI_CONCURRENCY",
            "openai_concurrency",
            fallback=DEFAULT_OPENAI_CONCURRENCY,
        )
        return cls(
            model=model,
            effort=normalize_reasoning_effort(effort),
            concurrency=max(1, slots),
            timeout=max(1, timeout),
            max_retries=max(0, max_retries),
        )


@dataclass(frozen=True)
class OpenAIUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def __add__(self, other: OpenAIUsage) -> OpenAIUsage:
        return type(self)(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.reasoning_tokens + other.reasoning_tokens,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


@dataclass(frozen=True)
class OpenAIResponse:
    payload: dict[str, Any]
    usage: OpenAIUsage


class OpenAIResponsesCaller:
    """Own one SDK client, retry policy, semaphore, parser, and usage tally."""

    def __init__(
        self,
        config: OpenAIResponsesConfig,
        *,
        client: Any | None = None,
    ) -> None:
        self.config = config
        if client is None:
            load_env()
            client = AsyncOpenAI(
                api_key=os.getenv("OPENAI_API_KEY"),
                base_url=os.getenv("OPENAI_BASE_URL") or None,
                timeout=config.timeout,
                # The SDK retries the same transient status family and honors
                # Retry-After; keeping the configured retry count preserves the
                # old paid-call attempt ceiling without a second retry loop.
                max_retries=config.max_retries,
            )
        self.client = client
        # Bounds concurrent in-flight `responses.create` calls only. Callers
        # (e.g. judge_batch's asyncio.gather) may schedule far more tasks at
        # once; the excess just waits here instead of hitting the API.
        self.semaphore = asyncio.Semaphore(config.concurrency)
        self.usage = OpenAIUsage()

    async def __aenter__(self) -> OpenAIResponsesCaller:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self.client.close()

    async def call(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        schema_name: str,
        context: str,
    ) -> OpenAIResponse:
        """Run one strict-schema response under the shared concurrency limit."""
        async with self.semaphore:
            # The billing boundary: the only network call in this module.
            # config.max_retries transient-status attempts happen inside this
            # one await before it returns or raises; usage below tallies only
            # the response the SDK ultimately hands back.
            response = await self.client.responses.create(
                model=self.config.model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                **self._request_kwargs(schema, schema_name),
            )
        usage = self._usage(response)
        self.usage = self.usage + usage
        return OpenAIResponse(self._payload(response, context), usage)

    def _request_kwargs(
        self,
        schema: dict[str, Any],
        schema_name: str,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "max_output_tokens": int(
                os.getenv(
                    "POWERPACKS_DEEP_CONTEXT_MAX_OUTPUT_TOKENS",
                    str(DEFAULT_MAX_OUTPUT_TOKENS),
                )
            ),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        if is_reasoning_model(self.config.model):
            # reasoning/service_tier are only valid on reasoning models — the
            # API rejects them on non-reasoning models, so this can't be an
            # unconditional default.
            kwargs["reasoning"] = {"effort": self.config.effort}
            kwargs["service_tier"] = openai_service_tier()
        return kwargs

    @staticmethod
    def _payload(response: Any, context: str) -> dict[str, Any]:
        if getattr(response, "status", None) == "incomplete":
            reason = getattr(
                getattr(response, "incomplete_details", None),
                "reason",
                "unknown",
            )
            print(
                f"⚠️  {context}: LLM output was TRUNCATED (incomplete: {reason}) — "
                "raise POWERPACKS_DEEP_CONTEXT_MAX_OUTPUT_TOKENS",
                file=sys.stderr,
            )
        raw = str(getattr(response, "output_text", "") or "").strip()
        if not raw:
            # Fallback only: output_text is the SDK's convenience join of the
            # same output/content chunks walked here, for responses where it
            # comes back empty despite content being present.
            parts: list[str] = []
            for item in getattr(response, "output", None) or ():
                for chunk in getattr(item, "content", None) or ():
                    if value := getattr(chunk, "text", None):
                        parts.append(value)
            raw = "".join(parts).strip()
        if not raw:
            raise ValueError(f"{context}: empty response")
        return json.loads(raw)

    @staticmethod
    def _usage(response: Any) -> OpenAIUsage:
        usage = getattr(response, "usage", None)
        if usage is None:
            return OpenAIUsage()
        details = getattr(usage, "output_tokens_details", None)
        return OpenAIUsage(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            reasoning_tokens=(
                int(getattr(details, "reasoning_tokens", 0) or 0)
                if details is not None
                else 0
            ),
        )


def estimate_cost_usd(input_tokens: int, output_tokens: int, model: str) -> float:
    """Estimate Responses cost using the shared model-price table.

    Report-only: an unpriced model silently returns 0.0 rather than raising.
    Nothing here gates spend, so failing open is safe.
    """
    prices = CHAT_MODEL_PRICES_PER_1K_USD.get(model)
    if not prices:
        return 0.0
    raw = (
        (input_tokens / 1000.0) * prices["input"]
        + (output_tokens / 1000.0) * prices["output"]
    )
    return round(raw * openai_price_multiplier(), 6)
