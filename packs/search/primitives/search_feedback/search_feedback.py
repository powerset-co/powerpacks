#!/usr/bin/env python3
"""Log user edits in a search run and ship one aggregated feedback row.

Two subcommands over one run dir (fast `.powerpacks/search/<slug>` or deep
`.powerpacks/deep-search/<jd-slug>`):

- `log` appends one JSON line to `<run-dir>/user-edits.jsonl` — a filter or
  query change at the review gate, a pond query/payload edit, or any feedback
  the user gives about the results. Identifiers only, never message content.
- `send` reads that file (plus `decision.json` when present) and submits ONE
  aggregated row to the Powerset feedback endpoint via the send_feedback
  primitive. Signed-out users get `status: needs_auth` and exit 0 — the local
  log is the durable record either way. A successful submit rotates the log
  into `<run-dir>/feedback-sent.json`, so a repeat `send` is `no_edits` and a
  rerun that reuses the same slug dir starts a fresh log.

Changelog:
- 2026-08-31: initial version; same-day review fixes (log rotation on submit,
  default_set_id from send_feedback).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.powerset.primitives.send_feedback.send_feedback import (  # noqa: E402
    FeedbackRequest,
    SendFeedback,
    default_set_id,
)

EDIT_KINDS = ("filter_edit", "query_edit", "pond_edit", "result_feedback")
EDITS_FILE = "user-edits.jsonl"
SENT_FILE = "feedback-sent.json"
FEEDBACK_SOURCE = "powerpacks-search-user-edits"


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_edits(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / EDITS_FILE
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def log_edit(run_dir: Path, *, kind: str, note: str,
             before: str = "", after: str = "") -> dict[str, Any]:
    if kind not in EDIT_KINDS:
        raise SystemExit(f"--kind must be one of {', '.join(EDIT_KINDS)}")
    if not note.strip():
        raise SystemExit("a non-empty --note is required")
    entry: dict[str, Any] = {"at": _now(), "kind": kind, "note": note.strip()}
    if before:
        entry["before"] = before
    if after:
        entry["after"] = after
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / EDITS_FILE).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"status": "logged", "kind": kind, "count": len(read_edits(run_dir)),
            "path": str(run_dir / EDITS_FILE)}


def _feedback_type(edits: list[dict[str, Any]]) -> str:
    """Result feedback outranks edit-only runs — first rule wins.
    `filter_edit` is pinned as the closest existing API type for every
    edit-only run (query/pond edits included); the exact kinds ride in
    metadata.edits for triage. Deliberately NOT data_inconsistency: this row
    is taste telemetry, and a concrete wrong-person/data fix goes through
    $feedback so it reaches the admin triage queue."""
    if any(entry.get("kind") == "result_feedback" for entry in edits):
        return "bad_search"
    return "filter_edit"


def _comment(slug: str, edits: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for entry in edits:
        counts[entry["kind"]] = counts.get(entry["kind"], 0) + 1
    parts = ", ".join(f"{count} {kind}" for kind, count in sorted(counts.items()))
    return f"search '{slug}': user made {len(edits)} change(s) during the run ({parts})"


def send(run_dir: Path, *, dry_run: bool = False,
         env_file: Path | None = None) -> dict[str, Any]:
    edits = read_edits(run_dir)
    if not edits:
        return {"status": "no_edits", "path": str(run_dir / EDITS_FILE)}
    slug = run_dir.name
    metadata: dict[str, Any] = {"source": FEEDBACK_SOURCE, "run": slug, "edits": edits}
    decision_path = run_dir / "decision.json"
    if decision_path.is_file():
        metadata["decision"] = json.loads(decision_path.read_text(encoding="utf-8"))
    request = FeedbackRequest(
        comment=_comment(slug, edits),
        feedback_type=_feedback_type(edits),
        metadata=metadata,
        set_id=default_set_id(),
    )
    payload = SendFeedback(request, env_file=env_file, dry_run=dry_run).run()
    if payload["status"] == "submitted":
        # Rotate: the sent record keeps the local copy, and the emptied log
        # means a rerun in this slug dir never re-ships or mixes old edits.
        (run_dir / SENT_FILE).write_text(json.dumps(
            {"sent_at": _now(), "edit_count": len(edits),
             "feedback_id": payload.get("feedback_id") or "",
             "feedback_type": request.feedback_type, "edits": edits},
            indent=2) + "\n", encoding="utf-8")
        (run_dir / EDITS_FILE).unlink()
    return payload


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    log_p = sub.add_parser("log", help="append one user edit to the run's user-edits.jsonl")
    log_p.add_argument("--run-dir", required=True)
    log_p.add_argument("--kind", required=True, choices=EDIT_KINDS)
    log_p.add_argument("--note", required=True,
                       help="one line saying what the user changed or reported, identifiers only")
    log_p.add_argument("--before", default="", help="value before the edit, when it exists")
    log_p.add_argument("--after", default="", help="value after the edit, when it exists")
    send_p = sub.add_parser("send", help="submit one aggregated feedback row for the run")
    send_p.add_argument("--run-dir", required=True)
    send_p.add_argument("--dry-run", action="store_true",
                        help="print the exact request body without sending")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_dir = Path(args.run_dir)
    if args.command == "log":
        payload = log_edit(run_dir, kind=args.kind, note=args.note,
                           before=args.before, after=args.after)
    else:
        payload = send(run_dir, dry_run=args.dry_run, env_file=_REPO_ROOT / ".env")
    emit(payload)
    # needs_auth is deliberately OK here (unlike send_feedback's exit 3): the
    # local log is the record and the skill must stay quiet about sign-in.
    ok = ("logged", "submitted", "dry_run", "no_edits", "needs_auth")
    return 0 if payload["status"] in ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
