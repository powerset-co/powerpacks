"""Resume imported contacts through native Deep Context, Modal, and validation.

Native manifests and SQLite own completed work. Routine processing follows
the onboarding automatic budget; the installation manifest displays the next action.
A LinkedIn connections list newer than its import is imported on Modal first,
an ungated step.

Changelog:
  2026-10-05: every write names a status_prose event; what a run deferred is
      listed with its reason in the ready step's details (`left_to_fix`).
  2026-10-03: import the scraped LinkedIn connections before fan-in; build the
      owner profile from the LinkedIn session and the Gmail address instead of
      asking; a failed Modal run is retried, not re-downloaded, unless it failed
      on the spend cap.
  2026-10-04: a step that stops with SystemExit is recorded as failed with its
      message instead of ending the process; research that cannot run (no
      Parallel key) is skipped with a warning and the index still builds.
  2026-10-05: the index no longer asks to upload contacts; the user approves
      sending data to Parallel and OpenAI once, up front (or setup reads only
      LinkedIn).
  2026-10-05: setup no longer waits on the LinkedIn review: matches the judges
      could not settle are left for the user, the index builds, and the ready
      step offers the review (count and link) for when they have time.
  2026-10-05: enrichment and profile lookups that fail for any reason are
      deferred: search is built without them, the ready message lists what is
      left to fix, and work the last enrichment left is tried once per run.
  2026-10-05: missing profiles are fetched under the index step instead of
      stepping the page back to enrich.
  2026-10-05: the ready step no longer saves a review count; the page reads
      the review queue's own count, so the two always agree.
  2026-10-04: indexing goes ahead when a cached profile has no jobs listed (it
      used to raise on every resume); upload consent is asked before a $500+
      spend approval, so the two questions no longer bounce.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
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
from packs.ingestion.primitives.deep_context.db.workflow_views import enrichment_work, workflow_state
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
from packs.powerset.primitives.install.status import InstallStatus
from packs.powerset.primitives.install.status_prose import PROSE
from packs.powerset.primitives.install.steps import PROCESSING_STEPS, InstallState, InstallStep

_PEOPLE = ".powerpacks/network-import/merged/people.csv"
_LINKEDIN_PEOPLE = ".powerpacks/network-import/import/linkedin/people.csv"
_INDEX = ".powerpacks/search-index"
_AUTO_SPEND_USD = 500
# Skipped steps the finished setup still lists for the user to fix.
_FOLLOW_UP_STEPS = (InstallStep.CREDENTIALS.value, InstallStep.ENRICH.value)


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
                        "discover.linkedin")
        self._run("fan-in", lambda: PeopleMerge(output_dir=self.people.parent).run().to_payload(), "discover.merging")
        db_path = self.root / ".powerpacks/deep-context/deep-context.sqlite"
        scrub_august_deep_context_store(db_path)
        self.db = Db(db_path)
        self._run("ensure-parents", lambda: EnsureParents(db=self.db, people_csv=self.people).run().to_payload(),
                  "discover.people")
        check = CheckReadiness(db=self.db, people_csv=self.people, **self.collection_options)
        readiness = self._run("check", lambda: readiness_payload(check.run()), "discover.checking")
        if readiness["checks"]["canonical_sqlite"]["status"] == "seed_required":
            self._run("seed", lambda: Seed(db=self.db).run().to_payload(), "discover.reusing")
            readiness = self._run("check", lambda: readiness_payload(check.run()), "discover.checking")
        if readiness["checks"]["owner_json"]["status"] == "absent" and not self.owner.is_file():
            # The LinkedIn scrape records the signed-in profile; the mailbox is the owner's email.
            linkedin_url = (read_json(self.root / SCRAPE_RECORD, {}) or {}).get("owner_url", "")
            email = next(iter(self.saved.gmail_email), "") or self.account_email
            if not (linkedin_url and email):
                self._write("discover.owner_needed", action={"command": readiness["next_command"]})
                raise _Stopped
            self._run("owner", lambda: BuildOwner(db=self.db, linkedin_url=linkedin_url, email=email)
                      .run().to_payload(), "discover.owner")
        elif self.owner.is_file():
            self._run("owner", lambda: BuildOwner(db=self.db).run().to_payload(), "discover.owner")
        if not self._collected():
            self._run("collect", lambda: CollectPersonContext(db=self.db, deep_cap=1600,
                      **self.collection_options).run().to_payload(), "discover.reading")

    def _discover(self) -> None:
        synthesize = SynthesizePersonContext(db=self.db)
        estimate = self._run("synthesize estimate", synthesize.estimate, "discover.estimating")
        if estimate["people"] or estimate["jev_people"]:
            if estimate["estimated_cost_ceiling_usd"] >= _AUTO_SPEND_USD:
                self._approval(SpendStep.SYNTHESIZE, estimate)
            self._run("synthesize", lambda: synthesize.run().to_payload(), "discover.learning")
        self._run("compose", lambda: ComposeDossier(db=self.db).run().to_payload(), "discover.composing")
        self._run("validate", lambda: ValidateDossiers(db=self.db).run(), "discover.validating")
        cluster = ClusterMergeCandidates(db=self.db)
        estimate = self._run("cluster estimate", cluster.estimate, "discover.duplicates")
        if estimate["estimated_cost_usd"] >= _AUTO_SPEND_USD:
            self._approval(SpendStep.CLUSTER, estimate)
        self._run("cluster", lambda: cluster.run().to_payload(), "discover.combining")
        self._run("parents", lambda: BuildParents(db=self.db).run().to_payload(), "discover.grouping")
        self._write("discover.done")

    def _deferred(self, event: str, error: BaseException) -> None:
        """An optional step that failed is skipped; search is built without it and the next run tries again."""
        with self.status.log_path.open("a", encoding="utf-8") as log:
            log.write(f"[install] {event}: {type(error).__name__}: {error}\n")
        reason = {"event": event, "error_type": type(error).__name__, "error": str(error)}
        self.left_to_fix.append(reason)
        self._write(event, details=reason)

    def _enrich(self) -> None:
        """Run enrichment; LinkedIn matches the judges left wait for the user."""
        self.step = InstallStep.ENRICH
        state = workflow_state(self.db)
        work = enrichment_work(self.db)
        # What the last enrichment could not finish (a lookup or a judgment) is tried once per setup run.
        retry = state.next_action == "realize" and bool(work.lookups or work.judgments)
        while state.next_action != "realize" or retry:
            if state.next_action == "enrich" or retry:
                retry = False
                self.step = InstallStep.ENRICH
                estimate = estimate_enrichment(self.db, state)
                if estimate.estimated_usd >= _AUTO_SPEND_USD:
                    self._approval(SpendStep.ENRICH, estimate.to_payload())
                pipeline = EnrichmentPipeline(self.db)
                try:
                    self._run("enrich", lambda: pipeline.run(total=estimate.research.deduped_total,
                              budget=estimate.research.estimated_usd,
                              request_fingerprint=estimate.research.request_fingerprint), "enrich.running")
                except (Exception, SystemExit) as error:
                    self._deferred("enrich.deferred", error)
                    return
                self._write("enrich.done")
            elif state.next_action == "review_linkedin":
                # Matches the judges could not settle wait for the user until search is built.
                self._write("enrich.done")
                return
            else:
                raise ValueError(f"Context processing is unfinished: {state.next_action}")
            state = workflow_state(self.db)
        self._write("enrich.done")

    def _index(self, previous_input: str, previous_index: bool, previous_mtime: int,
               dispatched: dict | None) -> None:
        self.step = InstallStep.INDEX
        realize = ExportPeople(db=self.db, out_dir=self.people.parent)
        realized = self._run("realize", realize.run, "index.preparing")
        if realized["profiles_missing"]:
            # Fetched under the index row so the page never steps back; a failure is left on enrich to fix.
            try:
                self._run("profile-prefetch", lambda: PrefetchProfiles(db=self.db, fetch=True).run().to_payload(),
                          "index.profiles")
            except (Exception, SystemExit) as error:
                self._deferred("profiles.deferred", error)
            # A cached profile with no jobs listed stays "missing"; realize exports it anyway.
            self._run("realize", realize.run, "index.preparing")
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
                    details={"left_to_fix": self.left_to_fix} if self.left_to_fix else None)

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
            return self.status.read()
