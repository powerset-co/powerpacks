"""Validate GTM JSON at the HTTP edge and preserve full provider evidence."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from packs.gtm.primitives.discover.models import RankedCandidate, Result
from packs.ingestion.primitives.common.jsonio import write_json, write_jsonl

Operation = Literal["discover", "refine", "sort"]
HTTP_TIMEOUT_SECONDS = 180


def post(*, base: str, token: str, operation: Operation, request_text: str,
         response_path: Path, candidates_path: Path) -> Result | tuple[RankedCandidate, ...]:
    request = urllib.request.Request(
        base + f"/v2/gtm/{operation}", data=request_text.encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        response_text = response.read()
    result: Result | tuple[RankedCandidate, ...]
    if operation == "sort":
        result = TypeAdapter(tuple[RankedCandidate, ...]).validate_json(response_text)
        rows = json.loads(response_text)
    else:
        result = TypeAdapter(Result).validate_json(response_text)
        rows = json.loads(response_text)["candidates"]
    write_json(response_path, json.loads(response_text))
    write_jsonl(candidates_path, rows)
    return result


def save_request(path: Path, text: str) -> None:
    write_json(path, json.loads(text))
