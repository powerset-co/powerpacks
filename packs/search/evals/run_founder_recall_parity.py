#!/usr/bin/env python3
"""Founder-only data selection entry for the canonical recall evaluator."""

import sys
from pathlib import Path

if __package__ in {None, ""}:
    repository = Path(__file__).resolve().parents[3]
    if str(repository) not in sys.path:
        sys.path.insert(0, str(repository))

from packs.search.evals.run_recall_parity import main


if __name__ == "__main__":
    main(default_bucket="founders", output_name="founder-recall-parity")
