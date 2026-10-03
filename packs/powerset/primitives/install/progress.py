"""Keep existing processing commands on the installation page."""
from __future__ import annotations

import argparse
import os
import runpy
import shlex
import sys
from pathlib import Path
from typing import Callable

from packs.ingestion.primitives.common.gates import EXIT_NEEDS_APPROVAL
from packs.ingestion.primitives.deep_context.db.readiness import CANONICAL_DB, has_parents
from packs.powerset.primitives.install.status import (
    PROCESSING_STEPS, InstallState, InstallStatus, InstallStep,
)


PROCESSING_COMMANDS = {
    "ensure-parents": (InstallStep.DEEP_CONTEXT, "Discovering your contacts"),
    "seed": (InstallStep.DEEP_CONTEXT, "Discovering your contacts"),
    "collect": (InstallStep.DEEP_CONTEXT, "Reading your messages"),
    "synthesize": (InstallStep.DEEP_CONTEXT, "Learning about your contacts"),
    "compose": (InstallStep.DEEP_CONTEXT, "Preparing your contacts"),
    "cluster": (InstallStep.DEEP_CONTEXT, "Combining duplicate contacts"),
    "parents": (InstallStep.DEEP_CONTEXT, "Preparing your contacts"),
    "enrich": (InstallStep.ENRICH, "Enriching your contacts"),
    "finish-reviews": (InstallStep.ENRICH, "Checking identities"),
    "assemble-synthetic": (InstallStep.ENRICH, "Preparing identities"),
    "profile-prefetch": (InstallStep.ENRICH, "Preparing profiles"),
    "reconcile-deep-research": (InstallStep.ENRICH, "Researching identities"),
    "review": (InstallStep.REVIEW, "Waiting for your review"),
    "review-status": (InstallStep.REVIEW, "Waiting for your review"),
    "realize": (InstallStep.INDEX, "Preparing your search index"),
}


def _next_action(path: Path) -> str:
    from packs.ingestion.primitives.deep_context.db.store import open_existing_db
    from packs.ingestion.primitives.deep_context.db.workflow_views import workflow_state
    return workflow_state(open_existing_db(path)).next_action


def run_with_progress(root: Path, step: InstallStep, message: str, command: list[str],
                      run: Callable[[], int], *, complete: bool = True,
                      db_path: Path | None = None) -> int:
    status = InstallStatus(root)
    if not status.manifest_path.exists():
        return run()
    previous = status.read()
    db_path = (db_path or root / CANONICAL_DB).resolve()
    if step is InstallStep.REVIEW and (
        not has_parents(db_path) or (
            _next_action(db_path) != "review_linkedin" and previous["step"] != InstallStep.REVIEW
        )
    ):
        return run()
    plan = [item for item in previous["plan"] if item not in PROCESSING_STEPS]
    plan.extend(item.value for item in PROCESSING_STEPS
                if item is not InstallStep.REVIEW or step is InstallStep.REVIEW or "review" in previous["plan"])
    retry = shlex.join(command)

    def write(state: InstallState, text: str, current: InstallStep = step, *, action: dict | None = None) -> None:
        status.write(step=current, status=state, message=text, pid=os.getpid(),
                     retry_command=retry, plan=plan, action=action)

    if step is InstallStep.ENRICH:
        write(InstallState.COMPLETED, "Done", InstallStep.DEEP_CONTEXT)
    review_action = {"kind": "review", "text": "Review the matches that need your input."}
    write(InstallState.WAITING if step is InstallStep.REVIEW else InstallState.RUNNING, message,
          action=review_action if step is InstallStep.REVIEW else None)
    try:
        code = run()
        action = _next_action(db_path) if not code and step in (InstallStep.ENRICH, InstallStep.REVIEW) else None
    except BaseException:
        write(InstallState.FAILED, "This step stopped. I can check the error and retry.")
        raise
    if code:
        write(InstallState.WAITING if code in (10, EXIT_NEEDS_APPROVAL) else InstallState.FAILED,
              "Waiting for approval" if code == EXIT_NEEDS_APPROVAL else "This step needs attention. I can check it and continue.")
        return code
    if action == "review_linkedin":
        if InstallStep.REVIEW not in plan:
            plan.insert(plan.index(InstallStep.INDEX), InstallStep.REVIEW.value)
        if step is InstallStep.ENRICH:
            write(InstallState.COMPLETED, "Done")
        write(InstallState.WAITING, "Waiting for your review", InstallStep.REVIEW, action=review_action)
    elif action == "realize":
        write(InstallState.COMPLETED, "Done")
        if step is InstallStep.ENRICH and previous["steps"].get("review", {}).get("status") == InstallState.WAITING:
            write(InstallState.COMPLETED, "Done", InstallStep.REVIEW)
        write(InstallState.WAITING, "Ready to continue", InstallStep.INDEX)
    elif action or not complete:
        current = step if step is InstallStep.ENRICH else {
            "enrich": InstallStep.ENRICH, "synthesize": InstallStep.DEEP_CONTEXT,
        }.get(action, step)
        write(InstallState.WAITING, "Ready to continue", current)
    else:
        write(InstallState.COMPLETED, "Done")
        next_step = {InstallStep.DEEP_CONTEXT: InstallStep.ENRICH, InstallStep.INDEX: InstallStep.VALIDATE,
                     InstallStep.VALIDATE: InstallStep.READY}[step]
        write(InstallState.COMPLETED if next_step is InstallStep.READY else InstallState.WAITING,
              "Your search is ready" if next_step is InstallStep.READY else "Ready to continue", next_step)
    return code


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", required=True)
    parser.add_argument("module", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    module, *arguments = args.module[1:]
    command = [str(Path.cwd() / "bin/deep-context"), args.command, *arguments]

    def run() -> int:
        sys.argv = [module, *arguments]
        try:
            runpy.run_module(module, run_name="__main__", alter_sys=True)
        except SystemExit as error:
            if error.code is None or isinstance(error.code, int):
                return error.code or 0
            raise
        return 0

    if (args.command not in PROCESSING_COMMANDS or any(flag in arguments for flag in ("--help", "-h", "--dry-run"))
            or args.command == "profile-prefetch" and "--fetch" not in arguments):
        raise SystemExit(run())
    db_parser = argparse.ArgumentParser(add_help=False)
    db_parser.add_argument("--db", type=Path, default=Path.cwd() / CANONICAL_DB)
    db_args, _ = db_parser.parse_known_args(arguments)
    step, message = PROCESSING_COMMANDS[args.command]
    raise SystemExit(run_with_progress(Path.cwd(), step, message, command, run,
                                      complete=False, db_path=db_args.db))


if __name__ == "__main__":
    main()
