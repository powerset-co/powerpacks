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
from packs.powerset.primitives.install.status import InstallState, InstallStatus, InstallStep


def run_with_progress(root: Path, step: InstallStep, message: str, command: list[str],
                      run: Callable[[], int], *, complete: bool = True) -> int:
    status = InstallStatus(root)
    if not status.manifest_path.exists():
        return run()
    retry = shlex.join(command)

    def write(state: InstallState, text: str, current: InstallStep = step) -> None:
        status.write(step=current, status=state, message=text, pid=os.getpid(), retry_command=retry)

    write(InstallState.RUNNING, message)
    try:
        code = run()
    except BaseException:
        write(InstallState.FAILED, "This step stopped. I can check the error and retry.")
        raise
    if code:
        write(InstallState.WAITING if code in (10, EXIT_NEEDS_APPROVAL) else InstallState.FAILED,
              "Waiting for approval" if code == EXIT_NEEDS_APPROVAL else "This step needs attention. I can check it and continue.")
    elif not complete:
        write(InstallState.WAITING, "Ready to continue")
    else:
        write(InstallState.COMPLETED, "Done")
        next_step = {InstallStep.DEEP_CONTEXT: InstallStep.INDEX, InstallStep.INDEX: InstallStep.VALIDATE,
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

    code = run_with_progress(Path.cwd(), InstallStep.DEEP_CONTEXT, {
        "collect": "Reading your messages", "synthesize": "Learning about your contacts",
        "enrich": "Working on your network", "realize": "Preparing your network",
    }.get(args.command, "Preparing your contacts"), command, run, complete=args.command == "realize")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
