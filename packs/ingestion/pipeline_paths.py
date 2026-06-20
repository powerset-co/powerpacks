"""Canonical local Powerpacks pipeline file contracts.

All normal pipeline stages should read/write repo-local `.powerpacks` paths from
this module.  Keep ad-hoc file path CLI arguments for explicit debug/test use
only; agent-facing docs and setup flows should point here so stages do not
invent new artifact locations.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

POWERPACKS_DIR: Final = Path(".powerpacks")
INGESTION_DIR: Final = POWERPACKS_DIR / "ingestion"
NETWORK_IMPORT_DIR: Final = POWERPACKS_DIR / "network-import"
MESSAGES_DIR: Final = POWERPACKS_DIR / "messages"
SEARCH_INDEX_DIR: Final = POWERPACKS_DIR / "search-index"

ACCOUNTS_JSON: Final = INGESTION_DIR / "accounts.json"
ONBOARDING_LEDGER_JSON: Final = INGESTION_DIR / "onboarding-run.json"

MESSAGES_CONTACTS_CSV: Final = MESSAGES_DIR / "contacts.csv"

GMAIL_LEDGER_JSON: Final = NETWORK_IMPORT_DIR / "gmail" / "import-run.json"
GMAIL_SYNC_LEDGER_JSON: Final = NETWORK_IMPORT_DIR / "gmail" / "sync-run.json"
LINKEDIN_LEDGER_JSON: Final = NETWORK_IMPORT_DIR / "linkedin" / "import-run.json"
TWITTER_LEDGER_JSON: Final = NETWORK_IMPORT_DIR / "twitter" / "import-run.json"
ENRICHMENT_LEDGER_JSON: Final = NETWORK_IMPORT_DIR / "enrichment" / "import-run.json"
PROFILE_CACHE_DIR: Final = NETWORK_IMPORT_DIR / "profile_cache_v2"

MERGED_DIR: Final = NETWORK_IMPORT_DIR / "merged"
MERGED_PEOPLE_CSV: Final = MERGED_DIR / "people.csv"
MERGE_REVIEW_CSV: Final = MERGED_DIR / "review_pairs.csv"
MERGE_MANIFEST_JSON: Final = MERGED_DIR / "manifest.json"

ENRICHMENT_RUN_ID: Final = "current"
ENRICHMENT_RUN_DIR: Final = NETWORK_IMPORT_DIR / "enrichment" / ENRICHMENT_RUN_ID
ENRICHED_PEOPLE_CSV: Final = ENRICHMENT_RUN_DIR / "people_enriched.csv"

INDEX_RUN_ID: Final = "current"
INDEX_RUN_DIR: Final = SEARCH_INDEX_DIR / INDEX_RUN_ID
INDEX_LEDGER_JSON: Final = INDEX_RUN_DIR / "ledger.json"

# Index record outputs consumed by local search / parity checks.
INDEX_RECORDS_DIR: Final = INDEX_RUN_DIR / "records"
INDEX_PEOPLE_RECORDS_JSONL: Final = INDEX_RECORDS_DIR / "people.records.jsonl"
INDEX_COMPANIES_RECORDS_JSONL: Final = INDEX_RECORDS_DIR / "companies.records.jsonl"
INDEX_SCHOOLS_RECORDS_JSONL: Final = INDEX_RECORDS_DIR / "schools.records.jsonl"
INDEX_EDUCATION_RECORDS_JSONL: Final = INDEX_RECORDS_DIR / "education.records.jsonl"
INDEX_SUMMARIES_RECORDS_JSONL: Final = INDEX_RECORDS_DIR / "summaries.records.jsonl"

# The best canonical input for indexing.  It is produced by enrichment when that
# stage has run; otherwise the merge output is the deterministic fallback.
INDEX_INPUT_CANDIDATES: Final = (ENRICHED_PEOPLE_CSV, MERGED_PEOPLE_CSV)


@dataclass(frozen=True)
class PipelineStage:
    id: str
    command: str
    deps: tuple[Path, ...]
    outs: tuple[Path, ...]
    notes: str = ""


def canonical_index_input() -> Path:
    """Return the repo-local people CSV the indexer should consume."""

    for path in INDEX_INPUT_CANDIDATES:
        if path.exists():
            return path
    return MERGED_PEOPLE_CSV


PIPELINE_DAG: Final = (
    PipelineStage(
        id="setup/onboarding",
        command="uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py run",
        deps=(),
        outs=(ACCOUNTS_JSON, ONBOARDING_LEDGER_JSON),
        notes="Interactive account/source setup; records non-secret source state.",
    ),
    PipelineStage(
        id="messages/import",
        command="uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py run",
        deps=(),
        outs=(MESSAGES_CONTACTS_CSV,),
        notes="Optional local iMessage/WhatsApp contact metadata source.",
    ),
    PipelineStage(
        id="gmail/import",
        command="uv run --project . python packs/ingestion/primitives/gmail_network_import/gmail_network_import.py run",
        deps=(ACCOUNTS_JSON,),
        outs=(NETWORK_IMPORT_DIR / "gmail" / "*/people.csv", GMAIL_LEDGER_JSON),
        notes="Optional Gmail contact seed/import source.",
    ),
    PipelineStage(
        id="linkedin/import",
        command="uv run --project . python packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py run --csv <Connections.csv> --source-user <label>",
        deps=(ACCOUNTS_JSON,),
        outs=(NETWORK_IMPORT_DIR / "linkedin" / "*/people.csv", LINKEDIN_LEDGER_JSON),
        notes="Requires the external LinkedIn export path because it is user-supplied raw input.",
    ),
    PipelineStage(
        id="twitter/import",
        command="uv run --project . python packs/ingestion/primitives/twitter_network_import/twitter_network_import.py run --handle <handle>",
        deps=(ACCOUNTS_JSON,),
        outs=(NETWORK_IMPORT_DIR / "twitter" / "*/people.csv", TWITTER_LEDGER_JSON),
        notes="Optional Twitter/X source; external calls remain approval-gated.",
    ),
    PipelineStage(
        id="merge",
        command="uv run --project . python packs/ingestion/primitives/merge_network_sources/merge_network_sources.py run",
        deps=(NETWORK_IMPORT_DIR / "*" / "*" / "people.csv", MESSAGES_CONTACTS_CSV),
        outs=(MERGED_PEOPLE_CSV, MERGE_REVIEW_CSV, MERGE_MANIFEST_JSON),
        notes="Discovers canonical source artifacts under .powerpacks; no path args in normal runs.",
    ),
    PipelineStage(
        id="enrich",
        command="uv run --project . python packs/ingestion/primitives/enrich_people/enrich_people.py run",
        deps=(MERGED_PEOPLE_CSV,),
        outs=(ENRICHED_PEOPLE_CSV, ENRICHMENT_LEDGER_JSON),
        notes="Uses merge output by default; paid provider calls remain approval-gated.",
    ),
    PipelineStage(
        id="index",
        command="uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py run --force",
        deps=INDEX_INPUT_CANDIDATES,
        outs=(
            INDEX_LEDGER_JSON,
            INDEX_PEOPLE_RECORDS_JSONL,
            INDEX_COMPANIES_RECORDS_JSONL,
            INDEX_SCHOOLS_RECORDS_JSONL,
            INDEX_EDUCATION_RECORDS_JSONL,
            INDEX_SUMMARIES_RECORDS_JSONL,
        ),
        notes="Uses enriched people if present, else merged people; writes one canonical current run.",
    ),
)


def pipeline_dag_as_dict() -> list[dict[str, object]]:
    return [
        {
            "id": stage.id,
            "command": stage.command,
            "deps": [str(path) for path in stage.deps],
            "outs": [str(path) for path in stage.outs],
            "notes": stage.notes,
        }
        for stage in PIPELINE_DAG
    ]
