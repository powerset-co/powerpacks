"""Queue parsing, dossier input shaping, fingerprinting, and paid-result reuse."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from packs.ingestion.primitives.deep_context.db.models import ArtifactRow
from packs.ingestion.primitives.deep_context.enrich.parallel_research import config
from packs.ingestion.primitives.common.legacy import legacy_parallel_input_fingerprint


@dataclass(frozen=True)
class ResearchQueueRow:
    """One canonical provider queue row between selection and projection."""

    parent_id: str
    candidate_exists: bool
    row_key: str
    handle: str
    source_person_ids: tuple[str, ...]
    source_candidate_public_identifier: str
    display_name: str
    bio: str = ""
    known_info: str = ""
    primary_email: str = ""
    phone_e164: str = ""
    retarget_hint: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_exists, bool):
            raise TypeError("candidate_exists must be a bool")
        if not self.handle or self.handle != self.handle.strip():
            raise ValueError("research handle must be non-empty and trimmed")


def build_input(row: ResearchQueueRow, handle: str) -> dict[str, Any]:
    """Collapse a queue row into one dossier plus optional human guidance.

    Example, for a row with display_name="Jordan Bravo",
    primary_email="casey@example.com": {"handle": "jbravo",
    "dossier": "Name: Jordan Bravo\\nEmail: casey@example.com\\n..."}.
    This dict, unchanged, becomes the SDK RunInputParam.input and feeds the
    paid request fingerprint below.
    """
    name = row.display_name.strip()
    guidance = row.retarget_hint.strip()
    known = row.known_info.strip()
    lines = [f"Name: {name or handle}"]
    for label, value in (
        ("Relationship dossier", row.bio),
        ("Email", row.primary_email),
        ("Phone", row.phone_e164),
        ("Additional context", known),
    ):
        text = str(value).strip()
        if text:
            lines.append(f"{label}: {text}")
    payload: dict[str, Any] = {"handle": handle, "dossier": "\n".join(lines)}
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
    handle: str,
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
            "input": build_input(row, handle),
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
                row.handle,
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
    """Reuse projected paid outputs; changed inputs overwrite the fixed path.

    The only resume evidence is a projected DB artifact row. Driver projects
    each accepted provider output before reading the next stream event, so a
    rerun submits only rows that did not reach that checkpoint.
    """
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
                row, handle, processor=processor, beta_header=beta_header
            )
            legacy = legacy_parallel_input_fingerprint(build_input(row, handle))
            if stored in {current, legacy}:
                skipped += 1
                continue
        todo.append(row)
    return todo, skipped
