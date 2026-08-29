"""Typed display receipt for the message-collection stage."""
from __future__ import annotations

from typing import Any

from pydantic import Field

from packs.ingestion.primitives.pipeline.contract import StageManifest


class CollectPersonContextManifest(StageManifest):
    source: str = "collect_person_context"
    privacy_schema_version: int = 2
    dry_run: bool = False
    people_total: int = 0
    people_with_context: int = 0
    people_skipped_existing: int = 0
    total_messages_sampled: int = 0
    people_capped: int = 0
    channel_message_counts: dict[str, int] = Field(default_factory=dict)
    contacts_per_sec: float = 0.0
    messages_per_sec: float = 0.0
    ms_per_contact: float | int = 0
    deep_cap_per_person: int = 0
    groups_included: bool = False
    max_group_size: int = 0
    bundles_purged_for_scope: int = 0
    orphan_bundles_removed: int = 0
    msgvault_available: bool = False
    chat_db_available: bool = False
    chat_db_probe: dict[str, Any] = Field(default_factory=dict)
    wacli_available: bool = False
    out_dir: str = ""
    elapsed_ms: int = 0
    updated_at: str = ""
    privacy: dict[str, Any] = Field(default_factory=dict)
