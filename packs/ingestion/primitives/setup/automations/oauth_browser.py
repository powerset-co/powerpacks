"""Browser automation for Google OAuth app setup and Gmail consent.

Drives Google Console in Chrome through the sibling google_oauth_browser.js
(playwright-core over a persistent profile): the create-OAuth-app flow that
downloads the client secret JSON, the add-test-users flow, and msgvault account
consent. Sign-in and challenges use a visible window in the same profile.

Changelog:
  2026-10-09: Use the bundled playwright-core before command-line npm dependencies.
  2026-09-23 (typed rows): `browser_status` / `browser_client_secret_path` are the
    named boundary for the browser script's JSON fields, so `browser_flows` reads
    typed values instead of `.get`-ing the payload; the subprocess result reads
    through `CommandResult`.
  2026-07-29 (setup style pass): DELETED `latest_client_secret` — the browser
    script reports the file it downloaded as `client_secret_path` and
    `browser_flows` reads that, so nothing has scanned the download directory
    for a newest match since the flow was extracted. Error text reads through
    `shell.command_error`.
  2026-07-23 (audit):
    - Split out of the former 1,770-line setup/msgvault_setup.py;
      google_oauth_browser.js moved here alongside its driver.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import tomllib
from pathlib import Path
from typing import Any, Callable

# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.setup.automations.gcloud_project import (  # noqa: E402
    GMAIL_SCOPES,
    console_urls,
)
from packs.ingestion.primitives.setup.automations.msgvault_home import (  # noqa: E402
    DEFAULT_HOME,
    configured_client_secret,
    config_path,
)
from packs.ingestion.primitives.setup.automations.shell import (  # noqa: E402
    command_error,
    parse_json_fragment,
    progress,
    run_command,
    run_streaming_command,
    tail,
)


DEFAULT_BROWSER_PROFILE = Path("~/.powerpacks/browser-profiles/google-oauth")
DEFAULT_DOWNLOAD_DIR = Path("~/.msgvault/oauth-downloads")
DEFAULT_NODE_DEPS = Path("~/.powerpacks/browser-node")
VENDORED_NODE_MODULES = _REPO_ROOT / "vendor/browser-node/node_modules"
DEFAULT_OAUTH_CLIENT_NAME = "local-msg-vault"
BROWSER_SCRIPT = Path(__file__).with_name("google_oauth_browser.js")


def authorize_account(
    home: Path, email: str, app_name: str, *, force: bool,
    profile_dir: Path = DEFAULT_BROWSER_PROFILE.expanduser(), timeout_seconds: int = 900,
    on_progress: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """Run msgvault's callback server and consent in the saved Google profile."""
    configured = configured_client_secret(home, app_name)
    if not configured or configured["status"] != "configured":
        return {"status": "error", "message": "msgvault OAuth client is not configured."}
    config = tomllib.loads(config_path(home).read_text())
    data_dir = Path(config.get("data", {}).get("data_dir", str(home))).expanduser()
    if not data_dir.is_absolute():
        data_dir = home / data_dir
    safe_email = email.replace("/", "_").replace("\\", "_").replace("..", "_")
    token = data_dir / "tokens" / f"{safe_email}.json"
    backup = token.with_suffix(".json.bkup")
    saved_token = force and token.exists()
    if saved_token:
        while backup.exists():
            backup = backup.with_name(backup.name + ".bkup")
        token.replace(backup)
    proc = None
    success = False
    try:
        with tempfile.TemporaryDirectory(prefix="msgvault-browser-") as temporary:
            # msgvault v0.14 calls `open` unconditionally. Only its child PATH
            # suppresses that launch; the browser below owns this OAuth URL.
            opener = Path(temporary) / "open"
            opener.write_text("#!/bin/sh\nexit 0\n")
            opener.chmod(0o700)
            cmd = ["msgvault", "--home", str(home), "--local", "--no-log-file", "add-account", email]
            if force:
                cmd.append("--force")
            if app_name:
                cmd.extend(["--oauth-app", app_name])
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    env={**os.environ, "PATH": temporary + os.pathsep + os.environ["PATH"]})
            lines: queue.Queue[str | None] = queue.Queue()

            def read_output() -> None:
                assert proc is not None and proc.stdout is not None
                for line in proc.stdout:
                    lines.put(line)
                lines.put(None)

            threading.Thread(target=read_output, daemon=True).start()
            while True:
                line = lines.get(timeout=30)
                if line is None:
                    success = proc.wait(timeout=5) == 0 and token.exists()
                    return {"status": "ok" if success else "error",
                            "message": "" if success else "msgvault account authorization failed."}
                if not line.strip().startswith("https://accounts.google.com/"):
                    continue
                deps = ensure_playwright_core()
                if deps["status"] != "ok":
                    return deps
                request = {"url": line.strip(), "email": email, "clientId": configured["client_id"],
                           "clientName": DEFAULT_OAUTH_CLIENT_NAME, "profileDir": str(profile_dir),
                           "timeoutSeconds": timeout_seconds}
                browser = run_streaming_command(
                    ["node", str(BROWSER_SCRIPT), "--mode", "authorize"], input_text=json.dumps(request),
                    timeout=timeout_seconds + 45,
                    on_progress=on_progress,
                    env={**os.environ, "NODE_PATH": deps["node_path"]},
                )
                payload = json.loads(browser.stdout)
                if not browser.ok:
                    return {"status": "needs_user_action", "message": "Google authorization browser stopped. Retry authorization."}
                if payload["status"] != "ok":
                    return payload
                success = proc.wait(timeout=30) == 0
                success = success and token.exists()
                return {"status": "ok" if success else "error",
                        "message": "" if success else "msgvault could not save the authorized account."}
    except (OSError, ValueError, queue.Empty, subprocess.TimeoutExpired):
        return {"status": "needs_user_action", "message": "msgvault authorization timed out or could not start. Retry authorization."}
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()
        if saved_token and not success:
            if token.exists():
                failed_token = token.with_suffix(".json.bkup")
                while failed_token.exists():
                    failed_token = failed_token.with_name(failed_token.name + ".bkup")
                token.replace(failed_token)
            backup.replace(token)


def ensure_playwright_core(node_deps: Path = DEFAULT_NODE_DEPS.expanduser()) -> dict[str, Any]:
    """Use bundled playwright-core, or install command-line dependencies with npm."""
    if (VENDORED_NODE_MODULES / "playwright-core").is_dir():
        return {"status": "ok", "installed": False, "node_path": str(VENDORED_NODE_MODULES)}
    if not shutil.which("node"):
        return {"status": "error", "message": "node is not installed"}
    if not shutil.which("npm"):
        return {"status": "error", "message": "npm is not installed"}
    package_dir = node_deps / "node_modules" / "playwright-core"
    if package_dir.exists():
        return {
            "status": "ok",
            "installed": False,
            "node_path": str(node_deps / "node_modules"),
        }
    node_deps.mkdir(parents=True, exist_ok=True)
    progress("Installing browser automation runtime...")
    result = run_command(["npm", "install", "--prefix", str(node_deps), "playwright-core"], timeout=300)
    if not result.ok:
        return {"status": "error", "message": command_error(result)}
    progress("Browser automation runtime ready.")
    return {
        "status": "ok",
        "installed": True,
        "node_path": str(node_deps / "node_modules"),
    }


def browser_status(payload: dict[str, Any]) -> str:
    """The status reported by google_oauth_browser.js, "error" when it reported none."""
    return str(payload.get("status") or "error")


def browser_client_secret_path(payload: dict[str, Any]) -> str:
    """The client-secret JSON path google_oauth_browser.js downloaded, "" when none."""
    return str(payload.get("client_secret_path") or "")


def run_browser_automation(
    *,
    project: str,
    email: str,
    oauth_client_name: str,
    profile_dir: Path,
    download_dir: Path,
    timeout_seconds: int,
    audience: str,
    on_progress: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """Drive google_oauth_browser.js to create the OAuth app and download its secret;
    `on_progress` gets each stage it reaches."""
    progress("Opening Chrome to create the Google OAuth app...")
    deps = ensure_playwright_core()
    if deps["status"] != "ok":
        return deps
    download_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "node",
        str(BROWSER_SCRIPT),
        "--project",
        project,
        "--email",
        email,
        "--client-name",
        oauth_client_name,
        "--profile-dir",
        str(profile_dir),
        "--download-dir",
        str(download_dir),
        "--timeout-seconds",
        str(timeout_seconds),
        "--audience",
        audience,
    ]
    env = {**os.environ, "NODE_PATH": deps["node_path"]}
    result = run_streaming_command(cmd, timeout=timeout_seconds + 180, env=env, on_progress=on_progress)
    payload: dict[str, Any]
    try:
        payload = parse_json_fragment(result.stdout)
    except json.JSONDecodeError:
        payload = {
            "status": "error",
            "message": command_error(result),
        }
    if not result.ok and browser_status(payload) == "ok":
        payload["status"] = "error"
    payload.setdefault("returncode", result.returncode)
    if result.stderr:
        payload.setdefault("log", tail(result.stderr))
    payload.setdefault("browser_deps", deps)
    if browser_status(payload) == "ok":
        progress("Google OAuth client secret downloaded.")
    else:
        progress("Chrome is waiting for Google OAuth setup to finish.")
    return payload


def run_browser_add_test_users(
    *,
    project: str,
    email: str,
    test_users: list[str],
    profile_dir: Path,
    download_dir: Path,
    timeout_seconds: int,
    oauth_client_name: str = DEFAULT_OAUTH_CLIENT_NAME,
    on_progress: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """Drive google_oauth_browser.js in add-test-users mode for the consent screen."""
    progress("Opening Chrome to add Google OAuth test users...")
    deps = ensure_playwright_core()
    if deps["status"] != "ok":
        return deps
    download_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "node",
        str(BROWSER_SCRIPT),
        "--mode",
        "add-test-users",
        "--project",
        project,
        "--email",
        email,
        "--client-name",
        oauth_client_name,
        "--profile-dir",
        str(profile_dir),
        "--download-dir",
        str(download_dir),
        "--timeout-seconds",
        str(timeout_seconds),
        "--test-users",
        ",".join(test_users),
    ]
    env = {**os.environ, "NODE_PATH": deps["node_path"]}
    result = run_streaming_command(cmd, timeout=timeout_seconds + 180, env=env, on_progress=on_progress)
    try:
        payload: dict[str, Any] = parse_json_fragment(result.stdout)
    except json.JSONDecodeError:
        payload = {
            "status": "error",
            "message": command_error(result),
        }
    if not result.ok and browser_status(payload) == "ok":
        payload["status"] = "error"
    payload.setdefault("returncode", result.returncode)
    if result.stderr:
        payload.setdefault("log", tail(result.stderr))
    payload.setdefault("browser_deps", deps)
    if browser_status(payload) == "ok":
        progress("Google OAuth test users updated.")
    else:
        progress("Chrome is waiting for Google OAuth test user setup to finish.")
    return payload


def build_user_action(
    project: str | None,
    email: str | None,
    app_name: str,
    home: Path,
    oauth_client_name: str = DEFAULT_OAUTH_CLIENT_NAME,
) -> dict[str, Any]:
    """Build the manual OAuth-app instructions payload with a continue command."""
    urls = console_urls(project)
    cmd = [
        "uv",
        "run",
        "--project",
        ".",
        "python",
        "packs/ingestion/primitives/setup/msgvault_setup.py",
        "setup",
        "--client-secret",
        "/path/to/client_secret.json",
    ]
    if email:
        cmd.extend(["--email", email])
    if app_name:
        cmd.extend(["--oauth-app", app_name])
    if home != DEFAULT_HOME.expanduser():
        cmd.extend(["--home", str(home)])
    return {
        "message": f"Create a Google OAuth Desktop app named {oauth_client_name}, download the client secret JSON, then rerun with --client-secret.",
        "urls": urls,
        "steps": [
            "Enable the Gmail API for the selected project.",
            "Configure the OAuth consent screen. Add yourself as a test user if the app is in testing.",
            "Add Gmail OAuth scopes: " + ", ".join(GMAIL_SCOPES) + ".",
            f"Create an OAuth client named {oauth_client_name} with application type Desktop app.",
            "Download the JSON file and pass it back with --client-secret.",
        ],
        "automation_note": "Workspace accounts may require an internal OAuth audience or adding the Gmail address as a test user before authorization.",
        "instructions_url": "packs/ingestion/primitives/setup/msgvault_setup.py",
        "expected_client_type": "Desktop app",
        "oauth_client_name": oauth_client_name,
        "continue_command": " ".join(cmd),
    }
