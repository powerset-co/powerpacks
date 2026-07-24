#!/usr/bin/env python3
"""Import discovered Gmail contacts (directory-only — the only mode).

Free and local: apply the shared identity directory to the discovered Gmail
queues, materialize `import/gmail/people.csv`, and write the still-unresolved
contacts to `import/gmail/candidates.csv` for the deep-context processing
layer, which owns ALL resolution and enrichment: stored legacy resolutions
migrate into overrides/review.csv via `bin/deep-context migrate-legacy` (the
central source of truth the fan-in and the review flow read); new lookups run
through deep-context's judged, budget-gated stages.

Thin CLI entry: `run` loads the gmail import steps module (file-loaded via
`load_gmail_import_steps` so it keeps its exact loader semantics), getattrs the
`GmailImport` orchestrator off it, and runs it. The orchestrator owns the fixed
import dir, the ledger run-state (still written to `ledger.json` this round),
the directory-apply -> stored-resolution-apply step chain, the matched-people /
candidates split, the directory quality gate, and the manifest. This module owns
only the CLI surface and `GMAIL_IMPORT_CONTRACT` (the package __init__ re-exports
it); exit 0 completed/skipped, 1 failed. No approval gate: nothing here spends.

Changelog:
  2026-07-23 (oop): the import flow (manifest no-op check, ledger construction,
    step dispatch, people/candidate materialization, quality gate, manifest) was
    folded into a `GmailImport` orchestrator in gmail/import_steps.py. run() is
    now a thin wrapper that getattrs GmailImport off the loaded steps module and
    passes GMAIL_IMPORT_CONTRACT in. CLI flags, exit codes, fixed output paths,
    ledger.json, and manifest payloads are unchanged.
  2026-07-23 (audit):
    - One upfront repo-root path bootstrap replaced the duplicated try/except
      import block.
    - Exit 20 / blocked_approval removed with the spend paths; nothing in this
      import can block on approval.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Repo-root bootstrap so packs.* imports work in module AND script mode
# (uv run .../importer.py); must be in-file because script-mode never imports
# the package __init__.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import emit  # noqa: E402
from packs.ingestion.primitives.common.paths import DEFAULT_ACCOUNTS  # noqa: E402
from packs.ingestion.primitives.imports.common import load_gmail_import_steps  # noqa: E402

GMAIL_IMPORT_CONTRACT = "gmail-directory-only-v2"


def run(args: argparse.Namespace) -> dict:
    """Load the gmail import steps module and run its `GmailImport` orchestrator."""
    steps_mod = load_gmail_import_steps()
    return steps_mod.GmailImport(args=args, contract=GMAIL_IMPORT_CONTRACT).run()


def build_parser() -> argparse.ArgumentParser:
    """CLI: one `run` command; `--force` bypasses the manifest no-op skip."""
    parser = argparse.ArgumentParser(description="Import discovered Gmail contacts (directory-only)")
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    parser.add_argument("--operator-id", default="local")
    parser.add_argument("--force", action="store_true", help="Re-run even if the import manifest is current (no no-op skip)")
    return parser


def main() -> int:
    """Exit 0 on success/skip, 1 on failure."""
    args = build_parser().parse_args()
    payload = run(args)
    emit(payload)
    return 1 if payload.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
