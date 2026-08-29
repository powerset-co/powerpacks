"""Command-line parsing and dispatch for the review UI."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import webbrowser
from http.server import ThreadingHTTPServer

from packs.ingestion.primitives.deep_context.common import (
    CANONICAL_DB,
    REVIEW_MANIFEST,
)
from packs.ingestion.primitives.deep_context.db.identity_views import linkedin_review
from packs.ingestion.primitives.deep_context.db.models import JUDGE_CONFIRM_THRESHOLD
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.db.workflow_views import workflow_state

from .server import make_handler
from .sqlite_adapter import SqliteReviewAdapter

LEGACY_COMMON_FLAGS = (
    "--review", "--verdicts", "--facts-dir", "--people-csv",
    "--synthetic-people", "--manifest", "--enrichment-manifest",
)
LEGACY_SERVE_FLAGS = ("--parents-dir", "--dossier-dir", "--profile-cache-dir", "--avatar-dir",
                      "--detach-threshold")
MISSING_DB = f"Deep Context database is missing: {CANONICAL_DB}; run bin/deep-context migrate-sqlite"

def _url(host: str, port: int, stage: str) -> str:
    route = "directory" if stage == "directory" else f"?stage={stage}"
    return f"http://{host}:{port}/{route}"


def _announce(status: str, url: str, **extra: object) -> None:
    print(json.dumps({"primitive": "reconcile_review_web", "status": status,
                      "url": url, **extra}, indent=2))


def workflow_status(**_: object) -> dict[str, object]:
    if not CANONICAL_DB.exists():
        raise StoreError(MISSING_DB)
    api = SqliteReviewAdapter(Db(CANONICAL_DB))
    payload = api.workflow_status()
    commands = {
        "review_people": "bin/deep-context review",
        "enrich": "wait for the user to approve Enrich Contacts in the review UI",
        "review_linkedin": "wait for LinkedIn Yes/No decisions in the review UI",
        "realize": "bin/deep-context stop && bin/deep-context realize",
    }
    payload.update({"command": commands[payload["next_action"]], "poll_after_seconds": 60})
    return payload


def cmd_serve(args: argparse.Namespace) -> None:
    if not CANONICAL_DB.exists():
        raise SystemExit(MISSING_DB)
    db = Db(CANONICAL_DB)
    try:
        with urllib.request.urlopen(
            f"http://{args.host}:{args.port}/api/status", timeout=1,
        ) as response:
            live = json.loads(response.read())
    except (OSError, json.JSONDecodeError):
        live = {}
    stage = args.stage or "directory"
    url = _url(args.host, args.port, stage)
    if live.get("primitive") == "reconcile_review_web":
        _announce("reused", url, manifest=str(REVIEW_MANIFEST), stage=stage)
        if args.open:
            webbrowser.open(url)
        return
    handler = make_handler(
        confirm_threshold=args.confirm_threshold, run_jobs=True, db=db,
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    host, port = server.server_address
    url = _url(host, port, stage)
    state = workflow_state(db)
    _announce("serving", url, manifest=str(REVIEW_MANIFEST),
              parents=len(linkedin_review(db, "parents")), progress=state["progress"])
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)

def cmd_status(args: argparse.Namespace) -> None:
    try:
        status = workflow_status()
    except StoreError as exc:
        raise SystemExit(str(exc)) from exc
    if getattr(args, "wait", False):
        started = time.monotonic()
        deadline = started + max(1, int(args.timeout))
        while status["next_action"] != "realize" and time.monotonic() < deadline:
            time.sleep(1)
            status = workflow_status()
        status["waited_seconds"] = int(time.monotonic() - started)
        if status["next_action"] != "realize":
            status["status"] = "waiting"
    print(json.dumps(status, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the staged deep-context people review UI.")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve")
    status = sub.add_parser("status")
    for flag in LEGACY_COMMON_FLAGS:
        for target in (serve, status):
            target.add_argument(flag, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    for flag in LEGACY_SERVE_FLAGS:
        serve.add_argument(flag, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    serve.add_argument("--confirm-threshold", type=float, default=JUDGE_CONFIRM_THRESHOLD)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--stage", choices=("worth", "enrich", "linkedin", "done", "directory"))
    serve.add_argument("--fresh", action="store_true", help=argparse.SUPPRESS)
    serve.add_argument("--open", action="store_true")
    status.add_argument("--wait", action="store_true")
    status.add_argument("--timeout", type=int, default=900)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "status":
        cmd_status(args)
    else:
        cmd_serve(args if args.command == "serve" else parser.parse_args(["serve", *(argv or [])]))
    return 0
