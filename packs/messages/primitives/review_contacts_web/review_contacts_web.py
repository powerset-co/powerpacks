#!/usr/bin/env python3
"""Local web editor for Powerpacks message contacts CSV files."""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from packs.shared.csv_io import CsvIO
except ModuleNotFoundError:  # pragma: no cover - direct script fallback
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.shared.csv_io import CsvIO


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
    "enrich_decision",
]

MIN_NAME_TOKENS = 2
MIN_TOKEN_LEN = 2
MIN_TOTAL_ALPHA = 5
NAME_CLEAN_RE = re.compile(r"[^A-Za-zÀ-ÿ'’\-\s]")
MULTISPACE_RE = re.compile(r"\s+")
BLOCKED_LAST_NAME_TOKENS = {"hinge", "raya", "tinder", "bumble"}
VALID_TABS = {"all", "matched", "suggested", "unmatched", "low_signal", "skipped"}


def read_contacts(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        return DEFAULT_COLUMNS[:], []
    with path.open(newline="") as f:
        reader = CsvIO.dict_reader(f)
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


def falsy(value: str) -> bool:
    return value.strip().lower() in {"0", "false", "no", "n", "off"}


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def normalize_name(name: str) -> str:
    cleaned = NAME_CLEAN_RE.sub(" ", name or "")
    return MULTISPACE_RE.sub(" ", cleaned).strip()


def normalize_last_name_tokens(name: str) -> set[str]:
    cleaned = normalize_name(name).lower()
    parts = cleaned.split()
    if len(parts) < 2:
        return set()
    return {token for token in parts[1:] if token}


def normalize_phoneish(value: str) -> str:
    return "".join(ch for ch in value or "" if ch.isdigit())


def has_searchable_name(name: str) -> bool:
    cleaned = normalize_name(name)
    if not cleaned:
        return False
    tokens = [token for token in cleaned.split(" ") if len(token) >= MIN_TOKEN_LEN]
    if len(tokens) < MIN_NAME_TOKENS:
        return False
    return sum(1 for ch in cleaned if ch.isalpha()) >= MIN_TOTAL_ALPHA


def has_match(row: dict[str, str]) -> bool:
    status = (row.get("match_status") or "").strip().lower()
    return status == "matched" or bool((row.get("matched_person_id") or "").strip())


def drop_reason(row: dict[str, str]) -> str:
    raw_name = row.get("name") or ""
    phone_digits = normalize_phoneish(row.get("phone", ""))
    raw_name_digits = normalize_phoneish(raw_name)
    if phone_digits and raw_name_digits and phone_digits.endswith(raw_name_digits):
        return "name is phone"
    name = normalize_name(raw_name)
    if not name:
        return "no name"
    if normalize_last_name_tokens(name) & BLOCKED_LAST_NAME_TOKENS:
        return "blocked name token"
    if not has_searchable_name(name):
        return "bad name"
    return ""


def contact_selected(row: dict[str, str]) -> bool:
    decision = (row.get("enrich_decision") or "").strip().lower()
    if decision in {"yes", "enrich", "include", "true", "1"}:
        return True
    if decision in {"no", "skip", "exclude", "false", "0"}:
        return False
    if truthy(row.get("skip", "")):
        return False
    if has_match(row):
        return True
    return not drop_reason(row)


def row_bucket(row: dict[str, str]) -> str:
    if truthy(row.get("skip", "")) or (row.get("enrich_decision") or "").strip().lower() in {"no", "skip", "exclude", "false", "0"}:
        return "skipped"
    status = (row.get("match_status") or "").strip().lower()
    if has_match(row):
        return "matched"
    if status == "suggested":
        return "suggested"
    if drop_reason(row):
        return "low_signal"
    return "unmatched"


def matches_filter(row: dict[str, str], params: dict[str, list[str]]) -> bool:
    q = (params.get("q") or [""])[0].strip().lower()
    tab = (params.get("tab") or ["all"])[0].strip().lower()
    status = (params.get("status") or [""])[0].strip().lower()
    source = (params.get("source") or [""])[0].strip().lower()
    skip = (params.get("skip") or [""])[0].strip().lower()
    if tab not in VALID_TABS:
        tab = "all"
    if tab != "all" and row_bucket(row) != tab:
        return False
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
    summary = {
        "total": len(rows),
        "selected": 0,
        "matched": 0,
        "suggested": 0,
        "unmatched": 0,
        "low_signal": 0,
        "skipped": 0,
        "named": 0,
        "no_name": 0,
    }
    for row in rows:
        if contact_selected(row):
            summary["selected"] += 1
        name = (row.get("name") or "").strip()
        if name:
            summary["named"] += 1
        else:
            summary["no_name"] += 1
        summary[row_bucket(row)] += 1
    return summary


def page_html(path: Path, fieldnames: list[str], rows: list[dict[str, str]], params: dict[str, list[str]], saved: bool = False) -> bytes:
    visible = [(idx, row) for idx, row in enumerate(rows) if matches_filter(row, params)]
    summary = summarize(rows)
    active_tab = (params.get("tab") or ["all"])[0].strip().lower()
    if active_tab not in VALID_TABS:
        active_tab = "all"
    q = (params.get("q") or [""])[0]
    status = (params.get("status") or [""])[0]
    source = (params.get("source") or [""])[0]
    skip = (params.get("skip") or [""])[0]
    def tab_href(tab: str) -> str:
        tab_params = {
            key: values[0]
            for key, values in params.items()
            if key not in {"tab", "saved"} and values and values[0]
        }
        if tab != "all":
            tab_params["tab"] = tab
        if not tab_params:
            return "/"
        return "/?" + urllib.parse.urlencode(tab_params)

    def tab_link(tab: str, label: str, count_key: str) -> str:
        count = summary[count_key]
        klass = "tab active" if active_tab == tab else "tab"
        return f"<a class='{klass}' href='{esc(tab_href(tab))}'><span>{esc(label)}</span><strong>{count}</strong></a>"

    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>Powerpacks Contact Review</title>",
        "<style>",
        ":root{color-scheme:light;--bg:#f6f7f9;--panel:#ffffff;--line:#d7dde5;--text:#17202a;--muted:#5b6876;--soft:#eef1f5;--ink:#17202a;--ok:#0f766e}",
        "*{box-sizing:border-box}",
        "body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;margin:0;color:var(--text);background:var(--bg)}",
        ".wrap{max-width:1500px;margin:0 auto;padding:28px 24px 40px}",
        "header{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin-bottom:18px}",
        "h1{font-size:24px;line-height:1.15;margin:0 0 7px;font-weight:700}",
        ".meta{color:var(--muted);font-size:13px;line-height:1.4;max-width:920px;overflow-wrap:anywhere}",
        ".stats{display:grid;grid-template-columns:repeat(3,minmax(108px,1fr));gap:8px;min-width:360px}",
        ".stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:9px 10px}",
        ".stat span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}",
        ".stat strong{display:block;font-size:20px;margin-top:2px}",
        ".tabs{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 12px;border-bottom:1px solid var(--line)}",
        ".tab{display:flex;align-items:center;gap:8px;text-decoration:none;color:var(--muted);padding:9px 12px;border:1px solid transparent;border-bottom:0;border-radius:8px 8px 0 0;font-size:13px}",
        ".tab strong{font-size:12px;color:var(--text);background:var(--soft);border-radius:999px;padding:2px 7px}",
        ".tab.active{color:var(--text);background:var(--panel);border-color:var(--line);margin-bottom:-1px}",
        "form.filters{display:flex;gap:8px;flex-wrap:wrap;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;margin-bottom:14px}",
        "input,select,textarea{font:inherit;border:1px solid #b8c1cc;border-radius:6px;padding:7px 8px;background:white;color:var(--text);min-width:0}",
        "textarea{resize:vertical}",
        ".filters input[name=q]{min-width:320px;flex:1}",
        "button{font:inherit;border:1px solid var(--ink);background:var(--ink);color:white;border-radius:6px;padding:7px 12px;cursor:pointer}",
        "button:hover{background:#2a3642}",
        ".clear{display:inline-flex;align-items:center;color:var(--muted);text-decoration:none;padding:0 6px}",
        ".cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px}",
        ".card{appearance:none;text-align:left;color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px;cursor:pointer;min-height:208px;box-shadow:0 1px 2px rgba(15,23,42,.04);transition:border-color .12s,box-shadow .12s,opacity .12s}",
        ".card:hover{border-color:#aeb8c5;box-shadow:0 3px 10px rgba(15,23,42,.08);background:var(--panel)}",
        ".card.selected{border-color:#65b8ac;background:#f2fbf9}",
        ".card.excluded{opacity:.66}",
        ".card.saving{outline:2px solid #f6c76b}",
        ".card-head{display:flex;justify-content:space-between;gap:10px;align-items:flex-start;margin-bottom:10px}",
        ".name{font-size:16px;font-weight:700;line-height:1.25}",
        ".decision{font-size:12px;font-weight:700;border-radius:999px;padding:4px 8px;background:#f1f5f9;color:#334155;white-space:nowrap}",
        ".selected .decision{background:#ccefe8;color:#0f5f59}",
        ".excluded .decision{background:#eceff3;color:#5b6876}",
        ".line{font-size:13px;color:var(--muted);line-height:1.38;margin:3px 0;overflow-wrap:anywhere}",
        ".line strong{color:#334155;font-weight:600}",
        ".match{border-top:1px solid #e5e9ef;margin-top:10px;padding-top:10px}",
        ".match a{color:#0f5f59;text-decoration:none}",
        ".match a:hover{text-decoration:underline}",
        ".badge{display:inline-block;border-radius:999px;padding:2px 7px;background:var(--soft);font-size:12px;color:#334155;margin-top:6px}",
        ".badge.good{background:#d9f3ee;color:#0f5f59}",
        ".badge.warn{background:#fff1d6;color:#7a4b00}",
        ".toast{position:fixed;right:16px;bottom:16px;background:#17202a;color:white;border-radius:8px;padding:9px 12px;font-size:13px;opacity:0;transform:translateY(8px);transition:opacity .15s,transform .15s;pointer-events:none}",
        ".toast.show{opacity:1;transform:translateY(0)}",
        ".saved{background:#e5f8f4;border:1px solid #8bd3c7;color:#0f5f59;border-radius:8px;padding:10px 12px;margin-bottom:12px}",
        ".muted{color:var(--muted)}",
        ".empty{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:24px;color:var(--muted)}",
        "@media(max-width:900px){.wrap{padding:20px 14px}header{display:block}.stats{grid-template-columns:repeat(2,minmax(0,1fr));min-width:0;margin-top:14px}.filters input[name=q]{min-width:180px}.tab{padding:8px 9px}.cards{grid-template-columns:1fr}}",
        "</style></head><body>",
        "<div class='wrap'>",
        "<header><div>",
        "<h1>Powerpacks Contact Review</h1>",
        f"<div class='meta'>{esc(path)} &middot; showing {len(visible)} of {len(rows)}. Click a card to toggle yes/no for enrichment; every change autosaves.</div>",
        "</div><div class='stats'>",
        f"<div class='stat'><span>yes to enrich</span><strong data-count='selected'>{summary['selected']}</strong></div>",
        f"<div class='stat'><span>matched</span><strong>{summary['matched']}</strong></div>",
        f"<div class='stat'><span>suggested</span><strong>{summary['suggested']}</strong></div>",
        f"<div class='stat'><span>unmatched</span><strong>{summary['unmatched']}</strong></div>",
        f"<div class='stat'><span>low signal</span><strong>{summary['low_signal']}</strong></div>",
        f"<div class='stat'><span>skipped</span><strong>{summary['skipped']}</strong></div>",
        f"<div class='stat'><span>no name</span><strong>{summary['no_name']}</strong></div>",
        "</div></header>",
        "<nav class='tabs'>",
        tab_link("all", "All", "total"),
        tab_link("matched", "Matched", "matched"),
        tab_link("suggested", "Suggested", "suggested"),
        tab_link("unmatched", "Unmatched", "unmatched"),
        tab_link("low_signal", "Low signal", "low_signal"),
        tab_link("skipped", "Skipped", "skipped"),
        "</nav>",
    ]
    if saved:
        parts.append("<div class='saved'>Saved.</div>")
    parts.extend([
        "<form class='filters' method='get' action='/'>",
        f"<input type='hidden' name='tab' value='{esc(active_tab if active_tab != 'all' else '')}'>",
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
    ])
    if not visible:
        parts.append("<div class='empty'>No contacts match this view.</div>")
        parts.append("</div></body></html>")
        return "".join(parts).encode("utf-8")
    parts.append("<section class='cards'>")
    for idx, row in visible[:500]:
        selected = contact_selected(row)
        bucket = row_bucket(row)
        reason = drop_reason(row)
        badge_class = "good" if bucket == "matched" else "warn" if bucket in {"suggested", "unmatched"} else ""
        link = row.get("matched_linkedin_url", "")
        link_html = f"<a href='{esc(link)}' target='_blank' rel='noreferrer'>{esc(link)}</a>" if link else "<span class='muted'>no LinkedIn URL</span>"
        decision = "YES" if selected else "NO"
        card_class = "card selected" if selected else "card excluded"
        parts.extend([
            f"<article class='{card_class}' role='button' tabindex='0' data-row='{idx}' data-selected='{str(selected).lower()}'>",
            "<div class='card-head'>",
            f"<div><div class='name'>{esc(row.get('name') or '(no name)')}</div><span class='badge {badge_class}'>{esc(bucket.replace('_', ' '))}</span></div>",
            f"<div class='decision'>{decision}</div>",
            "</div>",
            f"<div class='line'><strong>phone</strong> {esc(row.get('phone'))}</div>",
            f"<div class='line'><strong>source</strong> {esc(row.get('source') or 'unknown')} &middot; <strong>messages</strong> {esc(row.get('message_count') or '0')}</div>",
            f"<div class='line'><strong>last</strong> {esc(row.get('last_message') or 'unknown')}</div>",
            f"<div class='line'><strong>groups</strong> {esc(row.get('group_names') or 'none')}</div>",
            f"<div class='line'><strong>drop rule</strong> {esc(reason or 'none')}</div>",
            "<div class='match'>",
            f"<div class='line'><strong>match</strong> {esc(row.get('match_status') or 'unmatched')} {esc(row.get('matched_name') or '')}</div>",
            f"<div class='line'>{link_html}</div>",
            f"<div class='line'>{esc(row.get('match_reason') or '')}</div>",
            "</div>",
            "</article>",
        ])
    parts.append("</section>")
    if len(visible) > 500:
        parts.append("<p class='muted'>Showing first 500 filtered rows. Narrow the filter to edit more.</p>")
    parts.extend([
        "<div id='toast' class='toast'>Saved</div>",
        "<script>",
        "const toast=document.getElementById('toast');let toastTimer=null;",
        "function showToast(text){toast.textContent=text;toast.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>toast.classList.remove('show'),1100)}",
        "function setCard(card,selected){card.dataset.selected=String(selected);card.classList.toggle('selected',selected);card.classList.toggle('excluded',!selected);card.querySelector('.decision').textContent=selected?'YES':'NO'}",
        "async function toggle(card){const was=card.dataset.selected==='true';const next=!was;card.classList.add('saving');try{const body=new URLSearchParams({row:card.dataset.row,selected:String(next)});const res=await fetch('/toggle',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body});if(!res.ok)throw new Error(await res.text());setCard(card,next);showToast(next?'Saved: yes':'Saved: no');const c=document.querySelector('[data-count=selected]');if(c){c.textContent=String(Number(c.textContent||0)+(next?1:-1))}}catch(e){showToast('Save failed');}finally{card.classList.remove('saving')}}",
        "document.querySelectorAll('.card').forEach(card=>{card.addEventListener('click',e=>{if(e.target.closest('a'))return;toggle(card)});card.addEventListener('keydown',e=>{if(e.key===' '||e.key==='Enter'){e.preventDefault();toggle(card)}})});",
        "</script>",
        "</div></body></html>",
    ])
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
            if parsed.path != "/toggle":
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
            selected = (form.get("selected") or [""])[0].strip().lower()
            if selected not in {"true", "false"}:
                self.send_bytes(b"selected must be true or false", "text/plain", status=400)
                return
            if "skip" not in fieldnames:
                fieldnames.append("skip")
            if "enrich_decision" not in fieldnames:
                fieldnames.append("enrich_decision")
            row["skip"] = "false" if selected == "true" else "true"
            row["enrich_decision"] = "yes" if selected == "true" else "no"
            atomic_write_contacts(contacts_path, fieldnames, rows)
            self.send_bytes(json.dumps({"ok": True, "row": row_idx, "selected": selected == "true"}).encode(), "application/json")

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
