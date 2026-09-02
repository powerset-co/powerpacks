"""Stdlib HTTP server for local deep-search result review."""

from __future__ import annotations

import argparse
import csv
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
RESULTS_CSV_FIELDS = (
    "Person ID", "Group", "Labels", "Name", "LinkedIn URL", "Current Role",
    "Current Company", "Location", "Rationale",
)


def _write_results_csv(root: Path, search: SearchResult, raw: object) -> int:
    if not isinstance(raw, dict):
        raise ValueError("assignments must be an object")
    candidates = {}
    for pond in search.ponds:
        for candidate in pond.candidates:
            candidates.setdefault(candidate.person_id, candidate)
    assignments = {
        str(person_id): list(dict.fromkeys(
            str(tag).strip()[:40] for tag in tags if str(tag).strip()))
        for person_id, tags in raw.items()
        if person_id in candidates and isinstance(tags, list)
    }
    path = root / search.run_id / "results.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULTS_CSV_FIELDS)
        writer.writeheader()
        for candidate in candidates.values():
            group = search.group_of(candidate.person_id)
            writer.writerow({
                "Person ID": candidate.person_id,
                "Group": group.label if group else "",
                "Labels": " | ".join(assignments.get(candidate.person_id, ())),
                "Name": candidate.name,
                "LinkedIn URL": candidate.linkedin_url,
                "Current Role": candidate.title,
                "Current Company": candidate.company,
                "Location": candidate.location,
                "Rationale": candidate.reasoning,
            })
    return sum(bool(tags) for tags in assignments.values())


def make_handler(load: Callable[[], tuple[SearchResult, ...]],
                 feedback_sender: FeedbackSender = submit_results_feedback, *,
                 run_root: Path | None = None):

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
            searches = load()
            by_run = {search.run_id: search for search in searches}
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
            path = urllib.parse.urlparse(self.path).path
            if path not in {"/feedback", "/tags"}:
                self.send_bytes(b"not found", "text/plain", status=404)
                return
            origin = (self.headers.get("Origin") or "").strip()
            if origin and (urllib.parse.urlparse(origin).hostname or "").lower() not in {
                    "127.0.0.1", "localhost", "::1"}:
                self.send_bytes(b"cross-origin request rejected", "text/plain", status=403)
                return
            by_run = {search.run_id: search for search in load()}
            length = min(int(self.headers.get("Content-Length", "0")), 32_768)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            run_id = (form.get("run_id") or [""])[0].strip()
            search = by_run.get(run_id)
            if search is None:
                self.send_bytes(b"search not found", "text/plain", status=404)
                return
            if path == "/tags":
                try:
                    if run_root is None:
                        raise ValueError("results root unavailable")
                    assignments = json.loads((form.get("assignments") or ["{}"])[0])
                    labeled = _write_results_csv(run_root, search, assignments)
                except (OSError, ValueError) as exc:
                    self.send_bytes(str(exc).encode("utf-8"), "text/plain", status=400)
                    return
                self.send_json({"ok": True, "labeled": labeled})
                return
            comment = (form.get("comment") or [""])[0].strip()
            person_id = (form.get("person_id") or [""])[0].strip()
            if not comment or len(comment) > 4000:
                self.send_bytes(b"comment must be 1-4000 characters", "text/plain", status=400)
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
    cache: dict[str, object] = {}

    def _stamp() -> tuple[tuple[str, float], ...]:
        pattern = f"{run_dir.name}/results.json" if run_dir else "*/results.json"
        return tuple(sorted((str(path), path.stat().st_mtime) for path in root.glob(pattern)))

    def load() -> tuple[SearchResult, ...]:
        stamp = _stamp()
        if cache.get("stamp") != stamp:
            cache["searches"] = load_searches(root, run_dir.name if run_dir else None)
            cache["stamp"] = stamp
        return cache["searches"]

    if run_dir and not load():
        parser.error(f"no summarized results found in {run_dir}")
    server = ThreadingHTTPServer((args.host, args.port), make_handler(load, run_root=root))
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    payload = {"primitive": "deep_search_results_web", "status": "serving",
               "url": url, "results_root": str(root), "searches": len(load())}
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
