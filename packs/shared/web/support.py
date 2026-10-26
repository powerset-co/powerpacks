"""Answer Powerset support requests serially, keeping Codex reports for send retries."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso, write_json
from packs.powerset.primitives.agent_inbox import agent_inbox
from packs.powerset.primitives.agent_inbox.messages import MAX_RESULT, SupportRequest, SupportResult
from packs.powerset.primitives.install.controller import permission_app
from packs.shared.web import account, features

GUIDE = Path("packs/shared/skills/remote-assist.md")
REPORTS = Path(".powerpacks/desktop/support")
CODEX_HOME = Path(".powerpacks/desktop/codex")
RUN_TIMEOUT_SECONDS = 60 * 60
_LOG = logging.getLogger(__name__)


def _thread(message: dict, repo_root: Path) -> str:
    previous = []
    for path in (repo_root / ".powerpacks/inbox").glob("*.json"):
        row = json.loads(path.read_text(encoding="utf-8"))
        if (row["kind"] not in ("support_request", "support_result") or row["id"] == message["id"]
                or row["from"]["operator_id"] != message["from"]["operator_id"]
                or row["payload"]["thread_id"] != message["payload"]["thread_id"]
                or (row["created_at"], row["id"]) > (message["created_at"], message["id"])):
            continue
        previous.append(row)
    lines = []
    for row in sorted(previous, key=lambda row: (row["created_at"], row["id"])):
        lines.append(f"{row['kind']}: {row['payload']['text']}")
        report = repo_root / REPORTS / f"{row['id']}.md"
        if row["kind"] == "support_request" and report.exists():
            lines.append(f"support_result: {report.read_text(encoding='utf-8')}")
    return "\n\n".join(lines)


def _run(message: dict, repo_root: Path) -> str:
    report = repo_root / REPORTS / f"{message['id']}.md"
    if report.exists():
        return report.read_text(encoding="utf-8")
    report.parent.mkdir(parents=True, exist_ok=True)
    home = repo_root / CODEX_HOME
    home.mkdir(parents=True, exist_ok=True)
    prompt = (repo_root / GUIDE).read_text(encoding="utf-8")
    prompt += f"\n\nThread so far:\n{_thread(message, repo_root)}\n\nRequest:\n{message['payload']['text']}"
    app = permission_app() if sys.platform == "darwin" else None
    bundled = Path(app) / "Contents/MacOS/codex" if app else None
    binary = str(bundled) if bundled and bundled.is_file() else "codex"
    with report.with_suffix(".log").open("w", encoding="utf-8") as log:
        try:
            subprocess.run(
                [binary, "-c", "cli_auth_credentials_store=file",
                 "-c", "sandbox_workspace_write.network_access=true",
                 "exec", "--skip-git-repo-check", "--sandbox", "workspace-write",
                 # .powerpacks is often a link to a folder outside the checkout: name its real path.
                 "--add-dir", str((repo_root / ".powerpacks").resolve()),
                 # uv keeps its cache here; without it every bin/* command dies on "Failed to initialize cache".
                 "--add-dir", str(Path.home() / ".cache/uv"),
                 "--output-last-message", str(report), prompt],
                cwd=repo_root, env={**os.environ, "CODEX_HOME": str(home)},
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, timeout=RUN_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            _LOG.warning("Support Codex: %s", type(error).__name__)
    if not report.exists() or not report.read_text(encoding="utf-8").strip():
        report.write_text("The support run did not finish. Check its local support log.", encoding="utf-8")
    return report.read_text(encoding="utf-8")


def answer_pending(*, repo_root: Path, env_file: Path) -> None:
    """Called by one worker; newly pulled requests wait on disk while Codex runs."""
    if not account.is_team(account.read(repo_root)["email"]):
        return
    messages = [json.loads(path.read_text(encoding="utf-8"))
                for path in (repo_root / ".powerpacks/inbox").glob("*.json")]
    for message in sorted(messages, key=lambda row: (row["created_at"], row["id"])):
        if message["kind"] != "support_request" or "answered_at" in message:
            continue
        path = repo_root / ".powerpacks/inbox" / f"{message['id']}.json"
        if not account.is_team(message["from"].get("email")):
            _LOG.warning("Support request %s refused: sender is not @powerset.co", message["id"])
            write_json(path, {**message, "answered_at": now_iso(), "refused": "not @powerset.co"})
            continue
        request = SupportRequest.parse(message["payload"])
        try:
            text = (_run(message, repo_root) if features.read(repo_root).support
                    else "Remote support is off in Settings on this install.")
            result = SupportResult(text[:MAX_RESULT], request.thread_id, message["id"])
            agent_inbox.send(env_file, message["from"]["operator_id"], "support_result", asdict(result))
        except (SystemExit, Exception) as error:
            _LOG.warning("Support request %s: %s", message["id"], type(error).__name__)
            return
        write_json(path, {**message, "answered_at": now_iso()})
        # An update or restart is handed to the app only now, after the result is sent; the app is
        # about to relaunch, so the next request waits for the new process.
        handed = False
        for name in ("update-requested", "restart-requested"):
            staged = repo_root / ".powerpacks/support" / name
            if staged.exists():
                target = repo_root / ".powerpacks/desktop" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                staged.replace(target)
                handed = True
        if handed:
            return
