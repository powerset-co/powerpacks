#!/usr/bin/env python3
"""Root Powerpacks wrapper for the shared Powerset Auth0 PKCE primitive."""

from __future__ import annotations

import runpy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "packs" / "messages" / "primitives" / "powerset_auth" / "powerset_auth.py"

if not TARGET.exists():
    raise SystemExit(f"missing bundled powerset_auth primitive: {TARGET}")

runpy.run_path(str(TARGET), run_name="__main__")
