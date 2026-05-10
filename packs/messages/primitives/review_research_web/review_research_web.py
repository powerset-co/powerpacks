#!/usr/bin/env python3
"""Local web reviewer for messages deep-research CSVs."""

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

VALID_TABS = {"in_network", "yes", "maybe", "no"}


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


def row_tab(row: dict[str, str]) -> str:
    exclude = (row.get("exclude") or "").strip().lower()
    if truthy(exclude):
        return "no"
    if is_in_network(row):
        return "in_network"
    if falsy(exclude):
        return "yes"
    return bucket_label(row.get("bucket", ""))


def is_selected(row: dict[str, str]) -> bool:
    return row_tab(row) in {"in_network", "yes"}


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


def matches_filter(row: dict[str, str], params: dict[str, list[str]], research_dir: Path | None) -> bool:
    tab = (params.get("tab") or ["in_network"])[0].strip().lower()
    q = (params.get("q") or [""])[0].strip().lower()
    if tab not in VALID_TABS:
        tab = "in_network"
    if row_tab(row) != tab:
        return False
    if q:
        view = row_view(row, research_dir)
        haystack = " ".join([
            row.get("handle", ""),
            row.get("phone_e164", ""),
            row.get("group_names", ""),
            row.get("signals", ""),
            row.get("short_reason", ""),
            row.get("retarget_hint", ""),
            row.get("network_name", ""),
            row.get("network_linkedin_url", ""),
            row.get("network_match_method", ""),
            row.get("review_source", ""),
            view["name"],
            view["title_pairs"],
            view["schools"],
            view["linkedin_url"],
        ]).lower()
        if q not in haystack:
            return False
    return True


def summarize(rows: list[dict[str, str]]) -> dict[str, int]:
    out = {"in_network": 0, "yes": 0, "maybe": 0, "no": 0}
    for row in rows:
        out[row_tab(row)] += 1
    return out


def page_html(csv_path: Path, rows: list[dict[str, str]], params: dict[str, list[str]], research_dir: Path | None) -> bytes:
    summary = summarize(rows)
    active_tab = (params.get("tab") or ["in_network"])[0].strip().lower()
    if active_tab not in VALID_TABS:
        active_tab = "in_network"
    visible = [(idx, row) for idx, row in enumerate(rows) if matches_filter(row, params, research_dir)]
    q = (params.get("q") or [""])[0]

    def tab_href(tab: str) -> str:
        next_params = {
            key: values[0]
            for key, values in params.items()
            if key not in {"tab"} and values and values[0]
        }
        next_params["tab"] = tab
        return "/?" + urllib.parse.urlencode(next_params) if next_params else "/?tab=in_network"

    def tab_link(tab: str, label: str, count: int) -> str:
        klass = "tab active" if active_tab == tab else "tab"
        return f"<a class='{klass}' href='{esc(tab_href(tab))}'><span>{esc(label)}</span><strong>{count}</strong></a>"

    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>Powerpacks Research Review</title>",
        "<style>",
        ":root{color-scheme:light;--bg:#f5f6f8;--panel:#fff;--line:#d8dee6;--text:#17202a;--muted:#5f6c7a;--soft:#eef2f6;--ink:#17202a;--font:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif}",
        "*{box-sizing:border-box}body,button,input,textarea{font-family:var(--font)}body{margin:0;background:var(--bg);color:var(--text)}",
        ".wrap{max-width:1480px;margin:0 auto;padding:28px 24px 42px}",
        "header{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:18px}",
        "h1{font-size:24px;line-height:1.15;margin:0 0 7px}",
        ".meta{color:var(--muted);font-size:13px;line-height:1.4;overflow-wrap:anywhere}",
        ".stats{display:grid;grid-template-columns:repeat(4,minmax(104px,1fr));gap:8px;min-width:460px}",
        ".stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:9px 10px}.stat span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}.stat strong{font-size:20px}",
        ".tabs{display:flex;gap:6px;flex-wrap:wrap;border-bottom:1px solid var(--line);margin-bottom:12px}.tab{display:flex;gap:8px;align-items:center;padding:9px 12px;border:1px solid transparent;border-bottom:0;border-radius:8px 8px 0 0;text-decoration:none;color:var(--muted);font-size:13px}.tab.active{background:var(--panel);border-color:var(--line);color:var(--text);margin-bottom:-1px}.tab strong{font-size:12px;color:var(--text);background:var(--soft);border-radius:999px;padding:2px 7px}",
        ".filters{display:flex;gap:8px;flex-wrap:wrap;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;margin-bottom:14px}.filters input{font:inherit;border:1px solid #b8c1cc;border-radius:6px;padding:7px 8px;min-width:280px;flex:1}.filters button{font:inherit;border:1px solid var(--ink);background:var(--ink);color:#fff;border-radius:6px;padding:7px 12px}.filters a{display:inline-flex;align-items:center;color:var(--muted);text-decoration:none;padding:0 6px}",
        ".badge{display:inline-block;height:18px;line-height:18px;border-radius:999px;padding:0 7px;font-size:11px;font-weight:800;white-space:nowrap}.badge.retarget{background:#e9ddff;color:#5b21b6}",
        ".cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:12px}",
        ".card{background:var(--panel);border:1px solid var(--line);border-radius:8px;min-height:292px;padding:14px;cursor:pointer;box-shadow:0 1px 2px rgba(15,23,42,.04);transition:border-color .12s,box-shadow .12s,opacity .12s}.card:hover{border-color:#aeb8c5;box-shadow:0 3px 10px rgba(15,23,42,.08)}.card.selected{border-color:#63b7aa;background:#f1fbf8}.card.excluded{opacity:.64}.card.saving{outline:2px solid #f6c76b}",
        ".head{display:flex;justify-content:space-between;gap:10px;margin-bottom:10px}.name-row{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.name{font-weight:800;font-size:17px;line-height:1.2}.li-icon{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:4px;background:#0a66c2;color:#fff;text-decoration:none;font-size:12px;font-weight:900;line-height:1}.li-icon:hover{filter:brightness(.92);text-decoration:none}.decision{display:inline-block;height:20px;line-height:20px;font-size:12px;font-weight:800;border-radius:999px;padding:0 8px;background:#eceff3;color:#5b6876;white-space:nowrap}.selected .decision{background:#ccefe8;color:#0f5f59}",
        ".bucket{display:inline-block;height:20px;line-height:20px;border-radius:999px;padding:0 8px;background:#eef2f6;color:#334155;font-size:12px;margin-top:6px;white-space:nowrap;vertical-align:baseline}.bucket.yes{background:#d9f3ee;color:#0f5f59}.bucket.in_network{background:#d9f3ee;color:#0f5f59}.bucket.maybe{background:#fff1d6;color:#7a4b00}",
        ".line{font-size:13px;color:var(--muted);line-height:1.38;margin:5px 0;overflow-wrap:anywhere}.line strong{color:#334155;font-weight:650}.profile{border-top:1px solid #e5e9ef;margin-top:10px;padding-top:10px}.profile a{color:#0f5f59;text-decoration:none}.profile a:hover{text-decoration:underline}",
        ".hint{margin-top:10px}.hint label{display:block;color:#334155;font-size:12px;font-weight:700;margin-bottom:5px}.hint textarea{width:100%;min-height:54px;resize:vertical;border:1px solid #c6ced8;border-radius:7px;background:#fff;color:var(--text);font-size:13px;line-height:1.35;padding:7px 8px}.hint textarea:focus{outline:2px solid #9dd8cf;border-color:#63b7aa}.hint-actions{display:flex;align-items:center;gap:8px;margin-top:5px}.hint button{border:1px solid #9aa8b7;background:#fff;color:#334155;border-radius:6px;font-size:12px;line-height:1;font-weight:700;padding:6px 9px;cursor:pointer}.hint button:hover{border-color:#63b7aa;color:#0f5f59}.hint .hint-status{display:inline-block;min-height:16px;color:var(--muted);font-size:11px}",
        ".empty{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:24px;color:var(--muted)}.toast{position:fixed;right:16px;bottom:16px;background:#17202a;color:#fff;border-radius:8px;padding:9px 12px;font-size:13px;opacity:0;transform:translateY(8px);transition:opacity .15s,transform .15s;pointer-events:none}.toast.show{opacity:1;transform:translateY(0)}",
        "@media(max-width:900px){.wrap{padding:20px 14px}header{display:block}.stats{grid-template-columns:repeat(2,minmax(0,1fr));min-width:0;margin-top:14px}.cards{grid-template-columns:1fr}.filters input{min-width:180px}}",
        "</style></head><body><div class='wrap'>",
        "<header><div><h1>Powerpacks Research Review</h1>",
        f"<div class='meta'>{esc(csv_path)} &middot; showing {len(visible)} {esc(active_tab.replace('_', ' '))} rows. Click a card to toggle upload yes/no; every change autosaves.</div></div>",
        "<div class='stats'>",
        f"<div class='stat'><span>in network</span><strong data-count='in_network'>{summary['in_network']}</strong></div>",
        f"<div class='stat'><span>yes</span><strong data-count='yes'>{summary['yes']}</strong></div>",
        f"<div class='stat'><span>maybe</span><strong data-count='maybe'>{summary['maybe']}</strong></div>",
        f"<div class='stat'><span>no</span><strong data-count='no'>{summary['no']}</strong></div>",
        "</div></header><nav class='tabs'>",
        tab_link("in_network", "In Network", summary["in_network"]),
        tab_link("yes", "Yes", summary["yes"]),
        tab_link("maybe", "Maybe", summary["maybe"]),
        tab_link("no", "No", summary["no"]),
        "</nav>",
        "<form class='filters' method='get' action='/'>",
        f"<input type='hidden' name='tab' value='{esc(active_tab)}'>",
        f"<input name='q' placeholder='Search name, company, school, signal, LinkedIn' value='{esc(q)}'>",
        "<button type='submit'>Filter</button><a href='/'>clear</a></form>",
    ]
    if not visible:
        parts.append("<div class='empty'>No research rows match this view.</div>")
    else:
        parts.append("<section class='cards'>")
        for idx, row in visible[:500]:
            selected = is_selected(row)
            label = row_tab(row)
            label_text = "in network" if label == "in_network" else label
            view = row_view(row, research_dir)
            location = view["location"] or "unknown"
            groups = " | ".join(split_pipe(row.get("group_names", ""), limit=5)) or "none"
            signals = " | ".join(split_pipe(row.get("signals", ""), limit=5)) or "none"
            linkedin = view["linkedin_url"]
            github = view["github_url"]
            decision = "IN NETWORK" if label == "in_network" else ("YES" if selected else "NO")
            card_class = "card selected" if selected else "card excluded"
            bucket_class = f"bucket {label}" if label in {"in_network", "yes", "maybe"} else "bucket"
            linkedin_icon = f"<a class='li-icon' href='{esc(linkedin)}' target='_blank' rel='noreferrer' title='LinkedIn' aria-label='Open LinkedIn profile'>in</a>" if linkedin else ""
            retarget_badge = "<span class='badge retarget'>re-research</span>" if (row.get("retarget_status") or "").strip() else ""
            hint = row.get("retarget_hint", "")
            channel_bits = []
            if row.get("imessage_message_count"):
                channel_bits.append(f"iMessage {row.get('imessage_message_count')}")
            if row.get("whatsapp_message_count"):
                channel_bits.append(f"WhatsApp {row.get('whatsapp_message_count')}")
            channel_detail = f" ({' · '.join(channel_bits)})" if channel_bits else ""
            parts.extend([
                f"<article class='{card_class}' role='button' tabindex='0' data-row='{idx}' data-selected='{str(selected).lower()}' data-decision='{esc(label)}' data-network='{str(is_in_network(row)).lower()}'>",
                "<div class='head'>",
                f"<div><div class='name-row'><div class='name'>{esc(view['name'])}</div>{linkedin_icon}{retarget_badge}</div><span class='{bucket_class}'>{esc(label_text)}</span></div>",
                f"<div class='decision'>{decision}</div></div>",
                f"<div class='line'><strong>phone</strong> {esc(row.get('phone_e164') or 'unknown')} &middot; <strong>msgs</strong> {esc(row.get('total_messages') or '0')}{esc(channel_detail)}</div>",
                f"<div class='line'><strong>source</strong> {esc(row.get('message_source') or 'unknown')}</div>",
                f"<div class='line'><strong>location</strong> {esc(location)}</div>",
                f"<div class='line'><strong>groups</strong> {esc(groups)}</div>",
                f"<div class='line'><strong>network</strong> {esc((row.get('network_name') or 'none') if is_in_network(row) else 'none')}</div>",
                "<div class='profile'>",
                f"<div class='line'><strong>title@company</strong> {esc(view['title_pairs'] or 'unknown')}</div>",
                f"<div class='line'><strong>education</strong> {esc(view['schools'] or 'unknown')}</div>",
                f"<div class='line'><strong>reason</strong> {esc(row.get('short_reason') or 'none')}</div>",
                f"<div class='line'><strong>identity</strong> {esc(row.get('identity_risk') or 'none')}</div>",
                f"<div class='line'><strong>signals</strong> {esc(signals)}</div>",
                f"<div class='hint'><label for='hint-{idx}'>feedback</label><textarea id='hint-{idx}' data-row='{idx}' placeholder='LinkedIn URL, company, title, location, or any clue'>{esc(hint)}</textarea><div class='hint-actions'><button type='button' data-save-hint='{idx}'>Save feedback</button><span class='hint-status'></span></div></div>",
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
        "function bump(label,delta){const el=document.querySelector('[data-count='+label+']');if(el)el.textContent=String(Math.max(0,Number(el.textContent||0)+delta))}",
        "function cardDecision(card,selected){if(!selected)return'no';return card.dataset.network==='true'?'in_network':'yes'}",
        "function decisionLabel(decision){return decision==='in_network'?'in network':decision}",
        "function setCard(card,selected){const decision=cardDecision(card,selected);card.dataset.selected=String(selected);card.classList.toggle('selected',selected);card.classList.toggle('excluded',!selected);card.querySelector('.decision').textContent=decision==='in_network'?'IN NETWORK':(selected?'YES':'NO');card.dataset.decision=decision;const badge=card.querySelector('.bucket');if(badge){badge.textContent=decisionLabel(decision);badge.className='bucket '+(decision==='yes'||decision==='in_network'?decision:'')}}",
        "async function toggle(card){const was=card.dataset.selected==='true';const oldDecision=card.dataset.decision||'no';const next=!was;const nextDecision=cardDecision(card,next);card.classList.add('saving');try{const body=new URLSearchParams({row:card.dataset.row,selected:String(next)});const res=await fetch('/toggle',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body});if(!res.ok)throw new Error(await res.text());setCard(card,next);if(oldDecision!==nextDecision){bump(oldDecision,-1);bump(nextDecision,1)}showToast(next?(nextDecision==='in_network'?'Saved: in network':'Saved: upload yes'):'Saved: upload no')}catch(e){showToast('Save failed');}finally{card.classList.remove('saving')}}",
        "async function saveHint(el){const status=el.parentElement.querySelector('.hint-status');if(status)status.textContent='saving…';try{const body=new URLSearchParams({row:el.dataset.row,hint:el.value});const res=await fetch('/hint',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body});if(!res.ok)throw new Error(await res.text());if(status)status.textContent='saved';showToast('Saved hint')}catch(e){if(status)status.textContent='save failed';showToast('Hint save failed')}}",
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
                self.send_bytes(b"ok", "text/plain")
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
            if parsed.path not in {"/toggle", "/hint"}:
                self.send_bytes(b"not found", "text/plain", status=404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            try:
                row_idx = int((form.get("row") or [""])[0])
            except ValueError:
                self.send_bytes(b"bad row", "text/plain", status=400)
                return
            fieldnames, rows = read_rows(csv_path)
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
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
