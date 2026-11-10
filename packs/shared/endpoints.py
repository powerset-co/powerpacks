"""Base URLs for the Powerset gateway and OpenAI, overridable so a test run can point at local mocks."""
from __future__ import annotations

import os

DEFAULT_GATEWAY_URL = "https://proxy.powerset.dev"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


def gateway_url() -> str:
    return (os.getenv("POWERPACKS_GATEWAY_URL") or DEFAULT_GATEWAY_URL).rstrip("/")


def openai_base_url() -> str:
    """OPENAI_BASE_URL, then the older POWERPACKS_OPENAI_BASE and OPENAI_API_BASE; always ends in /v1."""
    base = (os.getenv("OPENAI_BASE_URL") or os.getenv("POWERPACKS_OPENAI_BASE")
            or os.getenv("OPENAI_API_BASE") or DEFAULT_OPENAI_BASE_URL).rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"
