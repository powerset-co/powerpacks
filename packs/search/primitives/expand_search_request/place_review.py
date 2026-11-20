"""Name the places a lookup returned that the query did not mean, using the API's review prompt.

The API's place lookup matches on words, so "New York" also returns "York" and "York Metropolitan
Area". The API's own expansion follows the lookup with this review (LOCATION_REFLECT_PROMPT in
network-search-api `api_v2/search/query/location_extraction.py`); `/v2/search/resolve` returns the
lookup unreviewed, so the app runs the review.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from packs.search.primitives.expand_search_request.parallel_extractors import EXTRACTOR_MODELS, _extract
from packs.search.primitives.shared.openai_client import make_async_openai_client

PLACE_KEYS = ("cities", "states", "countries", "metro_areas", "macro_regions")
PROMPT = (Path(__file__).parent / "prompts/location_reflect.txt").read_text()


async def unintended_places(query: str, places: list[str], *, model: str | None = None,
                            reasoning_effort: str | None = None) -> set[str]:
    client = make_async_openai_client(os.environ.get("OPENAI_API_KEY"))
    result = await _extract(client, "place_review", PROMPT, f"Query: {query}\nResults: {json.dumps(places)}",
                            model=model or EXTRACTOR_MODELS["location"], reasoning_effort=reasoning_effort)
    return set(result.get("remove") or []) & set(places)
