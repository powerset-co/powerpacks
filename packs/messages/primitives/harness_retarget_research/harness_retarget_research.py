#!/usr/bin/env python3
"""Run small retarget research batches through a local CLI harness.

This is a low-latency alternative to Parallel for small feedback batches. It
reads `retarget_queue.csv`, writes per-row prompts, and can invoke Codex/Claude
(or a custom command template) to perform web research using the harness' web
search/browser capabilities.

Output is the same profile artifact shape consumed by prepare_retarget_queue's
`mark-completed`: `<output-dir>/<handle>/01_research_parallel.json`.

No message bodies are read. Only contact/research metadata from the queue is
included in prompts.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_INPUT = Path(".powerpacks/messages/retarget_queue.csv")
DEFAULT_OUTPUT_DIR = Path(".powerpacks/messages/research_retarget")
DEFAULT_PROMPT_DIR = Path(".powerpacks/messages/retarget_harness_prompts")
PROFILE_FILE = "01_research_parallel.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        return [{k: v or "" for k, v in row.items()} for row in csv.DictReader(handle)]


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def safe_handle(value: str) -> str:
    value = (value or "unknown").strip()
    return re.sub(r"[^A-Za-z0-9_.@+-]+", "_", value).strip("_") or "unknown"


def row_handle(row: dict[str, str], idx: int) -> str:
    return safe_handle(row.get("handle") or row.get("retarget_handle") or f"retarget-{idx}")


def build_known_info(row: dict[str, str]) -> str:
    parts: list[str] = []
    for key, label in [
        ("retarget_source_handle", "Original handle"),
        ("display_name", "Display name"),
        ("first_name", "First name"),
        ("last_name", "Last name"),
        ("phone_e164", "Phone"),
        ("area_code", "Area code"),
        ("total_messages", "Message count"),
        ("message_source", "Message source"),
        ("group_names", "Group names"),
        ("bio", "Prior bio/headline"),
        ("retarget_hint", "User feedback / correction"),
        ("website_url", "Website"),
        ("location", "Location"),
        ("primary_email", "Email"),
        ("domain", "Email domain"),
    ]:
        value = (row.get(key) or "").strip()
        if value:
            parts.append(f"{label}: {value}")
    return "\n".join(parts)


def prompt_for_row(row: dict[str, str], handle: str) -> str:
    display = (row.get("display_name") or " ".join(x for x in [row.get("first_name", ""), row.get("last_name", "")] if x).strip() or handle)
    return f"""You are doing targeted public web research for a corrected contact profile.

Use your harness web search / browser tools if available. Do not use private message bodies. Use only the metadata below and public web evidence.

Goal: identify the person the user is correcting us toward, then return a single JSON object in the exact schema below. If the user supplied a LinkedIn URL or specific company/title clue, treat that as the strongest hint. Prefer the corrected person over the previous/ambiguous match.

Known metadata:
{build_known_info(row)}

Return ONLY valid JSON, no markdown fences. Schema:
{{
  "research_id": "{handle}-{date.today().isoformat()}",
  "query": "{display}",
  "status": "draft",
  "research_method": "harness-websearch",
  "person": {{
    "full_name": "best full name or empty",
    "first_name": "first name or empty",
    "last_name": "last name or empty",
    "also_known_as": ["{handle}", "{display}"],
    "confidence": 0.0,
    "sources": ["public URLs used"],
    "notes": "brief name evidence"
  }},
  "location": {{"city": "", "state": "", "country": "", "raw": "", "confidence": 0.0, "source": ""}},
  "headline": {{"text": "short current headline", "confidence": 0.0, "source": ""}},
  "summary": {{"text": "concise public-evidence summary", "confidence": 0.0, "source": "harness web research"}},
  "positions": [{{"title": "", "company_name": "", "company_domain": null, "company_linkedin_url": null, "description": null, "start_date": null, "end_date": null, "is_current": false, "confidence": 0.0, "sources": []}}],
  "education": [{{"school_name": "", "degree": null, "field_of_study": null, "start_year": null, "end_year": null, "confidence": 0.0, "source": ""}}],
  "social": {{"twitter_handle": null, "linkedin_url": null, "github_url": null, "personal_website": null, "primary_email": null, "primary_phone": null}},
  "metadata": {{"total_sources_consulted": 0, "estimated_completeness": 0.0, "gaps": [], "research_date": "{date.today().isoformat()}", "research_method": "harness-websearch", "research_notes": "explain uncertainty and why this is the corrected/best match", "source_channel": "retarget", "source_identifier": "{handle}"}}
}}
"""


def extract_json(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end + 1])
        raise


def validate_profile(profile: dict[str, Any]) -> dict[str, Any]:
    profile.setdefault("status", "draft")
    profile.setdefault("research_method", "harness-websearch")
    profile.setdefault("person", {})
    profile.setdefault("location", {})
    profile.setdefault("headline", {})
    profile.setdefault("summary", {})
    profile.setdefault("positions", [])
    profile.setdefault("education", [])
    profile.setdefault("social", {})
    profile.setdefault("metadata", {})
    metadata = profile["metadata"] if isinstance(profile["metadata"], dict) else {}
    metadata.setdefault("research_method", "harness-websearch")
    metadata.setdefault("research_date", date.today().isoformat())
    profile["metadata"] = metadata
    return profile


def command_template_for(harness: str) -> str:
    if harness == "codex":
        return "codex exec --skip-git-repo-check {prompt_instruction}"
    if harness == "claude":
        return "claude -p {prompt_instruction}"
    raise ValueError(f"unknown harness: {harness}")


def choose_harness(requested: str) -> str:
    if requested != "auto":
        return requested
    if shutil.which("codex"):
        return "codex"
    if shutil.which("claude"):
        return "claude"
    return "manual"


def render_command(template: str, prompt_path: Path, output_path: Path) -> list[str]:
    instruction = f"Read and follow the research prompt at {prompt_path}. Return only the requested JSON."
    rendered = template.format(
        prompt_path=str(prompt_path),
        output_path=str(output_path),
        prompt_instruction=shlex.quote(instruction),
    )
    return shlex.split(rendered)


def write_prompt(prompt_dir: Path, handle: str, prompt: str) -> Path:
    path = prompt_dir / handle / "prompt.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prompt, encoding="utf-8")
    return path


def profile_path(output_dir: Path, handle: str) -> Path:
    return output_dir / handle / PROFILE_FILE


def run_one_row(
    *,
    idx: int,
    row: dict[str, str],
    prompt_dir: Path,
    output_dir: Path,
    template: str,
    timeout: int,
    force: bool,
) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None]:
    handle = row_handle(row, idx)
    out = profile_path(output_dir, handle)
    if out.exists() and not force:
        return idx, {"handle": handle, "status": "skipped_existing", "output": str(out)}, None

    prompt_path = write_prompt(prompt_dir, handle, prompt_for_row(row, handle))
    cmd = render_command(template, prompt_path, out)
    raw_path = out.parent / "00_harness_stdout.txt"
    err_path = out.parent / "00_harness_stderr.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(cmd, cwd=Path.cwd(), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raw_path.write_text((exc.stdout or "") if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace"), encoding="utf-8")
        err_path.write_text((exc.stderr or "") if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace"), encoding="utf-8")
        return idx, None, {"handle": handle, "error": f"timed out after {timeout}s"}
    raw_path.write_text(completed.stdout or "", encoding="utf-8")
    err_path.write_text(completed.stderr or "", encoding="utf-8")
    if completed.returncode != 0:
        return idx, None, {"handle": handle, "returncode": completed.returncode, "stderr": (completed.stderr or "")[-500:]}
    try:
        profile = validate_profile(extract_json(completed.stdout or ""))
    except Exception as exc:
        return idx, None, {"handle": handle, "error": f"could not parse JSON: {exc}", "stdout": (completed.stdout or "")[-500:]}
    write_json(out, profile)
    return idx, {"handle": handle, "status": "completed", "output": str(out), "prompt": str(prompt_path)}, None


def cmd_prepare(args: argparse.Namespace) -> int:
    rows = read_csv(Path(args.input))
    if args.limit is not None:
        rows = rows[: args.limit]
    prompt_dir = Path(args.prompt_dir)
    written = []
    for idx, row in enumerate(rows):
        handle = row_handle(row, idx)
        prompt_path = write_prompt(prompt_dir, handle, prompt_for_row(row, handle))
        written.append({"handle": handle, "prompt": str(prompt_path), "output": str(profile_path(Path(args.output_dir), handle))})
    emit({"status": "prepared", "input": args.input, "rows": len(rows), "prompt_dir": str(prompt_dir), "prompts": written})
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    rows = read_csv(Path(args.input))
    if args.limit is not None:
        rows = rows[: args.limit]
    harness = choose_harness(args.harness)
    prompt_dir = Path(args.prompt_dir)
    output_dir = Path(args.output_dir)
    if harness == "manual" and not args.command_template:
        return cmd_prepare(args)
    template = args.command_template or command_template_for(harness)
    harness_label = "custom" if args.command_template else harness
    max_workers = max(1, int(args.max_workers or 1))
    results_by_idx: dict[int, dict[str, Any]] = {}
    failures_by_idx: dict[int, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(rows)))) as executor:
        futures = [
            executor.submit(
                run_one_row,
                idx=idx,
                row=row,
                prompt_dir=prompt_dir,
                output_dir=output_dir,
                template=template,
                timeout=args.timeout,
                force=args.force,
            )
            for idx, row in enumerate(rows)
        ]
        for future in concurrent.futures.as_completed(futures):
            idx, result, failure = future.result()
            if result is not None:
                results_by_idx[idx] = result
            if failure is not None:
                failures_by_idx[idx] = failure
    results = [results_by_idx[idx] for idx in sorted(results_by_idx)]
    failures = [failures_by_idx[idx] for idx in sorted(failures_by_idx)]
    status = "ok" if not failures else "partial"
    emit({"status": status, "harness": harness_label, "input": args.input, "max_workers": max_workers, "processed": len(results), "failed": len(failures), "results": results, "failures": failures})
    return 0 if not failures else 1


def cmd_status(args: argparse.Namespace) -> int:
    rows = read_csv(Path(args.input))
    output_dir = Path(args.output_dir)
    done = []
    missing = []
    for idx, row in enumerate(rows):
        handle = row_handle(row, idx)
        path = profile_path(output_dir, handle)
        (done if path.exists() else missing).append({"handle": handle, "output": str(path)})
    emit({"status": "ok", "input": args.input, "done": len(done), "missing": len(missing), "done_rows": done, "missing_rows": missing})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run small retarget research batches via Codex/Claude/local harness")
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--input", default=str(DEFAULT_INPUT))
    common.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    common.add_argument("--prompt-dir", default=str(DEFAULT_PROMPT_DIR))
    common.add_argument("--limit", type=int, help=argparse.SUPPRESS)

    prep = sub.add_parser("prepare", parents=[common])
    prep.set_defaults(func=cmd_prepare)

    run = sub.add_parser("run", parents=[common])
    run.add_argument("--harness", choices=["auto", "codex", "claude", "manual"], default="auto")
    run.add_argument("--command-template", default="", help="Custom command. Placeholders: {prompt_path}, {output_path}, {prompt_instruction}")
    run.add_argument("--timeout", type=int, default=900)
    run.add_argument("--max-workers", type=int, default=1)
    run.add_argument("--force", action="store_true")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", parents=[common])
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
