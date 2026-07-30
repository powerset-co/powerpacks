"""Pairing-time identity and the full-sync marker: was THIS link made by our flow?

Two things WhatsApp only records once, at pairing:

- the device identity `wacli_device_env` puts in the environment (platform,
  label, full-sync window) — changing it later has no effect, you have to
  re-pair;
- the fact that our flow sent `RequireFullSync`. There is no reliable way to
  read that back out of wacli's `session.db`, so the flow stamps
  `.powerpacks-pairing.json` into the store on the not-authenticated ->
  authenticated transition and `pairing_full_sync_status` reads it back. A
  linked session with no marker was paired the old way (upstream wacli or a
  pre-full-sync build) and would pull years more history if re-linked, which is
  what the `$import-messages` re-link prompt offers.

Changelog:
  2026-07-30 (wacli split): extracted from the single-file `whatsapp_wacli.py`;
    the marker read is now a typed `PairingMarker` parse (`payloads.py`) instead
    of a raw dict, and `read_pairing_marker` returns that record (still `None`
    for a missing or corrupt file). Emitted values unchanged.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import now_iso, write_json  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli import binary  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.payloads import PairingMarker  # noqa: E402

# Written into the store when OUR flow pairs (which always sends RequireFullSync).
# A linked session missing this marker was paired the old way — upstream wacli or
# a pre-full-sync build — and would pull years more history if re-linked. There's
# no reliable way to read "was RequireFullSync sent?" back out of session.db, so
# we stamp it at pair time instead.
PAIRING_MARKER_NAME = ".powerpacks-pairing.json"
DEFAULT_DEVICE_PLATFORM = os.environ.get("POWERPACKS_WACLI_DEVICE_PLATFORM", "DESKTOP")
DEFAULT_DEVICE_LABEL = os.environ.get("POWERPACKS_WACLI_DEVICE_LABEL", "Mac OS")
DEFAULT_FULL_SYNC_DAYS = os.environ.get("POWERPACKS_WACLI_FULL_SYNC_DAYS", "3650")


def wacli_device_env() -> dict[str, str]:
    """Device identity WhatsApp records at PAIRING time only (re-pair to change it).

    Only PlatformType DESKTOP makes WhatsApp's Linked Devices list render the OS
    label we set (WACLI_DEVICE_LABEL). whatsmeow's other platform enum names are
    reverse-engineered guesses whose *numbers* WhatsApp maps to its own fixed
    device names, ignoring the label (e.g. CATALINA/12 currently shows as
    "Portal TV", not macOS). See tulir/whatsmeow discussion #469. DESKTOP + a
    "Mac OS" label registers as a desktop and displays as macOS. Pre-set
    WACLI_DEVICE_* values in the environment win.
    """
    env = dict(os.environ)
    env.setdefault("WACLI_DEVICE_PLATFORM", DEFAULT_DEVICE_PLATFORM)
    env.setdefault("WACLI_DEVICE_LABEL", DEFAULT_DEVICE_LABEL)
    env.setdefault("WACLI_DEVICE_FULL_SYNC_DAYS", DEFAULT_FULL_SYNC_DAYS)
    return env


def pairing_marker_path(store: Path) -> Path:
    return store / PAIRING_MARKER_NAME


def write_pairing_marker(store: Path) -> None:
    """Record that this session was paired by our full-sync flow. Call on the
    not-authenticated -> authenticated transition (i.e. when WE just paired)."""
    write_json(pairing_marker_path(store), {
        "full_sync": True,
        "full_sync_days": DEFAULT_FULL_SYNC_DAYS,
        "wacli_version": binary.WACLI_PINNED_VERSION,
        "device_platform": os.environ.get("WACLI_DEVICE_PLATFORM", DEFAULT_DEVICE_PLATFORM),
        "paired_at": now_iso(),
    })


def read_pairing_marker(store: Path) -> PairingMarker | None:
    try:
        data = json.loads(pairing_marker_path(store).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return PairingMarker.from_payload(data)


def pairing_full_sync_status(store: Path, *, authenticated: bool) -> dict[str, Any]:
    """Whether the current WhatsApp link was set up with full history sync. A
    linked session with no full-sync marker predates our full-sync flow (upstream
    wacli or an old build), so re-linking would pull years more history."""
    if not authenticated:
        return {"state": "not_authenticated", "can_deepen": False}
    marker = read_pairing_marker(store)
    if marker and marker.full_sync:
        return {
            "state": "full_sync",
            "can_deepen": False,
            "paired_wacli_version": marker.wacli_version,
            "paired_at": marker.paired_at,
        }
    return {
        "state": "pre_full_sync",
        "can_deepen": True,
        "hint": (
            "This WhatsApp link was set up before full history sync. Re-link "
            "(log out and re-scan the QR) to pull years more history."
        ),
    }
