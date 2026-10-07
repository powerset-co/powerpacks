"""Resume imported contacts through deep-context v2, Modal indexing, and validation.

The v2 store and its stage manifests own completed work. Each paid stage estimates first and runs
when the estimate is under the onboarding automatic budget; at or over it, the page asks once and
the retry command carries the approval. A LinkedIn connections list newer than its import is
imported on Modal first, an ungated step.

Changelog:
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
  2026-10-03: import the scraped LinkedIn connections before fan-in; a failed Modal
      run is retried, not re-downloaded, unless it failed on the spend cap.
"""
from __future__ import annotations

import json
import os
import shlex
import sqlite3
import subprocess
import traceback
from argparse import Namespace
from contextlib import chdir, redirect_stderr, redirect_stdout
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable

from packs.indexing.primitives.build_processing_pipeline.build_processing_pipeline import estimate_run
from packs.indexing.primitives.validate_search_index.validate_search_index import validate as validate_search_index
from packs.ingestion.primitives.common.jsonio import parse_last_json, read_json, sha256_file
from packs.ingestion.primitives.deep_context_v2.collect.collect import DEFAULT_LIMIT as COLLECT_LIMIT
from packs.ingestion.primitives.deep_context_v2.collect.collect import Collect
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.dedupe.dedupe import DEFAULT_LIMIT as DEDUPE_LIMIT
from packs.ingestion.primitives.deep_context_v2.dedupe.dedupe import Dedupe
from packs.ingestion.primitives.deep_context_v2.enrich.enrich import Enrich
from packs.ingestion.primitives.deep_context_v2.import_load.load import ImportLoad
from packs.ingestion.primitives.deep_context_v2.node import Manifest, Node
from packs.ingestion.primitives.deep_context_v2.openai import load_env
from packs.ingestion.primitives.deep_context_v2.owner import build_owner
from packs.ingestion.primitives.deep_context_v2.realize.realize import Realize
from packs.ingestion.primitives.deep_context_v2.run import archive_v1
from packs.ingestion.primitives.deep_context_v2.synthesize.synthesize import DEFAULT_LIMIT as SYNTHESIZE_LIMIT
from packs.ingestion.primitives.deep_context_v2.synthesize.synthesize import Synthesize
from packs.ingestion.primitives.deep_context_v2.worth.worth import DEFAULT_LIMIT as WORTH_LIMIT
from packs.ingestion.primitives.deep_context_v2.worth.worth import Worth
from packs.ingestion.primitives.discover.linkedin.connections import CONNECTIONS_CSV, SCRAPE_RECORD
from packs.ingestion.primitives.discover.messages.extract_imessage import DEFAULT_CHAT_DB
from packs.powerset.primitives.install.status import InstallStatus
from packs.powerset.primitives.install.status_prose import PROSE
from packs.powerset.primitives.install.steps import PROCESSING_STEPS, InstallState, InstallStep
from packs.powerset.primitives.install.workflow import _parser as source_parser

_PEOPLE = ".powerpacks/network-import/merged/people.csv"
_LINKEDIN_PEOPLE = ".powerpacks/network-import/import/linkedin/people.csv"
_INDEX = ".powerpacks/search-index"
_DATA_ROOT = ".powerpacks"
_AUTO_SPEND_USD = 500
# Skipped steps the finished setup still lists for the user to fix.
_FOLLOW_UP_STEPS = (InstallStep.CREDENTIALS.value, InstallStep.ENRICH.value)


class SpendStep(str, Enum):
    SYNTHESIZE = "synthesize"
    DEDUPE = "dedupe"
    WORTH = "worth"
    ENRICH = "enrich"
    INDEX = "index"


class _Stopped(Exception):
    pass


def _capped(dispatched: dict) -> bool:
    """The Modal run stopped before spending because its estimate exceeded --max-usd."""
    if dispatched["status"] != "failed":
        return False
    native = dispatched["stages"]["indexing"]["payload"]
    return native.get("phase") == "estimate" and native.get("error") == "estimate exceeds --max-usd cap"


def _manifest_payload(manifest: Manifest) -> dict:
    """A v2 stage manifest as the payload `_run` reads: its status, counts and error."""
    return {"status": manifest.status, **manifest.counts, "error": manifest.error}


class ProcessingOnboarding:
    def __init__(self, root: Path, *, approved_spend: tuple[str, ...] = ()) -> None:
        self.root = root.resolve()
        self.status = InstallStatus(self.root)
        previous = self.status.read()
        self.retry = previous["retry_command"]
        self.plan = [step for step in previous["plan"] if step not in PROCESSING_STEPS]
        self.plan.extend(step.value for step in PROCESSING_STEPS
                         if step is not InstallStep.REVIEW or step.value in previous["plan"])
        self.approved = {SpendStep(step) for step in approved_spend}
        self.saved, _ = source_parser(add_help=False).parse_known_args(shlex.split(self.retry)[1:])
        self.account_email = previous.get("account_email") or ""
        self.step = InstallStep.DEEP_CONTEXT
        self.data_root = self.root / _DATA_ROOT
        self.people = self.root / _PEOPLE
        self.index = self.root / _INDEX
        self.owner = self.data_root / "deep-context" / "owner.json"
        self.python = ["uv", "run", "--project", ".", "python"]
        self.modal = [*self.python, "packs/indexing/modal/linkedin_modal_pipeline.py"]
        self.download = [*self.modal, "download", "--label", "gmail-index", "--wait", "--dest", _INDEX]
        self.dispatch_path = self.root / ".powerpacks/runs/setup-gmail-modal/status.json"
        self.conn: sqlite3.Connection | None = None  # the v2 store, opened by _prepare
        # What this run deferred, with why, for the agent to report once search is ready.
        self.left_to_fix: list[dict] = []

    def _write(self, event: str, *, action: dict | None = None, details: dict | None = None, **values) -> dict:
        """Write `event`; only a running step is owned by this process."""
        running = PROSE[event].state is InstallState.RUNNING
        record = self.status.write(event, step=self.step, pid=os.getpid() if running else 0, retry_command=self.retry,
                                   plan=self.plan, action=action, details=details, **values)
        self.step = InstallStep(record["step"])
        return record

    def _run(self, name: str, operation: Callable[[], dict], event: str) -> dict:
        self._write(event)
        self.status.directory.mkdir(parents=True, exist_ok=True)
        with self.status.log_path.open("a", encoding="utf-8") as log:
            log.write(f"[install] {name}\n")
            log.flush()
            with redirect_stdout(log), redirect_stderr(log):
                payload = operation()
            log.write(json.dumps(payload, default=str) + "\n")
        needs_action = payload.get("status") in {
            "needs_approval", "needs_user_action", "blocked_user_action"}
        if needs_action or payload.get("status") in {
            "failed", "fail", "missing", "blocked", "error", "not_ready", "not-ready"}:
            self._write("step.waiting" if needs_action else "step.failed", action={"command": name}, details=payload)
            raise _Stopped(payload.get("error") or payload.get("message") or payload.get("note") or name)
        if not payload:
            raise ValueError(f"No result from {name}")
        return payload

    def _stage(self, name: str, node: Node, event: str) -> dict:
        """One v2 stage through `_run`: its manifest's status, counts and error."""
        return self._run(name, lambda: _manifest_payload(node.run()), event)

    def _paid(self, step: SpendStep, node: Node, estimating: str, running: str) -> dict:
        """A paid v2 stage: estimate, ask at the automatic budget, run."""
        estimate = self._run(f"{step.value} estimate", node.estimate, estimating)
        if estimate["estimated_cost_usd"] >= _AUTO_SPEND_USD:
            self._approval(step, estimate)
        return self._stage(step.value, node, running)

    def _modal(self, command: list[str], event: str) -> dict:
        def rehost() -> dict:
            result = subprocess.run(command, cwd=self.root, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            print(result.stdout)
            payload = parse_last_json(result.stdout)
            if result.returncode:
                return {**payload, "status": "needs_approval" if result.returncode == 20 else "failed",
                        "returncode": result.returncode}
            return {"status": "completed"} if command == self.download else payload
        return self._run(shlex.join(command), rehost, event)

    def _approval(self, step: SpendStep, estimate: dict) -> None:
        if step in self.approved:
            self.approved.remove(step)
            return
        continuation = [*shlex.split(self.retry), "--approve-spend", step.value]
        self._write("spend.approval", action={"step": step.value, "command": self.retry, "estimate": estimate,
                                              "continue_command": shlex.join(continuation)})
        raise _Stopped

    def _prepare(self) -> None:
        connections, imported = self.root / CONNECTIONS_CSV, self.root / _LINKEDIN_PEOPLE
        if connections.is_file() and (not imported.is_file()
                                      or imported.stat().st_mtime_ns < connections.stat().st_mtime_ns):
            self._modal([*self.modal, "import-linkedin", "--csv", str(CONNECTIONS_CSV), "--dest", _LINKEDIN_PEOPLE],
                        "discover.linkedin")
        archive_v1(self.data_root)
        self.conn = open_store(store_path(self.data_root))
        if not self.owner.is_file():
            # The LinkedIn scrape records the signed-in profile; the mailbox is the owner's email.
            linkedin_url = (read_json(self.root / SCRAPE_RECORD, {}) or {}).get("owner_url", "")
            email = next(iter(self.saved.gmail_email), "") or self.account_email
            if not (linkedin_url and email):
                command = f"bin/deep-context-v2 owner --linkedin-url <your LinkedIn URL> --email {email or '<your email>'}"
                self._write("discover.owner_needed", action={"command": command})
                raise _Stopped
            self._run("owner", lambda: build_owner(self.data_root, linkedin_url, [email]), "discover.owner")
        self._stage("load", ImportLoad(self.conn, self.data_root), "discover.people")
        self._stage("collect", Collect(self.conn, self.data_root, COLLECT_LIMIT, DEFAULT_CHAT_DB), "discover.reading")

    def _discover(self) -> None:
        self._paid(SpendStep.SYNTHESIZE, Synthesize(self.conn, self.data_root, limit=SYNTHESIZE_LIMIT),
                   "discover.estimating", "discover.learning")
        self._paid(SpendStep.DEDUPE, Dedupe(self.conn, self.data_root, limit=DEDUPE_LIMIT),
                   "discover.duplicates", "discover.combining")
        self._paid(SpendStep.WORTH, Worth(self.conn, self.data_root, limit=WORTH_LIMIT),
                   "discover.grouping", "discover.grouping")
        self._write("discover.done")

    def _deferred(self, event: str, error: BaseException) -> None:
        """An optional step that failed is skipped; search is built without it and the next run tries again."""
        with self.status.log_path.open("a", encoding="utf-8") as log:
            log.write(f"[install] {event}: {type(error).__name__}: {error}\n")
        reason = {"event": event, "error_type": type(error).__name__, "error": str(error)}
        self.left_to_fix.append(reason)
        self._write(event, details=reason)

    def _enrich(self) -> None:
        """Run enrichment; LinkedIn matches the judges left wait for the user on the review page."""
        self.step = InstallStep.ENRICH
        node = Enrich(self.conn, self.data_root, limit=None)
        try:
            self._paid(SpendStep.ENRICH, node, "enrich.running", "enrich.running")
        except _Stopped:
            raise
        except (Exception, SystemExit) as error:
            self._deferred("enrich.deferred", error)
            return
        self._write("enrich.done")

    def _index(self, previous_input: str, previous_index: bool, previous_mtime: int,
               dispatched: dict | None) -> None:
        self.step = InstallStep.INDEX
        self._stage("realize", Realize(self.conn, self.data_root), "index.preparing")
        unchanged = previous_input == sha256_file(self.people)
        if unchanged:
            os.utime(self.people, ns=(self.people.stat().st_atime_ns, previous_mtime))
        if dispatched is not None:
            started = datetime.fromisoformat(dispatched["started_at"].replace("Z", "+00:00"))
            if not unchanged or previous_mtime > int(started.timestamp() * 1_000_000_000):
                self._write("index.recovery", action={"command": shlex.join(self.download)}, details=dispatched)
                raise _Stopped
            native = dispatched["stages"]["indexing"]["payload"]
            if _capped(dispatched):
                if native["estimated_usd"] >= _AUTO_SPEND_USD:
                    self._approval(SpendStep.INDEX, native)
                command = [*self.modal, "index-people", "--people-csv", _PEOPLE,
                           "--max-usd", str(native["estimated_usd"])]
                self._modal(command, "index.building")
            else:
                self._modal(self.download, "index.resuming")
        elif not (previous_index and unchanged) or SpendStep.INDEX in self.approved:
            estimate = self._run("index estimate", lambda: estimate_run(Namespace(
                                 input=self.people, output_dir=self.index, dry_run=True)),
                                 "index.estimating")
            estimate["note"] = "Local cache estimate; Modal checks its shared cache before spending."
            if estimate["estimated_cost_usd"] >= _AUTO_SPEND_USD:
                self._approval(SpendStep.INDEX, estimate)
            # Modal rejects only costs above its cap; keep automatic spend below $500.
            cap = max(_AUTO_SPEND_USD - 0.01, estimate["estimated_cost_usd"])
            command = [*self.modal, "index-people", "--people-csv", _PEOPLE, "--max-usd", str(cap)]
            payload = self._modal(command, "index.building")
            if payload["status"] != "completed":
                raise ValueError("Indexing did not complete.")
        if not all((self.index / filename).is_file() for filename in ("local-search.duckdb", "manifest.json")):
            raise ValueError("Indexing did not download the search index and manifest.")
        self._write("index.done")
        validation = self._run("validate-search-index", lambda: validate_search_index(
                              self.index / "local-search.duckdb", self.people), "validate.checking")
        if validation["status"] != "ok":
            raise ValueError(validation["summary"])
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
                previous_input = sha256_file(self.people) if self.people.is_file() else ""
                previous_mtime = self.people.stat().st_mtime_ns if previous_input else 0
                previous_index = bool(previous_input) and all(
                    (self.index / filename).is_file()
                    and (self.index / filename).stat().st_mtime_ns >= previous_mtime
                    for filename in ("local-search.duckdb", "manifest.json"))
                dispatched = json.loads(self.dispatch_path.read_text(encoding="utf-8")) if self.dispatch_path.is_file() else None
                # A run that failed for any reason but the spend cap is redispatched, not re-downloaded.
                if dispatched is not None and not (
                    dispatched["current_stage"] == "indexing" and (
                        dispatched["status"] == "running" or _capped(dispatched)
                        or dispatched["status"] == "completed" and not previous_index
                        and previous_mtime <= int(datetime.fromisoformat(
                            dispatched["started_at"].replace("Z", "+00:00")).timestamp() * 1_000_000_000)
                    )
                ):
                    dispatched = None
                self._prepare()
                self._discover()
                self._enrich()
                self._index(previous_input, previous_index, previous_mtime, dispatched)
            except _Stopped:
                pass
            except (Exception, SystemExit) as error:
                # Primitives stop with SystemExit and a message (e.g. a missing key).
                self.status.directory.mkdir(parents=True, exist_ok=True)
                with self.status.log_path.open("a", encoding="utf-8") as log:
                    traceback.print_exc(file=log)
                self._write("step.failed", details={"error_type": type(error).__name__, "error": str(error)})
            finally:
                if self.conn is not None:
                    self.conn.close()
            return self.status.read()
