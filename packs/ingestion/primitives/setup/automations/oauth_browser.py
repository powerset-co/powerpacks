"""Browser automation for Google OAuth Desktop app creation.

Drives Google Console in Chrome through the sibling google_oauth_browser.js
(playwright-core over a persistent profile): the create-OAuth-app flow that
downloads the client secret JSON, and the add-test-users flow. Also builds
the manual fallback instructions payload and locates downloaded
client_secret*.json files.

Changelog:
  2026-07-23 (audit):
    - Split out of the former 1,770-line setup/msgvault_setup.py;
      google_oauth_browser.js moved here alongside its driver.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

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
)
from packs.ingestion.primitives.setup.automations.shell import (  # noqa: E402
    parse_json_fragment,
    progress,
    run_command,
    run_streaming_command,
    tail,
)


DEFAULT_BROWSER_PROFILE = Path("~/.powerpacks/browser-profiles/google-oauth")
DEFAULT_DOWNLOAD_DIR = Path("~/.msgvault/oauth-downloads")
DEFAULT_NODE_DEPS = Path("~/.powerpacks/browser-node")
DEFAULT_OAUTH_CLIENT_NAME = "local-msg-vault"
BROWSER_SCRIPT = Path(__file__).with_name("google_oauth_browser.js")


def ensure_playwright_core(node_deps: Path = DEFAULT_NODE_DEPS.expanduser()) -> dict[str, Any]:
    """Ensure playwright-core is npm-installed under the node deps prefix."""
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
    if not result["ok"]:
        return {"status": "error", "message": tail(result.get("stderr") or result.get("stdout") or "")}
    progress("Browser automation runtime ready.")
    return {
        "status": "ok",
        "installed": True,
        "node_path": str(node_deps / "node_modules"),
    }


def run_browser_automation(
    *,
    project: str,
    email: str,
    oauth_client_name: str,
    profile_dir: Path,
    download_dir: Path,
    timeout_seconds: int,
    audience: str,
) -> dict[str, Any]:
    """Drive google_oauth_browser.js to create the OAuth app and download its secret."""
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
    result = run_streaming_command(cmd, timeout=timeout_seconds + 180, env=env)
    payload: dict[str, Any]
    try:
        payload = parse_json_fragment(result.get("stdout", ""))
    except json.JSONDecodeError:
        payload = {
            "status": "error",
            "message": tail(result.get("stderr") or result.get("stdout") or ""),
        }
    if not result["ok"] and payload.get("status") == "ok":
        payload["status"] = "error"
    payload.setdefault("returncode", result["returncode"])
    if result.get("stderr"):
        payload.setdefault("log", tail(result["stderr"]))
    payload.setdefault("browser_deps", deps)
    if payload.get("status") == "ok":
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
    result = run_streaming_command(cmd, timeout=timeout_seconds + 180, env=env)
    try:
        payload: dict[str, Any] = parse_json_fragment(result.get("stdout", ""))
    except json.JSONDecodeError:
        payload = {
            "status": "error",
            "message": tail(result.get("stderr") or result.get("stdout") or ""),
        }
    if not result["ok"] and payload.get("status") == "ok":
        payload["status"] = "error"
    payload.setdefault("returncode", result["returncode"])
    if result.get("stderr"):
        payload.setdefault("log", tail(result["stderr"]))
    payload.setdefault("browser_deps", deps)
    if payload.get("status") == "ok":
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


def latest_client_secret(paths: list[Path]) -> Path | None:
    """Return the newest client_secret*.json among the given files/directories."""
    matches: list[Path] = []
    for base in paths:
        if not base.exists():
            continue
        if base.is_file() and base.name.startswith("client_secret") and base.suffix == ".json":
            matches.append(base)
        elif base.is_dir():
            matches.extend(base.glob("client_secret*.json"))
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)
