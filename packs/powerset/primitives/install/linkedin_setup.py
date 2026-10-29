"""Read LinkedIn connections, import and index on Modal, then validate local search.

Changelog:
  2026-10-29: one fixed setup sequence; every invocation runs all six steps.
"""
from __future__ import annotations

import argparse
import os
import time
from contextlib import chdir, closing
from pathlib import Path

from packs.indexing.modal import api_runs
from packs.indexing.primitives.validate_search_index.validate_search_index import validate as validate_search_index
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso, open_store, store_path
from packs.ingestion.primitives.deep_context_v2.import_load.load import CONNECTIONS_CSV as LINKEDIN_CSV, write_connections
from packs.ingestion.primitives.deep_context_v2.openai import load_env
from packs.ingestion.primitives.deep_context_v2.run import realize
from packs.ingestion.primitives.discover.linkedin.connections import CONNECTIONS_CSV, LinkedInConnections
from packs.ingestion.primitives.discover.messages.wacli.runtime import PrimitiveBlocked
from packs.powerset.primitives.install.status import InstallStatus
from packs.powerset.primitives.install.status_prose import PROSE
from packs.powerset.primitives.install.steps import InstallStep


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve()
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
            current = tuple(payload.get(key) for key in ("message", "done", "total", "left_seconds"))
            if current != previous:
                details = {key: payload[key] for key in payload.keys() & {"done", "total", "left_seconds"}}
                write(event, message=payload.get("message"), details=details)
                previous = current
            if payload.get("status") == "failed":
                raise RuntimeError(payload.get("error") or f"Modal run {label} failed")
            if payload.get("status") == "completed":
                return payload
            time.sleep(3)

    def download(label: str, name: str, dest: Path) -> None:
        if api_runs.download(label, name, dest) < 0:
            raise RuntimeError(f"{name} missing from Modal run {label}")

    with chdir(root):
        try:
            load_env()
            write("linkedin.reading", plan=["linkedin", "index", "validate", "ready"])
            try:
                result = LinkedInConnections(csv_path=root / CONNECTIONS_CSV).run(on_count=count)
            except PrimitiveBlocked as error:
                write("linkedin.waiting", pid=0, details=error.payload)
                return 20
            values = {key: result[key] for key in result.keys() & {"connections", "added", "read", "total"}}
            if result["status"] not in {"ok", "completed", "linked", "skipped"}:
                event = dict.fromkeys(("needs_user_action", "blocked_user_action", "needs_approval"),
                                      "linkedin.waiting").get(result["status"], "linkedin.failed")
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
            label = api_runs.start("index-people", uploaded, max_usd=0.0)
            watch(label, "index.building")
            for name in ("local-search.duckdb", "manifest.json"):
                download(label, name, index / name)
            write("index.done")

            write("validate.checking")
            validation = validate_search_index(index / "local-search.duckdb", people_csv)
            if validation["status"] != "ok":
                raise RuntimeError(validation["summary"])
            write("validate.done", people=validation["total_people"])
            write("search.ready", pid=0, people=validation["total_people"])
            return 0
        except (Exception, SystemExit) as error:
            write("step.failed", pid=0, message=str(error), details={"error": str(error)})
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
