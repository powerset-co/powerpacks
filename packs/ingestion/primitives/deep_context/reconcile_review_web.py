#!/usr/bin/env python3
"""[Phase 3, review] Local web UI to review the LinkedIn self-heal at scale.

`review.csv` is keyed only on the candidate LinkedIn (`public_identifier`), so on
its own it's un-reviewable — you can't see which canonical PERSON ("parent") a link
belongs to, the other links considered, or why the model picked one. The parent
structure + profile + the judge's reasoning live in `reconcile/verdicts.jsonl`.

This primitive JOINS the two (plus the per-parent dossier in `parents/*.md`) and
serves a parent-grouped table:

  * one row per PARENT (canonical person), expandable to its candidate LinkedIn(s),
  * each candidate shows the profile we matched, the picked link, confidence, and the
    model's supporting / contradicting reasoning,
  * per-candidate actions autosave straight into `review.csv`:
        Keep   -> action=verify   approved=yes
        Detach -> action=detach   approved=yes
        Fix…   -> action=retarget approved=yes  (paste the correct LinkedIn URL)
  * quick filters (needs-review / verified / detached / conflicts / fixed / my
    decisions), search, and a risk sort that floats the lowest-confidence parents up.

It only reads the deep-context artifacts and writes `review.csv` (the same durable
table the fan-in merge re-applies). A `Fix…` decision is enriched + re-attached later
by `apply-retargets` + `realize`. No spend, local only.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.common import (
    DOSSIER_DIR,
    LINKEDIN_OVERRIDES_CSV,
    PARENTS_DIR,
    VERDICTS_JSONL,
    now_iso,
    read_jsonl,
)
from packs.ingestion.primitives.deep_context.reconcile_linkedin import (
    DEFAULT_CONFIRM,
    DEFAULT_DETACH,
    OVERRIDE_COLUMNS,
    USER_APPROVED,
    _VERDICT_TO_ACTION,
    _write_override_rows,
    load_override_rows,
)
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url

APPLIED_APPROVED = {"auto", "yes"}
VALID_TABS = {"all", "review", "verified", "detached", "conflict", "fixed", "decided"}


# --- model: join verdicts.jsonl (display) with review.csv (decisions) -------

def candidate_state(cand: dict[str, Any]) -> str:
    """The effective per-candidate state from its current decision row."""
    action = (cand.get("action") or "").strip().lower()
    approved = (cand.get("approved") or "").strip().lower()
    if action == "retarget" and approved in APPLIED_APPROVED:
        return "fixed"
    if approved in APPLIED_APPROVED:
        return "detached" if action == "detach" else "verified"
    if approved == "no":
        return "rejected"
    return "review"  # pending


def parent_status(parent: dict[str, Any]) -> str:
    """One status chip per parent, by priority (what most needs the user's eye first)."""
    states = [candidate_state(c) for c in parent["candidates"]]
    if "fixed" in states:
        return "fixed"
    if "review" in states:
        return "review"
    if "verified" in states:
        return "verified"
    if states and all(s in {"detached", "rejected"} for s in states):
        return "detached"
    return "review"


def picked_link(parent: dict[str, Any]) -> str:
    """The LinkedIn this parent currently resolves to (verified link, retarget target, or none)."""
    for c in parent["candidates"]:
        st = candidate_state(c)
        if st == "fixed":
            return c.get("new_url") or ""
        if st == "verified":
            return c.get("url") or ""
    return ""


def min_confidence(parent: dict[str, Any]) -> float:
    return min((c.get("confidence", 0.0) for c in parent["candidates"]), default=0.0)


def is_decided(parent: dict[str, Any]) -> bool:
    return any((c.get("approved") or "").strip().lower() in USER_APPROVED for c in parent["candidates"])


def build_parents(verdicts_path: Path, review_path: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    overrides = load_override_rows(review_path)
    parents: dict[str, dict[str, Any]] = {}
    for r in read_jsonl(verdicts_path):
        slug = r.get("parent_slug") or ""
        if not slug:
            continue
        p = parents.setdefault(slug, {"slug": slug, "name": r.get("name") or slug,
                                      "person_ids": [], "candidates": []})
        for pid in r.get("person_ids") or []:
            if pid not in p["person_ids"]:
                p["person_ids"].append(pid)
        pub = (r.get("candidate_key") or "").strip().lower()
        v = r.get("verdict") or {}
        li = r.get("linkedin") or {}
        dec = overrides.get(pub, {})
        p["candidates"].append({
            "pub": pub,
            "url": li.get("linkedin_url", ""),
            "full_name": li.get("full_name", ""),
            "headline": li.get("headline", ""),
            "experiences": li.get("experiences") or [],
            "education": li.get("education") or [],
            "location": li.get("location", ""),
            "has_profile": li.get("has_profile", False),
            "verdict": v.get("verdict", ""),
            "confidence": float(v.get("confidence") or 0.0),
            "supporting": v.get("supporting_evidence") or [],
            "contradicting": v.get("contradicting_evidence") or [],
            "reason": v.get("reason", ""),
            "plausibly_absent": bool(v.get("linkedin_plausibly_absent")),
            "recommend_dr": bool(v.get("recommend_deep_research")),
            "match_emails": r.get("match_emails") or [],
            "match_phones": r.get("match_phones") or [],
            "conflict": bool(r.get("conflict")),
            # current decision (from review.csv)
            "action": dec.get("action", ""),
            "approved": (dec.get("approved") or "").strip().lower(),
            "new_url": dec.get("new_linkedin_url", ""),
        })
    return list(parents.values()), overrides


def parent_in_tab(parent: dict[str, Any], tab: str) -> bool:
    if tab in ("", "all"):
        return True
    if tab == "decided":
        return is_decided(parent)
    if tab == "conflict":
        return any(c.get("conflict") for c in parent["candidates"]) or len(parent["candidates"]) > 1
    return parent_status(parent) == tab


def parent_matches_query(parent: dict[str, Any], q: str) -> bool:
    if not q:
        return True
    hay = [parent["name"], parent["slug"]]
    for c in parent["candidates"]:
        hay += [c["pub"], c["url"], c["full_name"], c["headline"], c["location"], c["reason"]]
        hay += c["match_emails"] + c["match_phones"]
    return q in " ".join(hay).lower()


def summarize(parents: list[dict[str, Any]]) -> dict[str, int]:
    s = {k: 0 for k in ("total", "review", "verified", "detached", "conflict", "fixed", "decided")}
    s["total"] = len(parents)
    for p in parents:
        s[parent_status(p)] += 1
        if parent_in_tab(p, "conflict"):
            s["conflict"] += 1
        if is_decided(p):
            s["decided"] += 1
    return s


# --- decision writer (the only mutation: upsert one row in review.csv) ------

def apply_decision(review_path: Path, verdicts_path: Path, pub: str, decision: str,
                   new_url: str, confirm_threshold: float, detach_threshold: float | None = None) -> dict[str, str]:
    """Upsert a single decision into review.csv (keyed by public_identifier)."""
    pub = (pub or "").strip().lower()
    rows = load_override_rows(review_path)
    row = rows.get(pub) or {k: "" for k in OVERRIDE_COLUMNS}
    row["public_identifier"] = pub
    if decision == "keep":
        row["action"], row["approved"], row["new_linkedin_url"], row["new_public_identifier"] = "verify", "yes", "", ""
    elif decision == "detach":
        row["action"], row["approved"], row["new_linkedin_url"], row["new_public_identifier"] = "detach", "yes", "", ""
    elif decision == "fix":
        url = normalize_linkedin_url(new_url or "")
        if not url:
            raise ValueError("fix needs a LinkedIn URL")
        row["action"], row["approved"] = "retarget", "yes"
        row["new_linkedin_url"] = url
        row["new_public_identifier"] = extract_public_identifier(url).lower()
    elif decision == "reset":
        # restore the model's original (non-conflict) call. Asymmetric, keep-biased bars:
        # confirmed auto-applies at the (low) confirm bar, wrong_person at the (high) detach bar.
        detach_threshold = confirm_threshold if detach_threshold is None else detach_threshold
        rec = next((r for r in read_jsonl(verdicts_path)
                    if (r.get("candidate_key") or "").strip().lower() == pub), None)
        v = (rec or {}).get("verdict") or {}
        vd = v.get("verdict", "")
        bar = detach_threshold if vd == "wrong_person" else confirm_threshold
        row["action"] = _VERDICT_TO_ACTION.get(vd, "verify")
        row["approved"] = "auto" if float(v.get("confidence") or 0) >= bar and vd in ("confirmed", "wrong_person") else ""
        row["new_linkedin_url"], row["new_public_identifier"] = "", ""
    else:
        raise ValueError(f"unknown decision: {decision}")
    row["source"] = "deep-context-review"
    row["updated_at"] = now_iso()
    rows[pub] = row
    _write_override_rows(review_path, rows)
    return {"action": row["action"], "approved": row["approved"], "new_url": row.get("new_linkedin_url", "")}


# --- dossier rendering (show the rich CHILD dossiers, not the thin parent stub) --

_CHILDREN_RE = re.compile(r"^children:\s*\[(.*?)\]", re.MULTILINE)


def _strip_frontmatter(md: str) -> str:
    if md.startswith("---"):
        end = md.find("\n---", 3)
        if end != -1:
            return md[end + 4:].lstrip("\n")
    return md


def render_dossier(parents_dir: Path, dossier_dir: Path, slug: str) -> str:
    """The message dossier for a person = its CHILD dossiers concatenated. The parent .md is a
    thin canonical stub (for singletons just a pointer), so reading it alone looks 'chopped off' —
    the real per-person context (summary, topics, timeline, identifiers) lives in the children."""
    pmd = parents_dir / f"{Path(slug).name}.md"
    if not pmd.exists():
        return "(no dossier on file)"
    ptext = pmd.read_text(encoding="utf-8")
    m = _CHILDREN_RE.search(ptext)
    children = re.findall(r'"([^"]+)"', m.group(1)) if m else []
    chunks = []
    for cs in children:
        cd = dossier_dir / f"{Path(cs).name}.md"
        if cd.exists():
            body = _strip_frontmatter(cd.read_text(encoding="utf-8"))
            body = "\n".join(ln for ln in body.splitlines() if "<!-- parent-link -->" not in ln)
            chunks.append(body.strip())
    if not chunks:
        return _strip_frontmatter(ptext).strip()
    sep = "\n\n" + "─" * 56 + "\n\n"
    header = f"This person was merged from {len(chunks)} message cluster(s):\n\n" if len(chunks) > 1 else ""
    return header + sep.join(chunks)


# --- rendering --------------------------------------------------------------

def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


STATUS_LABEL = {"review": "needs review", "verified": "verified", "detached": "detached", "fixed": "fixed"}


# Direction-aware judgment line: the confidence is confidence IN THE VERDICT, so a
# high-% wrong_person is a STRONG MISMATCH, not a strong match. Spell that out.
def judgment_line(verdict: str, confidence: float) -> str:
    pct = f"{confidence * 100:.0f}%"
    if verdict == "confirmed":
        return f"<div class='judgment j-confirmed'>✓ Looks like the same person · <b>{pct} sure</b></div>"
    if verdict == "wrong_person":
        return (f"<div class='judgment j-wrong'>✗ Looks like a <b>different</b> person · "
                f"<b>{pct} sure it's the wrong one</b></div>")
    return f"<div class='judgment j-review'>? Not enough to tell · {pct} sure either way</div>"


def render_candidate(idx: int, total: int, cand: dict[str, Any]) -> str:
    st = candidate_state(cand)
    option = f"<span class='opt'>Option {idx + 1} of {total}</span>" if total > 1 else ""
    judgment = judgment_line(cand["verdict"], cand["confidence"])
    profile = []
    if cand["headline"]:
        profile.append(f"<div class='hl'>{esc(cand['headline'])}</div>")
    if cand["location"]:
        profile.append(f"<div class='loc'>{esc(cand['location'])}</div>")
    if cand["experiences"]:
        exp = "".join(f"<li>{esc(e)}</li>" for e in cand["experiences"])
        profile.append(f"<ul class='exp'>{exp}</ul>")
    if cand["education"]:
        profile.append(f"<div class='edu'>🎓 {esc('; '.join(cand['education']))}</div>")
    if not cand["has_profile"]:
        profile.append("<div class='loc'>no enriched profile on file</div>")
    evid = []
    if cand["supporting"]:
        evid.append("<div class='ev good'><span>supports</span><ul>"
                    + "".join(f"<li>{esc(x)}</li>" for x in cand["supporting"]) + "</ul></div>")
    if cand["contradicting"]:
        evid.append("<div class='ev bad'><span>contradicts</span><ul>"
                    + "".join(f"<li>{esc(x)}</li>" for x in cand["contradicting"]) + "</ul></div>")
    contacts = " · ".join(filter(None, [", ".join(cand["match_emails"]), ", ".join(cand["match_phones"])]))
    flags = []
    if cand["plausibly_absent"]:
        flags.append("<span class='flag'>may have no LinkedIn</span>")
    if cand["recommend_dr"]:
        flags.append("<span class='flag'>deep-research suggested</span>")
    fixed_note = f"<div class='fixednote'>→ re-targeted to <a href='{esc(cand['new_url'])}' target='_blank' rel='noreferrer'>{esc(cand['new_url'])}</a></div>" if st == "fixed" and cand["new_url"] else ""
    return f"""
    <div class='cand cand-{st}' data-pub='{esc(cand['pub'])}'>
      <div class='cand-top'>
        <div class='cand-id'>
          {option}
          <a href='{esc(cand['url'])}' target='_blank' rel='noreferrer'>{esc(cand['url'] or cand['pub'])}</a>
          {''.join(flags)}
        </div>
        <div class='cand-state state-{st}'>{esc(STATUS_LABEL.get(st, st))}</div>
      </div>
      {judgment}
      <div class='profile'>{''.join(profile) or "<div class='loc'>—</div>"}</div>
      {f"<div class='contacts'><strong>from your messages:</strong> {esc(contacts)}</div>" if contacts else ""}
      <div class='reason'>{esc(cand['reason'])}</div>
      <div class='evidence'>{''.join(evid)}</div>
      {fixed_note}
      <div class='actions'>
        <button class='btn keep' data-act='keep'>Keep this LinkedIn</button>
        <button class='btn detach' data-act='detach'>Detach (wrong person)</button>
        <span class='fixwrap'><input class='fixurl' placeholder='paste correct LinkedIn URL'>
          <button class='btn fix' data-act='fix'>Fix</button></span>
        <button class='btn reset' data-act='reset' title='revert to the model decision'>↺</button>
      </div>
    </div>"""


def render_parent(idx: int, parent: dict[str, Any], expanded: bool) -> str:
    status = parent_status(parent)
    picked = picked_link(parent)
    cands_list = parent["candidates"]
    n_cand = len(cands_list)
    n_people = len(parent["person_ids"])
    conf = f"{min_confidence(parent) * 100:.0f}%"
    decided = " decided" if is_decided(parent) else ""
    multi = n_cand > 1
    picked_html = (f"<a href='{esc(picked)}' target='_blank' rel='noreferrer'>{esc(picked)}</a>"
                   if picked else "<span class='nopick'>no link chosen</span>")
    open_attr = " open" if (expanded or multi) else ""
    banner = (f"<div class='conflictbanner'>⚠ Merged person — {n_cand} different LinkedIns ended up on "
              f"this one person. Keep the right one and detach the rest.</div>" if multi else "")
    cands = "".join(render_candidate(i, n_cand, c) for i, c in enumerate(cands_list))
    parent_cls = "parent multi" if multi else "parent"
    return f"""
    <details class='{parent_cls} p-{status}{decided}' data-slug='{esc(parent['slug'])}' data-idx='{idx}'{open_attr}>
      <summary>
        <span class='chip chip-{status}'>{esc(STATUS_LABEL.get(status, status))}</span>
        <span class='pname'>{esc(parent['name'])}</span>
        {"<span class='multibadge'>" + str(n_cand) + " LinkedIns</span>" if multi else f"<span class='picked'>{picked_html}</span>"}
        <span class='pmeta'>{n_people} message cluster{'s' if n_people != 1 else ''} · conf {conf}</span>
      </summary>
      <div class='pbody'>
        {banner}
        {cands}
        <details class='dossier' data-slug='{esc(parent['slug'])}'>
          <summary>show message dossier</summary>
          <pre class='dosstext'>loading…</pre>
        </details>
      </div>
    </details>"""


def page_html(parents: list[dict[str, Any]], params: dict[str, list[str]],
              review_path: Path) -> bytes:
    tab = (params.get("tab") or ["review"])[0].strip().lower()  # default to the Needs-review pile
    if tab not in VALID_TABS:
        tab = "review"
    q = (params.get("q") or [""])[0].strip()
    sort = (params.get("sort") or ["risk"])[0].strip().lower()
    summary = summarize(parents)

    visible = [p for p in parents if parent_in_tab(p, tab) and parent_matches_query(p, q.lower())]
    if sort == "name":
        visible.sort(key=lambda p: p["name"].lower())
    elif sort == "conf":
        visible.sort(key=min_confidence, reverse=True)
    else:  # risk: pending first, then lowest confidence
        visible.sort(key=lambda p: (parent_status(p) != "review", min_confidence(p)))

    def tab_href(name: str) -> str:
        # Always set tab explicitly — "/" with no param now defaults to the review pile.
        qp = {k: v[0] for k, v in params.items() if k not in {"tab"} and v and v[0]}
        qp["tab"] = name
        return "/?" + urllib.parse.urlencode(qp)

    def tab_link(name: str, label: str, key: str) -> str:
        klass = "tab active" if tab == name else "tab"
        return f"<a class='{klass}' href='{esc(tab_href(name))}'><span>{esc(label)}</span><strong>{summary[key]}</strong></a>"

    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>Deep Context · LinkedIn Self-Heal Review</title>",
        "<style>", CSS, "</style></head><body><div class='wrap'>",
        "<header><div>",
        "<h1>LinkedIn self-heal review</h1>",
        f"<div class='meta'>{esc(review_path)} · {len(visible)} of {len(parents)} people · "
        "each row is a person; expand to see the LinkedIn(s) we matched, the reasoning, and to keep / detach / fix. "
        "Every change autosaves.</div>",
        "</div><div class='stats'>",
        f"<div class='stat'><span>needs review</span><strong data-count='review'>{summary['review']}</strong></div>",
        f"<div class='stat'><span>verified</span><strong>{summary['verified']}</strong></div>",
        f"<div class='stat'><span>detached</span><strong>{summary['detached']}</strong></div>",
        f"<div class='stat'><span>you decided</span><strong data-count='decided'>{summary['decided']}</strong></div>",
        "</div></header>",
        "<nav class='tabs'>",
        tab_link("conflict", "Merged", "conflict"),
        tab_link("review", "Needs review", "review"),
        tab_link("all", "All", "total"),
        "</nav>",
        "<form class='filters' method='get' action='/'>",
        f"<input type='hidden' name='tab' value='{esc(tab)}'>",
        f"<input name='q' placeholder='Search name, company, email, LinkedIn' value='{esc(q)}'>",
        "<select name='sort'>",
    ]
    for value, label in [("risk", "riskiest first"), ("conf", "most confident first"), ("name", "name A–Z")]:
        sel = " selected" if sort == value else ""
        parts.append(f"<option value='{value}'{sel}>{esc(label)}</option>")
    parts.append("</select><button type='submit'>Apply</button><a class='clear' href='/'>clear</a></form>")

    if not visible:
        parts.append("<div class='empty'>Nothing matches this view.</div>")
    else:
        # expand parents automatically when the user is in a focused (non-all) tab and the set is small
        expand = tab in {"conflict", "fixed"} and len(visible) <= 40
        parts.append("<section class='list'>")
        idx_by_slug = {p["slug"]: i for i, p in enumerate(parents)}
        for p in visible[:600]:
            parts.append(render_parent(idx_by_slug[p["slug"]], p, expand))
        parts.append("</section>")
        if len(visible) > 600:
            parts.append("<p class='muted'>Showing first 600. Narrow the filter to see more.</p>")

    parts.append("<div id='toast' class='toast'>Saved</div>")
    parts.append("<script>" + JS + "</script>")
    parts.append("</div></body></html>")
    return "".join(parts).encode("utf-8")


CSS = """
:root{color-scheme:light;--bg:#f6f7f9;--panel:#fff;--line:#d7dde5;--text:#17202a;--muted:#5b6876;--soft:#eef1f5;
--ok:#0f766e;--okbg:#e6f7f3;--bad:#b42318;--badbg:#fde7e3;--warn:#b25e00;--warnbg:#fff2dc;--fix:#5b3da8;--fixbg:#f0eafb}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;margin:0;color:var(--text);background:var(--bg)}
.wrap{max-width:1180px;margin:0 auto;padding:26px 22px 60px}
header{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:16px}
h1{font-size:23px;margin:0 0 6px;font-weight:700}
.meta{color:var(--muted);font-size:13px;line-height:1.45;max-width:760px;overflow-wrap:anywhere}
.stats{display:grid;grid-template-columns:repeat(4,minmax(86px,1fr));gap:8px;min-width:380px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.stat span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.stat strong{display:block;font-size:20px;margin-top:2px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 12px;border-bottom:1px solid var(--line)}
.tab{display:flex;align-items:center;gap:7px;text-decoration:none;color:var(--muted);padding:8px 12px;border:1px solid transparent;border-bottom:0;border-radius:8px 8px 0 0;font-size:13px}
.tab strong{font-size:12px;color:var(--text);background:var(--soft);border-radius:999px;padding:2px 7px}
.tab.active{color:var(--text);background:var(--panel);border-color:var(--line);margin-bottom:-1px}
form.filters{display:flex;gap:8px;flex-wrap:wrap;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:11px;margin-bottom:14px}
input,select{font:inherit;border:1px solid #b8c1cc;border-radius:6px;padding:7px 8px;background:#fff;color:var(--text)}
.filters input[name=q]{min-width:300px;flex:1}
button{font:inherit;border:1px solid #17202a;background:#17202a;color:#fff;border-radius:6px;padding:7px 12px;cursor:pointer}
button:hover{background:#2a3642}
.clear{display:inline-flex;align-items:center;color:var(--muted);text-decoration:none;padding:0 6px}
.empty{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:24px;color:var(--muted)}
.list{display:flex;flex-direction:column;gap:8px}
.parent{background:var(--panel);border:1px solid var(--line);border-radius:9px;overflow:hidden}
.parent[open]{box-shadow:0 2px 10px rgba(15,23,42,.06)}
.parent.decided{border-color:#9bd3c8}
summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:11px;padding:11px 14px}
summary::-webkit-details-marker{display:none}
.parent>summary:hover{background:#fafbfc}
.chip{font-size:11px;font-weight:700;border-radius:999px;padding:3px 9px;white-space:nowrap;text-transform:uppercase;letter-spacing:.03em}
.chip-review{background:var(--warnbg);color:var(--warn)}
.chip-verified{background:var(--okbg);color:var(--ok)}
.chip-detached{background:var(--badbg);color:var(--bad)}
.chip-fixed{background:var(--fixbg);color:var(--fix)}
.pname{font-weight:700;font-size:15px;min-width:150px}
.picked{font-size:12.5px;color:var(--muted);flex:1;overflow-wrap:anywhere}
.picked a{color:var(--ok);text-decoration:none}.picked a:hover{text-decoration:underline}
.nopick{color:var(--bad)}
.pmeta{font-size:11.5px;color:var(--muted);white-space:nowrap}
.multibadge{font-size:11.5px;font-weight:700;color:#8a5200;background:#ffe9c7;border-radius:999px;padding:2px 9px;flex:1;max-width:max-content}
.parent.multi{border-color:#f0c167}
.parent.multi[open]{box-shadow:0 2px 14px rgba(180,120,0,.14)}
.pbody{border-top:1px solid var(--line);padding:12px 14px;display:flex;flex-direction:column;gap:11px}
.conflictbanner{font-size:12.5px;font-weight:600;color:#8a5200;background:#fff6e6;border:1px solid #f0c167;border-radius:7px;padding:8px 11px}
.parent.multi .pbody{padding-left:14px}
.parent.multi .cand{border-left:4px solid #d7dde5;margin-left:2px}
.parent.multi .cand-verified{border-left-color:#2bb39a}
.parent.multi .cand-detached{border-left-color:#d97a68}
.opt{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:#8a5200;background:#ffe9c7;border-radius:999px;padding:2px 8px}
.judgment{font-size:13px;border-radius:6px;padding:6px 10px;margin-bottom:8px}
.judgment b{font-weight:700}
.j-confirmed{background:var(--okbg);color:#0c5b53}
.j-wrong{background:var(--badbg);color:#8a2318}
.j-review{background:var(--warnbg);color:#7a4b00}
.cand{border:1px solid var(--line);border-radius:8px;padding:11px 12px;background:#fcfdfe}
.cand-verified{border-color:#9bd3c8;background:#f4fbf9}
.cand-detached{border-color:#e6b0a6;background:#fdf6f4}
.cand-fixed{border-color:#c3b1e6;background:#f8f5fd}
.cand-top{display:flex;justify-content:space-between;gap:10px;align-items:flex-start;margin-bottom:7px}
.cand-id{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:13px}
.cand-id a{color:#0a58ca;text-decoration:none;font-weight:600}.cand-id a:hover{text-decoration:underline}
.verdict{font-size:11px;border-radius:999px;padding:2px 7px;background:var(--soft);color:#334155}
.v-confirmed{background:var(--okbg);color:var(--ok)}.v-wrong_person{background:var(--badbg);color:var(--bad)}.v-needs_review{background:var(--warnbg);color:var(--warn)}
.conf{font-size:12px;font-weight:700;color:#334155}
.flag{font-size:10.5px;border-radius:999px;padding:2px 6px;background:#eef1f5;color:#5b6876}
.flag.conflict{background:#ffe9c7;color:#8a5200}
.cand-state{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.03em;color:var(--muted)}
.state-verified{color:var(--ok)}.state-detached{color:var(--bad)}.state-fixed{color:var(--fix)}.state-review{color:var(--warn)}
.profile .hl{font-size:13px;color:#334155;font-weight:600}
.profile .loc{font-size:12px;color:var(--muted)}
.exp{margin:5px 0;padding-left:18px;font-size:12.5px;color:#334155}
.edu{font-size:12px;color:var(--muted);margin-top:3px}
.contacts{font-size:12px;color:#334155;margin-top:7px;overflow-wrap:anywhere}
.contacts strong{color:var(--muted);font-weight:600}
.reason{font-size:13px;color:#1f2937;margin:8px 0;line-height:1.45}
.evidence{display:flex;flex-direction:column;gap:6px;margin-bottom:6px}
.ev{font-size:12px;border-radius:6px;padding:6px 9px}
.ev span{font-weight:700;text-transform:uppercase;font-size:10px;letter-spacing:.04em;display:block;margin-bottom:3px}
.ev ul{margin:0;padding-left:16px;line-height:1.4}
.ev.good{background:var(--okbg);color:#0c5b53}.ev.bad{background:var(--badbg);color:#8a2318}
.fixednote{font-size:12px;color:var(--fix);margin-bottom:6px;overflow-wrap:anywhere}
.fixednote a{color:var(--fix)}
.actions{display:flex;gap:7px;flex-wrap:wrap;align-items:center;border-top:1px dashed var(--line);padding-top:9px}
.btn{border-color:#cdd5df;background:#fff;color:#334155;padding:6px 11px;font-size:12.5px}
.btn:hover{background:#f1f5f9}
.btn.keep.on{background:var(--ok);border-color:var(--ok);color:#fff}
.btn.detach.on{background:var(--bad);border-color:var(--bad);color:#fff}
.btn.fix.on{background:var(--fix);border-color:var(--fix);color:#fff}
.fixwrap{display:inline-flex;gap:4px;align-items:center}
.fixurl{min-width:230px;font-size:12px;padding:6px 7px}
.btn.reset{padding:6px 9px;color:var(--muted)}
.dossier summary{padding:6px 0;font-size:12px;color:var(--muted)}
.dosstext{white-space:pre-wrap;font-size:12px;line-height:1.5;background:#f8fafc;border:1px solid var(--line);border-radius:6px;padding:10px;max-height:340px;overflow:auto;color:#1f2937;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.toast{position:fixed;right:16px;bottom:16px;background:#17202a;color:#fff;border-radius:8px;padding:9px 13px;font-size:13px;opacity:0;transform:translateY(8px);transition:.15s;pointer-events:none}
.toast.show{opacity:1;transform:translateY(0)}
.muted{color:var(--muted)}
@media(max-width:820px){header{display:block}.stats{grid-template-columns:repeat(2,1fr);min-width:0;margin-top:12px}summary{flex-wrap:wrap}.picked{flex-basis:100%}}
"""

JS = r"""
const toast=document.getElementById('toast');let tt=null;
function showToast(t){toast.textContent=t;toast.classList.add('show');clearTimeout(tt);tt=setTimeout(()=>toast.classList.remove('show'),1200)}
function setCandState(cand,action,approved,newUrl){
  cand.classList.remove('cand-verified','cand-detached','cand-fixed','cand-review');
  let st='review';
  if(action==='retarget'&&(approved==='yes'||approved==='auto'))st='fixed';
  else if(approved==='yes'||approved==='auto')st=(action==='detach')?'detached':'verified';
  cand.classList.add('cand-'+st);
  const lbl={review:'needs review',verified:'verified',detached:'detached',fixed:'fixed'}[st];
  const se=cand.querySelector('.cand-state');se.className='cand-state state-'+st;se.textContent=lbl;
  cand.querySelectorAll('.btn').forEach(b=>b.classList.remove('on'));
  if(st==='verified')cand.querySelector('.btn.keep').classList.add('on');
  if(st==='detached')cand.querySelector('.btn.detach').classList.add('on');
  if(st==='fixed')cand.querySelector('.btn.fix').classList.add('on');
}
async function decide(cand,act){
  const pub=cand.dataset.pub;const body=new URLSearchParams({pub,decision:act});
  if(act==='fix'){const u=cand.querySelector('.fixurl').value.trim();if(!u){showToast('paste a LinkedIn URL first');return}body.set('new_url',u)}
  try{const r=await fetch('/decide',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body});
    if(!r.ok)throw new Error(await r.text());const j=await r.json();
    setCandState(cand,j.action,j.approved,j.new_url);
    showToast({keep:'Kept',detach:'Detached',fix:'Re-targeted',reset:'Reset to model'}[act]||'Saved');
  }catch(e){showToast('Save failed: '+e.message)}
}
document.querySelectorAll('.cand .btn').forEach(b=>b.addEventListener('click',e=>{e.preventDefault();decide(b.closest('.cand'),b.dataset.act)}));
// initialize button highlight from current state
document.querySelectorAll('.cand').forEach(c=>{const st=[...c.classList].find(x=>x.startsWith('cand-')&&x!=='cand');
  if(c.classList.contains('cand-verified'))c.querySelector('.btn.keep').classList.add('on');
  if(c.classList.contains('cand-detached'))c.querySelector('.btn.detach').classList.add('on');
  if(c.classList.contains('cand-fixed'))c.querySelector('.btn.fix').classList.add('on');});
// lazy-load dossier on first expand
document.querySelectorAll('details.dossier').forEach(d=>d.addEventListener('toggle',async()=>{
  if(!d.open||d.dataset.loaded)return;d.dataset.loaded='1';const pre=d.querySelector('.dosstext');
  try{const r=await fetch('/api/dossier?slug='+encodeURIComponent(d.dataset.slug));pre.textContent=r.ok?await r.text():'(no dossier found)';}
  catch(e){pre.textContent='(failed to load dossier)'}}));
"""


# --- server -----------------------------------------------------------------

def make_handler(review_path: Path, verdicts_path: Path, parents_dir: Path, dossier_dir: Path,
                 confirm_threshold: float, detach_threshold: float):
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
            if parsed.path == "/api/dossier":
                slug = (urllib.parse.parse_qs(parsed.query).get("slug") or [""])[0]
                text = render_dossier(parents_dir, dossier_dir, slug)
                self.send_bytes(text.encode("utf-8"), "text/plain; charset=utf-8")
                return
            if parsed.path != "/":
                self.send_bytes(b"not found", "text/plain", status=404)
                return
            parents, _ = build_parents(verdicts_path, review_path)
            params = urllib.parse.parse_qs(parsed.query)
            self.send_bytes(page_html(parents, params, review_path))

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/decide":
                self.send_bytes(b"not found", "text/plain", status=404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            pub = (form.get("pub") or [""])[0]
            decision = (form.get("decision") or [""])[0]
            new_url = (form.get("new_url") or [""])[0]
            if not pub or decision not in {"keep", "detach", "fix", "reset"}:
                self.send_bytes(b"bad request", "text/plain", status=400)
                return
            try:
                result = apply_decision(review_path, verdicts_path, pub, decision, new_url,
                                        confirm_threshold, detach_threshold)
            except ValueError as exc:
                self.send_bytes(str(exc).encode(), "text/plain", status=400)
                return
            self.send_bytes(json.dumps({"ok": True, "pub": pub, **result}).encode(), "application/json")

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    return Handler


def cmd_serve(args: argparse.Namespace) -> None:
    review_path = Path(args.review)
    verdicts_path = Path(args.verdicts)
    parents_dir = Path(args.parents_dir)
    if not verdicts_path.exists():
        print(json.dumps({"primitive": "reconcile_review_web", "status": "error",
                          "error": f"no verdicts at {verdicts_path} — run `bin/deep-context reconcile` first"}))
        return
    parents, _ = build_parents(verdicts_path, review_path)
    server = ThreadingHTTPServer((args.host, args.port),
                                 make_handler(review_path, verdicts_path, parents_dir, Path(args.dossier_dir),
                                              args.confirm_threshold, args.detach_threshold))
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    print(json.dumps({"primitive": "reconcile_review_web", "status": "serving", "url": url,
                      "review": str(review_path), "parents": len(parents),
                      "needs_review": summarize(parents)["review"]}, indent=2))
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the deep-context LinkedIn self-heal review UI.")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve")
    serve.add_argument("--review", default=str(LINKEDIN_OVERRIDES_CSV))
    serve.add_argument("--verdicts", default=str(VERDICTS_JSONL))
    serve.add_argument("--parents-dir", default=str(PARENTS_DIR))
    serve.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    serve.add_argument("--confirm-threshold", type=float, default=DEFAULT_CONFIRM)
    serve.add_argument("--detach-threshold", type=float, default=DEFAULT_DETACH)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--open", action="store_true")
    serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        args = build_parser().parse_args(["serve", *(argv or [])])
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
