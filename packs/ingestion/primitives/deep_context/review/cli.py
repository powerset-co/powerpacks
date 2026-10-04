"""Command-line parsing and dispatch for the review UI.

`serve` starts the one local server (or reuses the live one) and prints the URL to open:
`/` for the review at its current stage, `/?stage=worth|enrich|linkedin|done` for one
stage, `/people`, `/searches`. `start` detaches this same server for installation;
`/install` and its assets use only the standard library. Existing APIs mount lazily
when dependencies and data are ready. `status` prints what the agent should do next.

Changelog:
- 2026-10-02: start one detached server before dependency setup; reuse it for review.
- 2026-10-01: pending enrichment returns to the agent's enrich command.
- 2026-10-01: status routes synthesis directly to enrichment without worth review.
- 2026-10-01: the review is the React page at `/`; before a store exists `/` still
  redirects to /searches, ahead of the shell.
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
import os
import site
import socket
import subprocess
import threading
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from packs.shared.web.app import AppRoutes
from packs.ingestion.primitives.deep_context.db.readiness import CANONICAL_DB, has_parents
from packs.powerset.primitives.install.controller import InstallController, permission_app
from packs.powerset.primitives.install.status import InstallStatus
from packs.powerset.primitives.install.index_progress import read_index_progress

_PRIMITIVE = "reconcile_review_web"
_START_TIMEOUT_SECONDS = 10


# The actions the agent runs itself; every other action waits on the user.
_AGENT_ACTIONS = frozenset({"synthesize", "enrich", "realize"})

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
    route = stage if stage in {"install", "people", "searches"} else f"?stage={stage}"
    return f"http://{host}:{port}/{route}"


def searches_only_handler(root: Path | None = None) -> type[BaseHTTPRequestHandler]:
    """The same server before a deep-context store exists: the saved searches
    at their usual URLs, and People says what to run first."""
    from packs.ingestion.primitives.deep_context.shared.common import load_env
    from packs.search.primitives.deep_search.results_web import server as results_web
    from packs.search.primitives.deep_search.results_web.api import search_api
    from packs.ingestion.primitives.accounts.api import AccountsApi
    from packs.ingestion.primitives.refresh.api import TasksApi

    load_env()
    root = root if root is not None else results_web.DEFAULT_DEEP_SEARCH_ROOT
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
            # No store, no review: `/` is the searches, before the shell can claim it.
            if parsed.path == "/":
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", "/searches")
                self.end_headers()
                return
            if app.get(self, parsed) or accounts.get(self, parsed) or tasks.get(self, parsed) or searches_json.get(self, parsed):
                return
            if parsed.path == "/api/people/rows":
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
    print(json.dumps({"primitive": "reconcile_review_web", "status": status, "url": url, **extra}, indent=2), flush=True)


def workflow_status(**_: object) -> dict[str, object]:
    from packs.ingestion.primitives.deep_context.db.store import open_existing_db
    from .sqlite_adapter import SqliteReviewAdapter

    api = SqliteReviewAdapter(open_existing_db(CANONICAL_DB))
    payload = api.workflow_status()
    commands = {
        "synthesize": "bin/deep-context dry",
        "enrich": "bin/deep-context enrich",
        "review_linkedin": "wait for LinkedIn Yes/No decisions in the review UI",
        "realize": "bin/deep-context realize",
    }
    payload.update({"command": commands[payload["next_action"]], "poll_after_seconds": 60})
    return payload


def _health(host: str, port: int) -> dict[str, object] | None:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=1) as response:
            payload = json.loads(response.read())
            return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def _owned_listener(host: str, port: int, root: Path) -> dict[str, object] | None:
    live = _health(host, port)
    if live and live.get("primitive") == _PRIMITIVE and live.get("repo_root") == str(root):
        return live
    with socket.socket() as probe:
        if probe.connect_ex((host, port)) == 0:
            raise SystemExit(f"Port {port} belongs to another server. Use --port with a free port.")
    return None


def _stage(stage: str | None, root: Path) -> str:
    return stage or ("" if (root / CANONICAL_DB).is_file() else "searches")


def _load_project_packages(root: Path) -> None:
    # Bootstrap and the project environment use the same pinned Python.
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    packages = root / ".venv" / "lib" / version / "site-packages"
    if packages.is_dir() and str(packages) not in sys.path:
        site.addsitedir(str(packages))


def _persistent_handler(root: Path, args: argparse.Namespace) -> type[BaseHTTPRequestHandler]:
    """Serve built assets immediately; mount the existing APIs once their inputs exist."""
    app = AppRoutes()
    mounted: type[BaseHTTPRequestHandler] | None = None
    mounted_review = False
    mount_lock = threading.Lock()
    identity = {"primitive": _PRIMITIVE, "repo_root": str(root), "pid": os.getpid()}

    def mount() -> type[BaseHTTPRequestHandler]:
        nonlocal mounted, mounted_review
        with mount_lock:
            if mounted_review:
                return mounted
            _load_project_packages(root)
            ready = has_parents(root / CANONICAL_DB)
            path = root / CANONICAL_DB
            if mounted is not None and not ready:
                return mounted
            if ready:
                from packs.ingestion.primitives.deep_context.shared.common import load_env
                from packs.ingestion.primitives.deep_context.db.store import StoreError, open_existing_db
                from .server import make_handler

                load_env()
                options = {} if args.confirm_threshold is None else {"confirm_threshold": args.confirm_threshold}
                try:
                    mounted = make_handler(db=open_existing_db(path), run_jobs=True, **options)
                except StoreError as error:
                    raise SystemExit(str(error)) from error
                mounted_review = True
            else:
                mounted = searches_only_handler()
            return mounted

    install = InstallController(root)

    class Handler(BaseHTTPRequestHandler):
        def _json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, method: str) -> None:
            try:
                handler = mount()
            except ModuleNotFoundError:
                self._json({"error": "Powerpacks is still being installed. Run bin/setup-python if installation stopped.",
                            "retry_command": "bin/setup-python"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            except (ValueError, SystemExit) as error:
                self._json({"error": str(error), "retry_command": "bin/deep-context ensure-parents"},
                           HTTPStatus.SERVICE_UNAVAILABLE)
                return
            # The existing handler owns dispatch and super(); share this request's socket state.
            request = handler.__new__(handler)
            request.__dict__ = self.__dict__
            getattr(request, method)()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/healthz":
                self._json(identity)
                return
            if parsed.path == "/api/install":
                record = InstallStatus(root).read()
                if record["step"] == "index":
                    record["index_progress"] = read_index_progress(root, record["updated_at"])
                action = record.get("action")
                if action and action.get("kind") == "permission":
                    action["app_path"] = permission_app()
                qr = install.qr(record)
                if qr:
                    action["qr_url"] = f"/api/install/qr?t={qr.stat().st_mtime_ns}"
                self._json(record)
                return
            if parsed.path == "/api/install/qr":
                body = install.qr_image()
                if body is None:
                    self._json({"error": "Waiting for a fresh QR code"}, HTTPStatus.NOT_FOUND)
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path in {"/", "/api/status"} and not mounted_review:
                try:
                    ready = has_parents(root / CANONICAL_DB)
                except ValueError as error:
                    self._json({"error": str(error), "retry_command": "bin/deep-context ensure-parents"},
                               HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                if not ready:
                    if parsed.path == "/api/status":
                        self._json({**identity, "stage": "install"})
                    else:
                        destination = "/install" if (root / ".powerpacks/install/manifest.json").is_file() else "/searches"
                        self.send_response(HTTPStatus.FOUND)
                        self.send_header("Location", destination)
                        self.end_headers()
                    return
            if app.get(self, parsed):
                return
            self._dispatch("do_GET")

        def do_POST(self) -> None:  # noqa: N802
            _load_project_packages(root)
            if install.post(self, urllib.parse.urlparse(self.path).path):
                return
            self._dispatch("do_POST")

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            if code != HTTPStatus.OK:
                super().log_request(code, size)

    return Handler


def start_server(root: Path, *, host: str = "127.0.0.1", port: int = 8765,
                 stage: str | None = None, run_id: str = "", confirm_threshold: float | None = None,
                 open_browser: bool = False) -> dict[str, object]:
    """Start or reuse the persistent UI process; workflow primitives run in the caller."""
    root = root.resolve()
    selected_stage = _stage(stage, root)
    url = _url(host, port, selected_stage, run_id)
    live = _owned_listener(host, port, root)
    if live:
        if open_browser:
            webbrowser.open(url)
        return {"status": "reused", "url": url, "stage": selected_stage,
                "repo_root": str(root), "pid": live["pid"]}
    directory = root / ".powerpacks" / "install"
    directory.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, *(["-S"] if sys.flags.no_site else []), "-m", "packs.ingestion.primitives.deep_context.review.cli", "serve",
               "--host", host, "--port", str(port)]
    if stage:
        command.extend(["--stage", stage])
    if confirm_threshold is not None:
        command.extend(["--confirm-threshold", str(confirm_threshold)])
    environment = dict(os.environ)
    app = permission_app()
    if app:
        environment["POWERPACKS_PERMISSION_APP"] = app
    with (directory / "server.log").open("ab") as log:
        process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, start_new_session=True, env=environment)
    deadline = time.monotonic() + _START_TIMEOUT_SECONDS
    while time.monotonic() < deadline and process.poll() is None:
        live = _health(host, port)
        if live and live.get("primitive") == _PRIMITIVE and live.get("repo_root") == str(root):
            if open_browser:
                webbrowser.open(url)
            return {"status": "serving", "url": url, "stage": selected_stage,
                    "repo_root": str(root), "pid": live["pid"]}
        time.sleep(0.1)
    if process.poll() is None:
        process.terminate()
    raise SystemExit(f"Powerpacks could not start. Read {directory / 'server.log'}, fix the error, then retry.")


def cmd_start(args: argparse.Namespace) -> None:
    result = start_server(Path.cwd(), host=args.host, port=args.port, stage=args.stage,
                          run_id=args.run, confirm_threshold=args.confirm_threshold, open_browser=args.open)
    _announce(**result)


def cmd_serve(args: argparse.Namespace) -> None:
    root = Path.cwd().resolve()
    stage = _stage(args.stage, root)
    url = _url(args.host, args.port, stage, args.run)
    live = _owned_listener(args.host, args.port, root)
    if live:
        _announce("reused", url, stage=stage, repo_root=str(root), pid=live["pid"])
        if args.open:
            webbrowser.open(url)
        return
    with ThreadingHTTPServer((args.host, args.port), _persistent_handler(root, args)) as server:
        host, port = server.server_address
        url = _url(host, port, stage, args.run)
        _announce("serving", url, repo_root=str(root), pid=os.getpid())
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
    for command in ("serve", "start"):
        serve = sub.add_parser(command)
        serve.add_argument("--confirm-threshold", type=float)
        serve.add_argument("--host", default="127.0.0.1")
        serve.add_argument("--port", type=int, default=8765)
        serve.add_argument("--stage", choices=("install", "worth", "enrich", "linkedin", "done", "people", "searches"))
        serve.add_argument("--run", default="", help="with --stage searches: open this saved search")
        serve.add_argument("--open", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("--wait", action="store_true")
    status.add_argument("--timeout", type=int, default=900)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "status":
        cmd_status(args)
    elif args.command == "start":
        cmd_start(args)
    else:
        cmd_serve(args if args.command == "serve" else parser.parse_args(["serve", *(argv or [])]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
