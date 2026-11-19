"""Pick role title groups and keywords using the API's title-selection prompt."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.search.primitives.expand_search_request.parallel_extractors import EXTRACTOR_MODELS, _extract
from packs.search.primitives.shared.openai_client import make_async_openai_client

MAX_TITLE_GROUPS = 100
MAX_ADJACENT_PATTERNS = 15
TITLE_SELECTION_PROMPT = (Path(__file__).parent / "prompts/title_selection.txt").read_text()
ADJACENT_TITLE_SELECTION_PROMPT = (Path(__file__).parent / "prompts/adjacent_title_selection.txt").read_text()


@dataclass(frozen=True)
class TitleSelection:
    selected: list[str]
    keywords: list[str]


async def pick_title_clusters(
    query: str,
    groups: list[dict[str, Any]],
    *,
    system_prompt: str = TITLE_SELECTION_PROMPT,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> TitleSelection:
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY not set")
    lines = []
    for group in groups[:MAX_TITLE_GROUPS]:
        stem = group.get("stemmed") or group["stem"]
        examples = ", ".join((group.get("raw_titles") or group["titles"])[:3])
        lines.append(f'  "{stem}" (count={group["count"]}, e.g.: {examples})')
    user_content = (f'Query: "{query}"\n\n'
                    'Title clusters (stemmed form, count, raw examples):\n'
                    + "\n".join(lines)
                    + f'\n\nWhich clusters match people who would be described as "{query}"?')
    client = make_async_openai_client(os.environ.get("OPENAI_API_KEY"))
    previous_stage = os.environ.get("POWERPACKS_USAGE_STAGE")
    os.environ["POWERPACKS_USAGE_STAGE"] = "expand"
    try:
        result = await _extract(client, "title_selection", system_prompt, user_content,
                                model=model or EXTRACTOR_MODELS["role"], reasoning_effort=reasoning_effort)
    finally:
        if previous_stage is None:
            os.environ.pop("POWERPACKS_USAGE_STAGE", None)
        else:
            os.environ["POWERPACKS_USAGE_STAGE"] = previous_stage
    if not result:
        raise RuntimeError("Title selection returned no result")
    return TitleSelection(selected=result.get("selected", []), keywords=result.get("keywords", []))
