"""Shared embedding helper for search backends.

Offline test guard: when POWERPACKS_FAKE_EMBEDDINGS=1 is set, `embedding`
returns a deterministic 3-dim vector derived from the text instead of calling
OpenAI. This exists ONLY so hermetic tests can run retrieval subprocesses with
no API key; production flows never set it, and the tiny dimension matches the
3-dim vectors the test fixtures write.

Changelog:
- 2026-07-26: add the POWERPACKS_FAKE_EMBEDDINGS=1 offline test guard.
"""

from __future__ import annotations

import hashlib
import os


def ensure_openai_package() -> None:
    try:
        __import__("openai")
        return
    except ModuleNotFoundError:
        raise RuntimeError("Missing required package: openai. Run bin/setup-python.")


def _fake_embedding(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [byte / 255.0 for byte in digest[:3]]


async def embedding(text: str) -> list[float]:
    if os.getenv("POWERPACKS_FAKE_EMBEDDINGS") == "1":
        return _fake_embedding(text)
    ensure_openai_package()
    if __package__:
        from .openai_client import make_async_openai_client
    else:
        from openai_client import make_async_openai_client  # type: ignore[import-not-found]

    client = make_async_openai_client()
    response = await client.embeddings.create(
        input=[text],
        model=os.getenv("POWERPACKS_EMBEDDING_MODEL", "text-embedding-3-small"),
    )
    return response.data[0].embedding
