"""Presentation-only HTML for SQLite-hydrated Deep Context rows."""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any


DECISION_CHUNK_SIZE = 40
REVIEW_HTML = Path(__file__).with_name("reconcile_review.html")
REVIEW_CSS = Path(__file__).with_name("reconcile_review.css")
REVIEW_JS = Path(__file__).with_name("reconcile_review.js")
APPLIED = {"auto", "yes"}
GO_BACK_HTML = (
    "<p class='handoff-note'>Review complete — go back to Codex.</p>"
    "<div class='handoff-copy'><code>Review complete proceed with enrichment</code>"
    "<button class='button button-outline' type='button' data-copy-continue "
    "data-phrase='Review complete proceed with enrichment' data-toast='Copied'>Copy</button></div>"
)


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _primary_candidate(parent: dict[str, Any]) -> dict[str, Any]:
    candidates = parent.get("candidates") or []
    return next(
        (candidate for candidate in candidates if candidate.get("primary")),
        candidates[0] if candidates else {},
    )


def _worth_key(parent: dict[str, Any]) -> str:
    return str((parent.get("worth_row") or {}).get("key") or "")


def _effective_worth(parent: dict[str, Any]) -> str:
    return str((parent.get("worth_row") or {}).get("effective") or "maybe").lower()


def _needs_worth_review(parent: dict[str, Any]) -> bool:
    return _effective_worth(parent) == "maybe" and not any(
        candidate.get("synthetic") for candidate in parent.get("candidates") or []
    )


def _candidate_state(candidate: dict[str, Any]) -> str:
    action = str(candidate.get("action") or "").lower()
    approved = str(candidate.get("approved") or "").lower()
    if approved == "no" or action == "exclude":
        return "excluded"
    if action == "detach" and approved in APPLIED:
        return "detached"
    if action == "retarget" and approved in APPLIED:
        return "fixed"
    if action == "verify" and approved in APPLIED:
        return "verified"
    if candidate.get("llm_reject"):
        return "rejected"
    return "review"


def _contacts(candidate: dict[str, Any]) -> str:
    values = [*(candidate.get("match_emails") or []), *(candidate.get("match_phones") or [])]
    return " · ".join(dict.fromkeys(str(value) for value in values if value))


def _avatar(parent: dict[str, Any], candidate: dict[str, Any]) -> str:
    name = str(candidate.get("full_name") or parent.get("name") or "?")
    words = re.findall(r"[A-Za-z0-9]+", name)
    initials = "?" if not words else (words[0][0] + (words[-1][0] if len(words) > 1 else "")).upper()
    pub = str(candidate.get("profile_pub") or candidate.get("pub") or "").lower()
    image = (
        f"<img src='/api/avatar?pub={urllib.parse.quote(pub)}' alt='' "
        "onerror='this.remove()'>"
        if pub and not candidate.get("synthetic")
        else ""
    )
    return f"<span class='avatar'><span>{esc(initials)}</span>{image}</span>"


_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")


def markdown_to_html(markdown: str) -> str:
    """Render the small dossier markdown subset after escaping source text."""
    out: list[str] = []
    bullets: list[str] = []

    def flush() -> None:
        if bullets:
            out.append("<ul>" + "".join(f"<li>{item}</li>" for item in bullets) + "</ul>")
            bullets.clear()

    for raw in _COMMENT_RE.sub("", markdown).splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            flush()
            level = min(6, len(heading.group(1)) + 2)
            out.append(f"<h{level}>{esc(heading.group(2))}</h{level}>")
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            bullets.append(esc(bullet.group(1)))
            continue
        flush()
        out.append(f"<p>{esc(line)}</p>")
    flush()
    return "".join(out)


def _profile(parent: dict[str, Any], candidate: dict[str, Any]) -> str:
    name = str(candidate.get("full_name") or parent.get("name") or "This person")
    url = "" if candidate.get("synthetic") else str(candidate.get("url") or "")
    link = (
        f"<a class='linkedin-label' href='{esc(url)}' target='_blank' rel='noreferrer'>"
        "View LinkedIn<span aria-hidden='true'>↗</span></a>"
        if url
        else ""
    )
    contacts = _contacts(candidate)
    headline = str(candidate.get("headline") or "")
    location = str(candidate.get("location") or "")
    rows = "".join(
        f"<div><dt>{label}</dt><dd>{esc(value)}</dd></div>"
        for label, value in (("Contact", contacts), ("Summary", headline), ("Location", location))
        if value
    )
    return (
        f"<div class='profile-card'>{_avatar(parent, candidate)}<div class='profile-copy'>"
        f"<h2>{esc(name)}</h2>{link}</div></div>"
        f"<section class='details'><dl>{rows}</dl></section>"
    )


def render_worth_card(
    parent: dict[str, Any], parents_dir: Path, dossier_dir: Path, profile_cache_dir: Path | None = None,
) -> str:
    del parents_dir, dossier_dir, profile_cache_dir
    candidate = _primary_candidate(parent)
    key = _worth_key(parent)
    slug = str(parent.get("dossier_slug") or parent.get("slug") or "")
    return (
        "<article class='decision-card worth-card' data-card>"
        f"{_profile(parent, candidate)}"
        "<details class='worth-why'><summary>Why? Give feedback (optional)</summary>"
        "<textarea data-worth-note rows='2' maxlength='2000'></textarea></details>"
        "<div class='binary-actions'>"
        f"<button class='button button-outline' data-worth='no' data-pub='{esc(key)}' "
        f"data-parent='{esc(slug)}'>No</button>"
        f"<button class='button button-primary' data-worth='yes' data-pub='{esc(key)}' "
        f"data-parent='{esc(slug)}'>Yes</button></div></article>"
    )


def _guidance_form(candidate: dict[str, Any], slug: str) -> str:
    pub = str(candidate.get("row_key") or candidate.get("pub") or "")
    return (
        f"<form class='retarget-guidance' data-retarget-form data-pub='{esc(pub)}' "
        f"data-parent='{esc(slug)}'><textarea name='guidance' maxlength='2000'></textarea>"
        "<button class='button button-primary' type='submit'>Retarget</button></form>"
    )


def render_linkedin_card(
    parent: dict[str, Any], candidates: list[dict[str, Any]] | dict[str, Any],
    parents_dir: Path, dossier_dir: Path, profile_cache_dir: Path | None = None,
    *, failure_note: str = "",
) -> str:
    del parents_dir, dossier_dir, profile_cache_dir
    options = candidates if isinstance(candidates, list) else [candidates]
    options = options or [_primary_candidate(parent)]
    slug = str(parent.get("slug") or "")
    cards = []
    for candidate in options:
        pub = str(candidate.get("row_key") or candidate.get("pub") or "")
        cards.append(
            "<li class='linkedin-option'>"
            f"{_profile(parent, candidate)}<div class='binary-actions'>"
            f"<button data-decision='detach' data-pub='{esc(pub)}' data-parent='{esc(slug)}'>No</button>"
            f"<button data-decision='keep' data-pub='{esc(pub)}' data-parent='{esc(slug)}'>Yes</button>"
            f"</div>{_guidance_form(candidate, slug)}</li>"
        )
    failure = f"<p class='failure-note'>{esc(failure_note)}</p>" if failure_note else ""
    return (
        f"<article class='decision-card identity-card' data-card data-parent='{esc(slug)}'>"
        f"{failure}<h2>Check LinkedIn</h2><ul class='linkedin-options'>{''.join(cards)}</ul></article>"
    )


def _decision_row_html(parent: dict[str, Any], decision: str) -> str:
    candidate = _primary_candidate(parent)
    key = _worth_key(parent)
    slug = str(parent.get("slug") or "")
    target, label = ("no", "Move to No") if decision == "yes" else ("yes", "Move to Yes")
    return (
        f"<article class='decision-row' data-name='{esc(parent.get('name'))}'>"
        f"{_profile(parent, candidate)}<button data-worth='{target}' data-pub='{esc(key)}' "
        f"data-parent='{esc(slug)}'>{label}</button></article>"
    )


def decision_rows_payload(
    parents: list[dict[str, Any]], decision: str, *, offset: int = 0, limit: int = DECISION_CHUNK_SIZE,
) -> dict[str, Any]:
    rows = [parent for parent in parents if _effective_worth(parent) == decision]
    rows.sort(key=lambda parent: str(parent.get("name") or "").lower())
    offset = max(0, offset)
    chunk = rows[offset : offset + max(1, limit)]
    return {
        "view": decision,
        "total": len(rows),
        "offset": offset,
        "rows": [
            {"key": _worth_key(parent), "name": str(parent.get("name") or ""),
             "html": _decision_row_html(parent, decision)}
            for parent in chunk
        ],
    }


def render_decision_table(
    parents: list[dict[str, Any]], decision: str, **_: Any,
) -> str:
    payload = decision_rows_payload(parents, decision)
    body = "".join(str(row["html"]) for row in payload["rows"])
    return f"<div class='decision-table' data-view='{esc(decision)}'>{body}</div>"


def worth_pending_entries(parents: list[dict[str, Any]]) -> list[dict[str, str]]:
    queue = sorted(
        (parent for parent in parents if _needs_worth_review(parent)),
        key=lambda parent: str(parent.get("name") or "").lower(),
    )
    return [{"key": _worth_key(parent), "name": str(parent.get("name") or "")} for parent in queue]


def worth_search_html(view: str, pending: list[dict[str, str]] | None = None) -> str:
    data = ""
    if pending is not None:
        payload = json.dumps(pending, ensure_ascii=False).replace("<", "\\u003c")
        data = f"<script type='application/json' data-worth-pending>{payload}</script>"
    return (
        f"<div class='worth-search' data-search-view='{esc(view)}'>"
        "<input class='worth-search-input' type='search' placeholder='Search people…'>"
        f"{data}</div>"
    )


def render_decision_tabs(progress: dict[str, int], active: str, *, preview: bool = False) -> str:
    suffix = "&amp;preview=1" if preview else ""
    tabs = (("review", "Review", progress["worth_pending"]),
            ("yes", "Yes", progress["worth_yes"]), ("no", "No", progress["worth_no"]))
    return "<nav class='decision-tabs'>" + "".join(
        f"<a class='decision-tab{' active' if key == active else ''}' "
        f"href='/?stage=worth&amp;view={key}{suffix}'>{label}<span>{count}</span></a>"
        for key, label, count in tabs
    ) + "</nav>"


def _phase_view(params: dict[str, list[str]], progress: dict[str, int], manifest_path: Path) -> str:
    del progress, manifest_path
    requested = str((params.get("stage") or [""])[0]).lower()
    return requested if requested in {"worth", "enrich", "linkedin", "done"} else "worth"


def render_enrichment(
    enrichment: dict[str, Any], progress: dict[str, int], *, worth_complete: bool = False,
) -> str:
    if progress["worth_pending"] and not worth_complete:
        return f"<div class='empty-state'><h2>Review in progress</h2><p>{progress['worth_pending']} decisions left</p></div>"
    status = str(enrichment.get("status") or enrichment.get("state") or "not_started")
    counts = enrichment.get("counts") or {}
    if status in {"running", "submitted", "research_complete"}:
        return f"<div class='empty-state'><h2>Enriching contacts</h2><p>{int(counts.get('completed') or 0)} complete</p></div>"
    if status == "needs_approval":
        estimate = float(enrichment.get("estimated_usd") or 0)
        return f"<div class='empty-state'><h2>Ready to enrich</h2><button data-approve-enrichment>Approve ${estimate:.2f}</button></div>"
    if status == "completed":
        return "<div class='empty-state'><h2>Contacts enriched</h2><button data-complete='enrich'>Continue</button></div>"
    if status in {"failed", "completed_with_errors"}:
        return f"<div class='empty-state'><h2>Enrichment paused</h2><p>{esc(enrichment.get('error'))}</p></div>"
    return "<div class='empty-state'><h2>Preparing enrichment</h2></div>"


def _step(number: int, label: str, active: bool, complete: bool, count: int = 0, href: str = "") -> str:
    state = " active" if active else (" complete" if complete and not count else "")
    marker = "✓" if complete and not count else str(number)
    content = f"<span>{marker}</span><div>{esc(label)}{'<small>'+str(count)+' left</small>' if count else ''}</div>"
    return f"<a class='step{state}' href='{esc(href)}'>{content}</a>" if href else f"<div class='step{state}'>{content}</div>"


def _carousel_nav() -> str:
    return ("<button class='carousel-nav' data-carousel='prev'>&#8249;</button>"
            "<button class='carousel-nav' data-carousel='next'>&#8250;</button>")


def worth_review_body(
    parents: list[dict[str, Any]], progress: dict[str, int], parents_dir: Path, dossier_dir: Path,
    *, debug: bool = False, index: int = 0, profile_cache_dir: Path | None = None,
    exclude: frozenset[str] | None = None, auto_continue: bool = False,
) -> str:
    queue = [parent for parent in parents if _needs_worth_review(parent)]
    if exclude:
        queue = [parent for parent in queue if _worth_key(parent).lower() not in exclude]
    if queue:
        queue.sort(key=lambda parent: str(parent.get("name") or "").lower())
        index %= len(queue)
        card = render_worth_card(queue[index], parents_dir, dossier_dir, profile_cache_dir)
        return f"<div class='carousel-shell'>{_carousel_nav()}{card}</div>" if debug else card
    auto = " data-auto-complete" if auto_continue else ""
    return ("<div class='empty-state'><h2>Decisions ready</h2>"
            f"<p>{progress['lookup_ready']} people will be enriched</p>"
            f"<button data-complete='worth'{auto}>Continue</button></div>")


def linkedin_finished_body(
    progress: dict[str, int], *, linkedin_complete: bool, retargets_in_flight: int = 0,
    auto_continue: bool = False,
) -> str:
    auto = " data-auto-complete" if auto_continue and not linkedin_complete else ""
    tail = GO_BACK_HTML if linkedin_complete else f"<button data-complete='linkedin'{auto}>Finish</button>"
    running = f"<p>{retargets_in_flight} re-research still running</p>" if retargets_in_flight else ""
    return f"<div class='empty-state'><h2>LinkedIn profiles checked</h2><p>{progress['linkedin_done']} decisions saved</p>{running}{tail}</div>"


def _read_dossier(parents_dir: Path, dossier_dir: Path, slug: str) -> str:
    for path in (parents_dir / f"{Path(slug).name}.md", dossier_dir / f"{Path(slug).name}.md"):
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8")
        except OSError:
            return ""
    return ""


def render_person_detail(
    parent: dict[str, Any], parents_dir: Path, dossier_dir: Path, profile_cache_dir: Path | None = None,
) -> str:
    del profile_cache_dir
    candidate = _primary_candidate(parent)
    slug = str(parent.get("dossier_slug") or parent.get("slug") or "")
    dossier = markdown_to_html(_read_dossier(parents_dir, dossier_dir, slug))
    key = _worth_key(parent)
    effective = _effective_worth(parent)
    targets = ("no",) if effective == "yes" else (("yes",) if effective == "no" else ("yes", "no"))
    actions = "".join(
        f"<button data-dir-worth='{target}' data-pub='{esc(key)}' data-parent='{esc(slug)}'>Move to {target.title()}</button>"
        for target in targets
    )
    return (f"<article class='person-detail' data-person-slug='{esc(slug)}'>{actions}"
            f"{_profile(parent, candidate)}{_guidance_form(candidate, slug)}"
            f"<section class='directory-dossier'>{dossier}</section></article>")


def directory_page_html(
    parents: list[dict[str, Any]], params: dict[str, list[str]], *, parents_dir: Path,
    dossier_dir: Path, profile_cache_dir: Path | None = None, handoff: bool = False,
) -> bytes:
    del profile_cache_dir
    entries = [
        {"slug": str(parent.get("slug") or ""), "name": str(parent.get("name") or ""),
         "worth": _effective_worth(parent)}
        for parent in sorted(parents, key=lambda parent: str(parent.get("name") or "").lower())
        if parent.get("slug")
    ]
    selected = str((params.get("person") or [""])[0]).lower()
    parent = next((item for item in parents if str(item.get("slug") or "").lower() == selected), None)
    detail = render_person_detail(parent, parents_dir, dossier_dir) if parent else f"<div class='empty-state'><h2>{len(entries)} people</h2></div>"
    payload = json.dumps(entries, ensure_ascii=False).replace("<", "\\u003c")
    content = (
        f"{GO_BACK_HTML if handoff else ''}<div class='directory-layout' data-directory>"
        "<aside><input type='search' placeholder='Search people…'><nav data-directory-list></nav></aside>"
        f"<section data-directory-detail>{detail}</section></div>"
        f"<script type='application/json' data-directory-people>{payload}</script>"
    )
    document = REVIEW_HTML.read_text(encoding="utf-8")
    replacements = {"{{TITLE}}": "Directory", "{{STAGE}}": "directory", "{{PREVIEW}}": "false",
                    "{{EXTERNAL_UPDATES}}": "false", "{{STATE_TOKEN}}": "",
                    "{{ENRICHMENT_STATUS}}": "", "{{STEPPER}}": "", "{{CONTENT}}": content}
    for key, value in replacements.items():
        document = document.replace(key, value)
    return document.encode()
