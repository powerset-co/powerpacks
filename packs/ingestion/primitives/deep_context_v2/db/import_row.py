"""One import row: a people.csv row as the Gmail and messages importers write it, in the columns v2 reads.

The row is kept whole as evidence in `candidates.import_json`, as the CSV cells were written. This model
is its one parse: load reads each CSV row through it, and a reader of `import_json` reads the same
cells through it again. A row the importers did not write this way fails here, naming the column.

Created: 2026-10-07
"""
from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, field_validator

from packs.ingestion.primitives.deep_context_v2.db.schema import SourceChannel


class ImportRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    full_name: str                               # blank when the importer had no name for the address
    primary_email: str                           # blank on a phone row
    primary_phone: str                           # blank on an email row
    source_channels: tuple[SourceChannel, ...]   # "imessage,whatsapp" when one phone was seen in both apps
    interaction_counts: dict[str, int]           # channel -> messages; the cell is blank when none were counted
    last_interaction: str                        # ISO-8601 UTC

    @field_validator("source_channels", mode="before")
    @classmethod
    def _split_channels(cls, value: str) -> list[str]:
        channels: list[str] = []
        for part in value.split(","):
            channels.append(part.strip())
        return channels

    @field_validator("interaction_counts", mode="before")
    @classmethod
    def _parse_counts(cls, value: str) -> object:
        if not value:
            return {}
        return json.loads(value)
