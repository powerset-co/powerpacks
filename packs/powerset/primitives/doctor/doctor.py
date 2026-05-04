#!/usr/bin/env python3
"""Powerpacks self-setup health check.

Runs every prereq check the `powerset-login` skill needs, in one pass, and
returns a structured JSON report. Each check has a stable `id`, a `status`
of `ok | warn | missing | fail`, a human-readable `message`, and (when
applicable) a `fix_command` the agent can show the user before running.

Stdlib-only. Never prints secret values. Safe to run repeatedly.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Locate sibling primitives without importing them as modules.
SELF_DIR = Path(__file__).resolve().parent
PACK_DIR = SELF_DIR.parent
AUTH = PACK_DIR / "auth" / "auth.py"
PROVISION = PACK_DIR / "provision_runtime_env" / "provision_runtime_env.py"

DEFAULT_CREDS = Path(os.environ.get(
    "POWERPACKS_CREDENTIALS_PATH",
    str(Path.home() / ".powerpacks" / "credentials.json"),
))
DEFAULT_PROJECT = os.environ.get("POWERPACKS_GCP_PROJECT", "powerset-search")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def run(cmd: list[str], *, timeout: int = 30) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError as exc:
        return 127, "", f"command not found: {exc}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"


def check(
    id_: str,
    status: str,
    message: str,
    *,
    fix_kind: str = "none",
    fix_command: Any = None,
    fix_args: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a check record.

    `fix_kind` is the most important field for the powerset-login skill:

    - `none`              — nothing to do (status `ok` or `warn` you can ignore)
    - `auto`              — safe to run without prompting (no network, no
                            new costs, no new credentials)
    - `interactive`       — our primitive that pops a browser and waits for
                            the user (auth0 login, etc.). Safe to invoke
                            without asking each time — the user is right
                            there and the browser ask IS the consent.
    - `shell_install`     — OS-level install / package change. Always show
                            the command and ask before running.
    - `human_action`      — cannot be fixed locally (e.g. ping #powerpacks)

    `fix_args` are the argv-style command + args that `doctor fix` runs for
    `auto` and `interactive` kinds.
    """
    out = {"id": id_, "status": status, "message": message, "fix_kind": fix_kind}
    if fix_command is not None:
        out["fix_command"] = fix_command
    if fix_args is not None:
        out["fix_args"] = fix_args
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_python() -> dict[str, Any]:
    version = sys.version.split()[0]
    parts = tuple(int(p) for p in version.split(".")[:2])
    if parts >= (3, 9):
        return check("python", "ok", f"python {version}", version=version)
    return check(
        "python", "fail",
        f"python {version} is too old; need >= 3.9",
        version=version,
        fix_kind="shell_install",
        fix_command="brew install python3 && which python3",
    )


def check_gcloud_installed() -> dict[str, Any]:
    path = shutil.which("gcloud")
    if path:
        return check("gcloud_installed", "ok", "gcloud is on PATH", path=path)
    return check(
        "gcloud_installed", "missing",
        "gcloud CLI is not installed; needed to provision runtime secrets",
        fix_kind="shell_install",
        fix_command={
            "macos": "brew install --cask google-cloud-sdk",
            "linux": "curl https://sdk.cloud.google.com | bash && exec -l $SHELL",
        },
    )


def check_gcloud_account() -> dict[str, Any]:
    code, out, err = run([
        "gcloud", "auth", "list",
        "--filter=status:ACTIVE",
        "--format=value(account)",
    ])
    if code == 127:
        return check(
            "gcloud_account", "missing",
            "gcloud not installed; cannot check active account",
        )
    if code != 0 or not out.strip():
        return check(
            "gcloud_account", "missing",
            "no active gcloud account",
            fix_kind="interactive",
            fix_command="gcloud auth login",
            fix_args=["gcloud", "auth", "login"],
        )
    account = out.strip().splitlines()[0].strip()
    if not account.endswith("@powerset.co"):
        return check(
            "gcloud_account", "warn",
            f"active gcloud account {account} is not @powerset.co; switch with `gcloud config set account you@powerset.co`",
            account=account,
            fix_kind="human_action",
            fix_command="gcloud auth login  # then: gcloud config set account you@powerset.co",
        )
    return check("gcloud_account", "ok", f"signed in as {account}", account=account)


def check_gcloud_adc() -> dict[str, Any]:
    """ADC (application-default credentials) for SDK clients that don't shell out to gcloud."""
    code, out, _ = run(["gcloud", "auth", "application-default", "print-access-token"], timeout=15)
    if code == 0 and out.strip():
        return check("gcloud_adc", "ok", "application-default credentials present")
    return check(
        "gcloud_adc", "warn",
        "application-default credentials not set up; some SDK clients may need them",
        fix_kind="interactive",
        fix_command="gcloud auth application-default login",
        fix_args=["gcloud", "auth", "application-default", "login"],
    )


def check_auth0_login() -> dict[str, Any]:
    if not DEFAULT_CREDS.exists():
        return check(
            "auth0_login", "missing",
            f"no Auth0 credentials at {DEFAULT_CREDS}",
            fix_kind="interactive",
            fix_command=f"python {AUTH} login",
            fix_args=[sys.executable, str(AUTH), "login"],
        )
    code, out, _ = run([sys.executable, str(AUTH), "whoami", "--credentials-path", str(DEFAULT_CREDS)])
    payload = {}
    try:
        payload = json.loads(out) if out else {}
    except json.JSONDecodeError:
        pass
    if code != 0 or payload.get("status") != "logged_in":
        return check(
            "auth0_login", "missing",
            "Auth0 credentials missing or expired",
            fix_kind="interactive",
            fix_command=f"python {AUTH} login",
            fix_args=[sys.executable, str(AUTH), "login"],
        )
    return check(
        "auth0_login", "ok",
        f"logged in to Auth0 as {payload.get('email')}",
        email=payload.get("email"),
        seconds_remaining=payload.get("seconds_remaining"),
        expired=payload.get("expired"),
    )


def check_auth0_role() -> dict[str, Any]:
    if not DEFAULT_CREDS.exists():
        return check(
            "auth0_role", "missing",
            "cannot check role until Auth0 login is complete",
        )
    code, out, _ = run([
        sys.executable, str(AUTH), "inspect",
        "--credentials-path", str(DEFAULT_CREDS),
        "--allow-unauthorized",
    ])
    payload = {}
    try:
        payload = json.loads(out) if out else {}
    except json.JSONDecodeError:
        return check("auth0_role", "fail", "auth inspect produced no parseable output")
    authorization = payload.get("authorization")
    if authorization in ("admin", "user"):
        return check(
            "auth0_role", "ok",
            f"Auth0 role is {authorization}",
            email=payload.get("email"),
            authorization=authorization,
            roles=payload.get("roles"),
        )
    return check(
        "auth0_role", "warn",
        f"Auth0 token has no `user` or `admin` role; some primitives may refuse to run",
        email=payload.get("email"),
        roles=payload.get("roles"),
        fix_kind="human_action",
        fix_command="ping #powerpacks on Slack with your @powerset.co email; ask to be added to the Powerpacks role",
    )


def check_user_secrets(profile: str, project: str) -> dict[str, Any]:
    code, out, _ = run([
        sys.executable, str(PROVISION),
        "probe", "--profile", profile, "--gcp-project", project,
    ], timeout=120)
    try:
        payload = json.loads(out) if out else {}
    except json.JSONDecodeError:
        return check("user_secrets", "fail", "probe produced no parseable output")
    status = payload.get("status")
    if status == "ok":
        return check(
            "user_secrets", "ok",
            f"per-user secrets accessible for {payload.get('email')} ({len(payload.get('accessible', []))} keys)",
            email=payload.get("email"),
            accessible=payload.get("accessible"),
        )
    if status == "partial":
        return check(
            "user_secrets", "warn",
            f"some per-user secrets accessible, others missing or denied",
            accessible=payload.get("accessible"),
            denied=payload.get("denied"),
            not_provisioned=payload.get("not_provisioned"),
        )
    if status == "not_provisioned":
        return check(
            "user_secrets", "missing",
            f"no per-user secrets exist yet for {payload.get('email')}; ask a Powerpacks maintainer to add you",
            email=payload.get("email"),
            fix_kind="human_action",
            fix_command="ping #powerpacks on Slack with your @powerset.co email",
        )
    if status == "not_privileged":
        return check(
            "user_secrets", "missing",
            f"per-user secrets exist but you cannot read them",
            email=payload.get("email"),
            fix_kind="human_action",
            fix_command="ping #powerpacks on Slack with your @powerset.co email",
        )
    if status == "gcloud_missing":
        return check(
            "user_secrets", "missing",
            "cannot probe per-user secrets until gcloud is signed in",
        )
    return check("user_secrets", "fail", f"probe returned unexpected status: {status}")


def check_env_file(env_file: Path, profile: str) -> dict[str, Any]:
    code, out, _ = run([
        sys.executable, str(PROVISION),
        "check", "--profile", profile, "--env-file", str(env_file),
    ])
    try:
        payload = json.loads(out) if out else {}
    except json.JSONDecodeError:
        return check("env_file", "fail", f"could not parse check output for {env_file}")
    if payload.get("status") == "ok":
        return check("env_file", "ok", f"all required keys present in {env_file}")
    return check(
        "env_file", "missing",
        f"{len(payload.get('missing', []))} keys missing from {env_file}",
        missing=payload.get("missing"),
        fix_kind="auto",
        fix_command=(
            f"python {PROVISION} pull --profile {profile} --env-file {env_file}"
            f" --confirm --best-effort"
        ),
        fix_args=[
            sys.executable, str(PROVISION),
            "pull", "--profile", profile,
            "--env-file", str(env_file),
            "--confirm", "--best-effort",
        ],
    )


# ---------------------------------------------------------------------------
# Subcommand
# ---------------------------------------------------------------------------

def collect_checks(args: argparse.Namespace) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    checks.append(check_python())
    checks.append(check_gcloud_installed())

    gcloud_ok = checks[-1]["status"] == "ok"
    if gcloud_ok:
        checks.append(check_gcloud_account())
        if not args.skip_adc:
            checks.append(check_gcloud_adc())

    checks.append(check_auth0_login())
    if checks[-1]["status"] == "ok":
        checks.append(check_auth0_role())

    if gcloud_ok:
        active = next(
            (c for c in checks if c["id"] == "gcloud_account" and c["status"] == "ok"), None
        )
        if active:
            checks.append(check_user_secrets(args.profile, args.gcp_project))

    checks.append(check_env_file(args.env_file, args.profile))
    return checks


def summarize(checks: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"ok": 0, "warn": 0, "missing": 0, "fail": 0}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1

    overall = (
        "ok" if counts["fail"] == 0 and counts["missing"] == 0 and counts["warn"] == 0
        else "warn" if counts["fail"] == 0 and counts["missing"] == 0
        else "needs_setup"
    )

    by_kind: dict[str, list[dict[str, Any]]] = {"auto": [], "interactive": [], "shell_install": [], "human_action": []}
    for c in checks:
        if c["status"] not in ("missing", "fail"):
            continue
        kind = c.get("fix_kind", "none")
        if kind in by_kind:
            by_kind[kind].append(c)

    return {
        "counts": counts,
        "overall": overall,
        "by_fix_kind": {k: [c["id"] for c in v] for k, v in by_kind.items()},
        "next_actions": [c.get("fix_command") for c in checks if c["status"] in ("missing", "fail") and c.get("fix_command")],
    }


def cmd_run(args: argparse.Namespace) -> int:
    checks = collect_checks(args)
    summary = summarize(checks)
    emit({
        "primitive": "powerset_doctor",
        "command": "run",
        "checked_at": now_iso(),
        "profile": args.profile,
        "gcp_project": args.gcp_project,
        "env_file": str(args.env_file),
        "checks": checks,
        **summary,
    })
    return 0 if summary["overall"] == "ok" else 1


def cmd_fix(args: argparse.Namespace) -> int:
    """Run all safe automatic fixes; optionally also browser-flow ones.

    `auto`         — always run (no network or new credentials, e.g. pulling
                     env from already-accessible per-user secrets).
    `interactive`  — only run when --interactive is passed. These pop a
                     browser; the user is right there and the browser is
                     the consent surface.
    `shell_install`— never run automatically. The agent must surface the
                     command and ask the user.
    `human_action` — cannot be fixed locally (Slack ping, etc.).
    """
    checks_before = collect_checks(args)
    summary_before = summarize(checks_before)
    eligible_kinds = {"auto"}
    if args.interactive:
        eligible_kinds.add("interactive")

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for c in checks_before:
        if c["status"] not in ("missing", "fail"):
            continue
        kind = c.get("fix_kind", "none")
        if kind in eligible_kinds and c.get("fix_args"):
            cmd = c["fix_args"]
            print(f"[doctor] running fix for {c['id']} ({kind}): {' '.join(cmd)}", file=sys.stderr)
            code, out, err = run(cmd, timeout=600)
            applied.append({
                "id": c["id"],
                "fix_kind": kind,
                "returncode": code,
                "stderr_tail": err.strip()[-400:] if err else "",
            })
        else:
            skipped.append({
                "id": c["id"],
                "fix_kind": kind,
                "reason": (
                    "interactive (pass --interactive to allow)" if kind == "interactive"
                    else "shell install (agent must ask user before running)" if kind == "shell_install"
                    else "human action required (Slack)" if kind == "human_action"
                    else "no fix_args"
                ),
                "fix_command": c.get("fix_command"),
            })

    checks_after = collect_checks(args)
    summary_after = summarize(checks_after)

    emit({
        "primitive": "powerset_doctor",
        "command": "fix",
        "checked_at": now_iso(),
        "profile": args.profile,
        "gcp_project": args.gcp_project,
        "env_file": str(args.env_file),
        "interactive": bool(args.interactive),
        "applied": applied,
        "skipped": skipped,
        "before": {"overall": summary_before["overall"], "counts": summary_before["counts"]},
        "after": {
            "overall": summary_after["overall"],
            "counts": summary_after["counts"],
            "by_fix_kind": summary_after["by_fix_kind"],
            "next_actions": summary_after["next_actions"],
        },
        "checks": checks_after,
    })
    return 0 if summary_after["overall"] == "ok" else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Powerpacks setup doctor")
    sub = parser.add_subparsers(dest="command", required=True)
    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--profile", default="search-core")
        p.add_argument("--env-file", type=Path, default=Path(".env"))
        p.add_argument("--gcp-project", default=DEFAULT_PROJECT)
        p.add_argument("--skip-adc", action="store_true",
                       help="Skip the application-default credentials check")

    runp = sub.add_parser("run", help="Read-only: run every prereq check and emit a structured report")
    common(runp)
    runp.set_defaults(func=cmd_run)

    fixp = sub.add_parser(
        "fix",
        help=(
            "Run safe automatic fixes (env pull, etc.). Pass --interactive"
            " to also run browser-based logins. Never runs OS-level installs."
        ),
    )
    common(fixp)
    fixp.add_argument(
        "--interactive",
        action="store_true",
        help="Also run interactive (browser-popping) fixes like auth0 login and gcloud auth login",
    )
    fixp.set_defaults(func=cmd_fix)
    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
