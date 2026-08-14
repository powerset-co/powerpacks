"""Queue parsing, dossier input shaping, fingerprinting, and paid-result reuse."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Iterable

from packs.ingestion.primitives.deep_context.db.models import ArtifactRow
from packs.ingestion.primitives.deep_context.enrich.parallel_research import config
from packs.ingestion.primitives.common.legacy import legacy_parallel_input_fingerprint


class ContactChannel(StrEnum):
    """Which contact identifier a research subject was reached through.

    Decided exactly once, at selection.build_queue_row (the queue-construction
    edge) — every reader downstream trusts this value instead of re-guessing
    a default. Distinct from db.models.SourceChannel
    (import provenance: gmail_msgvault/imessage/whatsapp/linkedin_csv) and
    collection.MessageChannel (message-body channel); this vocabulary answers
    one narrower question — how do we address this one Parallel subject.
    """

    EMAIL = "email"
    PHONE = "phone"


@dataclass(frozen=True)
class ResearchQueueRow:
    """One canonical provider queue row between selection and projection."""

    parent_id: str
    candidate_exists: bool
    row_key: str
    handle: str
    source_parent_slug: str
    source_person_ids: tuple[str, ...]
    source_candidate_public_identifier: str
    display_name: str
    source_channel: ContactChannel
    bio: str = ""
    known_info: str = ""
    primary_email: str = ""
    phone_e164: str = ""
    area_code: str = ""
    retarget_hint: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_exists, bool):
            raise TypeError("candidate_exists must be a bool")
        if not isinstance(self.source_channel, ContactChannel):
            raise TypeError("source_channel must be a ContactChannel")

    def csv_dict(self, fields: Iterable[str]) -> dict[str, str]:
        """Serialize the provider-owned CSV projection at its write edge."""
        values = {
            "handle": self.handle,
            "source_parent_slug": self.source_parent_slug,
            "source_person_ids": json.dumps(self.source_person_ids, ensure_ascii=False),
            "source_candidate_public_identifier": (self.source_candidate_public_identifier),
            "display_name": self.display_name,
            "bio": self.bio,
            "known_info": self.known_info,
            "primary_email": self.primary_email,
            "phone_e164": self.phone_e164,
            "area_code": self.area_code,
            "source_channel": self.source_channel,
            "retarget_hint": self.retarget_hint,
        }
        return {field: values[field] for field in fields}


def candidate_handle(row: ResearchQueueRow) -> str:
    """Return the stable fixed-directory key for one queue row."""
    handle = row.handle.strip()
    if handle:
        return handle
    email = row.primary_email.strip()
    if email:
        return email.split("@", 1)[0].lower().replace(".", "_")
    digits = re.sub(r"\D", "", row.phone_e164)
    if digits:
        return f"phone-{digits[-10:]}"
    name = row.display_name.strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", name).strip("_") or "unknown"


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
        ("Area code", row.area_code),
        ("Additional context", known),
    ):
        text = str(value).strip()
        if text:
            lines.append(f"{label}: {text}")
    payload: dict[str, Any] = {"handle": handle, "dossier": "\n".join(lines)}
    if guidance:
        payload["guidance"] = guidance
    return payload


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
    data = json.dumps(
        {
            "input": build_input(row, handle),
            "processor": processor,
            "task_spec": config.TASK_SPEC,
            "beta_header": beta_header,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def filter_already_done(
    rows: Iterable[ResearchQueueRow],
    projected_research: Iterable[ArtifactRow],
    *,
    processor: str = config.DEFAULT_PROCESSOR,
    beta_header: str = config.DEFAULT_BETA_HEADER,
) -> tuple[list[ResearchQueueRow], int]:
    """Reuse projected paid outputs; changed inputs overwrite the fixed path.

    The only resume evidence is a projected DB artifact row. Driver projects
    each accepted provider output before reading the next stream event, so an
    interrupted stream reuses every observed success. A submission whose HTTP
    response was lost remains ambiguous because Parallel exposes no request
    idempotency key and repo policy forbids a provider-run ledger.
    """
    completed = {
        artifact.artifact_key.removeprefix("research:").lower(): artifact.input_fingerprint
        for artifact in projected_research
    }
    todo: list[ResearchQueueRow] = []
    skipped = 0
    seen: set[str] = set()
    for source in rows:
        handle = candidate_handle(source)
        if handle in seen:
            continue
        seen.add(handle)
        row = replace(source, handle=handle)
        if handle.lower() in completed:
            stored = str(completed[handle.lower()] or "")
            # A projected artifact with no stored fingerprint (pre-fingerprinting
            # installs) is trusted as reused rather than treated as unverifiable —
            # the alternative is re-billing every such row once on upgrade.
            current = input_fingerprint(
                row, handle, processor=processor, beta_header=beta_header
            )
            legacy = legacy_parallel_input_fingerprint(build_input(row, handle))
            if not stored or stored in {current, legacy}:
                skipped += 1
                continue
        todo.append(row)
    return todo, skipped
