#!/usr/bin/env python3
"""Tolerant harness for messages-pack primitives."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


DEFAULT_OUTPUT_DIR = Path(".powerpacks/messages")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def imessage_primitive() -> Path:
    return repo_root() / "packs/messages/primitives/extract_imessage_contacts/extract_imessage_contacts.py"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_step(step_id: str, command: list[str], cwd: Path, log_dir: Path, timeout: int) -> dict[str, Any]:
    started = time.time()
    result = subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    elapsed_ms = int((time.time() - started) * 1000)
    stdout_path = log_dir / f"{step_id}.stdout.log"
    stderr_path = log_dir / f"{step_id}.stderr.log"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text(result.stdout or "", encoding="utf-8")
    stderr_path.write_text(result.stderr or "", encoding="utf-8")

    parsed_stdout = None
    if result.stdout.strip().startswith("{"):
        try:
            parsed_stdout = json.loads(result.stdout)
        except json.JSONDecodeError:
            parsed_stdout = None

    return {
        "id": step_id,
        "status": "completed" if result.returncode == 0 else "failed",
        "returncode": result.returncode,
        "elapsed_ms": elapsed_ms,
        "command": command,
        "artifacts": {
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
        },
        "output": parsed_stdout,
    }


def repair_note(step: dict[str, Any]) -> dict[str, Any]:
    output = step.get("output") if isinstance(step.get("output"), dict) else {}
    diagnostics = output.get("diagnostics") or output
    return {
        "step_id": step.get("id"),
        "summary": "Primitive failed; inspect diagnostics and patch or rerun with explicit local paths.",
        "diagnostics": diagnostics,
        "suggested_next_actions": [
            "Check whether Terminal/Codex has Full Disk Access for ~/Library/Messages/chat.db.",
            "If the Messages schema differs, patch extract_imessage_contacts.py and add a fixture test.",
            "Rerun the harness with --chat-db or --addressbook-glob if local paths are nonstandard.",
        ],
    }


def cmd_imessage(args: argparse.Namespace) -> None:
    run_id = args.run_id or f"messages-imessage-{uuid4()}"
    output_dir = Path(args.output_dir)
    run_dir = output_dir / "runs" / run_id
    logs_dir = run_dir / "logs"
    csv_path = output_dir / "imessage.contacts.csv"
    jsonl_path = output_dir / "imessage.contacts.jsonl"
    primitive = imessage_primitive()

    base = [sys.executable, str(primitive)]
    if args.chat_db:
        base.extend(["--chat-db", args.chat_db])
    if args.addressbook_glob:
        base.extend(["--addressbook-glob", args.addressbook_glob])

    steps = []
    steps.append(run_step("check_imessage", [*base, "check"], repo_root(), logs_dir, args.timeout))

    if args.check_only:
        pass
    else:
        extract_command = [
            *base,
            "extract",
            "--output-csv",
            str(csv_path),
            "--output-jsonl",
            str(jsonl_path),
            "--manifest",
            str(run_dir / "extract_imessage.manifest.json"),
        ]
        if args.include_contact_only:
            extract_command.append("--include-contact-only")
        steps.append(run_step("extract_imessage", extract_command, repo_root(), logs_dir, args.timeout))

    failed = [step for step in steps if step["status"] != "completed"]
    manifest = {
        "run_id": run_id,
        "created_at": now_iso(),
        "primitive": "messages_harness",
        "channel": "imessage",
        "status": "failed" if failed else "completed",
        "steps": steps,
        "repair_notes": [repair_note(step) for step in failed],
        "artifacts": {
            "run_dir": str(run_dir),
            "manifest": str(run_dir / "manifest.json"),
            "csv": str(csv_path),
            "jsonl": str(jsonl_path),
        },
    }
    write_json(run_dir / "manifest.json", manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    if failed and args.strict:
        raise SystemExit(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run messages pack primitives with tolerant manifests")
    sub = parser.add_subparsers(dest="command", required=True)

    imessage = sub.add_parser("imessage")
    imessage.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    imessage.add_argument("--run-id")
    imessage.add_argument("--chat-db")
    imessage.add_argument("--addressbook-glob")
    imessage.add_argument("--include-contact-only", action="store_true")
    imessage.add_argument("--check-only", action="store_true")
    imessage.add_argument("--strict", action="store_true")
    imessage.add_argument("--timeout", type=int, default=600)
    imessage.set_defaults(func=cmd_imessage)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
