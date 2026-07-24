#!/usr/bin/env python3
"""Canonical filesystem locations for the ingestion pipeline.

Every stage writes to a single, fixed directory and overwrites in place — reruns
are idempotent because the output path is stable, not because of a run id. This
module is the ONE home for those default paths, previously copy-pasted across
discover/import primitives.

- `DEFAULT_BASE_DIR` — `.powerpacks/network-import`, the network-import root.
- `DEFAULT_DISCOVER_DIR` / `DEFAULT_IMPORT_DIR` — the discover and import roots
  under it (per-vertical subdirs hang off these).
- `DEFAULT_DIRECTORY_CSV` — the cross-source `directory.csv` aggregate.
- `DEFAULT_ACCOUNTS` — the packaged `accounts.json` linked-source state.
- `DEFAULT_PROFILE_CACHE_DIR` — the LinkedIn profile enrichment cache.
- `DEFAULT_MSGVAULT_DB` — the local msgvault SQLite db, honoring `$MSGVAULT_HOME`.
- `MESSAGES_OUT_DIR` — `.powerpacks/messages`, the iMessage/WhatsApp scratch dir.
- `source_import_dir(source)` — a source's import output dir under the import root.
- `gmail_discover_dir(base_dir, account_email)` — the fixed per-account Gmail
  discover output dir (`<base>/discover/gmail/<account-slug>`).

Changelog:
  2026-07-23 (audit consolidation): created; unifies the DEFAULT_BASE_DIR (x5),
    DEFAULT_IMPORT_DIR, DEFAULT_DIRECTORY_CSV, DEFAULT_ACCOUNTS (x2),
    DEFAULT_PROFILE_CACHE_DIR (x2), DEFAULT_MSGVAULT_DB (the
    $MSGVAULT_HOME-honoring variant is canonical), and MESSAGES_OUT_DIR (x3)
    copies, and adds `source_import_dir` for the four sites that re-derived
    `DEFAULT_IMPORT_DIR / <source>`.
  2026-07-23 (audit): absorbed `gmail_discover_dir` from
    discover/gmail/discover_engine.py so the discover CLI and the gmail
    discover orchestrator build the child output path from one shared builder.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.discover.common import source_slug  # noqa: E402

DEFAULT_BASE_DIR = Path(".powerpacks/network-import")
DEFAULT_DISCOVER_DIR = DEFAULT_BASE_DIR / "discover"
DEFAULT_IMPORT_DIR = DEFAULT_BASE_DIR / "import"
DEFAULT_DIRECTORY_CSV = DEFAULT_BASE_DIR / "directory.csv"
DEFAULT_ACCOUNTS = Path(".powerpacks/ingestion/accounts.json")
DEFAULT_PROFILE_CACHE_DIR = DEFAULT_BASE_DIR / "profile_cache_v2"
DEFAULT_MSGVAULT_DB = Path(os.environ.get("MSGVAULT_HOME", str(Path.home() / ".msgvault"))) / "msgvault.db"
MESSAGES_OUT_DIR = Path(".powerpacks/messages")


def source_import_dir(source: str) -> Path:
    """Return `<import root>/<source>`, a source's fixed import output directory."""
    return DEFAULT_IMPORT_DIR / source


def gmail_discover_dir(base_dir: Path, account_email: str = "") -> Path:
    """Return the fixed per-account Gmail discover output directory.

    `<base_dir>/discover/gmail/<slug>`, where the slug is `source_slug(account_email)`
    and an empty account collapses to `all`. The path is stable per account, so the
    discover CLI child overwrites in place and the orchestrator can rebuild the same
    path without reading it back from the child."""
    return base_dir / "discover" / "gmail" / source_slug(account_email or "all")
