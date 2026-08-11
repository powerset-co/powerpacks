"""Pinned Parallel task specification, processors, pricing, and runtime defaults."""

from __future__ import annotations

import json
import os
from typing import Any

from packs.ingestion.primitives.deep_context.prompts.loader import load_prompt

DEFAULT_BASE_URL = os.environ.get(
    "POWERPACKS_PARALLEL_BASE_URL", "https://api.parallel.ai"
)
DEFAULT_BETA_HEADER = os.environ.get(
    "POWERPACKS_PARALLEL_BETA", "search-extract-2025-10-10"
)
DEFAULT_PROCESSOR = os.environ.get("POWERPACKS_PARALLEL_PROCESSOR", "core2x")
# USD per completed person. This table is consulted by the caller
# (research_reconcile.selection) to build the pre-submit --approve-spend estimate;
# nothing in this package enforces it against the actual bill.
PROCESSOR_PRICING_USD = {"core": 0.025, "core2x": 0.05, "pro": 0.10}
ALLOWED_PROCESSORS = frozenset(PROCESSOR_PRICING_USD)
# Cap per task_group.add_runs() call, not a spend cap — a 500-row batch is still
# 500 billed runs, submitted in one HTTP call.
DEFAULT_BATCH_SIZE = 500
# Poll every 15s for up to 2h; a group still active at the deadline is not an
# error here — parallel_client.ParallelClient.execute falls through to fetch
# results anyway once time runs out.
DEFAULT_POLL_INTERVAL = 15
DEFAULT_MAX_WAIT = 7200

RESEARCH_INSTRUCTIONS = load_prompt("contact_research_instructions")
_SCHEMAS = json.loads(load_prompt("contact_research_schema"))
PERSON_RESEARCH_INPUT_SCHEMA: dict[str, Any] = _SCHEMAS["input"]
PERSON_RESEARCH_OUTPUT_SCHEMA: dict[str, Any] = _SCHEMAS["output"]
# Identical for every submitted run; only ParallelRunInput.input (the per-person
# dossier) varies. Changing instructions/schemas here changes what every future
# run costs to interpret but does not itself cost anything — submission does.
TASK_SPEC = {
    "instructions": RESEARCH_INSTRUCTIONS,
    "input_schema": {"json_schema": PERSON_RESEARCH_INPUT_SCHEMA},
    "output_schema": {"json_schema": PERSON_RESEARCH_OUTPUT_SCHEMA},
}


def validate_processor(processor: str) -> str:
    """Refuse an unsupported processor string before it reaches the paid API.

    Not a spend gate — --approve-spend/budget are enforced upstream in
    research_reconcile; this only stops a typo'd processor from billing at an
    unexpected rate.
    """
    if processor not in ALLOWED_PROCESSORS:
        raise SystemExit(
            f"processor '{processor}' is blocked for Powerpacks contact research; "
            f"allowed processors: {', '.join(sorted(ALLOWED_PROCESSORS))}"
        )
    return processor
