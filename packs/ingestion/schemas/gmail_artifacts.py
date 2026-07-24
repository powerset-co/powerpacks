"""Shared Gmail-artifact column contracts for the ingestion stages.

Single home for the Gmail LinkedIn-resolution CSV column orders that both the
discover stage (`discover/gmail/extract_gmail.py`) and the import stage
(`imports/directory.py`, `imports/gmail/import_steps.py`) must agree on. Kept
here — next to `people_schema.py` / `candidates_schema.py` — so the stages
import ONE definition instead of each carrying a byte-identical copy that can
silently drift.

- `LINKEDIN_RESOLUTION_QUEUE_COLUMNS`: the `linkedin_resolution_queue.csv`
  Gmail discovery emits (and `deep_context/build_email_context.py` re-derives)
  — the candidate contacts handed to LinkedIn resolution.
- `LINKEDIN_RESOLUTION_COLUMNS`: the resolution *results* contract
  (`handle,status,linkedin_url,confidence,...`) the Gmail apply path, the
  directory commit, and the resolution-merge all read and write.
- `LINKEDIN_RESOLUTIONS_APPLIED_COLUMNS`: the `linkedin_resolutions_applied.csv`
  audit sidecar the Gmail apply path writes (one row per person a stored
  resolution was attached to).

Changelog:
  2026-07-23 (audit): extracted from discover_engine.py, imports/directory.py,
    and imports/gmail/import_steps.py — LINKEDIN_RESOLUTION_COLUMNS was
    byte-identical in all three, LINKEDIN_RESOLUTION_QUEUE_COLUMNS in
    discover_engine.py with a downstream consumer. Values are unchanged.
  2026-07-23 (audit): added LINKEDIN_RESOLUTIONS_APPLIED_COLUMNS, lifted from the
    inline header list in discover_engine.apply_linkedin_resolutions_to_people.
"""
from __future__ import annotations

LINKEDIN_RESOLUTION_QUEUE_COLUMNS = [
    "handle",
    "id",
    "account_emails",
    "source_ids",
    "display_name",
    "full_name",
    "primary_email",
    "company_guess",
    "primary_email_type",
    "total_messages",
    "thread_count",
    "last_interaction",
    "source",
    "source_channels",
]
LINKEDIN_RESOLUTION_COLUMNS = ["handle", "status", "linkedin_url", "confidence", "matched_name", "matched_headline", "evidence", "reasoning"]
LINKEDIN_RESOLUTIONS_APPLIED_COLUMNS = ["primary_email", "linkedin_url", "public_identifier", "confidence", "matched_name"]
