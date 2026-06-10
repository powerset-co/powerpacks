"""Shared embedding helper for search backends."""

from __future__ import annotations

import os


def ensure_openai_package() -> None:
    try:
        __import__("openai")
        return
    except ModuleNotFoundError:
        raise RuntimeError("Missing required package: openai. Run bin/setup-python.")


async def embedding(text: str) -> list[float]:
    ensure_openai_package()
    import openai

    client = openai.AsyncOpenAI()
    response = await client.embeddings.create(
        input=[text],
        model=os.getenv("POWERPACKS_EMBEDDING_MODEL", "text-embedding-3-small"),
    )
    return response.data[0].embedding
