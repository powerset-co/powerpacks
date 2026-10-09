"""The one local Powerpacks server: the React app, setup progress, the Check LinkedIn review, People,
Searches, Accounts and Tasks, on one port.

`start` detaches this server before the project's Python environment exists, so this module imports
only the standard library and the install status modules at load; everything else mounts on the
first request once <root>/.powerpacks/deep-context/deep-context-v2.sqlite exists. Until then `/`
goes to `/install` while setup runs, else to `/searches`. `serve` runs it in the foreground.

The review routes are deep_context_v2's (review/api.py); the People page's are the share stage's
(share/web/server.py). Both read the one store connection, one request at a time.

Changelog:
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
import os
import re
import signal
import site
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from packs.powerset.primitives.install.controller import InstallController, permission_app
from packs.powerset.primitives.install.index_progress import read_index_progress
from packs.powerset.primitives.install.status import InstallStatus
from packs.shared.web.app import AppRoutes

PRIMITIVE = "reconcile_review_web"  # the health identity every Powerpacks page has answered with
DEFAULT_PORT = 8765
START_TIMEOUT_SECONDS = 10
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


NO_NETWORK = {"error": "No network yet. Finish setup to build it.", "retry_command": "bin/deep-context-v2 run"}


def mounted_handler(root: Path) -> type[BaseHTTPRequestHandler]:
    """The full server once the project packages import: the app, Accounts, Tasks and Searches, and
    People and the review once the v2 store exists (they answer "no network yet" before that)."""
    from packs.ingestion.primitives.accounts.api import AccountsApi
    from packs.ingestion.primitives.deep_context_v2.db.store import open_store
    from packs.ingestion.primitives.deep_context_v2.openai import load_env
    from packs.ingestion.primitives.deep_context_v2.review.api import ReviewApi
    from packs.ingestion.primitives.refresh.api import TasksApi
    from packs.ingestion.primitives.share.web.server import share_routes
    from packs.ingestion.primitives.share.web.sets import CloudError
    from packs.search.primitives.deep_search.results_web.api import search_api
    from packs.search.primitives.deep_search.results_web.server import DEFAULT_DEEP_SEARCH_ROOT, _send_json, search_routes
    from packs.shared.web import asks_loop

    load_env()
    data_root: Path = root / ".powerpacks"
    app = AppRoutes()
    # One connection for the review and the People page, one store request at a time: both were
    # written for one request at a time. It opens on the first request after the store appears.
    store_lock = threading.Lock()
    network: dict[str, tuple[ReviewApi, Any]] = {}

    def network_routes() -> tuple[ReviewApi, Any] | None:
        """Called under the store lock. The Ask the Set daemon starts with the store too."""
        if "routes" not in network and (root / STORE).is_file():
            conn = open_store(root / STORE, shared=True)
            network["routes"] = (ReviewApi(conn, data_root), share_routes(conn, data_root))
            threading.Thread(target=asks_loop.run, kwargs={"repo_root": root, "env_file": root / ".env"},
                             name="asks", daemon=True).start()
        return network.get("routes")

    class LazySets:
        """The sets once the store exists; before that an ask says there is no network yet."""
        def __getattr__(self, name: str) -> Any:
            routes = network_routes()  # the ask routes hold the store lock already
            if routes is None:
                raise CloudError(NO_NETWORK["error"])
            return getattr(routes[1].sets, name)

    searches = search_routes(DEFAULT_DEEP_SEARCH_ROOT, base="/searches")
    searches_json = search_api(searches, LazySets(), store_lock)
    accounts = AccountsApi()
    tasks = TasksApi()

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
                    network_routes()  # the daemon the dot reports on starts with the store
                self._relay()
                return
            if app.get(self, parsed) or accounts.get(self, parsed) or tasks.get(self, parsed):
                return
            if searches_json.get(self, parsed) or searches.get(self, parsed):
                return
            with store_lock:
                routes = network_routes()
                if routes is None:
                    _send_json(self, NO_NETWORK, status=HTTPStatus.SERVICE_UNAVAILABLE)
                elif not routes[1].get(self, parsed):
                    routes[0].get(self, parsed)  # answers its own 404

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/relay/connect":
                asks_loop.WAKE.set()
                self._relay()
                return
            if app.post(self, parsed) or accounts.post(self, parsed) or tasks.post(self, parsed) or searches_json.post(self, parsed) or searches.post(self, parsed):
                return
            with store_lock:
                routes = network_routes()
                if routes is None:
                    _send_json(self, NO_NETWORK, status=HTTPStatus.SERVICE_UNAVAILABLE)
                elif not routes[1].post(self, parsed):
                    routes[0].post(self, parsed)

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    return Handler


def persistent_handler(root: Path) -> type[BaseHTTPRequestHandler]:
    """Serve the built app and setup progress at once; mount the rest once the store exists."""
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
    with ThreadingHTTPServer((args.host, args.port), persistent_handler(root)) as server:
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
