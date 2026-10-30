"""Process imported message sources: deep context, enrichment, Modal indexing and validation.

Changelog:
  2026-10-29: LinkedIn setup moves to linkedin_setup.py; this run processes message imports.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import time
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
from packs.ingestion.primitives.deep_context_v2.run import archive_v1, index_command, realize, stages
from packs.ingestion.primitives.discover.linkedin.connections import CONNECTIONS_CSV, SCRAPE_RECORD
from packs.memory.runner import keep_updated
from packs.powerset.primitives.install.status import InstallStatus
from packs.powerset.primitives.install.status_prose import PROSE
from packs.powerset.primitives.install.steps import InstallState, InstallStep
from packs.powerset.primitives.install.controller import LINKEDIN_PEOPLE
from packs.powerset.primitives.install.workflow import _parser as source_parser

_LINKEDIN_PEOPLE = LINKEDIN_PEOPLE
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
# The live line of a stage's phase as its paid calls come back (Node.progress), at most every
# _COUNT_SECONDS so the manifest is not rewritten per call.
_COUNT_EVENTS = {
    ("synthesize", "facts"): "discover.learning.count",
    ("enrich", "research"): "enrich.researching.count",
    ("enrich", "judge"): "enrich.judging.count",
}
_COUNT_SECONDS = 2.0


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
        """One v2 stage under its page events: its estimate (when it prices its work), then its run
        with its counts on the page as they come in."""
        estimating, running = _EVENTS[name]
        if estimating:
            self._run(f"{name} estimate", node.estimate, estimating)
        node.on_progress = self._counter(name)
        self._run(name, lambda: _manifest_payload(node.run()), running)

    def _counter(self, name: str) -> Callable[[str, int, int], None]:
        """The stage's progress callback: its phase's count event, written at most every _COUNT_SECONDS
        and when the phase is done."""
        written_at = 0.0

        def count(phase: str, done: int, total: int) -> None:
            nonlocal written_at
            event = _COUNT_EVENTS.get((name, phase))
            if event is None or not total:
                return
            now = time.monotonic()
            if done < total and now - written_at < _COUNT_SECONDS:
                return
            written_at = now
            self._write(event, done=done, total=total, details={"done": done, "total": total})

        return count

    def _modal(self, command: list[str], env: dict[str, str] | None = None) -> dict:
        """A Modal command, its output into the install log."""
        result = subprocess.run(command, cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        print(result.stdout)
        return {"status": "failed" if result.returncode else "completed", "returncode": result.returncode}

    def _linkedin(self) -> None:
        """Import scraped connections on Modal when newer than their import."""
        connections, imported = self.root / CONNECTIONS_CSV, self.root / _LINKEDIN_PEOPLE
        if connections.is_file() and (not imported.is_file()
                                      or imported.stat().st_mtime_ns < connections.stat().st_mtime_ns):
            command = [*_MODAL, "import-linkedin", "--csv", str(CONNECTIONS_CSV), "--dest", _LINKEDIN_PEOPLE]
            self._run(shlex.join(command), lambda: self._modal(command), "discover.linkedin")

    def _owner(self) -> None:
        """The owner profile, once: the LinkedIn scrape records the signed-in profile, else setup's saved
        profile link (`--linkedin-url`); the mailbox is the email."""
        if (self.data_root / "deep-context" / "owner.json").is_file():
            return
        linkedin_url = ((read_json(self.root / SCRAPE_RECORD, {}) or {}).get("owner_url", "")
                        or self.saved.linkedin_url)
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

    def _enrich(self, build: Callable[[], Node]) -> None:
        """Run enrichment; LinkedIn matches the judges left wait for the user on the review page."""
        self.step = InstallStep.ENRICH
        node = build()
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
        result = self._run("realize", lambda: {"status": "completed", "people_csv": str(realize(self.data_root))},
                           "index.preparing")
        self._build_index(Path(result["people_csv"]))

    def _build_index(self, people: Path) -> None:
        """The Modal index of `people` unless the index on disk was built from this very file (the
        fingerprint the download wrote beside it), then the search check."""
        fingerprint = hashlib.sha256(people.read_bytes()).hexdigest()
        built = self.index / "people.sha256"
        if (not (self.index / "manifest.json").is_file() or not (self.index / "local-search.duckdb").is_file()
                or not built.is_file() or built.read_text(encoding="utf-8").strip() != fingerprint):
            command = index_command(self.data_root, people)
            self._run(shlex.join(command), lambda: self._modal(command), "index.building")
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
        """The import run over the connected sources: owner, deep context, enrichment, the index."""
        return self._guarded(self._messages)

    def _messages(self) -> None:
        self._linkedin()
        archive_v1(self.data_root)
        with closing(open_store(store_path(self.data_root))) as conn:
            self._owner()
            *discover, (_, enrich) = stages(conn, self.data_root)
            for name, build in discover:
                self._stage(name, build())
            self._write("discover.done")
            self._enrich(enrich)
        self._index()

    def _guarded(self, work: Callable[[], None]) -> dict:
        """`work` under the project root; a stop leaves its record, anything else fails the step."""
        with chdir(self.root):
            load_env()
            try:
                work()
                keep_updated(self.root)
            except _Stopped:
                pass
            except (Exception, SystemExit) as error:
                # Primitives stop with SystemExit and a message (e.g. a missing key).
                self.status.directory.mkdir(parents=True, exist_ok=True)
                with self.status.log_path.open("a", encoding="utf-8") as log:
                    traceback.print_exc(file=log)
                self._write("step.failed", details={"error_type": type(error).__name__, "error": str(error)})
            return self.status.read()
