#!/usr/bin/env python3
"""CLI for the share stage: write the share list.

Flow: run the ShareList node, emit its manifest.

Changelog:
  2026-09-24: deleted the `tag` command; a UI records tags and confirmations
    through TagStore.
  2026-09-24: created; `label` folded into the share node (JEV answers during
    deep_synthesize, so there is nothing to spend here).
"""

from __future__ import annotations

from packs.ingestion.primitives.common.gates import exit_code_for_status
from packs.ingestion.primitives.deep_context.common import emit
from packs.ingestion.primitives.share.share_list import ShareList


def main() -> int:
    payload = ShareList().run().to_payload()
    emit(payload)
    return exit_code_for_status(payload["status"])


if __name__ == "__main__":
    raise SystemExit(main())
