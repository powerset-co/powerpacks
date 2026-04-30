#!/usr/bin/env python3
"""Harness for running powerset-contacts/contact-exporter from Powerpacks.

This primitive keeps extraction as an external local-tool boundary. It records
the command, logs, and manifest, but it does not parse message stores itself.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


DEFAULT_RUN_ROOT = Path(".powerpacks/messages/runs")
DEFAULT_OUTPUT = Path(".powerpacks/messages/contacts.csv")
CHANNEL_COMMANDS = {
    "imessage": "imessage",
    "whatsapp": "whatsapp",
    "full": "full",
    "sync-candidates": "sync-candidates",
    "match-local": "match-local",
    "review": "review",
    "upload": "upload",
    "whoami": "whoami",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def sibling_powerset_contacts() -> Path:
    return repo_root().parent / "powerset-contacts"


def command_version(command: list[str], cwd: Path | None = None) -> str | None:
    try:
        result = subprocess.run(
            [*command, "--version"],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or result.stderr).strip() or None


def resolve_backend(explicit_bin: str | None = None, explicit_repo: str | None = None) -> dict[str, Any]:
    """Resolve a contact-exporter command without installing anything."""
    if explicit_bin:
        path = shutil.which(explicit_bin) or explicit_bin
        return {
            "available": True,
            "mode": "explicit_bin",
            "command_prefix": [path],
            "cwd": None,
            "version": command_version([path]),
        }

    env_bin = os.getenv("POWERSET_CONTACT_EXPORTER_BIN")
    if env_bin:
        path = shutil.which(env_bin) or env_bin
        return {
            "available": True,
            "mode": "env_bin",
            "command_prefix": [path],
            "cwd": None,
            "version": command_version([path]),
        }

    discovered = shutil.which("contact-exporter") or shutil.which("powerset-contacts")
    if discovered:
        return {
            "available": True,
            "mode": "path",
            "command_prefix": [discovered],
            "cwd": None,
            "version": command_version([discovered]),
        }

    repo = Path(explicit_repo) if explicit_repo else sibling_powerset_contacts()
    venv_cli = repo / ".venv" / "bin" / "contact-exporter"
    if venv_cli.exists():
        return {
            "available": True,
            "mode": "sibling_venv",
            "command_prefix": [str(venv_cli)],
            "cwd": str(repo),
            "version": command_version([str(venv_cli)], cwd=repo),
        }

    if (repo / "pyproject.toml").exists() and shutil.which("uv"):
        return {
            "available": True,
            "mode": "sibling_uv",
            "command_prefix": ["uv", "run", "contact-exporter"],
            "cwd": str(repo),
            "version": command_version(["uv", "run", "contact-exporter"], cwd=repo),
        }

    return {
        "available": False,
        "mode": "missing",
        "command_prefix": [],
        "cwd": str(repo) if repo.exists() else None,
        "version": None,
        "error": "contact-exporter was not found on PATH and ../powerset-contacts is not runnable",
    }


def build_command(args: argparse.Namespace, backend: dict[str, Any]) -> list[str]:
    if args.channel not in CHANNEL_COMMANDS:
        raise ValueError(f"unsupported channel: {args.channel}")
    if args.channel == "upload" and not args.confirm_upload:
        raise SystemExit("upload requires --confirm-upload")

    command = [*backend["command_prefix"], CHANNEL_COMMANDS[args.channel]]
    if args.local:
        command.append("--local")
    if args.api_base_url:
        command.extend(["--api-base-url", args.api_base_url])
    if args.operator_id and args.channel in {"imessage", "whatsapp", "full", "sync-candidates", "match-local"}:
        command.extend(["--operator-id", args.operator_id])

    if args.channel in {"imessage", "whatsapp", "full"}:
        command.extend(["--output", str(args.output)])
    elif args.channel in {"review", "upload", "match-local"}:
        command.extend(["--file", str(args.input or args.output)])
    elif args.channel == "sync-candidates":
        command.extend(["--output", str(args.output)])

    if args.reset and args.channel == "whatsapp":
        command.append("--reset")
    if args.reset and args.channel == "full":
        command.append("--reset-whatsapp")
    if args.include_small_groups and args.channel in {"imessage", "full"}:
        command.append("--include-small-groups")

    for value in args.extra_arg or []:
        command.append(value)
    return command


def manifest_base(args: argparse.Namespace, backend: dict[str, Any], command: list[str], run_dir: Path) -> dict[str, Any]:
    return {
        "run_id": args.run_id,
        "created_at": now_iso(),
        "primitive": "powerset_contacts_harness",
        "channel": args.channel,
        "backend": {
            "mode": backend.get("mode"),
            "cwd": backend.get("cwd"),
            "version": backend.get("version"),
        },
        "command": command,
        "dry_run": bool(args.dry_run),
        "returncode": None,
        "artifacts": {
            "run_dir": str(run_dir),
            "stdout": str(run_dir / "stdout.log"),
            "stderr": str(run_dir / "stderr.log"),
            "manifest": str(run_dir / "manifest.json"),
        },
    }


def cmd_check(args: argparse.Namespace) -> None:
    backend = resolve_backend(args.contact_exporter, args.powerset_contacts_repo)
    print(json.dumps(backend, indent=2, sort_keys=True))
    if not backend.get("available"):
        raise SystemExit(1)


def cmd_run(args: argparse.Namespace) -> None:
    backend = resolve_backend(args.contact_exporter, args.powerset_contacts_repo)
    if not backend.get("available"):
        print(json.dumps(backend, indent=2, sort_keys=True))
        raise SystemExit(1)

    args.run_id = args.run_id or f"messages-{uuid4()}"
    run_dir = Path(args.run_root) / args.run_id
    command = build_command(args, backend)
    manifest = manifest_base(args, backend, command, run_dir)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        manifest["artifacts"]["requested_output"] = str(args.output)
    if args.input:
        manifest["artifacts"]["input"] = str(args.input)

    if args.dry_run:
        write_json(run_dir / "manifest.json", manifest)
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return

    started = time.time()
    result = subprocess.run(
        command,
        cwd=backend.get("cwd"),
        capture_output=True,
        text=True,
        timeout=args.timeout,
    )
    elapsed_ms = int((time.time() - started) * 1000)

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "stdout.log").write_text(result.stdout or "", encoding="utf-8")
    (run_dir / "stderr.log").write_text(result.stderr or "", encoding="utf-8")
    manifest["returncode"] = result.returncode
    manifest["elapsed_ms"] = elapsed_ms
    manifest["completed_at"] = now_iso()
    if args.output:
        manifest["artifacts"]["output_exists"] = Path(args.output).exists()
    write_json(run_dir / "manifest.json", manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Powerpacks messages contact-exporter harness")
    parser.add_argument("--contact-exporter", help="Explicit contact-exporter executable")
    parser.add_argument("--powerset-contacts-repo", help="Path to a powerset-contacts checkout")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check")
    check.set_defaults(func=cmd_check)

    run = sub.add_parser("run")
    run.add_argument("--channel", choices=sorted(CHANNEL_COMMANDS), required=True)
    run.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    run.add_argument("--input", type=Path)
    run.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    run.add_argument("--run-id")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--confirm-upload", action="store_true")
    run.add_argument("--local", action="store_true")
    run.add_argument("--api-base-url")
    run.add_argument("--operator-id")
    run.add_argument("--reset", action="store_true")
    run.add_argument("--include-small-groups", action="store_true")
    run.add_argument("--timeout", type=int, default=3600)
    run.add_argument("--extra-arg", action="append", help="Additional argument passed through to contact-exporter")
    run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
