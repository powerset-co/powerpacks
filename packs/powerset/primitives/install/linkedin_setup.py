"""Read LinkedIn connections, import and index on Modal, then validate local search.

Changelog:
  2026-10-29: one fixed setup sequence; every invocation runs all six steps.
"""
from __future__ import annotations

import argparse
import os
import time
import traceback
from contextlib import chdir, closing
from pathlib import Path

from packs.indexing.modal import api_runs
from packs.indexing.primitives.validate_search_index.validate_search_index import validate as validate_search_index
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso, open_store, store_path
from packs.ingestion.primitives.deep_context_v2.import_load.load import CONNECTIONS_CSV as LINKEDIN_CSV, write_connections
from packs.ingestion.primitives.deep_context_v2.openai import load_env
from packs.ingestion.primitives.deep_context_v2.run import INDEX_MAX_USD, realize
from packs.ingestion.primitives.discover.linkedin.connections import CONNECTIONS_CSV, LinkedInConnections
from packs.powerset.primitives.install import report
from packs.powerset.primitives.install.status import InstallStatus
from packs.powerset.primitives.install.status_prose import PROSE
from packs.powerset.primitives.install.steps import InstallStep


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    report.install(root)
    data_root = root / ".powerpacks"
    index = data_root / "search-index"
    status = InstallStatus(root)
    step = InstallStep.LINKEDIN

    def write(event: str, *, pid: int = os.getpid(), **values) -> dict:
        nonlocal step
        step = PROSE[event].step or step
        return status.write(event, step=step, pid=pid, retry_command="bin/linkedin-setup", **values)

    def count(read: int, total: int | None) -> None:
        event = "linkedin.reading.count" if total is not None else "linkedin.reading.read"
        write(event, read=read, total=total, details={"read": read, "total": total})

    def watch(label: str, event: str) -> dict:
        previous = None
        while True:
            payload = api_runs.status(label) or {}
            # The run's end carries no code: return before writing, so the row never falls back
            # to the step's opening line.
            if payload.get("status") == "failed":
                raise RuntimeError(payload.get("error") or f"Modal run {label} failed")
            if payload.get("status") == "completed":
                return payload
            current = tuple(payload.get(key) for key in ("code", "done", "total", "left_seconds"))
            if current != previous:
                # Modal writes a code and numbers; the words are status_prose.py's ("<event>.<code>").
                details = {key: payload[key] for key in payload.keys() & {"done", "total", "left_seconds"}}
                write(f"{event}.{payload['code']}" if payload.get("code") else event,
                      **{key: payload[key] for key in payload.keys() & {"done", "total"}}, details=details)
                previous = current
            time.sleep(3)

    def download(label: str, name: str, dest: Path) -> None:
        if api_runs.download(label, name, dest) < 0:
            raise RuntimeError(f"{name} missing from Modal run {label}")

    with chdir(root):
        try:
            load_env()
            status.manifest_path.unlink(missing_ok=True)
            write("linkedin.reading", plan=["linkedin", "index", "validate", "ready"])
            result = LinkedInConnections(csv_path=root / CONNECTIONS_CSV).run(on_count=count)
            values = {key: result[key] for key in result.keys() & {"connections", "added", "read", "total"}}
            if result["status"] != "completed":
                event = {"needs_user_action": "linkedin.waiting", "failed": "linkedin.failed"}[result["status"]]
                write(event, pid=0, details=result, **values)
                return {"linkedin.waiting": 20, "linkedin.failed": 1}[event]
            write(f"linkedin.done.{result.get('outcome')}", details=result, **values)

            write("index.linkedin")
            uploaded = api_runs.upload(root / CONNECTIONS_CSV, "connections.csv")
            label = api_runs.start("import-linkedin", uploaded, source_user="linkedin")
            watch(label, "index.linkedin")
            download(label, "people.csv", data_root / LINKEDIN_CSV)

            write("index.preparing")
            with closing(open_store(store_path(data_root))) as conn:
                write_connections(conn, data_root / LINKEDIN_CSV, now_iso())
            people_csv = realize(data_root)

            write("index.building")
            uploaded = api_runs.upload(people_csv, "people.csv")
            label = api_runs.start("index-people", uploaded, max_usd=float(INDEX_MAX_USD))
            watch(label, "index.building")
            for name in ("local-search.duckdb", "manifest.json"):
                download(label, name, index / name)
            write("index.done")

            write("validate.checking")
            validation = validate_search_index(index / "local-search.duckdb", people_csv)
            if validation["status"] != "ok":
                raise RuntimeError(validation["summary"])
            write("validate.done", people=validation["total_people"])
            write("search.ready", pid=0, people=validation["total_people"], follow_ups="")
            return 0
        except (Exception, SystemExit) as error:
            status.directory.mkdir(parents=True, exist_ok=True)
            with status.log_path.open("a", encoding="utf-8") as log:
                traceback.print_exc(file=log)
            write("step.failed", pid=0, message=str(error), details={"error": str(error)})
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
