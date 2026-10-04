"""Installer-owned progress, read by the local status page before dependencies exist."""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import emit
from packs.ingestion.primitives.common.manifests import write_stage_manifest


class InstallState(str, Enum):
    RUNNING = "running"
    WAITING = "waiting"
    FAILED = "failed"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class InstallStep(str, Enum):
    RUNTIME = "runtime"
    DEPENDENCIES = "dependencies"
    SKILLS = "skills"
    ACCOUNT = "account"
    CREDENTIALS = "credentials"
    CONNECTION = "connection"
    NETWORK = "network"
    SOURCES = "sources"
    GMAIL_TOOLS = "gmail_tools"
    GMAIL_LOGIN = "gmail_login"
    GMAIL_SYNC = "gmail_sync"
    GMAIL_IMPORT = "gmail_import"
    IMESSAGE_ACCESS = "imessage_access"
    IMESSAGE_IMPORT = "imessage_import"
    WHATSAPP_TOOLS = "whatsapp_tools"
    WHATSAPP_LOGIN = "whatsapp_login"
    WHATSAPP_SYNC = "whatsapp_sync"
    WHATSAPP_IMPORT = "whatsapp_import"
    LINKEDIN = "linkedin"
    DEEP_CONTEXT = "deep_context"
    ENRICH = "enrich"
    REVIEW = "review"
    INDEX = "index"
    VALIDATE = "validate"
    READY = "ready"


STEP_LABELS = {
    "runtime": "Prepare your Mac", "dependencies": "Install Powerpacks",
    "skills": "Add your skills",
    "account": "Sign in", "credentials": "Connect search",
    "connection": "Connect your agent", "network": "Check your network",
    "sources": "Choose your contacts", "gmail_tools": "Prepare Gmail",
    "gmail_login": "Connect Gmail", "gmail_sync": "Sync Gmail",
    "gmail_import": "Add Gmail contacts", "imessage_access": "Connect iMessage",
    "imessage_import": "Add iMessage contacts", "whatsapp_tools": "Prepare WhatsApp",
    "whatsapp_login": "Link WhatsApp", "whatsapp_sync": "Sync WhatsApp",
    "whatsapp_import": "Add WhatsApp contacts", "linkedin": "Sync LinkedIn",
    "deep_context": "Discovering your contacts", "enrich": "Enriching your contacts",
    "review": "Waiting for your review",
    "index": "Build your search index",
    "validate": "Check your search", "ready": "Ready",
}
DEFAULT_PLAN = ["runtime", "dependencies", "skills", "account", "credentials", "connection", "network"]
PROCESSING_STEPS = (InstallStep.DEEP_CONTEXT, InstallStep.ENRICH, InstallStep.REVIEW,
                    InstallStep.INDEX, InstallStep.VALIDATE, InstallStep.READY)


@dataclass(frozen=True)
class _InstallManifest:
    step: InstallStep
    status: InstallState
    message: str
    installer_pid: int
    log_path: str
    retry_command: str
    steps: dict[str, dict[str, str]] = field(default_factory=dict)
    plan: list[str] = field(default_factory=lambda: list(DEFAULT_PLAN))
    action: dict | None = None
    account_email: str | None = None
    network_name: str | None = None
    person_count: int | None = None
    primitive: str = "powerpacks_install"

    def to_payload(self) -> dict:
        return {**asdict(self), "labels": STEP_LABELS}

    @classmethod
    def from_record(cls, record: dict) -> "_InstallManifest":
        return cls(step=InstallStep(record["step"]), status=InstallState(record["status"]),
                   message=record["message"], installer_pid=int(record["installer_pid"]),
                   log_path=record["log_path"], retry_command=record["retry_command"],
                   steps=record.get("steps", {}), plan=record.get("plan", list(DEFAULT_PLAN)),
                   action=record.get("action"), account_email=record.get("account_email"),
                   network_name=record.get("network_name"), person_count=record.get("person_count"))


class InstallStatus:
    def __init__(self, root: Path) -> None:
        self.directory = Path(root).resolve() / ".powerpacks/install"
        self.manifest_path = self.directory / "manifest.json"
        self.log_path = self.directory / "install.log"

    def write(self, *, step: InstallStep, status: InstallState, message: str, pid: int,
              retry_command: str = "bin/bootstrap", account_email: str | None = None,
              network_name: str | None = None, person_count: int | None = None,
              plan: list[str] | None = None, action: dict | None = None) -> dict:
        previous = {} if step is InstallStep.RUNTIME and status is InstallState.RUNNING else self.read()
        steps = previous.get("steps", {})
        previous_step = previous.get("step")
        if (previous_step != step and previous.get("status") == InstallState.RUNNING
                and previous_step not in PROCESSING_STEPS):
            steps[previous_step] = {"status": InstallState.COMPLETED.value,
                                    "message": previous["message"]}
        if step is InstallStep.SOURCES:
            steps = {key: value for key, value in steps.items() if key not in PROCESSING_STEPS}
        steps[step.value] = {"status": status.value, "message": message}
        manifest = _InstallManifest(step=step, status=status, message=message,
                                    installer_pid=pid, log_path=str(self.log_path),
                                    retry_command=retry_command, steps=steps,
                                    plan=plan if plan is not None else previous.get("plan", list(DEFAULT_PLAN)),
                                    action=action,
                                    account_email=account_email if account_email is not None else previous.get("account_email"),
                                    network_name=network_name if network_name is not None else previous.get("network_name"),
                                    person_count=person_count if person_count is not None else previous.get("person_count"))
        temporary = self.directory / "manifest.tmp"
        payload = write_stage_manifest(temporary, manifest)
        temporary.replace(self.manifest_path)
        return payload

    def read(self) -> dict:
        if not self.manifest_path.exists():
            return _InstallManifest(step=InstallStep.RUNTIME, status=InstallState.WAITING,
                                    message="Starting Powerpacks",
                                    installer_pid=0, log_path=str(self.log_path),
                                    retry_command="bin/bootstrap").to_payload()
        try:
            record = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            manifest = _InstallManifest.from_record(record)
            record["steps"] = manifest.steps
            record["plan"] = manifest.plan
            record["labels"] = STEP_LABELS
            record["action"] = manifest.action
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return _InstallManifest(step=InstallStep.RUNTIME, status=InstallState.FAILED,
                                    message="Installation status could not be read. Ask the agent to check the installation log and rerun bin/bootstrap.",
                                    installer_pid=0, log_path=str(self.log_path),
                                    retry_command="bin/bootstrap").to_payload()
        if manifest.status is InstallState.RUNNING or (
            manifest.status is InstallState.WAITING and manifest.installer_pid > 0
        ):
            try:
                os.kill(manifest.installer_pid, 0)
            except ProcessLookupError:
                record["status"] = InstallState.WAITING.value
                record["installer_pid"] = 0
                record["message"] = "Setup paused. I can resume it from here."
                record["action"] = {"kind": "resume", "text": record["message"],
                                    "command": manifest.retry_command}
                record.setdefault("steps", {})[manifest.step.value] = {
                    "status": InstallState.WAITING.value, "message": record["message"],
                }
        return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["write"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--step", type=InstallStep, choices=list(InstallStep), required=True)
    parser.add_argument("--status", type=InstallState, choices=list(InstallState), required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--retry-command", default="bin/bootstrap")
    args = parser.parse_args()
    emit(InstallStatus(args.root).write(step=args.step, status=args.status, message=args.message,
                                      pid=args.pid, retry_command=args.retry_command))


if __name__ == "__main__":
    main()
