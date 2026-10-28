"""The one local Powerpacks server: the React app, setup progress, the Check LinkedIn review, People,
Searches, Accounts and Tasks, on one port.

`start` detaches this server before the project's Python environment exists, so this module imports
only the standard library and the install status modules at load; everything else mounts on the
first request once the project packages import. Before the network is built, `/`
goes to `/install` while setup runs, else to `/searches`. `serve` runs it in the foreground.

The review routes are deep_context_v2's (review/api.py); the People page's are the share stage's
(share/web/server.py). Both read the one store connection, one request at a time.

Changelog:
- 2026-10-17: start the inbox daemon with the server, independently of mounted routes.
- 2026-10-14: GET/POST /api/search/scope: where chat searches, a Powerset set or the local index (search_scope.py).
- 2026-10-13: POST /api/feedback files the People drawer's flag note with Powerset (feedback.py).
- 2026-10-13: GET /api/install/connect reads the Connect screen's run; its QR is /api/install/qr?connect=1.
- 2026-10-12: open an empty store on the first store-backed request, so Sets and the relay work before import.
- 2026-10-09: GET /api/account answers who is signed in to Powerset for the side nav's footer.
- 2026-10-09: Accounts, Tasks and Searches answer before the network store exists; only People
  and the review wait for it.
- 2026-10-09: `serve --exit-with PID` stops with the desktop app that runs it.
- 2026-10-08: start the Ask the Set daemon when the store-backed routes mount.
- 2026-10-08: GET /api/relay answers the daemon's relay state for the top bar's dot; POST
  /api/relay/connect wakes a signed-out daemon after the page's sign-in.
- 2026-10-07: created from v1's `deep_context/review/cli.py` and `server.py`: the same persistent
  handler, port ownership and health identity (`reconcile_review_web`, kept so an older release's
  page on the port is recognised and replaced), with the v1 review, its event stream and the
  guided re-research gone. deep_context_v2/run.py starts this server for the review.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import site
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from packs.powerset.primitives.install import report
from pathlib import Path
from typing import Any

from packs.powerset.primitives.install.controller import InstallController, permission_app
from packs.powerset.primitives.install.index_progress import read_index_progress
from packs.powerset.primitives.install.status import InstallStatus
from packs.shared.web import account, features, feedback, providers, search_scope
from packs.shared.web.app import AppRoutes

PRIMITIVE = "reconcile_review_web"  # the health identity every Powerpacks page has answered with
DEFAULT_PORT = 8765
START_TIMEOUT_SECONDS = 10
MAX_FEATURE_REQUEST_BYTES = 1024
MAX_PROVIDER_REQUEST_BYTES = 4096
MAX_SCOPE_REQUEST_BYTES = 1024
MAX_FEEDBACK_REQUEST_BYTES = 32_768  # as much as results_web's _save_feedback reads
MAX_TEAM_REQUEST_BYTES = 160 * 1024  # support text, including UTF-8 and JSON escaping
# Every Powerpacks page server's command line, old releases included, so a page another checkout
# left on the port is stopped and replaced; anything else on the port is refused.
PAGE_COMMAND = re.compile(r"reconcile_review_web|deep_context\.review|packs\.shared\.web\.server")
STORE = Path(".powerpacks/deep-context/deep-context-v2.sqlite")
INSTALL_MANIFEST = Path(".powerpacks/install/manifest.json")
STAGES = ("install", "linkedin", "people", "searches")


def url_for(host: str, port: int, stage: str, run_id: str = "") -> str:
    """The page to open: `/` is the review, `/install`, `/people` and `/searches` are their own routes."""
    if stage == "searches" and run_id:
        return f"http://{host}:{port}/searches/run?run_id={urllib.parse.quote(run_id)}"
    if not stage or stage == "linkedin":
        return f"http://{host}:{port}/"
    return f"http://{host}:{port}/{stage}"


def _load_project_packages(root: Path) -> None:
    """Bootstrap and the project environment use the same pinned Python; the venv's packages join the path."""
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    packages = root / ".venv" / "lib" / version / "site-packages"
    if packages.is_dir() and str(packages) not in sys.path:
        site.addsitedir(str(packages))


def mounted_handler(root: Path) -> type[BaseHTTPRequestHandler]:
    """The full server once the project packages import, with an empty store before import."""
    from packs.ingestion.primitives.accounts.api import AccountsApi
    from packs.ingestion.primitives.deep_context_v2.db.store import open_store
    from packs.ingestion.primitives.deep_context_v2.openai import load_env
    from packs.ingestion.primitives.deep_context_v2.review.api import ReviewApi
    from packs.ingestion.primitives.refresh.api import TasksApi
    from packs.ingestion.primitives.share.web.server import share_routes
    from packs.search.primitives.deep_search.results_web.api import search_api
    from packs.search.primitives.deep_search.results_web.server import search_routes
    from packs.shared.web import asks_loop
    from packs.shared.web.memory import MemoryApi
    from packs.shared.web.collaboration import Collaboration, CollaborationRoutes

    load_env()
    data_root: Path = root / ".powerpacks"
    app = AppRoutes()
    # One connection for the review and the People page, one store request at a time: both were
    # written for one request at a time. It opens on the first store-backed request.
    store_lock = threading.Lock()
    network: dict[str, tuple[ReviewApi, Any]] = {}
    collaboration: dict[str, CollaborationRoutes] = {}

    def network_routes() -> tuple[ReviewApi, Any]:
        """Called under the store lock."""
        if "routes" not in network:
            conn = open_store(root / STORE, shared=True)
            network["routes"] = (ReviewApi(conn, data_root), share_routes(conn, data_root))
            collaboration["routes"] = CollaborationRoutes(Collaboration(network["routes"][1].sets, searches))
        return network["routes"]

    class LazySets:
        """Open the sets on the first ask; its routes hold the store lock already."""
        def __getattr__(self, name: str) -> Any:
            return getattr(network_routes()[1].sets, name)

    searches = search_routes(data_root / "deep-search", base="/searches")
    searches_json = search_api(searches, LazySets(), store_lock)
    accounts = AccountsApi()
    tasks = TasksApi()
    memory = MemoryApi(root)

    class Handler(BaseHTTPRequestHandler):
        def _relay(self) -> None:
            body = json.dumps(asks_loop.STATUS).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/relay":
                with store_lock:
                    network_routes()
                self._relay()
                return
            if app.get(self, parsed) or accounts.get(self, parsed) or tasks.get(self, parsed) or memory.get(self, parsed):
                return
            if searches_json.get(self, parsed) or searches.get(self, parsed):
                return
            with store_lock:
                routes = network_routes()
                if not collaboration["routes"].get(self, parsed) and not routes[1].get(self, parsed):
                    routes[0].get(self, parsed)  # answers its own 404

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/relay/connect":
                with store_lock:
                    network_routes()
                asks_loop.WAKE.set()
                self._relay()
                return
            if app.post(self, parsed) or accounts.post(self, parsed) or tasks.post(self, parsed) or memory.post(self, parsed) or searches_json.post(self, parsed) or searches.post(self, parsed):
                return
            with store_lock:
                routes = network_routes()
                if not collaboration["routes"].post(self, parsed) and not routes[1].post(self, parsed):
                    routes[0].post(self, parsed)

    return Handler


def persistent_handler(root: Path) -> type[BaseHTTPRequestHandler]:
    """Serve the built app and setup progress at once; mount the rest once its packages import."""
    app = AppRoutes()
    install = InstallController(root)
    identity = {"primitive": PRIMITIVE, "repo_root": str(root), "pid": os.getpid()}
    mounted: dict[str, type[BaseHTTPRequestHandler]] = {}
    lock = threading.Lock()

    def mount() -> type[BaseHTTPRequestHandler]:
        """The full handler, built on the first request once the project packages import."""
        with lock:
            if "handler" not in mounted:
                _load_project_packages(root)
                mounted["handler"] = mounted_handler(root)
            return mounted["handler"]

    class Handler(BaseHTTPRequestHandler):
        def handle(self) -> None:
            # A request that throws is logged, so it reports itself (report.py).
            try:
                super().handle()
            except Exception:
                logging.getLogger(__name__).exception("Request failed: %s", getattr(self, "path", ""))

        def _json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location: str) -> None:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", location)
            self.end_headers()

        def _team(self, parsed: urllib.parse.ParseResult) -> bool:
            if (self.command, parsed.path) not in {
                    ("GET", "/api/team"), ("GET", "/api/team/thread"), ("POST", "/api/team/ask")}:
                return False
            # Bootstrap starts this server before the project environment exists; the Team page's
            # agent inbox needs that environment, so it joins the path here like the mounted routes.
            _load_project_packages(root)
            from packs.shared.web import team
            me = account.read(root)
            if not account.is_team(me["email"]):
                self._json({"error": "Team requires a @powerset.co account"}, HTTPStatus.FORBIDDEN)
                return True
            origin = (self.headers.get("Origin") or "").strip()
            host = (self.headers.get("Host") or "").strip()
            scheme = "https" if getattr(self.connection, "cipher", None) else "http"
            hostname = (urllib.parse.urlsplit(f"//{host}").hostname or "").lower()
            if origin and (origin != f"{scheme}://{host}" or hostname not in {"127.0.0.1", "localhost", "::1"}):
                self._json({"error": "cross-origin request rejected"}, HTTPStatus.FORBIDDEN)
                return True
            try:
                if parsed.path == "/api/team":
                    self._json(team.members(root, me["email"]))
                elif parsed.path == "/api/team/thread":
                    query = urllib.parse.parse_qs(parsed.query)
                    self._json(team.thread(root, query.get("operator_id", [""])[0]))
                else:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_TEAM_REQUEST_BYTES:
                        raise ValueError("invalid request size")
                    self._json(team.ask(root, self.rfile.read(length)))
            except urllib.error.HTTPError as error:
                body = error.read().decode("utf-8", errors="replace")
                try:
                    reply = json.loads(body)
                except json.JSONDecodeError:
                    reply = {"error": body}
                self._json(reply, error.code)
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            except SystemExit:
                self._json({"error": "Sign in to Powerset again"}, HTTPStatus.UNAUTHORIZED)
            except OSError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_GATEWAY)
            return True

        def _dispatch(self, method: str) -> None:
            try:
                handler = mount()
            except ModuleNotFoundError:
                self._json({"error": "Powerpacks is still being installed. Run bin/setup-python if installation stopped.",
                            "retry_command": "bin/setup-python"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            # The mounted handler owns dispatch; it shares this request's socket state.
            request = handler.__new__(handler)
            request.__dict__ = self.__dict__
            getattr(request, method)()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if self._team(parsed):
                return
            if parsed.path == "/healthz":
                self._json(identity)
                return
            if parsed.path == "/api/install":
                record = InstallStatus(root).read()
                if record["step"] == "index":
                    record["index_progress"] = read_index_progress(root, record["updated_at"])
                qr = install.qr(record)
                if qr:
                    record["action"]["qr_url"] = f"/api/install/qr?t={qr.stat().st_mtime_ns}"
                self._json(record)
                return
            if parsed.path == "/api/install/first-look":
                self._json(install.first_look())
                return
            if parsed.path == "/api/install/first-look/ask":
                self._json(install.ask(urllib.parse.parse_qs(parsed.query)["key"][0]))
                return
            if parsed.path == "/api/install/preflight":
                self._json(install.preflight())
                return
            if parsed.path == "/api/install/connect":
                self._json(install.connection())
                return
            if parsed.path in ("/api/install/qr", "/api/install/linkedin-page", "/api/install/gmail-page", "/api/install/picture"):
                body = (install.linkedin_page_image() if parsed.path.endswith("linkedin-page")
                        else install.gmail_page_image() if parsed.path.endswith("gmail-page")
                        else install.picture(urllib.parse.parse_qs(parsed.query).get("id", [""])[0])
                        if parsed.path.endswith("picture")
                        else install.qr_image(connect="connect=1" in parsed.query.split("&")))
                if body is None:
                    self._json({"error": "No picture yet"}, HTTPStatus.NOT_FOUND)
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/account":
                self._json(account.read(root))
                return
            if parsed.path == "/api/settings/providers":
                self._json(providers.read(root))
                return
            if parsed.path == "/api/features":
                self._json(asdict(features.read(root)))
                return
            if parsed.path == "/api/search/scope":
                _load_project_packages(root)
                try:
                    self._json(search_scope.read(root))
                except Exception as error:
                    logging.getLogger(__name__).exception("Reading search scope failed")
                    self._json({"error": str(error)}, HTTPStatus.BAD_GATEWAY)
                return
            if parsed.path == "/api/status":
                stage = "linkedin" if (root / STORE).is_file() else "install"
                self._json({**identity, "stage": stage})
                return
            if parsed.path == "/" and not (root / STORE).is_file():
                self._redirect("/install" if (root / INSTALL_MANIFEST).is_file() else "/searches")
                return
            if app.get(self, parsed):
                return
            self._dispatch("do_GET")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if self._team(parsed):
                return
            if parsed.path in ("/api/features", "/api/feedback", "/api/search/scope", "/api/settings/providers"):
                origin = self.headers.get("Origin")
                if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host"):
                    self._json({"error": "cross-origin request rejected"}, HTTPStatus.FORBIDDEN)
                    return
            if parsed.path == "/api/feedback":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_FEEDBACK_REQUEST_BYTES:
                        raise ValueError("invalid request size")
                    self._json(*feedback.send(root, self.rfile.read(length)))
                except ValueError as error:
                    self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/features":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_FEATURE_REQUEST_BYTES:
                        raise ValueError("invalid request size")
                    flags = features.FeatureFlags.parse(json.loads(self.rfile.read(length)),
                                                        support=features.read(root).support)
                    self._json(features.save(root, flags))
                except ValueError as error:
                    self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/settings/providers":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_PROVIDER_REQUEST_BYTES:
                        raise ValueError("invalid request size")
                    self._json(providers.save(root, json.loads(self.rfile.read(length))))
                except ValueError as error:
                    self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/search/scope":
                _load_project_packages(root)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_SCOPE_REQUEST_BYTES:
                        raise ValueError("invalid request size")
                    payload = json.loads(self.rfile.read(length))
                    selected = payload.get("selected") if isinstance(payload, dict) else None
                    if not isinstance(selected, str) or not selected:
                        raise ValueError("selected is a set id or local")
                    self._json(search_scope.save(root, selected))
                except ValueError as error:
                    self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                except Exception as error:
                    logging.getLogger(__name__).exception("Saving search scope failed")
                    self._json({"error": str(error)}, HTTPStatus.BAD_GATEWAY)
                return
            if app.post(self, parsed):
                return
            _load_project_packages(root)
            if install.post(self, parsed.path):
                return
            self._dispatch("do_POST")

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            if code != HTTPStatus.OK:
                super().log_request(code, size)

    return Handler


# ---- the port


def health(host: str, port: int) -> dict[str, object] | None:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=1) as response:
            payload = json.loads(response.read())
            return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def owned_listener(host: str, port: int, root: Path) -> dict[str, object] | None:
    """This checkout's page on the port, or None after stopping another checkout's page there."""
    live = health(host, port)
    if live and live.get("primitive") == PRIMITIVE and live.get("repo_root") == str(root):
        return live
    stop_page(host, port)
    return None


def stop_page(host: str, port: int) -> None:
    """Stop the page (any checkout, any release) holding the port; refuse anything else. A review
    starts on a fresh server so it runs the code the checkout has now, not what a page left running
    from before an update has loaded."""
    listeners = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                               capture_output=True, text=True).stdout.split()
    for pid in listeners:
        command = subprocess.run(["ps", "-o", "command=", "-p", pid], capture_output=True, text=True).stdout
        if not PAGE_COMMAND.search(command):
            raise SystemExit(f"Port {port} belongs to another server. Use --port with a free port.")
        os.kill(int(pid), signal.SIGTERM)
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex((host, port)) != 0:
                return
        time.sleep(0.1)
    raise SystemExit(f"Port {port} is still in use. Use --port with a free port.")


def start_server(root: Path, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT, stage: str = "",
                 run_id: str = "", open_browser: bool = False) -> dict[str, object]:
    """Start or reuse the detached server; returns its status, URL, stage, root and pid."""
    root = root.resolve()
    url = url_for(host, port, stage, run_id)
    live = owned_listener(host, port, root)
    if live:
        if open_browser:
            webbrowser.open(url)
        return {"status": "reused", "url": url, "stage": stage, "repo_root": str(root), "pid": live["pid"]}
    directory = root / ".powerpacks" / "install"
    directory.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, *(["-S"] if sys.flags.no_site else []), "-m", "packs.shared.web.server", "serve",
               "--host", host, "--port", str(port)]
    environment = dict(os.environ)
    app = permission_app()
    if app:
        environment["POWERPACKS_PERMISSION_APP"] = app
    with (directory / "server.log").open("ab") as log:
        process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True, env=environment)
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline and process.poll() is None:
        live = health(host, port)
        if live and live.get("primitive") == PRIMITIVE and live.get("repo_root") == str(root):
            if open_browser:
                webbrowser.open(url)
            return {"status": "serving", "url": url, "stage": stage, "repo_root": str(root), "pid": live["pid"]}
        time.sleep(0.1)
    if process.poll() is None:
        process.terminate()
    raise SystemExit(f"Powerpacks could not start. Read {directory / 'server.log'}, fix the error, then retry.")


def _announce(status: str, url: str, **extra: object) -> None:
    print(json.dumps({"primitive": PRIMITIVE, "status": status, "url": url, **extra}, indent=2), flush=True)


def _run_asks(root: Path) -> None:
    while True:
        _load_project_packages(root)
        try:
            from packs.shared.web import asks_loop
        except ModuleNotFoundError:
            time.sleep(2)  # Setup can start the server before the project environment exists.
            continue
        break
    asks_loop.run(repo_root=root, env_file=root / ".env")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The one local Powerpacks page server.")
    parser.add_argument("command", choices=("serve", "start"), help="serve in the foreground, or start detached")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--stage", choices=STAGES, default="", help="the page the printed URL opens")
    parser.add_argument("--run", default="", help="with --stage searches: open this saved search")
    parser.add_argument("--open", action="store_true", help="open the URL in the browser")
    parser.add_argument("--exit-with", type=int, default=0, metavar="PID",
                        help="with serve: stop when this process (the desktop app) is gone")
    args = parser.parse_args(argv)
    root = Path.cwd().resolve()
    if args.command == "start":
        _announce(**start_server(root, host=args.host, port=args.port, stage=args.stage, run_id=args.run,
                                 open_browser=args.open))
        return 0
    url = url_for(args.host, args.port, args.stage, args.run)
    live = owned_listener(args.host, args.port, root)
    if live:
        _announce("reused", url, stage=args.stage, repo_root=str(root), pid=live["pid"])
        if args.open:
            webbrowser.open(url)
        return 0
    report.install(root)
    with ThreadingHTTPServer((args.host, args.port), persistent_handler(root)) as server:
        threading.Thread(target=_run_asks, args=(root,), name="asks", daemon=True).start()
        _announce("serving", url, repo_root=str(root), pid=os.getpid())
        if args.open:
            webbrowser.open(url)
        if args.exit_with:
            threading.Thread(target=_follow, args=(args.exit_with, server), daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nshutting down", file=sys.stderr)
    return 0


def _follow(pid: int, server: ThreadingHTTPServer) -> None:
    """Stop the server once `pid` (the desktop app) is gone, however it went."""
    while True:
        time.sleep(2)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            server.shutdown()
            return
        except PermissionError:
            continue


if __name__ == "__main__":
    raise SystemExit(main())
