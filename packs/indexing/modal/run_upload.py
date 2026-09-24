#!/usr/bin/env python3
"""In-sandbox entry for the Powerset upload: plan -> (optional) write -> status.

Runs as the Modal sandbox entrypoint so the upload completes server-side even if
the dispatching laptop disconnects. It constructs the same UploadPowerset class
the laptop CLI does, against the volume's runs/<label>/local-search.duckdb plus
the operator's uploaded input/share.csv and input/people.csv, and leaves the run
outcome at <run-vol>/status.json with the stage manifest beside it.

Credentials come from the workspace secrets (powerset-turbopuffer,
powerset-postgres); the operator id is passed in, because the sandbox has no
Powerpacks credentials file to derive it from.

Changelog:
  2026-09-24: created.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO = Path("/repo")
sys.path.insert(0, str(REPO))

from packs.indexing.modal.sandbox_common import now_iso, write_status  # noqa: E402
from packs.indexing.primitives.upload_powerset.upload_powerset import UploadPowerset  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--share-csv", required=True)
    ap.add_argument("--people-csv", required=True)
    ap.add_argument("--run-vol", required=True)
    ap.add_argument("--operator-id", required=True)
    ap.add_argument("--apply", action="store_true", help="write; without it the run only plans")
    args = ap.parse_args()

    run_vol = Path(args.run_vol)
    work = Path("/tmp/run/upload-powerset")
    status = {"status": "running", "phase": "plan", "started_at": now_iso()}
    write_status(run_vol, status)

    payload = UploadPowerset(
        db=Path(args.db),
        share_csv=Path(args.share_csv),
        people_csv=Path(args.people_csv),
        out_dir=work,
        operator_id=args.operator_id,
        dry_run=not args.apply,
    ).run()

    run_vol.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(work / "manifest.json", run_vol / "manifest.json")
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
    write_status(run_vol, status | {
        "status": "completed",
        "phase": "done",
        "dry_run": payload["dry_run"],
        "plan": payload["plan"],
        "result": payload["result"],
        "finished_at": now_iso(),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
