"""Resume imported contacts through native Deep Context, Modal, and validation.

Native manifests and SQLite own completed work. Routine processing follows
the onboarding automatic budget; the installation manifest displays the next action.
A LinkedIn connections list newer than its import is imported on Modal first,
an ungated step.

Changelog:
  2026-10-03: import the scraped LinkedIn connections before fan-in; build the
      owner profile from the LinkedIn session and the Gmail address instead of
      asking; a failed Modal run is retried, not re-downloaded, unless it failed
      on the spend cap.
  2026-10-04: a step that stops with SystemExit (research without a Parallel
      key) is recorded as failed with its message instead of ending the process.
  2026-10-04: indexing goes ahead when a cached profile has no jobs listed (it
      used to raise on every resume); upload consent is asked before a $500+
      spend approval, so the two questions no longer bounce.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
import traceback
from argparse import Namespace
from contextlib import chdir, redirect_stderr, redirect_stdout
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable

from packs.ingestion.primitives.common.jsonio import parse_last_json, sha256_file
from packs.ingestion.primitives.common.legacy import scrub_august_deep_context_store
from packs.ingestion.primitives.deep_context.collection.collect_person_context import CollectPersonContext
from packs.ingestion.primitives.common.jsonio import read_json
from packs.ingestion.primitives.discover.linkedin.connections import CONNECTIONS_CSV, SCRAPE_RECORD
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.workflow_views import workflow_state
from packs.ingestion.primitives.deep_context.enrich.enrichment_pipeline import EnrichmentPipeline
from packs.ingestion.primitives.deep_context.enrich.estimate import estimate_enrichment
from packs.ingestion.primitives.deep_context.enrich.profiles.prefetch import PrefetchProfiles
from packs.ingestion.primitives.deep_context.ensure_parents.ensure_parents import EnsureParents
from packs.ingestion.primitives.deep_context.merge_candidates.build_parents import BuildParents
from packs.ingestion.primitives.deep_context.merge_candidates.cluster_merge_candidates import ClusterMergeCandidates
from packs.ingestion.primitives.deep_context.migration.seed import Seed
from packs.ingestion.primitives.deep_context.realize.export_people import ExportPeople
from packs.ingestion.primitives.deep_context.shared.build_owner import BuildOwner
from packs.ingestion.primitives.deep_context.shared.check_readiness import CheckReadiness
from packs.ingestion.primitives.deep_context.shared.common import load_env
from packs.ingestion.primitives.deep_context.shared.readiness_models import readiness_payload
from packs.ingestion.primitives.deep_context.synthesis.compose_dossier import ComposeDossier
from packs.ingestion.primitives.deep_context.synthesis.synthesize_person_context import SynthesizePersonContext
from packs.ingestion.primitives.deep_context.synthesis.validate_dossiers import ValidateDossiers
from packs.ingestion.primitives.imports.merge_people import PeopleMerge
from packs.indexing.primitives.build_processing_pipeline.build_processing_pipeline import estimate_run
from packs.indexing.primitives.validate_search_index.validate_search_index import validate as validate_search_index
from packs.powerset.primitives.install.workflow import _parser as source_parser
from packs.powerset.primitives.install.status import (
    PROCESSING_STEPS, InstallState, InstallStatus, InstallStep,
)

_PEOPLE = ".powerpacks/network-import/merged/people.csv"
_LINKEDIN_PEOPLE = ".powerpacks/network-import/import/linkedin/people.csv"
_INDEX = ".powerpacks/search-index"
_AUTO_SPEND_USD = 500
_REVIEW_POLL_SECONDS = 5


class SpendStep(str, Enum):
    SYNTHESIZE = "synthesize"
    CLUSTER = "cluster"
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


class ProcessingOnboarding:
    def __init__(self, root: Path, *, approved_spend: tuple[str, ...] = (),
                 approve_upload: bool = False, port: int = 8765) -> None:
        self.root = root.resolve()
        self.status = InstallStatus(self.root)
        previous = self.status.read()
        self.retry = previous["retry_command"]
        self.plan = [step for step in previous["plan"] if step not in PROCESSING_STEPS]
        self.plan.extend(step.value for step in PROCESSING_STEPS
                         if step is not InstallStep.REVIEW or step.value in previous["plan"])
        self.approved = {SpendStep(step) for step in approved_spend}
        self.approve_upload = approve_upload
        self.port = port
        self.saved, _ = source_parser(add_help=False).parse_known_args(shlex.split(self.retry)[1:])
        self.account_email = previous.get("account_email") or ""
        store = self.saved.wacli_store
        self.collection_options = {"wacli_db": store.expanduser() / "wacli.db"} if store else {}
        self.step = InstallStep.DEEP_CONTEXT
        self.people = self.root / _PEOPLE
        self.index = self.root / _INDEX
        self.raw_manifest = self.root / ".powerpacks/deep-context/raw/manifest.json"
        self.imports = self.root / ".powerpacks/network-import/import"
        self.owner = self.root / ".powerpacks/deep-context/owner.json"
        self.python = ["uv", "run", "--project", ".", "python"]
        self.modal = [*self.python, "packs/indexing/modal/linkedin_modal_pipeline.py"]
        self.download = [*self.modal, "download", "--label", "gmail-index", "--wait", "--dest", _INDEX]
        self.dispatch_path = self.root / ".powerpacks/runs/setup-gmail-modal/status.json"

    def _write(self, state: InstallState, message: str, *, action: dict | None = None,
               live: bool = False) -> dict:
        return self.status.write(step=self.step, status=state, message=message,
                                 pid=os.getpid() if live or state is InstallState.RUNNING else 0,
                                 retry_command=self.retry, plan=self.plan, action=action)

    def _run(self, name: str, operation: Callable[[], dict], message: str) -> dict:
        self._write(InstallState.RUNNING, message, live=True)
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
            self._write(InstallState.WAITING if needs_action else InstallState.FAILED,
                        f"{message} stopped. Check the installation log.",
                        action={"kind": "error", "command": name, "result": payload})
            raise _Stopped
        if not payload:
            raise ValueError(f"No result from {name}")
        return payload

    def _modal(self, command: list[str], message: str) -> dict:
        def rehost() -> dict:
            result = subprocess.run(command, cwd=self.root, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            print(result.stdout)
            payload = parse_last_json(result.stdout)
            if result.returncode:
                return {**payload, "status": "needs_approval" if result.returncode == 20 else "failed",
                        "returncode": result.returncode}
            return {"status": "completed"} if command == self.download else payload
        return self._run(shlex.join(command), rehost, message)

    def _approval(self, step: SpendStep, estimate: dict, *, upload: bool = False) -> None:
        if upload and self.approve_upload:
            return
        if not upload and step in self.approved:
            self.approved.remove(step)
            return
        continuation = shlex.split(self.retry)
        if upload:
            continuation.append("--approve-upload")
        else:
            continuation.extend(["--approve-spend", step.value])
            if self.approve_upload:
                continuation.append("--approve-upload")
        self._write(InstallState.WAITING,
                    "Approve uploading your contacts and building your search index." if upload
                    else "Approve the estimated processing cost to continue.",
                    action={"kind": "approval", "step": step.value,
                            "command": self.retry, "estimate": estimate,
                            "upload": upload, "continue_command": shlex.join(continuation)})
        raise _Stopped

    def _collected(self) -> bool:
        if not self.raw_manifest.is_file():
            return False
        record = json.loads(self.raw_manifest.read_text(encoding="utf-8"))
        imported = [path for path in self.imports.glob("*/**/*")
                    if path.is_file() and path.name in {"manifest.json", "people.csv"}]
        return record["status"] == "completed" and all(
            path.stat().st_mtime_ns <= self.raw_manifest.stat().st_mtime_ns for path in imported)

    def _prepare(self) -> None:
        connections, imported = self.root / CONNECTIONS_CSV, self.root / _LINKEDIN_PEOPLE
        if connections.is_file() and (not imported.is_file()
                                      or imported.stat().st_mtime_ns < connections.stat().st_mtime_ns):
            self._modal([*self.modal, "import-linkedin", "--csv", str(CONNECTIONS_CSV), "--dest", _LINKEDIN_PEOPLE],
                        "Adding your LinkedIn connections")
        self._run("fan-in", lambda: PeopleMerge(output_dir=self.people.parent).run().to_payload(),
                  "Preparing your contacts")
        db_path = self.root / ".powerpacks/deep-context/deep-context.sqlite"
        scrub_august_deep_context_store(db_path)
        self.db = Db(db_path)
        self._run("ensure-parents", lambda: EnsureParents(db=self.db, people_csv=self.people).run().to_payload(),
                  "Discovering your contacts")
        check = CheckReadiness(db=self.db, people_csv=self.people, **self.collection_options)
        readiness = self._run("check", lambda: readiness_payload(check.run()), "Checking your contacts")
        if readiness["checks"]["canonical_sqlite"]["status"] == "seed_required":
            self._run("seed", lambda: Seed(db=self.db).run().to_payload(), "Reusing your previous context")
            readiness = self._run("check", lambda: readiness_payload(check.run()), "Checking your contacts")
        if readiness["checks"]["owner_json"]["status"] == "absent" and not self.owner.is_file():
            # The LinkedIn scrape records the signed-in profile; the mailbox is the owner's email.
            linkedin_url = (read_json(self.root / SCRAPE_RECORD, {}) or {}).get("owner_url", "")
            email = next(iter(self.saved.gmail_email), "") or self.account_email
            if not (linkedin_url and email):
                self._write(InstallState.WAITING, "Add your LinkedIn profile to continue.",
                            action={"kind": "owner", "command": readiness["next_command"],
                                    "text": "The agent needs your LinkedIn URL and email."})
                raise _Stopped
            self._run("owner", lambda: BuildOwner(db=self.db, linkedin_url=linkedin_url, email=email)
                      .run().to_payload(), "Preparing your profile")
        elif self.owner.is_file():
            self._run("owner", lambda: BuildOwner(db=self.db).run().to_payload(), "Preparing your profile")
        if not self._collected():
            self._run("collect", lambda: CollectPersonContext(db=self.db, deep_cap=1600,
                      **self.collection_options).run().to_payload(), "Reading your messages")

    def _discover(self) -> None:
        synthesize = SynthesizePersonContext(db=self.db)
        estimate = self._run("synthesize estimate", synthesize.estimate, "Estimating context processing")
        if estimate["people"] or estimate["jev_people"]:
            if estimate["estimated_cost_ceiling_usd"] >= _AUTO_SPEND_USD:
                self._approval(SpendStep.SYNTHESIZE, estimate)
            self._run("synthesize", lambda: synthesize.run().to_payload(), "Learning about your contacts")
        self._run("compose", lambda: ComposeDossier(db=self.db).run().to_payload(), "Preparing your contacts")
        self._run("validate", lambda: ValidateDossiers(db=self.db).run(), "Checking your contact context")
        cluster = ClusterMergeCandidates(db=self.db)
        estimate = self._run("cluster estimate", cluster.estimate, "Checking duplicate contacts")
        if estimate["estimated_cost_usd"] >= _AUTO_SPEND_USD:
            self._approval(SpendStep.CLUSTER, estimate)
        self._run("cluster", lambda: cluster.run().to_payload(), "Combining duplicate contacts")
        self._run("parents", lambda: BuildParents(db=self.db).run().to_payload(), "Preparing your contacts")
        self._write(InstallState.COMPLETED, "Done")

    def _enrich(self) -> None:
        self.step = InstallStep.ENRICH
        state = workflow_state(self.db)
        while state.next_action != "realize":
            if state.next_action == "enrich":
                self.step = InstallStep.ENRICH
                estimate = estimate_enrichment(self.db, state)
                if estimate.estimated_usd >= _AUTO_SPEND_USD:
                    self._approval(SpendStep.ENRICH, estimate.to_payload())
                pipeline = EnrichmentPipeline(self.db)
                self._run("enrich", lambda: pipeline.run(total=estimate.research.deduped_total,
                          budget=estimate.research.estimated_usd,
                          request_fingerprint=estimate.research.request_fingerprint), "Enriching your contacts")
                self._write(InstallState.COMPLETED, "Done")
            elif state.next_action == "review_linkedin":
                self._write(InstallState.COMPLETED, "Done")
                self.step = InstallStep.REVIEW
                if self.step.value not in self.plan:
                    self.plan.insert(self.plan.index(InstallStep.INDEX.value), self.step.value)
                url = f"http://127.0.0.1:{self.port}/?stage=linkedin"
                subprocess.run(["open", url], check=False)
                action = {"kind": "review", "url": url, "text": "Review the matches that need your input."}
                while state.next_action == "review_linkedin":
                    self._write(InstallState.WAITING, "Waiting for your review", action=action, live=True)
                    time.sleep(_REVIEW_POLL_SECONDS)
                    state = workflow_state(self.db)
                self._write(InstallState.COMPLETED, "Done")
                continue
            else:
                raise ValueError(f"Context processing is unfinished: {state.next_action}")
            state = workflow_state(self.db)
        self.step = InstallStep.ENRICH
        self._write(InstallState.COMPLETED, "Done")

    def _index(self, previous_input: str, previous_index: bool, previous_mtime: int,
               dispatched: dict | None) -> None:
        self.step = InstallStep.INDEX
        realize = ExportPeople(db=self.db, out_dir=self.people.parent)
        realized = self._run("realize", realize.run, "Preparing your search index")
        if realized["profiles_missing"]:
            self.step = InstallStep.ENRICH
            self._run("profile-prefetch", lambda: PrefetchProfiles(db=self.db, fetch=True).run().to_payload(),
                      "Preparing your profiles")
            self._write(InstallState.COMPLETED, "Done")
            self.step = InstallStep.INDEX
            # A cached profile with no jobs listed stays "missing"; realize exports it anyway.
            self._run("realize", realize.run, "Preparing your search index")
        unchanged = previous_input == sha256_file(self.people)
        if unchanged:
            os.utime(self.people, ns=(self.people.stat().st_atime_ns, previous_mtime))
        if dispatched is not None:
            started = datetime.fromisoformat(dispatched["started_at"].replace("Z", "+00:00"))
            if not unchanged or previous_mtime > int(started.timestamp() * 1_000_000_000):
                self._write(InstallState.WAITING, "An earlier index needs checking before another upload.",
                            action={"kind": "recovery", "command": shlex.join(self.download),
                                    "text": "The agent must check the previous indexing job and its input before continuing.",
                                    "result": dispatched})
                raise _Stopped
            native = dispatched["stages"]["indexing"]["payload"]
            if _capped(dispatched):
                if native["estimated_usd"] >= _AUTO_SPEND_USD:
                    self._approval(SpendStep.INDEX, native)
                command = [*self.modal, "index-people", "--people-csv", _PEOPLE,
                           "--max-usd", str(native["estimated_usd"])]
                self._modal(command, "Building your search index")
            else:
                self._modal(self.download, "Resuming your search index")
        elif not (previous_index and unchanged) or SpendStep.INDEX in self.approved:
            estimate = self._run("index estimate", lambda: estimate_run(Namespace(
                                 input=self.people, output_dir=self.index, dry_run=True)),
                                 "Estimating search indexing")
            estimate["note"] = "Local cache estimate; Modal checks its shared cache before spending."
            # Upload consent first: the spend continuation keeps it, so the two never bounce.
            self._approval(SpendStep.INDEX, estimate, upload=True)
            if estimate["estimated_cost_usd"] >= _AUTO_SPEND_USD:
                self._approval(SpendStep.INDEX, estimate)
            # Modal rejects only costs above its cap; keep automatic spend below $500.
            cap = max(_AUTO_SPEND_USD - 0.01, estimate["estimated_cost_usd"])
            command = [*self.modal, "index-people", "--people-csv", _PEOPLE, "--max-usd", str(cap)]
            payload = self._modal(command, "Building your search index")
            if payload["status"] != "completed":
                raise ValueError("Indexing did not complete.")
        if not all((self.index / filename).is_file() for filename in ("local-search.duckdb", "manifest.json")):
            raise ValueError("Indexing did not download the search index and manifest.")
        self._write(InstallState.COMPLETED, "Done")
        self.step = InstallStep.VALIDATE
        validation = self._run("validate-search-index", lambda: validate_search_index(
                              self.index / "local-search.duckdb", self.people), "Checking your search")
        if validation["status"] != "ok":
            raise ValueError(validation["summary"])
        self._write(InstallState.COMPLETED, validation["summary"])
        self.step = InstallStep.READY
        self.status.write(step=self.step, status=InstallState.COMPLETED,
                          message=validation["summary"],
                          pid=os.getpid(), retry_command=self.retry, plan=self.plan)

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
                self._write(InstallState.FAILED, str(error))
            return self.status.read()
