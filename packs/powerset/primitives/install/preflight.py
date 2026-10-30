"""Check desktop prerequisites; install Chromium, and wacli and the Google Cloud CLI at launch.

Changelog:
  2026-10-30: wacli and the Google Cloud CLI download at app launch (`python -m ... tools`, run by
      boot.rs after the browser), pinned; bundled in the app they failed Apple notarization (3.49.0).
  2026-10-29: the Google Cloud CLI ships in the app (desktop tools.rs); nothing to download here.
  2026-10-27: Chromium goes into Powerpacks' own folder (oauth_browser.ensure_chromium).
  2026-10-09: Share browser discovery and install prerequisites without Homebrew.
"""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable

from packs.ingestion.primitives.discover.messages.wacli.binary import ensure_wacli_installed
from packs.ingestion.primitives.discover.messages.wacli.runtime import PrimitiveBlocked
from packs.ingestion.primitives.setup.automations.oauth_browser import ensure_chromium


BROWSER_SCRIPT = Path(__file__).resolve().parents[3] / "ingestion/primitives/common/browser.js"
GCLOUD_VERSION = "591.0.0"
GCLOUD_SDK = Path.home() / ".powerpacks/google-cloud-sdk"


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


def install_gcloud(on_progress: Callable[[str], None]) -> None:
    """The Google Cloud CLI at GCLOUD_VERSION in ~/.powerpacks/google-cloud-sdk, replacing another."""
    stamp = GCLOUD_SDK / "VERSION"
    if stamp.is_file() and stamp.read_text().strip() == GCLOUD_VERSION:
        return
    arch = "arm" if platform.machine() == "arm64" else "x86_64"
    url = ("https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/"
           f"google-cloud-cli-{GCLOUD_VERSION}-darwin-{arch}.tar.gz")
    on_progress(f"Downloading the Google Cloud CLI {GCLOUD_VERSION}, once.")
    with tempfile.TemporaryDirectory(prefix="powerpacks-gcloud-") as temporary:
        archive = Path(temporary) / "gcloud.tar.gz"
        urllib.request.urlretrieve(url, archive)
        if GCLOUD_SDK.exists():
            shutil.rmtree(GCLOUD_SDK)
        GCLOUD_SDK.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as bundle:
            bundle.extractall(GCLOUD_SDK.parent, filter="data")


def install_tools(on_progress: Callable[[str], None]) -> dict:
    """wacli and the Google Cloud CLI at their pinned versions, each downloaded once. The app runs
    this at launch, after the browser, so Gmail and WhatsApp never wait for a download."""
    try:
        ensure_wacli_installed()
        install_gcloud(on_progress)
    except PrimitiveBlocked as blocked:
        return {"status": "failed", "message": blocked.payload.get("message", "wacli install failed")}
    except (OSError, tarfile.TarError) as error:
        return {"status": "failed", "message": str(error)}
    return {"status": "ok"}


if __name__ == "__main__":
    if sys.argv[1:] != ["tools"]:
        raise SystemExit("usage: python -m packs.powerset.primitives.install.preflight tools")
    result = install_tools(lambda line: print(line, flush=True))
    if result["status"] != "ok":
        print(result["message"], flush=True)
        raise SystemExit(1)
