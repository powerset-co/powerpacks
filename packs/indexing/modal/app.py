"""The index runs as a deployed Modal app, started by the Powerset API (network-search-api,
api_v2/routes/powerpacks_modal.py) so the laptop holds no Modal token for them.

Two functions, each running the same entrypoint the laptop's sandboxes run today, in its own
container over the team volume:

  import_linkedin(operator_id, source_user)  connections.csv -> enriched people.csv (run_linkedin.py)
  index_people(operator_id, max_usd)         people.csv -> local-search.duckdb + manifest (run_indexing.py)

Volume layout is unchanged: /data/operators/<operator-id>/input/{connections,people}.csv and
/data/operators/<operator-id>/runs/<label>/status.json, which the API reads back for the laptop.
The volume is committed every few seconds while the entrypoint runs, so each phase is visible.

Deploy: `uv run modal deploy packs/indexing/modal/app.py` (the deployed code is what runs, not the
laptop's checkout).

Created: 2026-10-26
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import modal

from packs.indexing.modal.linkedin_modal_pipeline import APP_NAME, CACHE_ROOT, VOLUME_NAME, build_image, powerset_api_secret
from packs.indexing.modal.sandbox_common import write_status

IMPORT_LABEL = "linkedin-import"
INDEX_LABEL = "index"
TIMEOUT = 7200

app = modal.App(APP_NAME)
volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
image = build_image()


def _run(label: str, operator_id: str, command: list[str]) -> None:
    """The entrypoint as a subprocess, the volume committed every few seconds so the API can follow
    status.json; a non-zero exit with no final status is recorded as failed."""
    run_vol = Path(f"/data/operators/{operator_id}/runs/{label}")
    write_status(run_vol, {"status": "running", "phase": "queued"})
    volume.commit()
    process = subprocess.Popen(command, env={**os.environ, "PYTHONPATH": "/repo"})
    while process.poll() is None:
        time.sleep(5)
        volume.commit()
    if process.returncode != 0 and not (run_vol / "status.json").is_file():
        write_status(run_vol, {"status": "failed", "phase": "start", "exit_code": process.returncode})
    volume.commit()


@app.function(image=image, volumes={"/data": volume}, secrets=[powerset_api_secret()], cpu=2, memory=4096, timeout=TIMEOUT)
def import_linkedin(operator_id: str, source_user: str = "linkedin") -> None:
    root = f"/data/operators/{operator_id}"
    _run(IMPORT_LABEL, operator_id, [
        "python", "/repo/packs/indexing/modal/run_linkedin.py",
        "--connections-csv", f"{root}/input/connections.csv",
        "--people-out", f"{root}/input/people.csv",
        "--cache-root", CACHE_ROOT,
        "--run-vol", f"{root}/runs/{IMPORT_LABEL}",
        "--operator-id", operator_id,
        "--source-user", source_user,
    ])


@app.function(image=image, volumes={"/data": volume},
              secrets=[modal.Secret.from_name("powerset-openai"), powerset_api_secret()],
              cpu=16, memory=16384, timeout=TIMEOUT)
def index_people(operator_id: str, max_usd: float) -> None:
    root = f"/data/operators/{operator_id}"
    _run(INDEX_LABEL, operator_id, [
        "python", "/repo/packs/indexing/modal/run_indexing.py",
        "--people-csv", f"{root}/input/people.csv",
        "--cache-root", CACHE_ROOT,
        "--run-vol", f"{root}/runs/{INDEX_LABEL}",
        "--operator-id", operator_id,
        "--compute-threads", "16",
        "--enrich", "--max-usd", str(max_usd),
    ])
