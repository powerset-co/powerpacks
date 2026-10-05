"""Queue parsing, dossier input shaping, fingerprinting, and paid-result reuse."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from packs.ingestion.primitives.deep_context.db.models import ArtifactRow
from packs.ingestion.primitives.deep_context.enrich.parallel_research import config


@dataclass(frozen=True)
class ResearchQueueRow:
    """One canonical provider queue row between selection and projection."""

    parent_id: str
    candidate_exists: bool
    row_key: str
    handle: str
    source_person_ids: tuple[str, ...]
    display_name: str
    bio: str = ""
    known_info: str = ""
    primary_email: str = ""
    phone_e164: str = ""
    retarget_hint: str = ""

    def __post_init__(self) -> None:
        if not self.handle or self.handle != self.handle.strip():
            raise ValueError("research handle must be non-empty and trimmed")


def build_input(row: ResearchQueueRow) -> dict[str, Any]:
    """Collapse a queue row into one dossier plus optional human guidance.

    Example, for a row with display_name="Jordan Bravo",
    primary_email="casey@example.com":
    {"dossier": "Name: Jordan Bravo\\nEmail: casey@example.com\\n..."}.
    This dict, unchanged, becomes the SDK RunInputParam.input and feeds the
    paid request fingerprint below.
    """
    guidance = row.retarget_hint.strip()
    lines = []
    for label, value in (
        ("Name", row.display_name),
        ("Relationship dossier", row.bio),
        ("Email", row.primary_email),
        ("Phone", row.phone_e164),
        ("Additional context", row.known_info),
    ):
        text = str(value).strip()
        if text:
            lines.append(f"{label}: {text}")
    payload: dict[str, Any] = {"dossier": "\n".join(lines)}
    if guidance:
        payload["guidance"] = guidance
    return payload


def _provider_contract(processor: str, beta_header: str) -> dict[str, Any]:
    return {
        "processor": processor,
        "task_spec": config.TASK_SPEC,
        "beta_header": beta_header,
    }


def _json_fingerprint(payload: object) -> str:
    data = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def input_fingerprint(
    row: ResearchQueueRow,
    *,
    processor: str = config.DEFAULT_PROCESSOR,
    beta_header: str = config.DEFAULT_BETA_HEADER,
) -> str:
    """Return the paid-cache key for the complete provider request contract.

    The canonical JSON below is the Parallel reuse boundary; changing a key,
    value, or serialization option makes every affected handle billable again.
    """
    return _json_fingerprint(
        {
            "input": build_input(row),
            **_provider_contract(processor, beta_header),
        }
    )


def request_plan_fingerprint(
    rows: Iterable[ResearchQueueRow],
    *,
    processor: str = config.DEFAULT_PROCESSOR,
    beta_header: str = config.DEFAULT_BETA_HEADER,
) -> str:
    """Bind a receipt to the deduplicated provider request contract.

    The full queue is intentional: after a successful run, those same requests
    move from pending to reused, but its completion receipt must still match.
    """
    requests: dict[str, str] = {}
    for row in rows:
        requests.setdefault(
            row.handle,
            input_fingerprint(
                row,
                processor=processor,
                beta_header=beta_header,
            ),
        )
    return _json_fingerprint(
        {
            "provider_contract": _provider_contract(processor, beta_header),
            "requests": sorted(requests.items()),
        }
    )


def filter_already_done(
    rows: Iterable[ResearchQueueRow],
    projected_research: Iterable[ArtifactRow],
    *,
    processor: str = config.DEFAULT_PROCESSOR,
    beta_header: str = config.DEFAULT_BETA_HEADER,
) -> tuple[list[ResearchQueueRow], int]:
    """Reuse only projected outputs with the exact input and provider contract."""
    completed = {
        artifact.artifact_key.removeprefix("research:").lower(): artifact.input_fingerprint
        for artifact in projected_research
    }
    todo: list[ResearchQueueRow] = []
    skipped = 0
    seen: set[str] = set()
    for source in rows:
        handle = source.handle.strip()
        if handle in seen:
            continue
        seen.add(handle)
        row = source
        if handle.lower() in completed:
            stored = str(completed[handle.lower()] or "")
            current = input_fingerprint(
                row, processor=processor, beta_header=beta_header
            )
            if stored == current:
                skipped += 1
                continue
        todo.append(row)
    return todo, skipped
