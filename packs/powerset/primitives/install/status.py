"""Installer-owned progress, read by the local status page before dependencies exist.

Every write names an event from `status_prose.PROSE`; its line, note, state and
step come from there. The manifest is the one record the page, the agent and the
CLI read; a primitive's own text goes into the action's details, not the line.

Changelog:
  2026-10-05: `write` takes an event instead of a message; the step enums moved
      to steps.py and the page's words to status_prose.py.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import emit
from packs.ingestion.primitives.common.manifests import write_stage_manifest
from packs.powerset.primitives.install.status_prose import page_prose, render
from packs.powerset.primitives.install.steps import DEFAULT_PLAN, PROCESSING_STEPS, InstallState, InstallStep


@dataclass(frozen=True)
class _InstallManifest:
    step: InstallStep
    status: InstallState
    event: str
    message: str
    installer_pid: int
    log_path: str
    retry_command: str
    steps: dict[str, dict[str, str]] = field(default_factory=dict)
    plan: list[str] = field(default_factory=lambda: list(DEFAULT_PLAN))
    action: dict | None = None
    note: str = ""
    account_email: str | None = None
    network_name: str | None = None
    person_count: int | None = None
    primitive: str = "powerpacks_install"

    def to_payload(self) -> dict:
        return {**asdict(self), "prose": page_prose()}

    @classmethod
    def from_record(cls, record: dict) -> "_InstallManifest":
        return cls(step=InstallStep(record["step"]), status=InstallState(record["status"]),
                   event=record.get("event", ""), message=record["message"],
                   installer_pid=int(record["installer_pid"]),
                   log_path=record["log_path"], retry_command=record["retry_command"],
                   steps=record.get("steps", {}), plan=record.get("plan", list(DEFAULT_PLAN)),
                   action=record.get("action"), note=record.get("note", ""),
                   account_email=record.get("account_email"),
                   network_name=record.get("network_name"), person_count=record.get("person_count"))


class InstallStatus:
    def __init__(self, root: Path) -> None:
        self.directory = Path(root).resolve() / ".powerpacks/install"
        self.manifest_path = self.directory / "manifest.json"
        self.log_path = self.directory / "install.log"

    def write(self, event: str, *, pid: int, step: InstallStep | None = None,
              retry_command: str = "bin/bootstrap", account_email: str | None = None,
              network_name: str | None = None, person_count: int | None = None,
              plan: list[str] | None = None, action: dict | None = None,
              details: dict | None = None, **values) -> dict:
        """Write `event` with its placeholders filled from `values`. An event without its
        own step applies to `step`, else to the step the run is on. `action` adds fields
        (a command, a URL) to the event's action; `details` keeps what the agent reads."""
        prose, message, note = render(event, values)
        if prose.step is None and step is None:
            step = InstallStep(self.read()["step"])
        step = prose.step or step
        status = prose.state
        # Details and extra fields travel in the action. One with no kind of its own is an
        # error when the step stopped, otherwise just the details.
        kind = prose.action or ("error" if prose.state in (InstallState.WAITING, InstallState.FAILED) else "details")
        action = ({"kind": kind, **(action or {}), **({"details": details} if details else {})}
                  if prose.action or action or details else None)
        previous = {} if event == "install.preparing_mac" else self.read()
        steps = previous.get("steps", {})
        previous_step = previous.get("step")
        if (previous_step != step and previous.get("status") == InstallState.RUNNING
                and previous_step not in PROCESSING_STEPS):
            steps[previous_step] = {"status": InstallState.COMPLETED.value,
                                    "message": previous["message"]}
        if step is InstallStep.SOURCES:
            steps = {key: value for key, value in steps.items() if key not in PROCESSING_STEPS}
        steps[step.value] = {"status": status.value, "message": message}
        manifest = _InstallManifest(step=step, status=status, event=event, message=message,
                                    installer_pid=pid, log_path=str(self.log_path),
                                    retry_command=retry_command, steps=steps,
                                    plan=plan if plan is not None else previous.get("plan", list(DEFAULT_PLAN)),
                                    action=action, note=note,
                                    account_email=account_email if account_email is not None else previous.get("account_email"),
                                    network_name=network_name if network_name is not None else previous.get("network_name"),
                                    person_count=person_count if person_count is not None else previous.get("person_count"))
        temporary = self.directory / "manifest.tmp"
        payload = write_stage_manifest(temporary, manifest)
        temporary.replace(self.manifest_path)
        return payload

    def read(self) -> dict:
        if not self.manifest_path.exists():
            return self._unwritten("install.starting")
        try:
            record = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            manifest = _InstallManifest.from_record(record)
            record["steps"] = manifest.steps
            record["plan"] = manifest.plan
            record["prose"] = page_prose()
            record["action"] = manifest.action
            record["note"] = manifest.note
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return self._unwritten("install.unreadable")
        if manifest.status is InstallState.RUNNING or (
            manifest.status is InstallState.WAITING and manifest.installer_pid > 0
        ):
            try:
                os.kill(manifest.installer_pid, 0)
            except ProcessLookupError:
                prose, message, _ = render("setup.paused", {})
                record.update(status=prose.state.value, event="setup.paused", installer_pid=0, message=message,
                              note="", action={"kind": prose.action, "command": manifest.retry_command})
                record.setdefault("steps", {})[manifest.step.value] = {"status": prose.state.value, "message": message}
        return record

    def _unwritten(self, event: str) -> dict:
        """What a read reports before the installer has written, or when it cannot be read."""
        prose, message, _ = render(event, {})
        return _InstallManifest(step=prose.step, status=prose.state, event=event, message=message,
                                installer_pid=0, log_path=str(self.log_path),
                                retry_command="bin/bootstrap").to_payload()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["write"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--step", type=InstallStep, choices=list(InstallStep),
                        help="the step for an event that has none of its own")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--retry-command", default="bin/bootstrap")
    args = parser.parse_args()
    emit(InstallStatus(args.root).write(args.event, step=args.step, pid=args.pid, retry_command=args.retry_command))


if __name__ == "__main__":
    main()
