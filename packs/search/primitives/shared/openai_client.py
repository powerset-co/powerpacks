"""Shared OpenAI client construction for search primitives.

One place that interprets OPENAI_API_BASE, so every primitive resolves the same
base URL whether or not the configured value carries the /v1 suffix the SDK
needs. Same normalization as llm_filter_candidates/llm_rerank_candidates'
openai_base_url; without it, a custom OPENAI_API_BASE like
"https://proxy.example.com" works in the older primitives but 404s in any
primitive that passes the raw value through.
"""
from __future__ import annotations

import os

import openai

DEFAULT_API_BASE = "https://api.openai.com"


def openai_base_url(api_base: str | None = None) -> str:
    """Resolve explicit arg > OPENAI_API_BASE env > default, always /v1-suffixed."""
    base = (api_base or os.environ.get("OPENAI_API_BASE") or DEFAULT_API_BASE).rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def make_openai_client(api_key: str, api_base: str | None = None) -> openai.OpenAI:
    return openai.OpenAI(api_key=api_key, base_url=openai_base_url(api_base))
