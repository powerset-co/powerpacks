"""Fixed on-disk locations for every wacli artifact.

All of them hang off `.powerpacks/messages/` (`MESSAGES_OUT_DIR`): the wacli
store directory itself, the login-QR page/PNG, the group-participants cache, and
the history-depth stage directory. Paths are fixed and overwritten in place —
there are no run ids and no per-run subdirectories.

Changelog:
  2026-07-30 (wacli split): extracted from the single-file `whatsapp_wacli.py`
    so the store/QR/depth modules share one definition instead of each holding
    its own copy. Values unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.paths import MESSAGES_OUT_DIR  # noqa: E402

DEFAULT_OUT_DIR = MESSAGES_OUT_DIR
DEFAULT_STORE = DEFAULT_OUT_DIR / "wacli"
DEFAULT_HISTORY_DEPTH_DIR = DEFAULT_OUT_DIR / "history-depth"
DEFAULT_QR_PNG = DEFAULT_OUT_DIR / "wacli-login-qr.png"
DEFAULT_QR_HTML = DEFAULT_OUT_DIR / "wacli-login-qr.html"
DEFAULT_GROUP_PARTICIPANTS_CACHE = DEFAULT_OUT_DIR / "wacli.group-participants.json"
