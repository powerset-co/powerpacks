"""Command-line parsing and dispatch for the review UI.

Changelog:
- 2026-09-30: the directory stage is gone; bare `serve` opens the current review stage.
- 2026-09-30: serve no longer counts LinkedIn parents at startup (minutes on a
  large store, read by nobody); `status --wait` polls every five seconds.
- 2026-09-26: the searches-only server serves the React shell at /people; its
  rows request answers 404 with what to run first.
- 2026-09-26: it serves the shell first (now also /searches and /searches/run), then the
  Searches JSON routes, then the legacy search routes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from packs.ingestion.primitives.deep_context.shared.common import (
    CANONICAL_DB,
    load_env,
)
from packs.ingestion.primitives.deep_context.db.models import RESEARCH_CONFIRM_THRESHOLD
from packs.ingestion.primitives.deep_context.db.store import open_existing_db
from packs.ingestion.primitives.deep_context.db.workflow_views import workflow_state
from packs.search.primitives.deep_search.results_web import server as results_web
from packs.search.primitives.deep_search.results_web.api import search_api
from packs.ingestion.primitives.accounts.api import AccountsApi
from packs.ingestion.primitives.refresh.api import TasksApi
from packs.shared.web.app import AppRoutes

from .server import make_handler
from .sqlite_adapter import SqliteReviewAdapter


# The actions the agent runs itself; every other action waits on the user.
_AGENT_ACTIONS = frozenset({"synthesize", "realize"})

# The status query walks every parent; a tight loop would keep the CPU busy
# for the whole review.
_WAIT_POLL_SECONDS = 5

# The People page's rows request before a store exists: the page shows this message.
_NO_PEOPLE = json.dumps({
    "error": "No people yet. Run bin/deep-context to build your network, then bin/deep-context review people.",
}).encode("utf-8")


def _url(host: str, port: int, stage: str, run_id: str = "") -> str:
    if stage == "searches" and run_id:
        return f"http://{host}:{port}/searches/run?run_id={urllib.parse.quote(run_id)}"
    if not stage:
        return f"http://{host}:{port}/"  # the current review stage
    route = stage if stage in {"people", "searches"} else f"?stage={stage}"
    return f"http://{host}:{port}/{route}"


def searches_only_handler(root: Path = results_web.DEFAULT_DEEP_SEARCH_ROOT) -> type[BaseHTTPRequestHandler]:
    """The same server before a deep-context store exists: the saved searches
    at their usual URLs, and People says what to run first."""
    routes = results_web.search_routes(root, base="/searches")
    viewer = results_web.make_handler(root, routes.load, catalog=routes.catalog, load_one=routes.load_one,
                                      base="/searches")
    app = AppRoutes()
    searches_json = search_api(routes)
    accounts = AccountsApi()
    tasks = TasksApi()

    class Handler(viewer):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if app.get(self, parsed) or accounts.get(self, parsed) or tasks.get(self, parsed) or searches_json.get(self, parsed):
                return
            if parsed.path == "/":
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", "/searches")
                self.end_headers()
            elif parsed.path == "/api/people/rows":
                self.send_response(HTTPStatus.NOT_FOUND)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(_NO_PEOPLE)))
                self.end_headers()
                self.wfile.write(_NO_PEOPLE)
            else:
                super().do_GET()

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if not (accounts.post(self, parsed) or tasks.post(self, parsed)):
                super().do_POST()

    return Handler


def _announce(status: str, url: str, **extra: object) -> None:
    print(json.dumps({"primitive": "reconcile_review_web", "status": status, "url": url, **extra}, indent=2))


def workflow_status(**_: object) -> dict[str, object]:
    api = SqliteReviewAdapter(open_existing_db(CANONICAL_DB))
    payload = api.workflow_status()
    commands = {
        "synthesize": "bin/deep-context dry",
        "review_people": "bin/deep-context review",
        "enrich": "wait for the user to approve Enrich Contacts in the review UI",
        "review_linkedin": "wait for LinkedIn Yes/No decisions in the review UI",
        "realize": "bin/deep-context stop && bin/deep-context realize",
    }
    payload.update({"command": commands[payload["next_action"]], "poll_after_seconds": 60})
    return payload


def cmd_serve(args: argparse.Namespace) -> None:
    load_env()
    try:
        with urllib.request.urlopen(
            f"http://{args.host}:{args.port}/api/status",
            timeout=1,
        ) as response:
            live = json.loads(response.read())
    except (OSError, json.JSONDecodeError):
        live = {}
    has_store = CANONICAL_DB.is_file()
    stage = args.stage or ("" if has_store else "searches")
    url = _url(args.host, args.port, stage, args.run)
    if live.get("primitive") == "reconcile_review_web":
        _announce("reused", url, stage=stage)
        if args.open:
            webbrowser.open(url)
        return
    # One local server: the review stages, People and the searches when the
    # deep-context store exists; the searches alone before it does.
    if has_store:
        db = open_existing_db(CANONICAL_DB)
        handler = make_handler(
            confirm_threshold=args.confirm_threshold,
            run_jobs=True,
            db=db,
        )
        extra = {"progress": asdict(workflow_state(db).progress)}
    else:
        handler = searches_only_handler()
        extra = {"note": "no deep-context store yet; serving the searches alone"}
    server = ThreadingHTTPServer((args.host, args.port), handler)
    host, port = server.server_address
    url = _url(host, port, stage, args.run)
    _announce("serving", url, **extra)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)


def cmd_status(args: argparse.Namespace) -> None:
    status = workflow_status()
    if args.wait:
        started = time.monotonic()
        deadline = started + max(1, int(args.timeout))
        while status["next_action"] not in _AGENT_ACTIONS and time.monotonic() < deadline:
            time.sleep(_WAIT_POLL_SECONDS)
            status = workflow_status()
        status["waited_seconds"] = int(time.monotonic() - started)
        if status["next_action"] not in _AGENT_ACTIONS:
            status["status"] = "waiting"
    print(json.dumps(status, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the staged deep-context people review UI.")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve")
    status = sub.add_parser("status")
    serve.add_argument("--confirm-threshold", type=float, default=RESEARCH_CONFIRM_THRESHOLD)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--stage", choices=("worth", "enrich", "linkedin", "done", "people", "searches"))
    serve.add_argument("--run", default="", help="with --stage searches: open this saved search")
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
