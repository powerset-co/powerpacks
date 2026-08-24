"""Stdlib HTTP server for static, read-only deep-search results."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from packs.powerset.primitives.send_feedback.send_feedback import FeedbackRequest

from . import RESULTS_CSS, RESULTS_JS
from .feedback import build_feedback_request, submit_results_feedback
from .model import SearchResult, load_searches
from .rendering import render_page, render_search_body

FeedbackSender = Callable[[FeedbackRequest], dict[str, object]]


def make_handler(searches: tuple[SearchResult, ...],
                 feedback_sender: FeedbackSender = submit_results_feedback):
    by_run = {search.run_id: search for search in searches}

    class Handler(BaseHTTPRequestHandler):
        def send_bytes(self, body: bytes, content_type: str = "text/html; charset=utf-8",
                       status: int = 200, *, cache: str = "no-store") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, payload: dict[str, object], status: int = 200) -> None:
            self.send_bytes(json.dumps(payload).encode("utf-8"),
                            "application/json; charset=utf-8", status=status)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/healthz":
                self.send_json({"primitive": "deep_search_results_web", "ok": True,
                                "searches": len(searches)})
                return
            if path == "/assets/results.css":
                self.send_bytes(RESULTS_CSS.read_bytes(), "text/css; charset=utf-8",
                                cache="no-cache")
                return
            if path == "/assets/results.js":
                self.send_bytes(RESULTS_JS.read_bytes(), "text/javascript; charset=utf-8",
                                cache="no-cache")
                return
            if path == "/api/search":
                run_id = (urllib.parse.parse_qs(parsed.query).get("run_id") or [""])[0]
                search = by_run.get(run_id)
                if search is None:
                    self.send_bytes(b"search not found", "text/plain", status=404)
                    return
                self.send_bytes(render_search_body(search).encode("utf-8"))
                return
            if path != "/":
                self.send_bytes(b"not found", "text/plain", status=404)
                return
            run_dir = (urllib.parse.parse_qs(parsed.query).get("run_dir") or [""])[0]
            if run_dir:
                search = by_run.get(Path(run_dir).name)
                if search is None:
                    self.send_bytes(b"search not found", "text/plain", status=404)
                    return
                self.send_bytes(render_page((search,)).encode("utf-8"))
                return
            self.send_bytes(render_page(searches).encode("utf-8"))

        def do_POST(self) -> None:  # noqa: N802
            if urllib.parse.urlparse(self.path).path != "/feedback":
                self.send_bytes(b"not found", "text/plain", status=404)
                return
            origin = (self.headers.get("Origin") or "").strip()
            if origin and (urllib.parse.urlparse(origin).hostname or "").lower() not in {
                    "127.0.0.1", "localhost", "::1"}:
                self.send_bytes(b"cross-origin request rejected", "text/plain", status=403)
                return
            length = min(int(self.headers.get("Content-Length", "0")), 32_768)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            comment = (form.get("comment") or [""])[0].strip()
            run_id = (form.get("run_id") or [""])[0].strip()
            person_id = (form.get("person_id") or [""])[0].strip()
            if not comment or len(comment) > 4000:
                self.send_bytes(b"comment must be 1-4000 characters", "text/plain", status=400)
                return
            search = by_run.get(run_id)
            if search is None:
                self.send_bytes(b"search not found", "text/plain", status=404)
                return
            candidate = search.candidate(person_id) if person_id else None
            if person_id and candidate is None:
                self.send_bytes(b"candidate not found", "text/plain", status=404)
                return
            try:
                request = build_feedback_request(search, comment, candidate)
                payload = feedback_sender(request)
            except (OSError, SystemExit, ValueError) as exc:
                self.send_json({"status": "failed", "error": str(exc)}, status=502)
                return
            status = 200 if payload.get("status") == "submitted" else 502
            self.send_json({"ok": status == 200, **payload}, status=status)

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--run-dir", help="show one completed deep-search run")
    scope.add_argument("--root", help="show every summarized run under this root")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--open", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    run_dir = Path(args.run_dir).resolve() if args.run_dir else None
    root = run_dir.parent if run_dir else Path(args.root).resolve()
    searches = load_searches(root)
    if run_dir:
        searches = tuple(search for search in searches if search.run_id == run_dir.name)
        if not searches:
            parser.error(f"no summarized results found in {run_dir}")
    server = ThreadingHTTPServer((args.host, args.port), make_handler(searches))
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    payload = {"primitive": "deep_search_results_web", "status": "serving",
               "url": url, "results_root": str(root), "searches": len(searches)}
    if run_dir:
        payload["run_dir"] = str(run_dir)
    print(json.dumps(payload, indent=2))
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
