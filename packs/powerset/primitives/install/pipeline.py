"""Resume imported contacts through deep-context v2, the Modal index, and validation.

The stages and their order are deep_context_v2/run.py's (`stages`); this walks them under the
status page's events. Every paid stage estimates (into the install log) and runs: the install
asks for nothing. A LinkedIn connections list newer than its import is imported on Modal first.
Enrichment that fails is deferred and listed at the end; search is built without it. The index
is built on Modal unless realize left people.csv as it was and an index is already there; only
validation marks search ready.

Changelog:
  2026-10-07: the stage order, realize (with the share list) and the index command come from
      deep_context_v2/run.py. Gone: the $500 spend approval and --approve-spend (the install
      budget always says yes), the Modal dispatch recovery and cap retry (a rerun builds again
      against the Modal cache), and the review step.
  2026-10-07: deep-context v2. The stages are load, collect, synthesize, dedupe, worth, enrich and
      realize (packs/ingestion/primitives/deep_context_v2); a v1 store beside the v2 one is archived
      first. Gone with v1: fan-in, seed, the readiness check, compose, validate-dossiers, parents,
      profile prefetch and the workflow-state loop. The owner profile is built by v2's owner step
      from the LinkedIn session and the Gmail address. The review is the page at `/`.
  2026-10-05: every write names a status_prose event; what a run deferred is
      listed with its reason in the ready step's details (`left_to_fix`).
  2026-10-05: setup no longer waits on the LinkedIn review: matches the judges
      could not settle are left for the user, the index builds, and the ready
      step offers the review (count and link) for when they have time.
  2026-10-05: enrichment that fails for any reason is deferred: search is built
      without it, the ready message lists what is left to fix.
  2026-10-03: import the scraped LinkedIn connections before fan-in.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import traceback
from contextlib import chdir, closing, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Callable

from packs.indexing.primitives.validate_search_index.validate_search_index import validate as validate_search_index
from packs.ingestion.primitives.common.jsonio import read_json
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.node import Manifest, Node
from packs.ingestion.primitives.deep_context_v2.openai import load_env
from packs.ingestion.primitives.deep_context_v2.owner import build_owner
from packs.ingestion.primitives.deep_context_v2.realize.realize import PEOPLE_CSV_RELATIVE_PATH
from packs.ingestion.primitives.deep_context_v2.run import (archive_v1, index_command, index_env, realize,
                                                            resolve_operator_id, stages)
from packs.ingestion.primitives.discover.linkedin.connections import CONNECTIONS_CSV, SCRAPE_RECORD
from packs.powerset.primitives.install.status import InstallStatus
from packs.powerset.primitives.install.status_prose import PROSE
from packs.powerset.primitives.install.steps import InstallState, InstallStep
from packs.powerset.primitives.install.workflow import _parser as source_parser

_LINKEDIN_PEOPLE = ".powerpacks/network-import/import/linkedin/people.csv"
_DATA_ROOT = ".powerpacks"
_MODAL = ["uv", "run", "--project", ".", "python", "packs/indexing/modal/linkedin_modal_pipeline.py"]
# Skipped steps the finished setup still lists for the user to fix.
_FOLLOW_UP_STEPS = (InstallStep.CREDENTIALS.value, InstallStep.ENRICH.value)
# Each stage's page events: while it estimates (empty: it prices nothing), while it runs.
_EVENTS = {
    "load": ("", "discover.people"),
    "collect": ("", "discover.reading"),
    "synthesize": ("discover.estimating", "discover.learning"),
    "dedupe": ("discover.duplicates", "discover.combining"),
    "worth": ("discover.grouping", "discover.grouping"),
    "enrich": ("enrich.running", "enrich.running"),
}


class _Stopped(Exception):
    pass


def _manifest_payload(manifest: Manifest) -> dict:
    """A v2 stage manifest as the payload `_run` reads: its status, counts and error."""
    return {"status": manifest.status, **manifest.counts, "error": manifest.error}


class ProcessingOnboarding:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.status = InstallStatus(self.root)
        previous = self.status.read()
        self.retry = previous["retry_command"]
        self.saved, _ = source_parser(add_help=False).parse_known_args(shlex.split(self.retry)[1:])
        self.account_email = previous.get("account_email") or ""
        self.step = InstallStep.DEEP_CONTEXT
        self.data_root = self.root / _DATA_ROOT
        self.index = self.data_root / "search-index"
        # What this run deferred, with why, for the agent to report once search is ready.
        self.left_to_fix: list[dict] = []

    def _write(self, event: str, *, action: dict | None = None, details: dict | None = None, **values) -> dict:
        """Write `event`; only a running step is owned by this process."""
        running = PROSE[event].state is InstallState.RUNNING
        record = self.status.write(event, step=self.step, pid=os.getpid() if running else 0, retry_command=self.retry,
                                   action=action, details=details, **values)
        self.step = InstallStep(record["step"])
        return record

    def _run(self, name: str, operation: Callable[[], dict], event: str) -> dict:
        """Run `operation` under `event`, its output in the install log; a failed result stops the run."""
        self._write(event)
        self.status.directory.mkdir(parents=True, exist_ok=True)
        with self.status.log_path.open("a", encoding="utf-8") as log:
            log.write(f"[install] {name}\n")
            log.flush()
            with redirect_stdout(log), redirect_stderr(log):
                payload = operation()
            log.write(json.dumps(payload, default=str) + "\n")
        if payload.get("status") in {"failed", "not_ready", "fail", "missing"}:
            self._write("step.failed", action={"command": name}, details=payload)
            raise _Stopped(payload.get("error") or payload.get("summary") or name)
        return payload

    def _stage(self, name: str, node: Node) -> None:
        """One v2 stage under its page events: its estimate (when it prices its work), then its run."""
        estimating, running = _EVENTS[name]
        if estimating:
            self._run(f"{name} estimate", node.estimate, estimating)
        self._run(name, lambda: _manifest_payload(node.run()), running)

    def _modal(self, command: list[str], env: dict[str, str] | None = None) -> dict:
        """A Modal command, its output into the install log."""
        result = subprocess.run(command, cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        print(result.stdout)
        return {"status": "failed" if result.returncode else "completed", "returncode": result.returncode}

    def _linkedin(self) -> None:
        """The scraped LinkedIn connections, imported on Modal when newer than their import."""
        connections, imported = self.root / CONNECTIONS_CSV, self.root / _LINKEDIN_PEOPLE
        if connections.is_file() and (not imported.is_file()
                                      or imported.stat().st_mtime_ns < connections.stat().st_mtime_ns):
            command = [*_MODAL, "import-linkedin", "--csv", str(CONNECTIONS_CSV), "--dest", _LINKEDIN_PEOPLE]
            self._run(shlex.join(command), lambda: self._modal(command), "discover.linkedin")

    def _owner(self) -> None:
        """The owner profile, once: the LinkedIn scrape records the signed-in profile; the mailbox is the email."""
        if (self.data_root / "deep-context" / "owner.json").is_file():
            return
        linkedin_url = (read_json(self.root / SCRAPE_RECORD, {}) or {}).get("owner_url", "")
        email = next(iter(self.saved.gmail_email), "") or self.account_email
        if not (linkedin_url and email):
            command = f"bin/deep-context-v2 owner --linkedin-url <your LinkedIn URL> --email {email or '<your email>'}"
            self._write("discover.owner_needed", action={"command": command})
            raise _Stopped
        self._run("owner", lambda: build_owner(self.data_root, linkedin_url, [email]), "discover.owner")

    def _deferred(self, event: str, error: BaseException) -> None:
        """An optional step that failed is skipped; search is built without it and the next run tries again."""
        with self.status.log_path.open("a", encoding="utf-8") as log:
            log.write(f"[install] {event}: {type(error).__name__}: {error}\n")
        reason = {"event": event, "error_type": type(error).__name__, "error": str(error)}
        self.left_to_fix.append(reason)
        self._write(event, details=reason)

    def _enrich(self, node: Node) -> None:
        """Run enrichment; LinkedIn matches the judges left wait for the user on the review page."""
        self.step = InstallStep.ENRICH
        try:
            self._stage("enrich", node)
        except _Stopped:
            raise
        except (Exception, SystemExit) as error:
            self._deferred("enrich.deferred", error)
            return
        self._write("enrich.done")

    def _index(self) -> None:
        self.step = InstallStep.INDEX
        people = self.data_root / PEOPLE_CSV_RELATIVE_PATH
        before = people.read_bytes() if people.is_file() else b""
        self._run("realize", lambda: {"status": "completed", "people_csv": str(realize(self.data_root))},
                  "index.preparing")
        if people.read_bytes() != before or not (self.index / "manifest.json").is_file():
            command = index_command(self.data_root, people)
            env = index_env(resolve_operator_id(""))
            self._run(shlex.join(command), lambda: self._modal(command, env), "index.building")
        self._write("index.done")
        validation = self._run("validate-search-index", lambda: validate_search_index(
                               self.index / "local-search.duckdb", people), "validate.checking")
        self._write("validate.done", people=validation["total_people"])
        # Search is ready; whatever was deferred is listed with what it needs. The page offers
        # the matches the judges left, counted live by the review queue.
        steps = self.status.read()["steps"]
        deferred = [steps[name]["message"] for name in _FOLLOW_UP_STEPS
                    if steps.get(name, {}).get("status") == InstallState.SKIPPED.value]
        self._write("search.ready", people=validation["total_people"], follow_ups=" ".join(deferred),
                    details={"validation": validation, "left_to_fix": self.left_to_fix})

    def run(self) -> dict:
        with chdir(self.root):
            load_env()
            try:
                self._linkedin()
                archive_v1(self.data_root)
                with closing(open_store(store_path(self.data_root))) as conn:
                    self._owner()
                    *discover, (_, enrich) = stages(conn, self.data_root)
                    for name, node in discover:
                        self._stage(name, node)
                    self._write("discover.done")
                    self._enrich(enrich)
                self._index()
            except _Stopped:
                pass
            except (Exception, SystemExit) as error:
                # Primitives stop with SystemExit and a message (e.g. a missing key).
                self.status.directory.mkdir(parents=True, exist_ok=True)
                with self.status.log_path.open("a", encoding="utf-8") as log:
                    traceback.print_exc(file=log)
                self._write("step.failed", details={"error_type": type(error).__name__, "error": str(error)})
            return self.status.read()
