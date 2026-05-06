#!/usr/bin/env python3
"""Local web editor for Powerpacks message contacts CSV files."""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DEFAULT_COLUMNS = [
    "phone",
    "name",
    "source",
    "is_in_group_chats",
    "group_names",
    "message_count",
    "last_message",
    "skip",
    "match_status",
    "matched_person_id",
    "matched_name",
    "matched_linkedin_url",
    "match_confidence",
    "match_method",
    "match_reason",
    "review_note",
]

EDITABLE_COLUMNS = [
    "name",
    "skip",
    "match_status",
    "matched_person_id",
    "matched_name",
    "matched_linkedin_url",
    "match_reason",
    "review_note",
]


def read_contacts(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        return DEFAULT_COLUMNS[:], []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or DEFAULT_COLUMNS)
        rows = [{key: value or "" for key, value in row.items()} for row in reader]
    for column in DEFAULT_COLUMNS:
        if column not in fieldnames:
            fieldnames.append(column)
            for row in rows:
                row[column] = ""
    return fieldnames, rows


def atomic_write_contacts(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    os.replace(tmp, path)


def truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def matches_filter(row: dict[str, str], params: dict[str, list[str]]) -> bool:
    q = (params.get("q") or [""])[0].strip().lower()
    status = (params.get("status") or [""])[0].strip().lower()
    source = (params.get("source") or [""])[0].strip().lower()
    skip = (params.get("skip") or [""])[0].strip().lower()
    if q:
        haystack = " ".join(row.get(key, "") for key in ["name", "phone", "matched_name", "matched_linkedin_url", "group_names"]).lower()
        if q not in haystack:
            return False
    if status and (row.get("match_status") or "").strip().lower() != status:
        return False
    if source and source not in (row.get("source") or "").strip().lower():
        return False
    if skip:
        skipped = truthy(row.get("skip", ""))
        if skip == "true" and not skipped:
            return False
        if skip == "false" and skipped:
            return False
    return True


def summarize(rows: list[dict[str, str]]) -> dict[str, int]:
    summary = {"total": len(rows), "matched": 0, "suggested": 0, "unmatched": 0, "skipped": 0}
    for row in rows:
        status = (row.get("match_status") or "unmatched").strip().lower() or "unmatched"
        if status in summary:
            summary[status] += 1
        if truthy(row.get("skip", "")):
            summary["skipped"] += 1
    return summary


def page_html(path: Path, fieldnames: list[str], rows: list[dict[str, str]], params: dict[str, list[str]], saved: bool = False) -> bytes:
    visible = [(idx, row) for idx, row in enumerate(rows) if matches_filter(row, params)]
    summary = summarize(rows)
    q = (params.get("q") or [""])[0]
    status = (params.get("status") or [""])[0]
    source = (params.get("source") or [""])[0]
    skip = (params.get("skip") or [""])[0]
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<title>Powerpacks Contact Review</title>",
        "<style>",
        "body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:24px;color:#1f2933;background:#f8fafc}",
        "header{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:18px}",
        "h1{font-size:22px;margin:0 0 6px}",
        ".meta{color:#52606d;font-size:13px}",
        ".pill{display:inline-block;border:1px solid #cbd5e1;border-radius:999px;padding:4px 8px;background:white;margin-right:6px;font-size:12px}",
        "form.filters{display:flex;gap:8px;flex-wrap:wrap;background:white;border:1px solid #d9e2ec;padding:12px;margin-bottom:16px}",
        "input,select,textarea{font:inherit;border:1px solid #bcccdc;border-radius:4px;padding:6px;background:white}",
        "button{font:inherit;border:1px solid #1f2933;background:#1f2933;color:white;border-radius:4px;padding:6px 10px;cursor:pointer}",
        "table{border-collapse:collapse;width:100%;background:white;border:1px solid #d9e2ec}",
        "th,td{border-bottom:1px solid #edf2f7;padding:8px;vertical-align:top;text-align:left;font-size:13px}",
        "th{position:sticky;top:0;background:#eef2f7;z-index:1}",
        ".rowform{display:grid;grid-template-columns:repeat(4,minmax(130px,1fr));gap:6px;min-width:560px}",
        ".note{grid-column:span 2}",
        ".save{align-self:end}",
        ".saved{background:#e3fcec;border:1px solid #8eedb2;padding:8px;margin-bottom:12px}",
        ".muted{color:#627d98}",
        "</style></head><body>",
        "<header><div>",
        "<h1>Powerpacks Contact Review</h1>",
        f"<div class='meta'>{esc(path)} · showing {len(visible)} of {len(rows)}</div>",
        "</div><div>",
        f"<span class='pill'>matched {summary['matched']}</span>",
        f"<span class='pill'>suggested {summary['suggested']}</span>",
        f"<span class='pill'>unmatched {summary['unmatched']}</span>",
        f"<span class='pill'>skipped {summary['skipped']}</span>",
        "</div></header>",
    ]
    if saved:
        parts.append("<div class='saved'>Saved.</div>")
    parts.extend([
        "<form class='filters' method='get' action='/'>",
        f"<input name='q' placeholder='Search name, phone, group, LinkedIn' value='{esc(q)}'>",
        "<select name='status'><option value=''>Any status</option>",
    ])
    for option in ["matched", "suggested", "unmatched"]:
        selected = " selected" if status == option else ""
        parts.append(f"<option value='{option}'{selected}>{option}</option>")
    parts.extend([
        "</select>",
        f"<input name='source' placeholder='source' value='{esc(source)}'>",
        "<select name='skip'><option value=''>Any skip</option>",
    ])
    for value, label in [("false", "not skipped"), ("true", "skipped")]:
        selected = " selected" if skip == value else ""
        parts.append(f"<option value='{value}'{selected}>{label}</option>")
    parts.extend([
        "</select><button type='submit'>Filter</button><a class='muted' href='/'>clear</a></form>",
        "<table><thead><tr><th>Contact</th><th>Signal</th><th>Match</th><th>Edit</th></tr></thead><tbody>",
    ])
    for idx, row in visible[:500]:
        skip_checked = "checked" if truthy(row.get("skip", "")) else ""
        parts.extend([
            "<tr>",
            f"<td><strong>{esc(row.get('name'))}</strong><br><span class='muted'>{esc(row.get('phone'))}</span><br>{esc(row.get('source'))}</td>",
            f"<td>messages: {esc(row.get('message_count'))}<br>last: {esc(row.get('last_message'))}<br>groups: {esc(row.get('group_names'))}</td>",
            f"<td>{esc(row.get('match_status'))}<br><strong>{esc(row.get('matched_name'))}</strong><br><a href='{esc(row.get('matched_linkedin_url'))}'>{esc(row.get('matched_linkedin_url'))}</a><br><span class='muted'>{esc(row.get('match_reason'))}</span></td>",
            "<td>",
            "<form class='rowform' method='post' action='/update'>",
            f"<input type='hidden' name='row' value='{idx}'>",
            f"<label>Name<br><input name='name' value='{esc(row.get('name'))}'></label>",
            f"<label>Status<br><select name='match_status'>",
        ])
        current_status = row.get("match_status", "")
        for option in ["", "matched", "suggested", "unmatched"]:
            selected = " selected" if current_status == option else ""
            label = option or "(blank)"
            parts.append(f"<option value='{option}'{selected}>{label}</option>")
        parts.extend([
            "</select></label>",
            f"<label>Skip<br><input type='checkbox' name='skip' value='true' {skip_checked}></label>",
            f"<label>Matched name<br><input name='matched_name' value='{esc(row.get('matched_name'))}'></label>",
            f"<label>Person ID<br><input name='matched_person_id' value='{esc(row.get('matched_person_id'))}'></label>",
            f"<label>LinkedIn<br><input name='matched_linkedin_url' value='{esc(row.get('matched_linkedin_url'))}'></label>",
            f"<label class='note'>Reason<br><textarea name='match_reason' rows='2'>{esc(row.get('match_reason'))}</textarea></label>",
            f"<label class='note'>Review note<br><textarea name='review_note' rows='2'>{esc(row.get('review_note'))}</textarea></label>",
            "<button class='save' type='submit'>Save</button>",
            "</form></td></tr>",
        ])
    parts.append("</tbody></table>")
    if len(visible) > 500:
        parts.append("<p class='muted'>Showing first 500 filtered rows. Narrow the filter to edit more.</p>")
    parts.append("</body></html>")
    return "".join(parts).encode("utf-8")


def make_handler(contacts_path: Path):
    class Handler(BaseHTTPRequestHandler):
        def send_bytes(self, body: bytes, content_type: str = "text/html; charset=utf-8", status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/healthz":
                self.send_bytes(b"ok", "text/plain")
                return
            if parsed.path == "/api/summary":
                _, rows = read_contacts(contacts_path)
                self.send_bytes(json.dumps(summarize(rows), indent=2).encode(), "application/json")
                return
            if parsed.path != "/":
                self.send_bytes(b"not found", "text/plain", status=404)
                return
            fieldnames, rows = read_contacts(contacts_path)
            params = urllib.parse.parse_qs(parsed.query)
            saved = (params.get("saved") or [""])[0] == "1"
            self.send_bytes(page_html(contacts_path, fieldnames, rows, params, saved=saved))

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/update":
                self.send_bytes(b"not found", "text/plain", status=404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            form = urllib.parse.parse_qs(body)
            try:
                row_idx = int((form.get("row") or [""])[0])
            except ValueError:
                self.send_bytes(b"bad row", "text/plain", status=400)
                return
            fieldnames, rows = read_contacts(contacts_path)
            if row_idx < 0 or row_idx >= len(rows):
                self.send_bytes(b"row out of range", "text/plain", status=400)
                return
            row = rows[row_idx]
            for column in EDITABLE_COLUMNS:
                if column == "skip":
                    row[column] = "true" if (form.get("skip") or [""])[0] == "true" else "false"
                elif column in form:
                    row[column] = (form.get(column) or [""])[0]
                if column not in fieldnames:
                    fieldnames.append(column)
            atomic_write_contacts(contacts_path, fieldnames, rows)
            self.send_response(303)
            self.send_header("Location", "/?saved=1")
            self.end_headers()

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    return Handler


def cmd_serve(args: argparse.Namespace) -> None:
    contacts_path = Path(args.contacts)
    fieldnames, rows = read_contacts(contacts_path)
    if not contacts_path.exists():
        atomic_write_contacts(contacts_path, fieldnames, rows)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(contacts_path))
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    print(json.dumps({
        "primitive": "review_contacts_web",
        "status": "serving",
        "contacts": str(contacts_path),
        "url": url,
        "row_count": len(rows),
    }, indent=2))
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve a local contacts CSV review editor")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--contacts", default=".powerpacks/messages/contacts.csv")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--open", action="store_true")
    serve.set_defaults(func=cmd_serve)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
