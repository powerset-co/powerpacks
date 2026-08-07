"""Pinned Parallel task specification, processors, pricing, and runtime defaults."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.prompts.loader import load_prompt

DEFAULT_BASE_URL = os.environ.get(
    "POWERPACKS_PARALLEL_BASE_URL", "https://api.parallel.ai"
)
DEFAULT_BETA_HEADER = os.environ.get(
    "POWERPACKS_PARALLEL_BETA", "search-extract-2025-10-10"
)
DEFAULT_PROCESSOR = os.environ.get("POWERPACKS_PARALLEL_PROCESSOR", "core2x")
ALLOWED_PROCESSORS = frozenset({"core", "core2x", "pro"})
PROCESSOR_PRICING_USD = {"core": 0.025, "core2x": 0.05, "pro": 0.10}
PROCESSOR_LATENCY = {
    "core": ("60s-5min", "about 1-5 min once submitted"),
    "core2x": ("60s-10min", "about 10-15 min once submitted"),
    "pro": ("2-10min", "about 2-10 min once submitted"),
}
DEFAULT_OUTPUT_DIR = Path(".powerpacks/messages/research")
DEFAULT_BATCH_SIZE = 500
DEFAULT_POLL_INTERVAL = 15
DEFAULT_MAX_WAIT = 7200
DEFAULT_RESULT_WORKERS = 4

RESEARCH_INSTRUCTIONS = load_prompt("contact_research_instructions")
_SCHEMAS = json.loads(load_prompt("contact_research_schema"))
PERSON_RESEARCH_INPUT_SCHEMA: dict[str, Any] = _SCHEMAS["input"]
PERSON_RESEARCH_OUTPUT_SCHEMA: dict[str, Any] = _SCHEMAS["output"]
TASK_SPEC = {
    "instructions": RESEARCH_INSTRUCTIONS,
    "input_schema": {"json_schema": PERSON_RESEARCH_INPUT_SCHEMA},
    "output_schema": {"json_schema": PERSON_RESEARCH_OUTPUT_SCHEMA},
}


def validate_processor(processor: str) -> str:
    if processor not in ALLOWED_PROCESSORS:
        raise SystemExit(
            f"processor '{processor}' is blocked for Powerpacks contact research; "
            f"allowed processors: {', '.join(sorted(ALLOWED_PROCESSORS))}"
        )
    return processor
