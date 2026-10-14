"""Check desktop prerequisites and install Chromium or a user-local Google Cloud CLI.

Changelog:
  2026-10-09: Share browser discovery and install prerequisites without Homebrew.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable

from packs.ingestion.primitives.setup.automations.oauth_browser import VENDORED_NODE_MODULES


BROWSER_SCRIPT = Path(__file__).resolve().parents[3] / "ingestion/primitives/common/browser.js"
GCLOUD_SDK = Path.home() / ".powerpacks/google-cloud-sdk"


def checks() -> dict:
    """Report the browser picked by automation and whether gcloud is installed."""
    try:
        result = subprocess.run(["node", str(BROWSER_SCRIPT), "--which"],
                                capture_output=True, text=True, check=True, timeout=10)
        browser = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        browser = None
    return {"browser": {"ok": browser is not None, "name": browser["name"] if browser else None},
            "gcloud": {"ok": bool(shutil.which("gcloud") or (GCLOUD_SDK / "bin/gcloud").is_file())}}


def _stream(command: list[str], on_progress: Callable[[str], None]) -> None:
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          env={**os.environ, "CLOUDSDK_PYTHON": sys.executable}) as process:
        assert process.stdout is not None
        last_line = ""
        for line in process.stdout:
            last_line = line.rstrip()
            on_progress(last_line)
        if process.wait() != 0:
            raise RuntimeError(last_line or f"{command[0]} failed")


def install(item: str, on_progress: Callable[[str], None]) -> dict:
    """Install chromium or gcloud, streaming progress and returning ok or failed."""
    try:
        if item == "chromium":
            _stream(["node", str(VENDORED_NODE_MODULES / "playwright-core/cli.js"), "install", "chromium"], on_progress)
        elif item == "gcloud":
            arch = "arm" if platform.machine() == "arm64" else "x86_64"
            url = f"https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-darwin-{arch}.tar.gz"
            GCLOUD_SDK.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="powerpacks-gcloud-") as temporary:
                archive = Path(temporary) / "gcloud.tar.gz"
                on_progress("Downloading Google Cloud CLI.")
                urllib.request.urlretrieve(url, archive)
                on_progress("Extracting Google Cloud CLI.")
                with tarfile.open(archive) as bundle:
                    bundle.extractall(GCLOUD_SDK.parent, filter="data")
            # The app supplies Python and PATH; install.sh only adds optional setup.
            _stream([str(GCLOUD_SDK / "bin/gcloud"), "--version"], on_progress)
        else:
            return {"status": "failed", "message": f"Unknown prerequisite: {item}"}
    except (OSError, tarfile.TarError, RuntimeError) as exc:
        return {"status": "failed", "message": str(exc)}
    return {"status": "ok"}
