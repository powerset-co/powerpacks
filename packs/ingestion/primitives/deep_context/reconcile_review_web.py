#!/usr/bin/env python3
"""Executable facade for the staged deep-context review web UI.

The implementation lives in ``deep_context.review_web``. This facade preserves
both direct file-path execution and ``python -m`` invocation while keeping
production consumers on concrete owner modules.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.deep_context.review_web import REVIEW_CSS, REVIEW_HTML, REVIEW_JS
from packs.ingestion.primitives.deep_context.review_web.cli import build_parser, cmd_status, main
from packs.ingestion.primitives.deep_context.review_web.server import cmd_serve

__all__ = [
    "REVIEW_CSS",
    "REVIEW_HTML",
    "REVIEW_JS",
    "build_parser",
    "cmd_serve",
    "cmd_status",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
