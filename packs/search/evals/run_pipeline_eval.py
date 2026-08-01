#!/usr/bin/env python3
"""Typed pipeline evaluator over deterministic structured recall cases."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

if __package__ in {None, ""}:
    repository = Path(__file__).resolve().parents[3]
    if str(repository) not in sys.path:
        sys.path.insert(0, str(repository))

from packs.search.evals.run_recall_parity import main as recall_main
from packs.search.evals.search_spec_factory import case_id, load_case, select_cases

__all__ = ["case_id", "load_case", "select_cases"]


def main(argv: Sequence[str] | None = None) -> None:
    recall_main(argv, output_name="pipeline-eval")


if __name__ == "__main__":
    main()
