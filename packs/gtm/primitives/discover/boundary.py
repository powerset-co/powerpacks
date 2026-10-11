"""Validate GTM JSON at the HTTP edge and preserve full provider evidence."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from packs.gtm.primitives.discover.models import RankedCandidate, Result, WebFinding
from packs.ingestion.primitives.common.jsonio import write_json, write_jsonl

Operation = Literal["discover", "refine", "sort", "web"]
HTTP_TIMEOUT_SECONDS = 180


def _send(base: str, token: str, path: str, request_text: str) -> bytes:
    request = urllib.request.Request(
        base + path, data=request_text.encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        body: bytes = response.read()
    return body


def post_web(*, base: str, token: str, request_text: str, response_path: Path) -> tuple[WebFinding, ...]:
    """Each search is one paid Parallel request; every page found is kept."""
    response_text = _send(base, token, "/v2/gtm/web", request_text)
    write_json(response_path, json.loads(response_text))
    return TypeAdapter(tuple[WebFinding, ...]).validate_json(response_text)


def post(*, base: str, token: str, operation: Operation, request_text: str,
         response_path: Path, candidates_path: Path) -> Result | tuple[RankedCandidate, ...]:
    response_text = _send(base, token, f"/v2/gtm/{operation}", request_text)
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
