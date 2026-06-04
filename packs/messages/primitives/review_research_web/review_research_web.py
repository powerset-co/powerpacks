#!/usr/bin/env python3
"""Local web reviewer for messages deep-research CSVs."""

from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import html
import json
import os
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


DEFAULT_COLUMNS = [
    "bucket",
    "handle",
    "full_name",
    "phone_e164",
    "area_code",
    "total_messages",
    "imessage_message_count",
    "whatsapp_message_count",
    "message_source",
    "last_message",
    "imessage_last_message",
    "whatsapp_last_message",
    "group_names",
    "location_city",
    "location_country",
    "top_titles",
    "top_companies",
    "top_title_company_pairs",
    "schools",
    "short_reason",
    "identity_risk",
    "signals",
    "retarget_hint",
    "retarget_status",
    "retarget_handle",
    "retarget_researched_at",
    "retarget_linkedin_url",
    "retarget_name_confidence",
    "retarget_notes",
    "retarget_profile_status",
    "exclude",
    "enrich_decision",
    "in_network",
    "network_match_status",
    "network_person_id",
    "network_name",
    "network_linkedin_url",
    "network_match_confidence",
    "network_match_method",
    "network_match_reason",
    "review_source",
]

VALID_TABS = {"yes", "maybe", "no", "in_network"}

TAB_INFO = {
    "yes": {
        "label": "Yes",
        "icon": """<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M20 6 9 17l-5-5'/><circle cx='12' cy='12' r='10'/></svg>""",
        "body": "These contacts are strong candidates for your Personal Network.",
    },
    "maybe": {
        "label": "Maybe",
        "icon": """<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><circle cx='12' cy='12' r='10'/><path d='M9.1 9a3 3 0 1 1 4.8 2.4c-.9.6-1.4 1.2-1.4 2.1'/><path d='M12 17h.01'/></svg>""",
        "body": "These contacts may be worth adding, but need your review.",
    },
    "no": {
        "label": "No",
        "icon": """<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><circle cx='12' cy='12' r='10'/><path d='m15 9-6 6'/><path d='m9 9 6 6'/></svg>""",
        "body": "These contacts don't appear to be a strong fit, but you can still approve them for your Personal Network.",
    },
    "in_network": {
        "label": "In Network",
        "icon": """<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2'/><circle cx='9' cy='7' r='4'/><path d='M22 21v-2a4 4 0 0 0-3-3.87'/><path d='M16 3.13a4 4 0 0 1 0 7.75'/></svg>""",
        "body": "These contacts already match someone in your Personal Network. Their phone number and message activity will be associated with their profiles. Deselect any you'd like to skip.",
    },
}

SEARCH_ICON = """<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' aria-hidden='true'><circle cx='11' cy='11' r='8'/><line x1='21' y1='21' x2='16.65' y2='16.65'/></svg>"""

REVIEW_CSS = """
:root{
  color-scheme:light;
  --bg:#F7F3EE;--bg-soft:#F7F2ED;--surface:#FDFAF7;--surface-2:#FBF6F1;
  --border:#E8DDD6;--border-strong:#DDD4C8;--input:#E2D9D0;
  --fg:#1A1614;--text-strong:#3A2E2A;--text-muted:#5C4D44;--muted-strong:#746358;
  --muted:#F0EAE2;--placeholder:#B8A898;
  --red:#F2502A;--red-dark:#C73E1F;--red-tint:#FEF2F0;--red-border:#FECDC9;
  --success:#15803D;--success-tint:#F0FDF4;--success-border:#BBF7D0;
  --warning:#C2410C;--warning-tint:#FFF7ED;--warning-border:#FED7AA;
  --danger:#B91C1C;--danger-tint:#FEF2F2;--danger-border:#FECACA;
  --info:#0369A1;--info-tint:#EEF4FB;--info-border:#C9DEF4;
  --font:-apple-system,BlinkMacSystemFont,Segoe UI,system-ui,sans-serif;
}
*{box-sizing:border-box}
body,button,input,textarea{font-family:var(--font)}
body{margin:0;background:var(--bg);color:var(--fg);line-height:1.6;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
.wrap{max-width:1480px;margin:0 auto;padding:40px 28px 48px}
header{margin-bottom:20px}
h1{font-size:22px;font-weight:700;letter-spacing:-.02em;line-height:1.15;color:var(--fg);margin:0 0 6px}
.meta{color:var(--muted-strong);font-size:13.5px;line-height:1.4;overflow-wrap:anywhere}

.tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.tab{display:inline-flex;gap:7px;align-items:center;padding:6px 12px;border:1px solid var(--input);border-radius:20px;text-decoration:none;color:var(--text-muted);background:var(--surface);font-size:13px;font-weight:600;box-shadow:0 1px 3px rgba(0,0,0,.04);transition:background .15s,border-color .15s,color .15s,transform .15s}
.tab:hover{background:var(--muted);border-color:#D0C4BA;transform:translateY(-1px)}
.tab.active{background:var(--red-tint);border-color:var(--red-border);color:var(--red)}
.tab.yes.active,.tab.in_network.active{background:var(--success-tint);border-color:var(--success-border);color:var(--success)}
.tab strong{font-size:11.5px;font-weight:600;color:inherit;background:rgba(255,255,255,.68);border-radius:20px;padding:1px 7px}

.info-panel{display:flex;align-items:flex-start;gap:12px;background:var(--surface);border:1px solid var(--border);border-left-width:3px;border-radius:16px;padding:16px 18px;margin:0 0 16px;box-shadow:0 1px 3px rgba(0,0,0,.04),0 4px 12px rgba(0,0,0,.04)}
.info-panel h2{font-size:15px;font-weight:650;letter-spacing:-.01em;line-height:1.25;margin:0 0 3px;color:var(--fg)}
.info-panel p{margin:0;color:var(--text-muted);font-size:13px;line-height:1.45}
.info-body{flex:1}.info-icon{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;border-radius:999px;flex:0 0 auto}
.info-icon svg{width:18px;height:18px}
.bulk-actions{display:flex;gap:8px;flex-wrap:wrap;margin-left:auto}
.bulk-actions button{font:inherit;border:1.5px solid var(--border-strong);background:var(--surface);color:var(--text-strong);border-radius:10px;font-size:12.5px;font-weight:600;padding:7px 11px;cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.05);transition:background .12s,border-color .12s,color .12s,transform .12s}
.bulk-actions button:hover{background:var(--muted);border-color:#C8BDB2;color:var(--text-strong);transform:translateY(-1px)}.bulk-actions button:disabled{opacity:.6;cursor:wait;transform:none}
.info-panel.yes,.info-panel.in_network{border-left-color:var(--success-border)}.info-panel.yes .info-icon,.info-panel.in_network .info-icon{background:var(--success-tint);color:var(--success);border:1px solid var(--success-border)}
.info-panel.maybe{border-left-color:var(--warning-border)}.info-panel.maybe .info-icon{background:var(--warning-tint);color:var(--warning);border:1px solid var(--warning-border)}
.info-panel.no{border-left-color:var(--danger-border)}.info-panel.no .info-icon{background:var(--danger-tint);color:var(--danger);border:1px solid var(--danger-border)}

.search{position:relative;margin-bottom:18px}
.search svg{position:absolute;left:14px;top:50%;transform:translateY(-50%);width:16px;height:16px;color:#8C7B70;pointer-events:none}
.search input{width:100%;font:inherit;border:1.5px solid var(--input);border-radius:12px;padding:11px 14px 11px 40px;background:var(--bg-soft);color:var(--fg);font-size:14px;outline:none;transition:border-color .15s,box-shadow .15s,background .15s}
.search input:focus{border-color:var(--red);background:var(--surface);box-shadow:0 0 0 3px rgba(242,80,42,.1)}
.search input::placeholder{color:var(--placeholder)}

.badge,.decision{display:inline-flex;align-items:center;gap:5px;height:auto;line-height:1.2;border-radius:999px;padding:3px 9px;font-size:11.5px;font-weight:600;white-space:nowrap}
.badge.retarget{background:var(--info-tint);color:var(--info);border:1px solid var(--info-border)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:14px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:16px;min-height:292px;padding:20px;cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.04),0 4px 12px rgba(0,0,0,.04);transition:border-color .15s,box-shadow .15s,opacity .12s,background .12s,transform .15s}
.card:hover{border-color:#D4C8BC;box-shadow:0 3px 12px rgba(0,0,0,.08);transform:translateY(-1px)}
.card.selected{background:var(--surface);border-color:var(--success-border)}.card.excluded{opacity:.48}.card.saving{outline:2px solid rgba(242,80,42,.38)}
.head{display:flex;justify-content:space-between;gap:10px;margin-bottom:12px}.name-row{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.name{font-weight:600;font-size:17px;letter-spacing:-.01em;line-height:1.22;color:var(--fg)}
.li-icon{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:4px;background:#0A66C2;color:#fff;text-decoration:none;font-size:12px;font-weight:900;line-height:1}.li-icon:hover{background:#004182;text-decoration:none}
.decision{background:var(--muted);color:var(--text-muted);border:1px solid var(--border)}
.selected .decision{background:var(--success-tint);color:var(--success);border:1px solid var(--success-border)}
.excluded .decision{background:var(--danger-tint);color:var(--danger);border:1px solid var(--danger-border)}
.line{font-size:13px;color:var(--text-muted);line-height:1.48;margin:4px 0;overflow-wrap:anywhere}.line strong{color:var(--text-strong);font-weight:600}
.profile{border-top:1px solid var(--border);margin-top:12px;padding-top:12px}.profile a{color:var(--red);text-decoration:none}.profile a:hover{text-decoration:underline}
.hint{margin-top:12px}.hint label{display:block;color:var(--text-strong);font-size:12px;font-weight:650;margin-bottom:6px}
.hint textarea{width:100%;min-height:72px;resize:vertical;border:1.5px solid var(--input);border-radius:12px;background:var(--bg-soft);color:var(--fg);font-size:14px;line-height:1.4;padding:10px 12px;outline:none}
.hint textarea:focus{border-color:var(--red);background:var(--surface);box-shadow:0 0 0 3px rgba(242,80,42,.1)}
.hint-actions{display:flex;align-items:center;gap:8px;margin-top:8px}.hint button{border:1.5px solid var(--border-strong);background:var(--surface);color:var(--text-strong);border-radius:10px;font-size:12.5px;line-height:1;font-weight:600;padding:7px 11px;cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.05);transition:background .12s,border-color .12s,color .12s,transform .12s}.hint button:hover{background:var(--muted);border-color:#C8BDB2;color:var(--text-strong);transform:translateY(-1px)}.hint .hint-status{display:inline-block;min-height:16px;color:var(--muted-strong);font-size:11px}
.empty{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:24px;color:var(--text-muted);box-shadow:0 1px 3px rgba(0,0,0,.04)}
.toast{position:fixed;right:16px;bottom:16px;background:var(--fg);color:#fff;border-radius:10px;padding:9px 12px;font-size:13px;opacity:0;transform:translateY(8px);transition:opacity .15s,transform .15s;pointer-events:none;box-shadow:0 8px 24px rgba(0,0,0,.18)}.toast.show{opacity:1;transform:translateY(0)}
@media(max-width:900px){.wrap{padding:20px 14px}.info-panel{display:block}.info-icon{margin-bottom:8px}.bulk-actions{margin:10px 0 0}.cards{grid-template-columns:1fr}}
""".strip()


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def truthy(value: str) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def falsy(value: str) -> bool:
    return (value or "").strip().lower() in {"0", "false", "no", "n", "off"}


def bucket_label(bucket: str) -> str:
    raw = (bucket or "").strip().lower()
    if raw in {"yes", "confident"}:
        return "yes"
    if raw in {"maybe", "medium", "review"}:
        return "maybe"
    if raw == "no":
        return "no"
    return "no"


def upload_bucket(row: dict[str, str]) -> str:
    """Effective upload bucket after explicit review decisions."""
    exclude = (row.get("exclude") or "").strip().lower()
    if truthy(exclude):
        return "no"
    if falsy(exclude):
        return "yes"
    return bucket_label(row.get("bucket", ""))


def is_in_network(row: dict[str, str]) -> bool:
    raw = (row.get("in_network") or "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return bool((row.get("network_person_id") or "").strip())


def has_review_name(row: dict[str, str]) -> bool:
    return any(
        str(row.get(key) or "").strip()
        for key in ("network_name", "full_name", "retarget_name", "matched_name")
    )


def row_tab(row: dict[str, str]) -> str:
    if is_in_network(row):
        return "in_network"
    exclude = (row.get("exclude") or "").strip().lower()
    if truthy(exclude):
        return "no"
    if falsy(exclude):
        return "yes"
    if not has_review_name(row):
        return "no"
    return bucket_label(row.get("bucket", ""))


def is_selected(row: dict[str, str]) -> bool:
    exclude = (row.get("exclude") or "").strip().lower()
    if truthy(exclude):
        return False
    if falsy(exclude):
        return True
    if is_in_network(row):
        return True
    if not has_review_name(row):
        return False
    return bucket_label(row.get("bucket", "")) == "yes"


def explicit_decision(row: dict[str, str]) -> str:
    exclude = (row.get("exclude") or "").strip().lower()
    if truthy(exclude):
        return "skip"
    if falsy(exclude):
        return "include"
    enrich = (row.get("enrich_decision") or "").strip().lower()
    if enrich in {"yes", "include"}:
        return "include"
    if enrich in {"no", "skip"}:
        return "skip"
    return "undecided"


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        return DEFAULT_COLUMNS[:], []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or DEFAULT_COLUMNS)
        rows = [{key: value or "" for key, value in row.items()} for row in reader]
    for column in DEFAULT_COLUMNS:
        if column not in fieldnames:
            fieldnames.append(column)
            for row in rows:
                row[column] = ""
    return fieldnames, rows


def atomic_write(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    os.replace(tmp, path)


def load_profile(research_dir: Path | None, handle: str) -> dict[str, Any]:
    if not research_dir or not handle:
        return {}
    path = research_dir / handle / "01_research_parallel.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def split_pipe(value: str, limit: int = 4) -> list[str]:
    return [part.strip() for part in (value or "").split("|") if part.strip()][:limit]


def parse_int(value: str) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def positions_from_profile(profile: dict[str, Any]) -> str:
    pairs: list[str] = []
    for pos in (profile.get("positions") or [])[:4]:
        title = (pos.get("title") or "").strip()
        company = (pos.get("company_name") or "").strip()
        if title and company:
            pairs.append(f"{title} @ {company}")
        elif title:
            pairs.append(title)
        elif company:
            pairs.append(f"@ {company}")
    return " | ".join(pairs)


def schools_from_profile(profile: dict[str, Any]) -> str:
    schools: list[str] = []
    for edu in (profile.get("education") or [])[:3]:
        school = (edu.get("school_name") or "").strip()
        if school:
            schools.append(school)
    return " | ".join(schools)


def social_url(profile: dict[str, Any], key: str) -> str:
    social = profile.get("social") or {}
    return (social.get(key) or "").strip()


def row_view(row: dict[str, str], research_dir: Path | None) -> dict[str, str]:
    profile = load_profile(research_dir, row.get("handle", ""))
    person = profile.get("person") or {}
    location = profile.get("location") or {}
    summary = profile.get("summary") or {}
    metadata = profile.get("metadata") or {}
    is_retargeted = bool((row.get("retarget_status") or "").strip())

    if is_retargeted:
        # Retarget results are merged back into the review CSV, while the
        # original handle still points at the first-pass profile artifact.
        # Prefer CSV fields so the card reflects the latest re-research pass.
        name = (row.get("full_name") or person.get("full_name") or "").strip() or "Unknown"
        city = (row.get("location_city") or location.get("city") or "").strip()
        country = (row.get("location_country") or location.get("country") or "").strip()
        title_pairs = row.get("top_title_company_pairs", "") or positions_from_profile(profile)
        schools = row.get("schools", "") or schools_from_profile(profile)
        linkedin = row.get("retarget_linkedin_url", "") or row.get("linkedin_url", "") or row.get("network_linkedin_url", "") or social_url(profile, "linkedin_url")
        notes = row.get("retarget_notes", "") or metadata.get("research_notes") or row.get("research_notes", "")
        summary_text = row.get("summary", "") or summary.get("text") or notes
    else:
        name = (row.get("network_name") or person.get("full_name") or row.get("full_name") or "").strip() or "Unknown"
        city = (location.get("city") or row.get("location_city") or "").strip()
        country = (location.get("country") or row.get("location_country") or "").strip()
        title_pairs = positions_from_profile(profile) or row.get("top_title_company_pairs", "")
        schools = schools_from_profile(profile) or row.get("schools", "")
        linkedin = social_url(profile, "linkedin_url") or row.get("linkedin_url", "") or row.get("network_linkedin_url", "")
        notes = metadata.get("research_notes") or row.get("research_notes", "")
        summary_text = summary.get("text") or row.get("summary", "")

    github = social_url(profile, "github_url") or row.get("github_url", "")
    return {
        "name": name,
        "location": ", ".join(part for part in [city, country] if part) or "",
        "title_pairs": title_pairs,
        "schools": schools,
        "linkedin_url": linkedin,
        "github_url": github,
        "summary": (summary_text or "").strip(),
        "research_notes": (notes or "").strip(),
    }


def review_display_name(row: dict[str, str], view: dict[str, str]) -> str:
    for value in (
        view.get("name", ""),
        row.get("network_name", ""),
        row.get("full_name", ""),
    ):
        text = str(value or "").strip()
        if text and text.lower() != "unknown":
            return text
    phone = str(row.get("phone_e164") or "").strip()
    if phone:
        return f"Contact {phone}"
    handle = str(row.get("handle") or "").strip()
    if handle:
        return handle
    return "Unnamed contact"


def matches_filter(row: dict[str, str], params: dict[str, list[str]], research_dir: Path | None) -> bool:
    tab = (params.get("tab") or ["yes"])[0].strip().lower()
    q = (params.get("q") or [""])[0].strip().lower()
    if tab not in VALID_TABS:
        tab = "yes"
    if row_tab(row) != tab:
        return False
    if q:
        view = row_view(row, research_dir)
        if q not in view["name"].lower():
            return False
    return True


def summarize(rows: list[dict[str, str]]) -> dict[str, int]:
    out = {"in_network": 0, "yes": 0, "maybe": 0, "no": 0}
    for row in rows:
        tab = row_tab(row)
        if tab == "in_network":
            if is_selected(row):
                out["in_network"] += 1
        else:
            out[tab] += 1
    return out


def app_summary(rows: list[dict[str, str]]) -> dict[str, int]:
    out = {
        "total": len(rows),
        "included": 0,
        "skipped": 0,
        "undecided": 0,
        "yes": 0,
        "maybe": 0,
        "no": 0,
        "inNetwork": 0,
        "retargetFeedback": 0,
        "researchSelected": 0,
    }
    for row in rows:
        if explicit_decision(row) == "include" and not is_in_network(row):
            out["researchSelected"] += 1
        if is_selected(row):
            out["included"] += 1
        else:
            out["skipped"] += 1
        if explicit_decision(row) == "undecided":
            out["undecided"] += 1
        if (row.get("retarget_hint") or "").strip():
            out["retargetFeedback"] += 1
        tab = row_tab(row)
        if tab == "in_network":
            if is_selected(row):
                out["inNetwork"] += 1
        else:
            out[tab] += 1
    return out


def apply_bulk_selection(rows: list[dict[str, str]], tab: str, selected: bool) -> int:
    """Apply an include/exclude decision to every CSV row in a logical tab.

    For in-network, target every matched network row even if it is currently
    excluded and therefore appears under No. This keeps select all/none whole
    rather than limited to the rendered or currently selected page.
    """
    if tab not in VALID_TABS:
        raise ValueError(f"unknown tab: {tab}")
    changed = 0
    next_exclude = "no" if selected else "yes"
    for row in rows:
        target = is_in_network(row) if tab == "in_network" else row_tab(row) == tab
        if not target:
            continue
        if (row.get("exclude") or "") != next_exclude:
            changed += 1
        row["exclude"] = next_exclude
    return changed


def app_row(row: dict[str, str], index: int, research_dir: Path | None) -> dict[str, Any]:
    view = row_view(row, research_dir)
    linkedin = view["linkedin_url"] or row.get("network_linkedin_url", "")
    return {
        "index": index,
        "bucket": row.get("bucket", ""),
        "tab": row_tab(row),
        "decision": explicit_decision(row),
        "selected": is_selected(row),
        "handle": row.get("handle", ""),
        "fullName": review_display_name(row, view),
        "phone": row.get("phone_e164", ""),
        "messageSource": row.get("message_source", ""),
        "totalMessages": parse_int(row.get("total_messages", "")),
        "imessageMessages": parse_int(row.get("imessage_message_count", "")),
        "whatsappMessages": parse_int(row.get("whatsapp_message_count", "")),
        "groupNames": row.get("group_names", ""),
        "networkName": row.get("network_name", ""),
        "networkLinkedInUrl": linkedin,
        "networkMatchStatus": row.get("network_match_status", ""),
        "networkMatchConfidence": row.get("network_match_confidence", ""),
        "titleCompanyPairs": view["title_pairs"] or row.get("top_title_company_pairs", ""),
        "schools": view["schools"] or row.get("schools", ""),
        "signals": row.get("signals", ""),
        "identityRisk": row.get("identity_risk", ""),
        "shortReason": row.get("short_reason", ""),
        "retargetHint": row.get("retarget_hint", ""),
        "retargetStatus": row.get("retarget_status", ""),
        "retargetLinkedInUrl": row.get("retarget_linkedin_url", ""),
        "retargetNotes": row.get("retarget_notes", ""),
        "reviewSource": row.get("review_source", ""),
    }


def matches_app_filter(row: dict[str, str], filter_name: str, query: str, research_dir: Path | None) -> bool:
    tab = row_tab(row)
    if filter_name in VALID_TABS and tab != filter_name:
        return False
    if filter_name == "included" and not is_selected(row):
        return False
    if filter_name == "skipped" and is_selected(row):
        return False
    if filter_name == "undecided" and explicit_decision(row) != "undecided":
        return False
    if filter_name == "feedback" and not (row.get("retarget_hint") or "").strip():
        return False
    if filter_name not in {*VALID_TABS, "all", "included", "skipped", "undecided", "feedback"}:
        return False
    q = query.strip().lower()
    if not q:
        return True
    view = row_view(row, research_dir)
    haystack = " ".join([
        view["name"],
        row.get("phone_e164", ""),
        row.get("network_name", ""),
        view["linkedin_url"],
        view["title_pairs"],
        view["schools"],
        row.get("short_reason", ""),
        row.get("retarget_hint", ""),
        row.get("retarget_notes", ""),
    ]).lower()
    return q in haystack


def review_api_response(
    csv_path: Path,
    rows: list[dict[str, str]],
    *,
    filter_name: str,
    query: str,
    offset: int,
    limit: int,
    research_dir: Path | None,
) -> dict[str, Any]:
    filtered = [
        (idx, row)
        for idx, row in enumerate(rows)
        if matches_app_filter(row, filter_name, query, research_dir)
    ]
    window = filtered[offset:offset + limit]
    stat = csv_path.stat() if csv_path.exists() else None
    return {
        "path": str(csv_path),
        "exists": csv_path.exists(),
        "updatedAt": None if not stat else datetime.datetime.fromtimestamp(stat.st_mtime, tz=datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "sizeBytes": None if not stat else stat.st_size,
        "rows": [app_row(row, idx, research_dir) for idx, row in window],
        "counts": app_summary(rows),
        "filteredCount": len(filtered),
        "offset": offset,
        "limit": limit,
        "hasMore": offset + len(window) < len(filtered),
    }


def cmd_json(args: argparse.Namespace) -> None:
    csv_path = Path(args.csv)
    _, rows = read_rows(csv_path)
    research_dir = Path(args.research_dir) if args.research_dir else None
    print(json.dumps(review_api_response(
        csv_path,
        rows,
        filter_name=args.filter,
        query=args.query,
        offset=max(0, args.offset),
        limit=max(1, args.limit),
        research_dir=research_dir,
    )))


def cmd_toggle(args: argparse.Namespace) -> None:
    csv_path = Path(args.csv)
    fieldnames, rows = read_rows(csv_path)
    if args.row < 0 or args.row >= len(rows):
        raise SystemExit(f"row out of range: {args.row}")
    if "exclude" not in fieldnames:
        fieldnames.append("exclude")
    rows[args.row]["exclude"] = "no" if args.selected else "yes"
    atomic_write(csv_path, fieldnames, rows)
    cmd_json(args)


def cmd_hint(args: argparse.Namespace) -> None:
    csv_path = Path(args.csv)
    fieldnames, rows = read_rows(csv_path)
    if args.row < 0 or args.row >= len(rows):
        raise SystemExit(f"row out of range: {args.row}")
    if "retarget_hint" not in fieldnames:
        fieldnames.append("retarget_hint")
    rows[args.row]["retarget_hint"] = args.hint.strip()
    atomic_write(csv_path, fieldnames, rows)
    cmd_json(args)


def cmd_bulk_toggle(args: argparse.Namespace) -> None:
    csv_path = Path(args.csv)
    fieldnames, rows = read_rows(csv_path)
    if "exclude" not in fieldnames:
        fieldnames.append("exclude")
    changed = apply_bulk_selection(rows, args.tab, args.selected)
    atomic_write(csv_path, fieldnames, rows)
    response = review_api_response(
        csv_path,
        rows,
        filter_name=args.filter,
        query=args.query,
        offset=max(0, args.offset),
        limit=max(1, args.limit),
        research_dir=Path(args.research_dir) if args.research_dir else None,
    )
    response["changed"] = changed
    print(json.dumps(response))


def render_info_panel(active_tab: str) -> str:
    info = TAB_INFO[active_tab]
    bulk_actions = ""
    if active_tab == "in_network":
        bulk_actions = (
            "<div class='bulk-actions' aria-label='Bulk in-network actions'>"
            "<button type='button' data-bulk-selected='true'>Select all</button>"
            "<button type='button' data-bulk-selected='false'>Select none</button>"
            "</div>"
        )
    return (
        f"<section class='info-panel {esc(active_tab)}' aria-label='{esc(info['label'])} guidance'>"
        f"<div class='info-icon' aria-hidden='true'>{info['icon']}</div>"
        f"<div class='info-body'><h2>{esc(info['label'])}</h2><p>{esc(info['body'])}</p></div>"
        f"{bulk_actions}</section>"
    )


def page_html(csv_path: Path, rows: list[dict[str, str]], params: dict[str, list[str]], research_dir: Path | None) -> bytes:
    summary = summarize(rows)
    active_tab = (params.get("tab") or ["yes"])[0].strip().lower()
    if active_tab not in VALID_TABS:
        active_tab = "yes"
    visible = [(idx, row) for idx, row in enumerate(rows) if matches_filter(row, params, research_dir)]
    q = (params.get("q") or [""])[0]

    def tab_href(tab: str) -> str:
        next_params = {
            key: values[0]
            for key, values in params.items()
            if key not in {"tab"} and values and values[0]
        }
        next_params["tab"] = tab
        return "/?" + urllib.parse.urlencode(next_params) if next_params else "/?tab=yes"

    def tab_link(tab: str, label: str, count: int) -> str:
        klass = f"tab {tab}{' active' if active_tab == tab else ''}"
        return f"<a class='{klass}' href='{esc(tab_href(tab))}'><span>{esc(label)}</span><strong data-count='{esc(tab)}'>{count}</strong></a>"

    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>Contact Review</title>",
        f"<style>{REVIEW_CSS}</style></head><body><div class='wrap'>",
        "<header><h1>Contact Review</h1>",
        "<div class='meta'>Click a card to toggle which contacts should be added to your Personal Network.</div></header>",
        "<nav class='tabs'>",
        tab_link("yes", "Yes", summary["yes"]),
        tab_link("maybe", "Maybe", summary["maybe"]),
        tab_link("no", "No", summary["no"]),
        tab_link("in_network", "In Network", summary["in_network"]),
        "</nav>",
        render_info_panel(active_tab),
        "<form class='search' method='get' action='/'>",
        f"<input type='hidden' name='tab' value='{esc(active_tab)}'>",
        SEARCH_ICON,
        f"<input name='q' placeholder='Search by name…' value='{esc(q)}' aria-label='Search by name'>",
        "</form>",
    ]
    if not visible:
        parts.append("<div class='empty'>No research rows match this view.</div>")
    else:
        parts.append("<section class='cards'>")
        for idx, row in visible[:500]:
            selected = is_selected(row)
            label = row_tab(row)
            view = row_view(row, research_dir)
            location = view["location"] or "unknown"
            groups = " | ".join(split_pipe(row.get("group_names", ""), limit=5)) or "none"
            signals = " | ".join(split_pipe(row.get("signals", ""), limit=5)) or "none"
            linkedin = view["linkedin_url"]
            decision = "Yes" if selected else "No"
            count_key = "in_network" if label == "in_network" and selected else (label if label != "in_network" else "")
            card_class = "card selected" if selected else "card excluded"
            linkedin_icon = f"<a class='li-icon' href='{esc(linkedin)}' target='_blank' rel='noreferrer' title='LinkedIn' aria-label='Open LinkedIn profile'>in</a>" if linkedin else ""
            is_retargeted = bool((row.get("retarget_status") or "").strip())
            retarget_badge = "<span class='badge retarget'>re-researched</span>" if is_retargeted else ""
            hint = "" if is_retargeted else row.get("retarget_hint", "")
            channel_bits = []
            if row.get("imessage_message_count"):
                channel_bits.append(f"iMessage {row.get('imessage_message_count')}")
            if row.get("whatsapp_message_count"):
                channel_bits.append(f"WhatsApp {row.get('whatsapp_message_count')}")
            channel_detail = f" ({' · '.join(channel_bits)})" if channel_bits else ""
            parts.extend([
                f"<article class='{card_class}' role='button' tabindex='0' data-row='{idx}' data-selected='{str(selected).lower()}' data-decision='{esc(count_key)}' data-network='{str(is_in_network(row)).lower()}'>",
                "<div class='head'>",
                f"<div><div class='name-row'><div class='name'>{esc(view['name'])}</div>{linkedin_icon}{retarget_badge}</div></div>",
                f"<div class='decision'>{decision}</div></div>",
                f"<div class='line'><strong>phone</strong> {esc(row.get('phone_e164') or 'unknown')}</div>",
                f"<div class='line'><strong>msgs</strong> {esc(row.get('total_messages') or '0')}{esc(channel_detail)}</div>",
                f"<div class='line'><strong>location</strong> {esc(location)}</div>",
                f"<div class='line'><strong>groups</strong> {esc(groups)}</div>",
                f"<div class='line'><strong>network</strong> {esc((row.get('network_name') or 'none') if is_in_network(row) else 'none')}</div>",
                "<div class='profile'>",
                f"<div class='line'><strong>title@company</strong> {esc(view['title_pairs'] or 'unknown')}</div>",
                f"<div class='line'><strong>education</strong> {esc(view['schools'] or 'unknown')}</div>",
                f"<div class='line'><strong>reason</strong> {esc(row.get('short_reason') or 'none')}</div>",
                f"<div class='line'><strong>identity</strong> {esc(row.get('identity_risk') or 'none')}</div>",
                f"<div class='line'><strong>signals</strong> {esc(signals)}</div>",
                ("<div class='line'><strong>latest result</strong> showing latest re-researched profile. Add new feedback below to run another pass.</div>" if is_retargeted else ""),
                f"<div class='hint'><label for='hint-{idx}'>{'new feedback' if is_retargeted else 'feedback'}</label><textarea id='hint-{idx}' data-row='{idx}' placeholder='LinkedIn URL, company, title, location, or any clue'>{esc(hint)}</textarea><div class='hint-actions'><button type='button' data-save-hint='{idx}'>Save feedback</button><span class='hint-status'></span></div></div>",
                "</div></article>",
            ])
        parts.append("</section>")
        if len(visible) > 500:
            parts.append("<p class='line'>Showing first 500 filtered rows. Narrow the filter to review more.</p>")
    parts.extend([
        "<div id='toast' class='toast'>Saved</div>",
        "<script>",
        "const toast=document.getElementById('toast');let toastTimer=null;",
        "function showToast(text){toast.textContent=text;toast.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>toast.classList.remove('show'),1100)}",
        "function bump(label,delta){if(!label)return;const el=document.querySelector('[data-count='+label+']');if(el)el.textContent=String(Math.max(0,Number(el.textContent||0)+delta))}",
        "function cardDecision(card,selected){if(card.dataset.network==='true')return selected?'in_network':'';if(!selected)return'no';return'yes'}",
        "function setCard(card,selected){const decision=cardDecision(card,selected);card.dataset.selected=String(selected);card.classList.toggle('selected',selected);card.classList.toggle('excluded',!selected);card.querySelector('.decision').textContent=selected?'Yes':'No';card.dataset.decision=decision}",
        "async function toggle(card){const was=card.dataset.selected==='true';const oldDecision=card.dataset.decision||'';const next=!was;const nextDecision=cardDecision(card,next);card.classList.add('saving');try{const body=new URLSearchParams({row:card.dataset.row,selected:String(next)});const res=await fetch('/toggle',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body});if(!res.ok)throw new Error(await res.text());setCard(card,next);if(oldDecision!==nextDecision){bump(oldDecision,-1);bump(nextDecision,1)}showToast(next?(nextDecision==='in_network'?'Saved: in network':'Saved: upload yes'):'Saved: excluded')}catch(e){showToast('Save failed');}finally{card.classList.remove('saving')}}",
        "async function saveHint(el){const status=el.parentElement.querySelector('.hint-status');if(status)status.textContent='saving…';try{const body=new URLSearchParams({row:el.dataset.row,hint:el.value});const res=await fetch('/hint',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body});if(!res.ok)throw new Error(await res.text());if(status)status.textContent='saved';showToast('Saved hint')}catch(e){if(status)status.textContent='save failed';showToast('Hint save failed')}}",
        "async function bulkSelect(btn){const selected=btn.dataset.bulkSelected==='true';document.querySelectorAll('[data-bulk-selected]').forEach(b=>b.disabled=true);try{const body=new URLSearchParams({tab:'in_network',selected:String(selected)});const res=await fetch('/bulk-toggle',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body});if(!res.ok)throw new Error(await res.text());showToast(selected?'Selected all in-network':'Deselected all in-network');setTimeout(()=>location.reload(),250)}catch(e){showToast('Bulk save failed');document.querySelectorAll('[data-bulk-selected]').forEach(b=>b.disabled=false)}}",
        "document.querySelectorAll('[data-bulk-selected]').forEach(btn=>btn.addEventListener('click',e=>{e.preventDefault();bulkSelect(btn)}));",
        "document.querySelectorAll('.card').forEach(card=>{card.addEventListener('click',e=>{if(e.target.closest('a,textarea,input,button,label'))return;toggle(card)});card.addEventListener('keydown',e=>{if(e.target.closest('textarea,input,button'))return;if(e.key===' '||e.key==='Enter'){e.preventDefault();toggle(card)}})});",
        "document.querySelectorAll('.hint textarea').forEach(el=>{let t=null;el.addEventListener('click',e=>e.stopPropagation());el.addEventListener('keydown',e=>{e.stopPropagation();if((e.metaKey||e.ctrlKey)&&e.key==='Enter'){e.preventDefault();saveHint(el)}});el.addEventListener('input',()=>{const status=el.parentElement.querySelector('.hint-status');if(status)status.textContent='unsaved';clearTimeout(t);t=setTimeout(()=>saveHint(el),1200)});el.addEventListener('blur',()=>{clearTimeout(t);saveHint(el)})});document.querySelectorAll('[data-save-hint]').forEach(btn=>{btn.addEventListener('click',e=>{e.stopPropagation();const box=btn.closest('.hint').querySelector('textarea');if(box)saveHint(box)})});"
        "</script></div></body></html>",
    ])
    return "".join(parts).encode("utf-8")


def make_handler(csv_path: Path, research_dir: Path | None):
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
                _, rows = read_rows(csv_path)
                self.send_bytes(json.dumps({
                    "status": "ok",
                    "csv": str(csv_path.resolve()),
                    "research_dir": str(research_dir.resolve()) if research_dir else None,
                    "row_count": len(rows),
                    "source_sha256": SOURCE_SHA256,
                }).encode(), "application/json")
                return
            _, rows = read_rows(csv_path)
            if parsed.path == "/api/summary":
                self.send_bytes(json.dumps(summarize(rows), indent=2).encode(), "application/json")
                return
            if parsed.path != "/":
                self.send_bytes(b"not found", "text/plain", status=404)
                return
            params = urllib.parse.parse_qs(parsed.query)
            self.send_bytes(page_html(csv_path, rows, params, research_dir))

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path not in {"/toggle", "/hint", "/bulk-toggle"}:
                self.send_bytes(b"not found", "text/plain", status=404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            fieldnames, rows = read_rows(csv_path)

            if parsed.path == "/bulk-toggle":
                selected = (form.get("selected") or [""])[0].strip().lower()
                tab = (form.get("tab") or [""])[0].strip().lower()
                if selected not in {"true", "false"}:
                    self.send_bytes(b"selected must be true or false", "text/plain", status=400)
                    return
                if tab not in VALID_TABS:
                    self.send_bytes(b"unknown tab", "text/plain", status=400)
                    return
                if "exclude" not in fieldnames:
                    fieldnames.append("exclude")
                changed = apply_bulk_selection(rows, tab, selected == "true")
                atomic_write(csv_path, fieldnames, rows)
                self.send_bytes(json.dumps({
                    "ok": True,
                    "tab": tab,
                    "selected": selected == "true",
                    "changed": changed,
                    "summary": summarize(rows),
                }).encode(), "application/json")
                return

            try:
                row_idx = int((form.get("row") or [""])[0])
            except ValueError:
                self.send_bytes(b"bad row", "text/plain", status=400)
                return
            if row_idx < 0 or row_idx >= len(rows):
                self.send_bytes(b"row out of range", "text/plain", status=400)
                return
            if parsed.path == "/hint":
                if "retarget_hint" not in fieldnames:
                    fieldnames.append("retarget_hint")
                hint = (form.get("hint") or [""])[0].strip()
                rows[row_idx]["retarget_hint"] = hint
                atomic_write(csv_path, fieldnames, rows)
                self.send_bytes(json.dumps({"ok": True, "row": row_idx}).encode(), "application/json")
                return
            selected = (form.get("selected") or [""])[0].strip().lower()
            if selected not in {"true", "false"}:
                self.send_bytes(b"selected must be true or false", "text/plain", status=400)
                return
            if "exclude" not in fieldnames:
                fieldnames.append("exclude")
            rows[row_idx]["exclude"] = "no" if selected == "true" else "yes"
            atomic_write(csv_path, fieldnames, rows)
            self.send_bytes(json.dumps({"ok": True, "row": row_idx, "selected": selected == "true"}).encode(), "application/json")

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    return Handler


def cmd_serve(args: argparse.Namespace) -> None:
    csv_path = Path(args.csv)
    research_dir = Path(args.research_dir) if args.research_dir else None
    fieldnames, rows = read_rows(csv_path)
    if not csv_path.exists():
        atomic_write(csv_path, fieldnames, rows)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(csv_path, research_dir))
    host, port = server.server_address
    url = f"http://{host}:{port}/?tab=yes"
    print(json.dumps({
        "primitive": "review_research_web",
        "status": "serving",
        "csv": str(csv_path),
        "research_dir": str(research_dir) if research_dir else None,
        "url": url,
        "row_count": len(rows),
        "source_sha256": SOURCE_SHA256,
    }, indent=2))
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve a local messages research review UI")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--csv", default=".powerpacks/messages/research_review.csv")
    serve.add_argument("--research-dir", default=".powerpacks/messages/research")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--open", action="store_true")
    serve.set_defaults(func=cmd_serve)

    def add_api_args(api: argparse.ArgumentParser) -> None:
        api.add_argument("--csv", default=".powerpacks/messages/research_review.csv")
        api.add_argument("--research-dir", default=".powerpacks/messages/research")
        api.add_argument("--filter", default="all")
        api.add_argument("--query", default="")
        api.add_argument("--offset", type=int, default=0)
        api.add_argument("--limit", type=int, default=100)

    json_cmd = sub.add_parser("json", help="Return review rows/counts as JSON for the setup app")
    add_api_args(json_cmd)
    json_cmd.set_defaults(func=cmd_json)

    toggle = sub.add_parser("toggle", help="Persist one include/exclude decision and return JSON")
    add_api_args(toggle)
    toggle.add_argument("--row", type=int, required=True)
    toggle.add_argument("--selected", choices=["true", "false"], required=True)
    toggle.set_defaults(func=lambda args: (setattr(args, "selected", args.selected == "true"), cmd_toggle(args))[1])

    hint = sub.add_parser("hint", help="Persist one retarget hint and return JSON")
    add_api_args(hint)
    hint.add_argument("--row", type=int, required=True)
    hint.add_argument("--hint", default="")
    hint.set_defaults(func=cmd_hint)

    bulk = sub.add_parser("bulk-toggle", help="Persist a tab-level include/exclude decision and return JSON")
    add_api_args(bulk)
    bulk.add_argument("--tab", choices=sorted(VALID_TABS), required=True)
    bulk.add_argument("--selected", choices=["true", "false"], required=True)
    bulk.set_defaults(func=lambda args: (setattr(args, "selected", args.selected == "true"), cmd_bulk_toggle(args))[1])

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
