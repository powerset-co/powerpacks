"""Load the pond prompts selected by the approved recruiter plan."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[4]
PROMPT_ROOT = ROOT / "packs/search/prompts"
POND_PROMPT_FAMILIES = {
    "general",
    "engineering",
    "marketing-sales",
    "customer-support",
    "operations-finance-people",
    "design",
}


def load_pond_prompt(plan: Mapping[str, Any], stage: str) -> str:
    family = str(plan.get("pond_prompt_family") or "general")
    if family not in POND_PROMPT_FAMILIES:
        raise ValueError(f"unknown pond prompt family: {family}")
    path = (PROMPT_ROOT / f"{stage}.txt" if family == "general" else
            PROMPT_ROOT / "families" / family / f"{stage}.txt")
    return path.read_text(encoding="utf-8").rstrip()
