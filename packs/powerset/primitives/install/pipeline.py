"""Resume imported contacts through native Deep Context, Modal, and validation.

Native manifests and SQLite own completed work. Explicit, scoped flags authorize
paid commands and uploads; the installation manifest only displays the next action.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import traceback
from datetime import datetime
from enum import Enum
from pathlib import Path

from packs.ingestion.primitives.common.gates import EXIT_NEEDS_APPROVAL
from packs.ingestion.primitives.common.jsonio import parse_last_json, sha256_file
from packs.powerset.primitives.install.status import (
    PROCESSING_STEPS, InstallState, InstallStatus, InstallStep,
)

_PEOPLE = ".powerpacks/network-import/merged/people.csv"
_INDEX = ".powerpacks/search-index"
_WAIT_SECONDS = 60


class SpendStep(str, Enum):
    SYNTHESIZE = "synthesize"
    CLUSTER = "cluster"
    ENRICH = "enrich"
    INDEX = "index"


class _Stopped(Exception):
    pass


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
        saved = shlex.split(self.retry)
        store = next((value for flag, value in zip(saved, saved[1:]) if flag == "--wacli-store"), None)
        self.collection_args = ["--wacli-db", str(Path(store).expanduser() / "wacli.db")] if store else []
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

    def _command(self, command: list[str], message: str, *, waiting: bool = False,
                 action: dict | None = None) -> dict:
        self._write(InstallState.WAITING if waiting else InstallState.RUNNING, message, action=action, live=True)
        self.status.directory.mkdir(parents=True, exist_ok=True)
        with self.status.log_path.open("a", encoding="utf-8") as log:
            log.write(f"[install] {shlex.join(command)}\n")
            log.flush()
            result = subprocess.run(command, cwd=self.root, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=log, text=True)
            log.write(result.stdout)
        payload = parse_last_json(result.stdout)
        if command == self.download and not result.returncode:
            payload = {"status": "completed"}
        needs_action = result.returncode in (10, EXIT_NEEDS_APPROVAL) or payload.get("status") in {
            "needs_approval", "needs_user_action", "blocked_user_action"}
        if result.returncode or needs_action or payload.get("status") in {
            "failed", "fail", "missing", "blocked", "error", "not_ready", "not-ready"}:
            self._write(InstallState.WAITING if needs_action else InstallState.FAILED,
                        f"{message} stopped. Check the installation log.",
                        action={"kind": "error", "command": shlex.join(command), "result": payload})
            raise _Stopped
        if not payload:
            raise ValueError(f"No JSON result from {shlex.join(command)}")
        return payload

    def _deep(self, command: str, *arguments: str, message: str) -> dict:
        return self._command([str(self.root / "bin/deep-context"), command, *arguments], message)

    def _approval(self, step: SpendStep, command: list[str], estimate: dict, *, upload: bool = False) -> None:
        if step in self.approved and (not upload or self.approve_upload):
            self.approved.remove(step)
            return
        continuation = shlex.split(self.retry) + ["--approve-spend", step.value]
        if upload:
            continuation.append("--approve-upload")
        self._write(InstallState.WAITING,
                    "Approve uploading your contacts and building your search index." if upload
                    else "Approve the estimated processing cost to continue.",
                    action={"kind": "approval", "step": step.value,
                            "command": shlex.join(command), "estimate": estimate,
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
        self._command([*self.python, "packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py",
                       "fan-in", "--people-csv", _PEOPLE], "Preparing your contacts")
        self._deep("ensure-parents", message="Discovering your contacts")
        readiness = self._deep("check", *self.collection_args, message="Checking your contacts")
        if readiness["checks"]["canonical_sqlite"]["status"] == "seed_required":
            self._deep("seed", message="Reusing your previous context")
            readiness = self._deep("check", *self.collection_args, message="Checking your contacts")
        if readiness["checks"]["owner_json"]["status"] == "absent" and not self.owner.is_file():
            self._write(InstallState.WAITING, "Add your LinkedIn profile to continue.",
                        action={"kind": "owner", "command": readiness["next_command"],
                                "text": "The agent needs your LinkedIn URL and email. A profile cache miss needs approval."})
            raise _Stopped
        if self.owner.is_file():
            self._deep("owner", message="Preparing your profile")
        if not self._collected():
            self._deep("collect", "--deep-cap", "1600", *self.collection_args, message="Reading your messages")

    def _discover(self) -> None:
        estimate = self._deep("synthesize", "--dry-run", message="Estimating context processing")
        if estimate["people"] or estimate["jev_people"]:
            command = [str(self.root / "bin/deep-context"), "synthesize"]
            self._approval(SpendStep.SYNTHESIZE, command, estimate)
            self._command(command, "Learning about your contacts")
        self._deep("compose", message="Preparing your contacts")
        self._deep("validate", message="Checking your contact context")
        estimate = self._deep("cluster", "--dry-run", message="Checking duplicate contacts")
        command = [str(self.root / "bin/deep-context"), "cluster"]
        if estimate["estimated_input_tokens"]:
            self._approval(SpendStep.CLUSTER, command, estimate)
        self._command(command, "Combining duplicate contacts")
        self._deep("parents", message="Preparing your contacts")
        self._write(InstallState.COMPLETED, "Done")

    def _enrich(self) -> None:
        self.step = InstallStep.ENRICH
        state = self._deep("review-status", message="Checking identities")
        while state["next_action"] != "realize":
            if state["next_action"] == "enrich":
                self.step = InstallStep.ENRICH
                estimate = self._deep("enrich", "--dry-run", message="Estimating contact enrichment")
                command = [str(self.root / "bin/deep-context"), "enrich"]
                self._approval(SpendStep.ENRICH, command, estimate)
                self._command(command, "Enriching your contacts")
                self._write(InstallState.COMPLETED, "Done")
            elif state["next_action"] == "review_linkedin":
                self._write(InstallState.COMPLETED, "Done")
                self.step = InstallStep.REVIEW
                if self.step.value not in self.plan:
                    self.plan.insert(self.plan.index(InstallStep.INDEX.value), self.step.value)
                page = self._deep("review", "linkedin", "--port", str(self.port), "--open", message="Opening your identity review")
                action = {"kind": "review", "url": page["url"], "text": "Review the matches that need your input."}
                while state["next_action"] == "review_linkedin":
                    state = self._command([str(self.root / "bin/deep-context"), "review-status", "--wait",
                                           "--timeout", str(_WAIT_SECONDS)], "Waiting for your review",
                                          waiting=True, action=action)
                self._write(InstallState.COMPLETED, "Done")
                continue
            else:
                raise ValueError(f"Context processing is unfinished: {state['next_action']}")
            state = self._deep("review-status", message="Checking identities")
        self.step = InstallStep.ENRICH
        self._write(InstallState.COMPLETED, "Done")

    def _index(self, previous_input: str, previous_index: bool, previous_mtime: int,
               dispatched: dict | None) -> None:
        self.step = InstallStep.INDEX
        realized = self._deep("realize", message="Preparing your search index")
        if realized["profiles_missing"]:
            self.step = InstallStep.ENRICH
            estimate = self._deep("profile-prefetch", message="Checking missing profiles")
            command = [str(self.root / "bin/deep-context"), "profile-prefetch", "--fetch"]
            self._approval(SpendStep.ENRICH, command, estimate)
            self._command(command, "Preparing your profiles")
            self._write(InstallState.COMPLETED, "Done")
            self.step = InstallStep.INDEX
            realized = self._deep("realize", message="Preparing your search index")
            if realized["profiles_missing"]:
                raise ValueError("Profiles remain missing after fetching; inspect the profile manifest.")
        unchanged = previous_input == sha256_file(self.people)
        if unchanged:
            os.utime(self.people, ns=(self.people.stat().st_atime_ns, previous_mtime))
        validate_command = [*self.python,
            "packs/indexing/primitives/validate_search_index/validate_search_index.py",
            "--people-csv", _PEOPLE, "--db", f"{_INDEX}/local-search.duckdb"]
        if dispatched is not None:
            started = datetime.fromisoformat(dispatched["started_at"].replace("Z", "+00:00"))
            if not unchanged or previous_mtime > int(started.timestamp() * 1_000_000_000):
                self._write(InstallState.WAITING, "An earlier index needs checking before another upload.",
                            action={"kind": "recovery", "command": shlex.join(self.download),
                                    "text": "The agent must check the previous indexing job and its input before continuing.",
                                    "result": dispatched})
                raise _Stopped
            self._command(self.download, "Resuming your search index")
        elif not (previous_index and unchanged) or SpendStep.INDEX in self.approved:
            estimate = self._command([*self.python,
                "packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py",
                "run", "--dry-run", "--input", _PEOPLE, "--output-dir", _INDEX], "Estimating search indexing")
            estimate["note"] = "Local cache estimate; the Modal shared cache and compute cost may differ. Contacts upload to the configured workspace."
            # Zero disables the native estimate gate. Keep it enabled for cache-only estimates too.
            cap = max(0.01, float(estimate["estimated_cost_usd"]))
            command = [*self.modal, "index-people", "--people-csv", _PEOPLE, "--max-usd", str(cap)]
            self._approval(SpendStep.INDEX, command, estimate, upload=True)
            payload = self._command(command, "Building your search index")
            if payload["status"] != "completed":
                raise ValueError("Indexing did not complete.")
        if not all((self.index / filename).is_file() for filename in ("local-search.duckdb", "manifest.json")):
            raise ValueError("Indexing did not download the search index and manifest.")
        self._write(InstallState.COMPLETED, "Done")
        self.step = InstallStep.VALIDATE
        validation = self._command(validate_command, "Checking your search")
        if validation["status"] != "ok":
            raise ValueError(validation["summary"])
        self._write(InstallState.COMPLETED, validation["summary"])
        self.step = InstallStep.READY
        self.status.write(step=self.step, status=InstallState.COMPLETED,
                          message=validation["summary"],
                          pid=os.getpid(), retry_command=self.retry, plan=self.plan)

    def run(self) -> dict:
        try:
            previous_input = sha256_file(self.people) if self.people.is_file() else ""
            previous_mtime = self.people.stat().st_mtime_ns if previous_input else 0
            previous_index = bool(previous_input) and all(
                (self.index / filename).is_file()
                and (self.index / filename).stat().st_mtime_ns >= previous_mtime
                for filename in ("local-search.duckdb", "manifest.json"))
            dispatched = json.loads(self.dispatch_path.read_text(encoding="utf-8")) if self.dispatch_path.is_file() else None
            if dispatched is not None and not (
                dispatched["current_stage"] == "indexing" and (
                    dispatched["status"] in {"running", "failed"}
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
        except Exception as error:
            self.status.directory.mkdir(parents=True, exist_ok=True)
            with self.status.log_path.open("a", encoding="utf-8") as log:
                traceback.print_exc(file=log)
            self._write(InstallState.FAILED, str(error))
        return self.status.read()
