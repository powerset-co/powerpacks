"""Responses-API helpers for medium/high reasoning calls.

The enrichment stages in ``llm_config.api_call_kwargs`` target the *Chat
Completions* API with ``reasoning_effort="low"`` — tuned for cheap, high-volume
classification. The deep-context dossier flow wants the opposite trade-off: a
*small* number of calls per person that each reason hard over raw message text,
so we use the **Responses API** with a configurable ``reasoning_effort``
(``medium`` / ``high``).

This module reuses the existing pricing table, service-tier multiplier, and
concurrency/usage-tier infra from ``llm_config`` / ``openai_usage_tiers`` so cost
accounting stays consistent with the rest of the repo. It deliberately does NOT
re-implement the streaming pool — callers fan out with
``openai_stream.drain_pool`` exactly like the chat-completions callers do.
"""
from __future__ import annotations

import json
import os
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)

from packs.indexing.lib.llm_config import (
    CHAT_MODEL_PRICES_PER_1K_USD,
    DEFAULT_MODEL,
    is_reasoning_model,
    openai_price_multiplier,
    openai_service_tier,
)

# Reasoning calls over a person's messages produce a compact structured object,
# but high effort burns reasoning tokens — keep a generous cap so a strict-schema
# response is never truncated mid-object (which the SDK surfaces as a parse error).
DEFAULT_MAX_OUTPUT_TOKENS = 4000
DEFAULT_REASONING_EFFORT = "medium"
VALID_EFFORTS = ("minimal", "low", "medium", "high")

# Retryable transient failures — same set the chat-completions callers retry.
_RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}


def reasoning_effort(default: str = DEFAULT_REASONING_EFFORT) -> str:
    """Effort from ``POWERPACKS_DEEP_CONTEXT_REASONING_EFFORT`` env, else *default*."""
    effort = os.getenv("POWERPACKS_DEEP_CONTEXT_REASONING_EFFORT", default).strip().lower()
    return effort if effort in VALID_EFFORTS else default


def responses_kwargs(
    model: str,
    *,
    effort: str | None = None,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    schema: dict[str, Any] | None = None,
    schema_name: str = "structured_output",
) -> dict[str, Any]:
    """Non-input kwargs for ``client.responses.create``.

    Mirrors ``llm_config.api_call_kwargs`` but for the Responses API: reasoning
    models get ``reasoning.effort`` + the flex/default service tier; a JSON
    schema (when given) is enforced via ``text.format`` strict mode.
    """
    kwargs: dict[str, Any] = {
        "max_output_tokens": int(
            os.getenv("POWERPACKS_DEEP_CONTEXT_MAX_OUTPUT_TOKENS", str(max_output_tokens))
        ),
    }
    if is_reasoning_model(model):
        kwargs["reasoning"] = {"effort": effort or reasoning_effort()}
        kwargs["service_tier"] = openai_service_tier()
    if schema is not None:
        kwargs["text"] = {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": schema,
            }
        }
    return kwargs


def is_retryable(exc: Exception) -> bool:
    """True if *exc* is a transient OpenAI failure worth retrying."""
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    if isinstance(exc, APIStatusError):
        return getattr(exc, "status_code", None) in _RETRY_STATUS
    return False


def response_text(response: Any) -> str:
    """Extract the text payload from a Responses-API result.

    Prefers ``output_text`` (the SDK's flattened convenience field); falls back
    to walking ``output[].content[].text`` for older/edge responses.
    """
    text = getattr(response, "output_text", None)
    if text:
        return text
    parts: list[str] = []
    for item in getattr(response, "output", None) or []:
        for chunk in getattr(item, "content", None) or []:
            value = getattr(chunk, "text", None)
            if value:
                parts.append(value)
    return "".join(parts)


def parse_json_response(response: Any, context: str = "responses call") -> dict[str, Any]:
    """Parse a strict-schema Responses result into a dict (raises on bad JSON)."""
    raw = response_text(response).strip()
    if not raw:
        raise ValueError(f"{context}: empty response")
    return json.loads(raw)


def usage_tokens(response: Any) -> dict[str, int]:
    """Normalize Responses ``usage`` into input/output/reasoning token counts."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    details = getattr(usage, "output_tokens_details", None)
    reasoning = int(getattr(details, "reasoning_tokens", 0) or 0) if details else 0
    return {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "reasoning_tokens": reasoning,
    }


def estimate_cost_usd(input_tokens: int, output_tokens: int, model: str = DEFAULT_MODEL) -> float:
    """USD estimate using the shared pricing table + active service-tier multiplier.

    Responses bills reasoning tokens as output tokens, so callers should fold
    ``reasoning_tokens`` into *output_tokens* before calling this.
    """
    prices = CHAT_MODEL_PRICES_PER_1K_USD.get(model)
    if not prices:
        return 0.0
    raw = (input_tokens / 1000.0) * prices["input"] + (output_tokens / 1000.0) * prices["output"]
    return round(raw * openai_price_multiplier(), 6)


def make_async_client(*, timeout: int = 120) -> AsyncOpenAI:
    """AsyncOpenAI client with retries disabled (callers own retry/backoff)."""
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL") or None
    return AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)
