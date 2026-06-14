#!/usr/bin/env python3
"""Pull local runtime keys from the Powerset API using the Auth0 login — no GCP.

The local machine is a thin dispatcher: heavy work runs on Modal (which holds
RapidAPI/Parallel/etc. as workspace secrets), so the laptop only needs a Modal
token (to dispatch) and an OpenAI key (local search LLM steps). Both are pulled
from the authenticated Powerset API with the user's Auth0 bearer:

    GET {API}/v2/integrations/modal/token  -> {"modal_token_id", "modal_token_secret"}
    GET {API}/v2/integrations/openai/key   -> {"openai_api_key"}

Endpoints are read-only and never mint: a 404/403 means "not provisioned for
this user" (an admin provisions out of band). Pulled values are written to
.env (upsert, preserving other lines, mode 0600). No gcloud, no Secret Manager.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
AUTH_SCRIPT = REPO / "packs/powerset/primitives/auth/auth.py"

DEFAULT_API_BASE = "https://search-api-7wk4uhe77q-uw.a.run.app"
API_BASE_ENV_KEYS = ("POWERPACKS_API_URL", "POWERSET_API_URL", "POWERPACKS_SEARCH_API_URL")

# env var -> (endpoint path, response field). The only keys the local machine
# needs once processing runs on Modal. Extend this map to pull more.
KEY_SOURCES: dict[str, tuple[str, str]] = {
    "MODAL_TOKEN_ID": ("/v2/integrations/modal/token", "modal_token_id"),
    "MODAL_TOKEN_SECRET": ("/v2/integrations/modal/token", "modal_token_secret"),
    "OPENAI_API_KEY": ("/v2/integrations/openai/key", "openai_api_key"),
}
ALLOWED_KEYS = set(KEY_SOURCES)


def emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def api_base() -> str:
    for key in API_BASE_ENV_KEYS:
        value = (os.environ.get(key) or "").strip()
        if value:
            return value.rstrip("/")
    return DEFAULT_API_BASE


def bearer_token() -> str:
    """Fresh Auth0 access token via auth.py (auto-refreshes); raises if signed out."""
    proc = subprocess.run(
        [sys.executable, str(AUTH_SCRIPT), "token", "--bearer-only"],
        capture_output=True, text=True,
    )
    token = (proc.stdout or "").strip()
    if proc.returncode != 0 or not token:
        raise SystemExit("not signed in to Powerset; run `$powerset login` first")
    return token


def fetch_endpoint(base: str, path: str, token: str, timeout: int = 30) -> tuple[str, dict | None]:
    """Return (state, payload). state: ok | not_provisioned | error."""
    req = urllib.request.Request(
        base + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return "ok", json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):
            return "not_provisioned", None
        return "error", {"http_status": exc.code}
    except urllib.error.URLError as exc:
        return "error", {"reason": str(exc.reason)}


def _quote(value: str) -> str:
    return f'"{value}"' if any(c in value for c in ' \t#"\'') else value


def write_env(path: Path, updates: dict[str, str]) -> list[str]:
    """Upsert keys, preserving comments/order/other keys. Mode 0600, atomic."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    index: dict[str, int] = {}
    for i, line in enumerate(lines):
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            index[s.split("=", 1)[0]] = i
    written: list[str] = []
    for key, value in updates.items():
        if not value:
            continue
        rendered = f"{key}={_quote(value)}"
        if key in index:
            lines[index[key]] = rendered
        else:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(rendered)
        written.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return written


def cmd_pull(args: argparse.Namespace) -> int:
    base = api_base()
    token = bearer_token()
    # Group keys by endpoint so each is fetched once.
    by_path: dict[str, list[str]] = {}
    for key, (path, _) in KEY_SOURCES.items():
        by_path.setdefault(path, []).append(key)

    values: dict[str, str] = {}
    endpoints: dict[str, str] = {}
    for path, keys in by_path.items():
        state, payload = fetch_endpoint(base, path, token)
        endpoints[path] = state
        if state == "ok" and payload:
            for key in keys:
                field = KEY_SOURCES[key][1]
                if payload.get(field):
                    values[key] = str(payload[field])

    env_path = Path(args.env_file)
    written = write_env(env_path, values) if values else []
    missing = [k for k in KEY_SOURCES if k not in values]
    status = "ok" if not missing else ("partial" if written else "not_provisioned")
    emit({
        "primitive": "pull_runtime_keys",
        "command": "pull",
        "status": status,
        "api_base": base,
        "endpoints": endpoints,
        "written": written,
        "missing": missing,
        "env_file": str(env_path),
    })
    return 0 if written else 2


def cmd_check(args: argparse.Namespace) -> int:
    env_path = Path(args.env_file)
    present = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                present[s.split("=", 1)[0]] = True
    have = [k for k in KEY_SOURCES if present.get(k)]
    missing = [k for k in KEY_SOURCES if not present.get(k)]
    emit({
        "primitive": "pull_runtime_keys",
        "command": "check",
        "status": "ok" if not missing else "missing",
        "have": have,
        "missing": missing,
        "env_file": str(env_path),
    })
    return 0 if not missing else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    default_env = str(REPO / ".env")
    pull = sub.add_parser("pull", help="fetch Modal token + OpenAI key from the API into .env")
    pull.add_argument("--env-file", default=default_env)
    pull.set_defaults(func=cmd_pull)
    check = sub.add_parser("check", help="report which runtime keys are present in .env")
    check.add_argument("--env-file", default=default_env)
    check.set_defaults(func=cmd_check)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
