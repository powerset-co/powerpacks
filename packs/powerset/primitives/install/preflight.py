"""Check desktop prerequisites and install Chromium.

Changelog:
  2026-10-29: the Google Cloud CLI ships in the app (desktop tools.rs); nothing to download here.
  2026-10-27: Chromium goes into Powerpacks' own folder (oauth_browser.ensure_chromium).
  2026-10-09: Share browser discovery and install prerequisites without Homebrew.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from packs.ingestion.primitives.setup.automations.oauth_browser import ensure_chromium


BROWSER_SCRIPT = Path(__file__).resolve().parents[3] / "ingestion/primitives/common/browser.js"


def checks() -> dict:
    """Report the browser picked by automation."""
    try:
        result = subprocess.run(["node", str(BROWSER_SCRIPT), "--which"],
                                capture_output=True, text=True, check=True, timeout=10)
        browser = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        browser = None
    return {"browser": {"ok": browser is not None, "name": browser["name"] if browser else None}}


def install(item: str, on_progress: Callable[[str], None]) -> dict:
    """Install Chromium, streaming progress and returning ok or failed."""
    if item != "chromium":
        return {"status": "failed", "message": f"Unknown prerequisite: {item}"}
    browser = ensure_chromium(on_progress)
    if browser["status"] != "ok":
        return {"status": "failed", "message": browser["message"]}
    return {"status": "ok"}
