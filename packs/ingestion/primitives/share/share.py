#!/usr/bin/env python3
"""CLI for the share stage: write the label export and the share list.

Flow: open the canonical store, run the ShareList node, emit its manifest.

Changelog:
  2026-09-24: the node writes SQLite tables, so the CLI opens the store.
  2026-09-24: deleted the `tag` command; the UI records tags through TagStore.
  2026-09-24: created; `label` folded into the share node.
"""

from __future__ import annotations

from packs.ingestion.primitives.common.gates import exit_code_for_status
from packs.ingestion.primitives.deep_context.db.store import open_existing_db
from packs.ingestion.primitives.deep_context.shared.common import CANONICAL_DB, emit
from packs.ingestion.primitives.share.share_list import ShareList


def main() -> int:
    payload = ShareList(db=open_existing_db(CANONICAL_DB)).run().to_payload()
    emit(payload)
    return exit_code_for_status(payload["status"])


if __name__ == "__main__":
    raise SystemExit(main())
