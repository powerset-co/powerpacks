"""HTML fragment and page rendering for the review UI."""

from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.enrichment_contract import (
    STATE_DONE,
    STATE_FREE_PENDING,
    STATE_NEEDS_APPROVAL,
    STATE_RUNNING,
    STATUS_COMPLETED,
    derive_enrichment_state,
    read_enrichment_manifest,
)
from packs.ingestion.primitives.deep_context.common import (
    DOSSIER_DIR,
    ENRICH_MANIFEST,
    FACTS_DIR,
    PARENTS_DIR,
    PROFILE_CACHE_DIR,
    RAW_DIR,
    REVIEW_MANIFEST,
    VERDICTS_JSONL,
)
from packs.ingestion.primitives.deep_context.reconcile_linkedin import (
    linkedin_view,
)
from packs.ingestion.schemas.people_schema import (
    extract_public_identifier,
)

from .model import APPLIED_APPROVED, _cached_profile_pic, _primary_candidate, _worth_key, parent_status
from .workflow import _effective_no_row, _effective_yes, enrichment_handoff_completed, in_worth_view, needs_worth_review, pending_linkedin_candidates, phase_is_completed, read_review_manifest, review_progress, review_state_token, worth_selection_from_parents

DECISION_CHUNK_SIZE = 40


REVIEW_HTML = Path(__file__).with_name("reconcile_review.html")


REVIEW_CSS = Path(__file__).with_name("reconcile_review.css")


REVIEW_JS = Path(__file__).with_name("reconcile_review.js")


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
        # Candidate rows point straight at their composed CHILD dossier (no parent
        # stub exists before research mints a person for them).
        child = dossier_dir / f"{Path(slug).name}.md"
        if child.exists():
            return _strip_frontmatter(child.read_text(encoding="utf-8")).strip()
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


_GROKKED_RE = re.compile(r"^\s*_?grokked\b.*$", re.IGNORECASE)


_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")


_WIKILINK_RE = re.compile(r"\[\[[^\]|]*\|([^\]]*)\]\]|\[\[([^\]]*)\]\]")


def _dossier_section(markdown: str, heading: str) -> str:
    """The raw body of one ``## <heading>`` section, or '' when it is absent.

    Uses the same anchor pattern the rest of this module uses to slice a
    dossier section (up to the next ``##`` heading or end of file)."""
    match = re.search(
        rf"(?ms)^##\s+{re.escape(heading)}\s*\n+(.*?)(?=^##\s|\Z)", markdown)
    return match.group(1).strip() if match else ""


def _clean_text(value: str) -> str:
    """Message-derived markdown -> clean readable text (no wiki links / emphasis)."""
    text = _WIKILINK_RE.sub(lambda m: m.group(1) or m.group(2) or "", value)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)          # bold
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)   # italic (underscore)
    return text.strip()


def relationship_summary(parents_dir: Path, dossier_dir: Path, slug: str) -> str:
    """The dossier's "Relationship & cadence" prose as clean, readable text.

    Shown on cards under the "Relationship" label — NOT the dossier's own
    "## Summary" (which holds the "Network worth:" line and is intentionally
    dropped). The trailing "_grokked ..._" bookkeeping line is stripped;
    '' when the section is absent.
    """
    markdown = render_dossier(parents_dir, dossier_dir, slug)
    body = _dossier_section(markdown, "Relationship & cadence")
    lines = [line for line in body.splitlines() if not _GROKKED_RE.match(line)]
    return _clean_text("\n".join(lines).strip())


def timeline_entries(parents_dir: Path, dossier_dir: Path, slug: str) -> list[str]:
    """The dossier's "Timeline" per-date bullet lines as clean, readable text."""
    markdown = render_dossier(parents_dir, dossier_dir, slug)
    body = _dossier_section(markdown, "Timeline")
    entries: list[str] = []
    for line in body.splitlines():
        bullet = _BULLET_RE.match(line)
        text = _clean_text(bullet.group(1) if bullet else line)
        if text:
            entries.append(text)
    return entries


_MEETING_URL_RE = re.compile(
    r"(?i)\b(?:meet\.google\.com|zoom\.us|teams\.microsoft\.com|teams\.live\.com|"
    r"webex\.com|calendly\.com|cal\.com|whereby\.com|gotomeeting\.com)\b")


def dossier_identifiers(parents_dir: Path, dossier_dir: Path, slug: str) -> list[str]:
    """Bare identifier values (emails/phones/etc.) from the dossier's "Identifiers"
    section, so the card can bubble them up into its Contact line. Display-side
    only (dossier files are never rewritten); meeting/scheduling URLs are
    dropped because they are not contact info."""
    markdown = render_dossier(parents_dir, dossier_dir, slug)
    body = _dossier_section(markdown, "Identifiers")
    values: list[str] = []
    seen: set[str] = set()
    for line in body.splitlines():
        bullet = _BULLET_RE.match(line)
        if not bullet:
            continue
        value = _clean_text(bullet.group(1))
        if not value or value.lower() in seen or _MEETING_URL_RE.search(value):
            continue
        values.append(value)
        seen.add(value.lower())
    return values


def render_dossier_markdown(parents_dir: Path, dossier_dir: Path, slug: str) -> str:
    """The card's dossier preview: exactly two extracted sections as safe HTML.

    "Relationship" is the "Relationship & cadence" prose (never the dossier's
    own "## Summary", which carries the Network worth line). "Timeline" keeps its
    per-date bullets. Everything else (name block, wiki links, Topics,
    Identifiers, Possible same person, worth lines) is dropped; identifiers are
    bubbled up into the card's Contact section instead. Absent sections are
    omitted rather than raising, and all text is HTML-escaped.
    """
    parts: list[str] = []
    summary = relationship_summary(parents_dir, dossier_dir, slug)
    if summary:
        paragraphs = "".join(
            f"<p>{esc(block.strip())}</p>"
            for block in re.split(r"\n\s*\n", summary) if block.strip())
        parts.append(f"<div><dt>Relationship</dt><dd>{paragraphs}</dd></div>")
    timeline = timeline_entries(parents_dir, dossier_dir, slug)
    if timeline:
        items = "".join(f"<li>{esc(entry)}</li>" for entry in timeline)
        parts.append(f"<div><dt>Timeline</dt><dd><ul class='fact-list'>{items}</ul></dd></div>")
    return "".join(parts)


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _initials(name: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", name or "")
    if not words:
        return "?"
    return (words[0][0] + (words[-1][0] if len(words) > 1 else "")).upper()


def _avatar(parent: dict[str, Any], candidate: dict[str, Any] | None = None, *, small: bool = False) -> str:
    cand = candidate or _primary_candidate(parent)
    name = str(cand.get("full_name") or parent.get("name") or "")
    pub = str(cand.get("profile_pub") or cand.get("pub") or "").strip().lower()
    can_load = bool(pub and not cand.get("import_candidate") and not cand.get("synthetic")
                    and (cand.get("profile_pic_url") or _cached_profile_pic(pub)))
    image = (f"<img src='/api/avatar?pub={urllib.parse.quote(pub)}' alt='' loading='eager' "
             "onerror='this.remove()'>" if can_load else "")
    return (f"<span class='avatar{' avatar-small' if small else ''}' aria-label='{esc(name)}'>"
            f"<span>{esc(_initials(name))}</span>{image}</span>")


def _machine_copy(parent: dict[str, Any]) -> tuple[str, str]:
    machine = parent.get("machine_worth") or parent.get("worth") or {}
    decision = str(machine.get("decision") or "maybe").lower()
    label = {"yes": "Suggested yes", "no": "Suggested no", "maybe": "AI is unsure"}.get(decision, "AI is unsure")
    return label, str(machine.get("reason") or "")


def _merge_contacts(contacts: list[str], identifiers: list[str]) -> list[str]:
    """Contact values plus the dossier's Identifiers, deduped case-insensitively
    against what is already shown (bubbling the dossier's aliases up into Contact)."""
    merged = list(contacts)
    seen = {value.lower() for value in merged}
    for value in identifiers:
        if value and value.lower() not in seen:
            merged.append(value)
            seen.add(value.lower())
    return merged


NO_PROFILE_SUMMARY = "Couldn't find profile"


def _display_reason(reason: str) -> str:
    """Card display only (stored CSV reasons are never rewritten): the
    "deep research: <process notes>" text is NEVER shown. A real summary before
    a "; deep research:" tail is kept; a reason that is only the research blob
    (or empty) yields '' — the caller decides between omitting the row and the
    no-profile fallback based on whether the card has a kept link."""
    marker = reason.lower().find("deep research:")
    cleaned = reason if marker == -1 else reason[:marker]
    return cleaned.strip().rstrip(";,·—–-").strip()


FACT_LIST_PINNED = 3


def _fact_list(items: list[str], *, pinned: int = FACT_LIST_PINNED) -> str:
    """Bullet list with the first ``pinned`` entries shown; longer lists keep the
    rest behind a "+ show N more" toggle (wired in reconcile_review.js)."""
    shown = "".join(f"<li>{esc(item)}</li>" for item in items[:pinned])
    hidden = "".join(f"<li hidden data-more-item>{esc(item)}</li>" for item in items[pinned:])
    extra = len(items) - pinned
    toggle = (f"<button type='button' class='show-more' data-show-more "
              f"data-more-label='+ show {extra} more' data-less-label='show fewer'>"
              f"+ show {extra} more</button>" if extra > 0 else "")
    return f"<ul class='fact-list'>{shown}{hidden}</ul>{toggle}"


def profile_fact_rows(candidate: dict[str, Any]) -> list[str]:
    """Work / Education dt-dd rows in the same section style as Contact."""
    rows: list[str] = []
    if candidate.get("experiences"):
        rows.append(f"<div><dt>Work</dt><dd>{_fact_list(candidate['experiences'])}</dd></div>")
    if candidate.get("education"):
        rows.append(f"<div><dt>Education</dt><dd>{_fact_list(candidate['education'])}</dd></div>")
    return rows


def _details(parent: dict[str, Any], candidate: dict[str, Any], *, identity: bool,
             profile_rows: list[str] | None = None,
             identifiers: list[str] | None = None,
             profile_placeholder: str = "",
             sparse_context: bool = False) -> str:
    contacts = _merge_contacts(
        [*(candidate.get("match_emails") or []), *(candidate.get("match_phones") or [])],
        identifiers or [])
    reason = _display_reason(str(candidate.get("reason") or ""))
    # The prefetch stage's LLM-written profile summary, lifted onto the candidate
    # from the local cache at render time (display-only; the server never calls
    # an LLM). It is PREFERRED for the Summary row over the stored judge reason.
    simple_summary = str(candidate.get("simple_summary") or "").strip()
    # Section order: Contact -> Summary (simple_summary || the judge/verify reason)
    # -> the lazily loaded dossier rows (Relationship -> Timeline) -> the profile
    # facts (Work / Education) when profile data exists. (No Evidence section.)
    rows: list[str] = []
    if contacts:
        rows.append(f"<div><dt>Contact</dt><dd>{esc(' · '.join(contacts))}</dd></div>")
    if simple_summary:
        rows.append(f"<div><dt>Summary</dt><dd>{esc(simple_summary)}</dd></div>")
    elif identity:
        if reason:
            rows.append(f"<div><dt>Summary</dt><dd>{esc(reason)}</dd></div>")
        elif not str(candidate.get("url") or "").strip():
            # no kept link AND nothing displayable -> researched-and-failed
            rows.append(f"<div><dt>Summary</dt><dd>{esc(NO_PROFILE_SUMMARY)}</dd></div>")
        # kept link with no displayable reason: omit the row entirely
    extra = f"<dl>{''.join(rows)}</dl>" if rows else ""
    context_notice = (
        "<p class='context-empty'>Not enough information.</p>"
        if sparse_context else ""
    )
    profile_dl = (f"<dl>{''.join(profile_rows)}</dl>" if profile_rows else "")
    dossier_slug = parent.get("dossier_slug") or parent.get("slug")
    # No "Details"/"Context" section labels; the dossier preview renders as more
    # dt/dd rows in the SAME dl style (no inset box), lazily via /api/dossier.
    return (f"<section class='details' data-slug='{esc(dossier_slug)}'>"
            f"<div class='details-body'>{profile_placeholder}{extra}{context_notice}"
            "<dl class='dossier-text' aria-busy='true'></dl>"
            f"{profile_dl}</div></section>")


def _scroll_region(content: str) -> str:
    return ("<div class='identity-scroll-shell'>"
            f"<div class='identity-scroll'>{content}</div>"
            "<button class='scroll-cue' type='button' data-scroll-cue "
            "aria-label='Scroll down' hidden>"
            "<svg viewBox='0 0 24 24' aria-hidden='true' focusable='false'>"
            "<path d='m7 9 5 5 5-5'></path></svg></button></div>")


_STALE_MESSAGE_DAYS = 3 * 365  # matches the messages sync default (3 years)


def _recent_messages_html(parent: dict[str, Any], raw_dir: Path = RAW_DIR) -> str:
    """The person's last few raw messages, straight from the collected bundles
    the judge itself read (deep-context's scoped body-reading surface; local
    display only, nothing leaves the machine). This is what lets a human
    one-click a "maybe" like a 2021 one-way 'which number should I use' text:
    the card SHOWS the evidence instead of hiding the person. A stale callout
    flags threads older than the sync default, since a full WhatsApp backfill
    happily surfaces half-decade-old fragments."""
    person_ids = list(parent.get("person_ids") or []) or \
        list((parent.get("worth_row") or {}).get("person_ids") or [])
    messages: list[dict[str, Any]] = []
    for pid in person_ids:
        bundle_path = raw_dir / f"{pid}.json"
        try:
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for msg in bundle.get("messages") or []:
            if isinstance(msg, dict) and str(msg.get("text") or "").strip():
                messages.append(msg)
    if not messages:
        return ""
    messages.sort(key=lambda m: str(m.get("at") or ""), reverse=True)
    newest = str(messages[0].get("at") or "")
    stale = ""
    try:
        newest_dt = datetime.fromisoformat(newest.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - newest_dt).days
        if age_days > _STALE_MESSAGE_DAYS:
            # One decimal, never floored: "Aug 2022 — 3 years ago" next to a
            # 3-year window read as a contradiction when it was really 3.9.
            stale = (f"<p class='worth-messages-stale'>Last message "
                     f"{newest_dt.strftime('%b %Y')} — {age_days / 365.25:.1f} years ago, "
                     "older than your 3-year sync window</p>")
    except ValueError:
        pass
    rows = []
    for msg in messages[:3]:
        who = "you" if str(msg.get("direction") or "") == "from_me" else "them"
        when = str(msg.get("at") or "")[:10]
        text = str(msg.get("text") or "").strip()
        text = text[:200] + ("…" if len(text) > 200 else "")
        rows.append(f"<li><span class='worth-message-meta'>{esc(msg.get('channel') or 'message')}"
                    f" · {esc(who)} · {esc(when)}</span> {esc(text)}</li>")
    return ("<div class='worth-messages'>"
            f"{stale}<ul>{''.join(rows)}</ul></div>")


def render_worth_card(parent: dict[str, Any], parents_dir: Path, dossier_dir: Path,
                      profile_cache_dir: Path = PROFILE_CACHE_DIR) -> str:
    candidate = _primary_candidate(parent)
    # Cache-only: prefer the prefetch stage's profile summary in the Summary row
    # (display-only; never a provider call). Worth cards render identity=False, so
    # this is the ONLY thing that can put a Summary row on them.
    candidate["simple_summary"] = _cached_simple_summary(candidate, profile_cache_dir)
    key = _worth_key(parent)
    name = str(parent.get("name") or candidate.get("full_name") or "This person")
    slug = parent.get("dossier_slug") or parent.get("slug")
    identifiers = dossier_identifiers(parents_dir, dossier_dir, slug)
    sparse_context = (
        not candidate.get("simple_summary")
        and not relationship_summary(parents_dir, dossier_dir, slug)
        and not timeline_entries(parents_dir, dossier_dir, slug)
    )
    scroll_content = f"""
        <div class='person-top'>
          {_avatar(parent, candidate)}
          <div class='person-copy'>
            <h2>{esc(name)}</h2>
          </div>
        </div>
        {_details(
            parent,
            candidate,
            identity=False,
            identifiers=identifiers,
            sparse_context=sparse_context,
        )}
        {_recent_messages_html(parent)}"""
    return f"""
    <article class='decision-card identity-card worth-card' data-card>
      {_scroll_region(scroll_content)}
      <div class='identity-decision'>
        <div class='binary-actions'>
          <button class='button button-outline' data-worth='no' data-pub='{esc(key)}' data-parent='{esc(slug)}'>No</button>
          <button class='button button-primary' data-worth='yes' data-pub='{esc(key)}' data-parent='{esc(slug)}'>Yes</button>
        </div>
      </div>
    </article>"""


def render_candidate(idx: int, total: int, cand: dict[str, Any], **_: Any) -> str:
    """Compatibility renderer used by focused model tests; the live UI renders a parent."""
    decision = str((cand.get("worth") or {}).get("decision") or "maybe")
    key = str(cand.get("worth_key") or cand.get("pub") or "")
    return (f"<div class='candidate-compat' data-pub='{esc(cand.get('pub'))}'>"
            f"<span data-model-worth='{esc(decision)}'></span>"
            f"<button data-worth='no' data-pub='{esc(key)}'>No</button>"
            f"<button data-worth='yes' data-pub='{esc(key)}'>Yes</button></div>")


def _cached_simple_summary(candidate: dict[str, Any], profile_cache_dir: Path) -> str:
    """The prefetch stage's persisted ``simple_summary`` for this card's profile,
    read from the local cache record (display-only; never a provider call).

    Synthetic rows have no real LinkedIn cache entry, so they never carry one."""
    if candidate.get("synthetic"):
        return ""
    pub = str(candidate.get("profile_pub") or candidate.get("pub") or "").strip().lower()
    url = str(candidate.get("url") or "")
    pub = pub or extract_public_identifier(url).lower()
    if not pub or pub.startswith("candidate:"):
        return ""
    cache_path = profile_cache_dir / f"{pub}.json"
    if not cache_path.exists():
        return ""
    try:
        record = json.loads(cache_path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return ""
    return str(record.get("simple_summary") or "").strip()


def _hydrate_card_profile(candidate: dict[str, Any], profile_cache_dir: Path) -> bool:
    """Cache-ONLY render-time hydration for a card whose verdict snapshot has no
    profile facts (e.g. the cache was empty at reconcile time and the offline
    prefetch stage has since filled it). Local file read, never a provider call.
    Also lifts the prefetch stage's ``simple_summary`` onto the candidate so the
    Summary row can prefer it. Returns True when the candidate still has no
    profile facts afterwards."""
    candidate["simple_summary"] = _cached_simple_summary(candidate, profile_cache_dir)
    if candidate.get("synthetic"):
        return False
    if (candidate.get("experiences") or candidate.get("education")
            or candidate.get("headline")):
        return False
    pub = str(candidate.get("profile_pub") or candidate.get("pub") or "").strip().lower()
    url = str(candidate.get("url") or "")
    if not pub and not url:
        return False
    view = linkedin_view({"public_identifier": pub or extract_public_identifier(url).lower(),
                          "linkedin_url": url}, profile_cache_dir)
    if view.get("has_profile"):
        for field in ("full_name", "headline", "profile_pic_url", "experiences",
                      "education", "location"):
            if view.get(field):
                candidate[field] = view[field]
        candidate["has_profile"] = True
        return False
    return True  # attached link, nothing cached -> surface the prefetch note


def _skip_link(pub: Any, parent_slug: Any) -> str:
    """The inline "Skip" affordance folded into the card's question line.

    A real ``<button>`` (keyboard-activatable, proper hit area, wired by the SAME
    ``data-decide`` click delegation as before) styled as a subtle secondary link.
    Behavior is unchanged from the old standalone Skip button: detach the card,
    keyed on the parent's pub/slug."""
    return (f"<button type='button' class='skip-link' data-decide='detach' "
            f"data-toast='Skipped' data-pub='{esc(pub)}' data-parent='{esc(parent_slug)}'>"
            "Skip</button>")


def render_linkedin_card(parent: dict[str, Any],
                         candidates: dict[str, Any] | list[dict[str, Any]],
                         parents_dir: Path, dossier_dir: Path,
                         profile_cache_dir: Path = PROFILE_CACHE_DIR) -> str:
    """One review card for a parent's pending LinkedIn candidate(s).

    A single candidate (the overwhelming common case — every normal person) renders
    EXACTLY as before via ``_render_single_linkedin_card``. A merged parent with more
    than one pending candidate — e.g. two synthetic profiles for the same person after
    a later cluster_merge — renders ONE card ("show the parent, not the children"): the
    person once, then the candidate profiles as a selectable option list. Picking one
    resolves the whole parent (the /decide endpoint keeps the pick and withdraws the
    siblings)."""
    cand_list = [candidates] if isinstance(candidates, dict) else list(candidates)
    if len(cand_list) <= 1:
        candidate = cand_list[0] if cand_list else {}
        return _render_single_linkedin_card(
            parent, candidate, parents_dir, dossier_dir, profile_cache_dir)
    return _render_multi_linkedin_card(
        parent, cand_list, parents_dir, dossier_dir, profile_cache_dir)


def _render_single_linkedin_card(parent: dict[str, Any], candidate: dict[str, Any],
                                 parents_dir: Path, dossier_dir: Path,
                                 profile_cache_dir: Path = PROFILE_CACHE_DIR) -> str:
    name = str(parent.get("name") or candidate.get("full_name") or "this person")
    synthetic = bool(candidate.get("synthetic"))
    cache_miss = _hydrate_card_profile(candidate, profile_cache_dir)
    profile_name = str(candidate.get("full_name") or name)
    # The header shows the name, avatar, and — for a REAL profile — the genuine
    # LinkedIn headline + View-LinkedIn link. For a researched/synthetic row the
    # "headline" is an LLM relationship blurb ("Also known as… My relationship…"),
    # which is redundant with the Summary/Relationship/Timeline sections below, so
    # it (and the "Researched profile" / "No LinkedIn found" labels) are dropped.
    # Both a real and a synthetic row present the SAME decision UI ("Is this the
    # right profile? Or Skip?" + [No] [Use this profile] + a hidden fix form behind
    # No). The "Skip" is an inline secondary link folded into the question line (same
    # detach behavior as the old standalone button). A synthetic row simply has no
    # genuine LinkedIn header (no View-LinkedIn link, no headline); the SEMANTIC
    # difference lives only in the /decide endpoint, which routes a keep on a
    # ``synth-`` pub through the synthetic approve gate.
    question = f"Is this the right profile? Or {_skip_link(candidate.get('pub'), parent.get('slug'))}?"
    eyebrow = ""
    if synthetic:
        link = ""
        header_headline = ""
    else:
        url = str(candidate.get("url") or "")
        link = (f"<a class='linkedin-label' href='{esc(url)}' target='_blank' rel='noreferrer'>View LinkedIn"
                "<span aria-hidden='true'>↗</span></a>") if url else ""
        header_headline = str(candidate.get("headline") or "")
    fix_form = f"""<form class='linkedin-fix-form' data-fix-form
          data-pub='{esc(candidate.get('pub'))}' data-parent='{esc(parent.get('slug'))}'>
        <label class='sr-only' for='fix-{esc(candidate.get('pub'))}'>LinkedIn URL</label>
        <div><input id='fix-{esc(candidate.get('pub'))}' name='new_url' inputmode='url'
          autocomplete='url' placeholder='linkedin.com/in/…' required>
        <button class='button button-outline' type='submit'>Use this</button></div>
      </form>"""
    # Attached LinkedIn with nothing in the local profile cache: neutral copy
    # only — no operator plumbing in the card. The UI never fetches; the skill's
    # profile-prefetch stage fills the cache and logs every miss in its manifest.
    placeholder = ("<p class='profile-note'>Not enough profile information "
                   "available.</p>" if cache_miss else "")
    identifiers = dossier_identifiers(
        parents_dir, dossier_dir, parent.get("dossier_slug") or parent.get("slug"))
    scroll_content = f"""
        {f"<div class='identity-eyebrow'>{esc(eyebrow)}</div>" if eyebrow else ""}
        <div class='profile-card'>
          {_avatar(parent, candidate)}
          <div class='profile-copy'>
            <h2>{esc(profile_name)}</h2>
            {link}
            {f"<p>{esc(header_headline)}</p>" if header_headline else ""}
            {f"<span>{esc(candidate.get('location'))}</span>" if candidate.get('location') else ""}
          </div>
        </div>
        {_details(parent, candidate, identity=True, profile_rows=profile_fact_rows(candidate),
                  identifiers=identifiers, profile_placeholder=placeholder)}"""
    return f"""
    <article class='decision-card identity-card' data-card data-parent='{esc(parent.get('slug'))}'>
      {_scroll_region(scroll_content)}
      <div class='identity-decision'>
        <div class='question'>{question}</div>
        <div class='binary-actions'>
          <button class='button button-outline' data-open-fix aria-expanded='false'
                  aria-controls='fix-section-{esc(candidate.get('pub'))}'>No</button>
          <button class='button button-primary' data-decide='keep'
                  data-pub='{esc(candidate.get('pub'))}'
                  data-parent='{esc(parent.get('slug'))}'>Use this profile</button>
        </div>
        <div class='alternate' id='fix-section-{esc(candidate.get('pub'))}' hidden>
          {fix_form}
        </div>
      </div>
    </article>"""


def _linkedin_option(parent: dict[str, Any], candidate: dict[str, Any],
                     profile_cache_dir: Path) -> str:
    """One selectable profile option inside a multi-candidate parent card.

    The card header already names the person once — options never repeat the
    name, avatar, headline, or location. Each option is just a facts list: a
    "LinkedIn" row (an ↗ icon link for a real profile, "N/A" for a synthetic
    no-LinkedIn row) followed by Summary/Work/Education, plus its own
    "Use this profile" action keyed on that option's pub. Picking it resolves
    the whole parent."""
    _hydrate_card_profile(candidate, profile_cache_dir)
    pub = str(candidate.get("pub") or "")
    synthetic = bool(candidate.get("synthetic"))
    url = "" if synthetic else str(candidate.get("url") or "")
    link = (f"<a class='linkedin-label' href='{esc(url)}' target='_blank' rel='noreferrer' "
            "aria-label='View LinkedIn profile'><span aria-hidden='true'>↗</span></a>"
            if url else "<span class='linkedin-label-na'>N/A</span>")
    summary = (str(candidate.get("simple_summary") or "").strip()
               or _display_reason(str(candidate.get("reason") or "")))
    rows: list[str] = [f"<div><dt>LinkedIn</dt><dd>{link}</dd></div>"]
    if summary:
        rows.append(f"<div><dt>Summary</dt><dd>{esc(summary)}</dd></div>")
    body = f"<dl>{''.join(rows + profile_fact_rows(candidate))}</dl>"
    return f"""
      <li class='linkedin-option' data-linkedin-option>
        {body}
        <button class='button button-primary' data-decide='keep'
                data-pub='{esc(pub)}' data-parent='{esc(parent.get('slug'))}'>Use this profile</button>
      </li>"""


def _render_multi_linkedin_card(parent: dict[str, Any], candidates: list[dict[str, Any]],
                                parents_dir: Path, dossier_dir: Path,
                                profile_cache_dir: Path = PROFILE_CACHE_DIR) -> str:
    """ONE card for a merged parent offering N candidate profiles as options.

    The person is shown ONCE (name/avatar/merged Summary/Relationship/Timeline), then the
    candidate profiles as a selectable option list. A shared "None of these" opens the
    add-LinkedIn fix form and "Skip" detaches; either is keyed on the parent's primary
    candidate so the existing single-pub /decide semantics still apply. Picking any one
    option resolves the whole parent (siblings are withdrawn server-side)."""
    name = str(parent.get("name") or "this person")
    primary = candidates[0]
    identifiers = dossier_identifiers(
        parents_dir, dossier_dir, parent.get("dossier_slug") or parent.get("slug"))
    options = "".join(
        _linkedin_option(parent, cand, profile_cache_dir) for cand in candidates)
    # The person header + merged context appears once; the per-option profiles follow.
    scroll_content = f"""
        <div class='profile-card'>
          {_avatar(parent, primary)}
          <div class='profile-copy'><h2>{esc(name)}</h2></div>
        </div>
        {_details(parent, primary, identity=True, identifiers=identifiers)}
        <div class='linkedin-options-intro'>We found more than one possible profile — pick the right one.</div>
        <ul class='linkedin-options'>{options}</ul>"""
    # "None of these" / the inline Skip act on the parent via its primary candidate's
    # pub, reusing the existing fix-form + detach paths unchanged. Skip is folded into
    # the question line (secondary link), not a standalone button.
    fix_form = f"""<form class='linkedin-fix-form' data-fix-form
          data-pub='{esc(primary.get('pub'))}' data-parent='{esc(parent.get('slug'))}'>
        <label class='sr-only' for='fix-{esc(primary.get('pub'))}'>LinkedIn URL</label>
        <div><input id='fix-{esc(primary.get('pub'))}' name='new_url' inputmode='url'
          autocomplete='url' placeholder='linkedin.com/in/…' required>
        <button class='button button-outline' type='submit'>Use this</button></div>
      </form>"""
    question = f"Is this the right profile? Or {_skip_link(primary.get('pub'), parent.get('slug'))}?"
    return f"""
    <article class='decision-card identity-card identity-card-multi' data-card
             data-parent='{esc(parent.get('slug'))}' data-multi-option>
      {_scroll_region(scroll_content)}
      <div class='identity-decision'>
        <div class='question'>{question}</div>
        <div class='binary-actions'>
          <button class='button button-outline' data-open-fix aria-expanded='false'
                  aria-controls='fix-section-{esc(primary.get('pub'))}'>None of these</button>
        </div>
        <div class='alternate' id='fix-section-{esc(primary.get('pub'))}' hidden>
          {fix_form}
        </div>
      </div>
    </article>"""


def render_parent(idx: int, parent: dict[str, Any], expanded: bool = False) -> str:
    """Compact compatibility view; live pages use the staged card renderers."""
    candidate = _primary_candidate(parent)
    return (f"<article class='person-row' data-slug='{esc(parent.get('slug'))}'>"
            f"{_avatar(parent, candidate, small=True)}<strong>{esc(parent.get('name'))}</strong>"
            f"<span>{esc(parent_status(parent))}</span></article>")


def _rejection_reason(parent: dict[str, Any]) -> str:
    worth = parent.get("worth") or {}
    machine = parent.get("machine_worth") or {}
    if worth.get("source") == "user":
        return "You said no"
    if any(str(candidate.get("action") or "").strip().lower() == "exclude"
           and str(candidate.get("approved") or "").strip().lower() in APPLIED_APPROVED
           for candidate in parent.get("candidates") or []):
        return "Excluded"
    reason = str(machine.get("reason") or "").strip()
    if not reason or reason.lower() == "not yet judged":
        reason = "Not worth adding"
    return reason if len(reason) <= 140 else reason[:137].rsplit(" ", 1)[0] + "…"


def _decision_scope(parents: list[dict[str, Any]], decision: str) -> list[dict[str, Any]]:
    """The full, name-sorted row set for one decision table (yes = Added, no = Rejected)."""
    if decision not in {"yes", "no"}:
        raise ValueError(f"unknown decision table: {decision}")
    rows_in_scope = [
        parent for parent in parents
        if in_worth_view(parent)
        and ((decision == "yes" and _effective_yes(parent))
             or (decision == "no" and _effective_no_row(parent)))
    ]
    rows_in_scope.sort(key=lambda item: str(item.get("name") or "").lower())
    return rows_in_scope


def _decision_row_html(parent: dict[str, Any], decision: str,
                       parents_dir: Path | None, dossier_dir: Path | None) -> str:
    """One expandable decision row (shared by the initial table and /api/decision-rows)."""
    candidate = _primary_candidate(parent)
    flip = "no" if decision == "yes" else "yes"
    flip_label = "No" if decision == "yes" else "Yes"
    reason = (_rejection_reason(parent) if decision == "no" else
              str((parent.get("machine_worth") or {}).get("reason") or "Worth adding"))
    dossier_slug = parent.get("dossier_slug") or parent.get("slug")
    identifiers = (dossier_identifiers(parents_dir, dossier_dir, dossier_slug)
                   if parents_dir is not None and dossier_dir is not None else [])
    sparse_context = (
        parents_dir is not None
        and dossier_dir is not None
        and not candidate.get("simple_summary")
        and not relationship_summary(parents_dir, dossier_dir, dossier_slug)
        and not timeline_entries(parents_dir, dossier_dir, dossier_slug)
    )
    contacts = _merge_contacts(
        [*(candidate.get("match_emails") or []), *(candidate.get("match_phones") or [])],
        identifiers)
    why_label = "Why no" if decision == "no" else "Why yes"
    fact_rows = []
    if contacts:
        fact_rows.append(f"<div><dt>Contact</dt><dd>{esc(' · '.join(contacts))}</dd></div>")
    fact_rows.append(f"<div><dt>{why_label}</dt><dd>{esc(reason)}</dd></div>")
    dossier_preview = (
        "<p class='context-empty'>Not enough information.</p>"
        if sparse_context
        else "<dl class='row-facts dossier-text' aria-busy='true'></dl>"
    )
    # Left-edge chevron = the expand/collapse affordance; the decision button
    # stays on the far right of the summary row. data-name is the live-search
    # filter's match target (lowercased display name, matched client-side).
    return f"""
        <details class='decision-row' data-card data-slug='{esc(dossier_slug)}' data-name='{esc(str(parent.get('name') or '').lower())}'>
          <summary class='decision-row-summary'>
            <span class='decision-row-caret' aria-hidden='true'></span>
            {_avatar(parent, candidate, small=True)}
            <div class='decision-row-main'><strong>{esc(parent.get('name'))}</strong><span>{esc(reason)}</span></div>
            <div class='decision-row-actions'>
              <button class='button button-ghost' data-worth='{flip}' data-pub='{esc(_worth_key(parent))}' data-parent='{esc(dossier_slug)}' aria-label='Mark {esc(parent.get('name'))} {flip_label}'>{flip_label}</button>
            </div>
          </summary>
          <div class='decision-row-detail'>
            <dl class='row-facts'>{''.join(fact_rows)}</dl>
            {dossier_preview}
          </div>
        </details>"""


def decision_rows_payload(parents: list[dict[str, Any]], decision: str, *,
                          offset: int = 0, limit: int = DECISION_CHUNK_SIZE,
                          parents_dir: Path | None = None,
                          dossier_dir: Path | None = None) -> dict[str, Any]:
    """One chunk of decision rows for the infinite-scroll list (/api/decision-rows).

    `total` is always the FULL scope size so the client can keep counts and its
    end-of-list state authoritative regardless of how much has been fetched."""
    rows_in_scope = _decision_scope(parents, decision)
    offset = max(0, offset)
    limit = max(1, limit)
    return {
        "view": decision,
        "total": len(rows_in_scope),
        "offset": offset,
        "rows": [_decision_row_html(parent, decision, parents_dir, dossier_dir)
                 for parent in rows_in_scope[offset:offset + limit]],
    }


def render_decision_table(parents: list[dict[str, Any]], decision: str, *,
                          chunk_size: int = DECISION_CHUNK_SIZE,
                          parents_dir: Path | None = None,
                          dossier_dir: Path | None = None) -> str:
    """The decision list shell plus its first chunk of rows.

    No pagination: the client's infinite scroll fetches further chunks from
    /api/decision-rows and virtualizes the list (data-total carries the full
    scope size so tab badges / end detection never depend on loaded rows)."""
    rows_in_scope = _decision_scope(parents, decision)
    if not rows_in_scope:
        return ("<div class='empty-state decision-empty'><div class='empty-mark'>0</div>"
                f"<h2>No {esc(decision)} decisions</h2></div>")
    chunk_size = max(1, chunk_size)
    rows = [_decision_row_html(parent, decision, parents_dir, dossier_dir)
            for parent in rows_in_scope[:chunk_size]]
    return ("<div class='decision-page'>"
            + worth_search_html(decision)
            + f"<section class='decision-list' data-decision-list data-view='{esc(decision)}' "
            f"data-total='{len(rows_in_scope)}' data-chunk='{chunk_size}'>"
            + "".join(rows) + "</section></div>")


def worth_pending_entries(parents: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Name + worth key of every still-pending Review person, in queue order —
    the typeahead's dataset, embedded in the rendered page as a JSON island and
    pruned client-side as decisions settle (no separate names endpoint)."""
    queue = [parent for parent in parents if needs_worth_review(parent)]
    queue.sort(key=lambda parent: str(parent.get("name") or "").lower())
    return [{"key": str(_worth_key(parent) or ""), "name": str(parent.get("name") or "")}
            for parent in queue]


def worth_search_html(view: str, pending: list[dict[str, str]] | None = None) -> str:
    """The ONE live-search input shared by all worth views (filters as you type;
    never a Search button). The Yes/No tables hide non-matching rows client-side
    with an "N of M" count; the Review card view (``pending`` given) gets a
    typeahead dropdown over the embedded pending queue that jumps straight to a
    picked person's card via /api/worth-card?pick=."""
    extras = ""
    if pending is not None:
        payload = json.dumps(pending, ensure_ascii=False).replace("<", "\\u003c")
        extras = (f"<script type='application/json' data-worth-pending>{payload}</script>"
                  "<ul class='worth-search-list' data-search-list role='listbox' hidden></ul>")
    return (f"<div class='worth-search' data-worth-search data-search-view='{esc(view)}'>"
            "<input class='worth-search-input' type='search' placeholder='Search people…' "
            "aria-label='Search people by name' autocomplete='off' spellcheck='false'>"
            "<span class='worth-search-count' data-search-count hidden></span>"
            f"{extras}</div>")


def render_decision_tabs(progress: dict[str, int], active: str, *, preview: bool = False) -> str:
    tabs = (("review", "Review", progress["worth_pending"]),
            ("yes", "Yes", progress["worth_yes"]),
            ("no", "No", progress["worth_no"]))
    links = []
    for key, label, count in tabs:
        current = " aria-current='page'" if key == active else ""
        preview_query = "&amp;preview=1" if preview else ""
        links.append(
            f"<a class='decision-tab{' active' if key == active else ''}' data-tab='{key}' "
            f"href='/?stage=worth&amp;view={key}{preview_query}'{current}>"
            f"{label}<span>{count}</span></a>")
    return "<nav class='decision-tabs' aria-label='People decisions'>" + "".join(links) + "</nav>"


def _phase_view(params: dict[str, list[str]], progress: dict[str, int], manifest_path: Path) -> str:
    requested = str((params.get("stage") or [""])[0]).strip().lower()
    return requested if requested in {"worth", "enrich", "linkedin", "done"} else "worth"


def render_enrichment(enrichment: dict[str, Any], progress: dict[str, int],
                      *, worth_complete: bool = False) -> str:
    """Render one derived enrichment state (see derive_enrichment_state) as HTML.
    Purely presentational — the state rules live in the derive function alone."""
    state = str(enrichment.get("state") or STATE_FREE_PENDING)
    counts = enrichment.get("counts") if isinstance(enrichment.get("counts"), dict) else {}
    total = max(0, int(counts.get("total") or progress["lookup_ready"] or 0))
    completed = min(total, max(0, int(counts.get("completed") or 0)))
    percent = round((completed / total) * 100) if total else 0
    progress_bar = f"""
      <div class='enrich-progress' role='progressbar' aria-label='Contact enrichment progress'
           aria-valuemin='0' aria-valuemax='{total}' aria-valuenow='{completed}'>
        <div class='enrich-progress-fill' style='width:{percent}%'></div>
      </div>"""
    agent_handoff_bar = """
      <div class='enrich-progress is-indeterminate' role='progressbar'
           aria-label='Preparing enrichment'>
        <div class='enrich-progress-fill'></div>
      </div>"""
    if progress["worth_pending"] and not worth_complete:
        # Blocks only an UNCOMPLETED worth stage. Once worth was completed,
        # later machine maybes never block enrichment — they soft-surface in
        # the Review tab (feed-forward).
        return ("<div class='empty-state enrich-state'><div class='empty-mark'>1</div>"
                "<h2>Review in progress</h2>"
                f"<p>{progress['worth_pending']} decisions left · {progress['lookup_ready']} currently yes</p>"
                f"{progress_bar}</div>")
    if state == STATE_RUNNING:
        if enrichment.get("phase") == "judging_retargets":
            # Commit-scoped heartbeat from the judging pass: honest counts, not a
            # static screen (see reconcile_deep_research's manifest heartbeat).
            done = max(0, int(enrichment.get("done") or 0))
            judging = max(done, int(enrichment.get("total") or 0))
            return ("<div class='empty-state enrich-state'><div class='progress-spinner' aria-hidden='true'></div>"
                    "<h2>Judging found profiles</h2>"
                    f"<p>{done} of {judging} checked</p>{agent_handoff_bar}</div>")
        return ("<div class='empty-state enrich-state'><div class='progress-spinner' aria-hidden='true'></div>"
                "<h2>Enriching contacts</h2>"
                f"<p>{completed} of {total} complete</p>{progress_bar}</div>")
    if state == STATE_NEEDS_APPROVAL:
        if enrichment.get("approval_current"):
            new_count = max(0, int(enrichment.get("would_submit") or 0))
            return ("<div class='empty-state enrich-state'>"
                    "<h2>Preparing enrichment</h2>"
                    f"<p>Approval saved · starting {new_count} new lookup"
                    f"{'s' if new_count != 1 else ''}</p>{agent_handoff_bar}</div>")
        if not enrichment.get("approvable"):
            # Net-new exists but the receipt is stale; the free job is refreshing
            # the estimate ($0) — the Approve button binds to the fresh receipt.
            return ("<div class='empty-state enrich-state'>"
                    "<h2>Preparing enrichment</h2>"
                    f"<p>Estimating {enrichment.get('net_new') or 0} new lookups</p>"
                    f"{agent_handoff_bar}</div>")
        estimate = float(enrichment.get("estimated_usd") or 0)
        new_count = max(0, int(enrichment.get("would_submit") or 0))
        reused = max(0, int(enrichment.get("reused_completed") or 0))
        details = f"{new_count} new · {reused} reused · up to ${estimate:.2f}"
        return ("<div class='empty-state enrich-state'><div class='empty-mark'>2</div>"
                "<h2>Ready to enrich</h2>"
                f"<p>{details}</p>{progress_bar}"
                f"<button class='button button-primary' data-approve-enrichment>Approve ${estimate:.2f}</button></div>")
    if state == STATE_DONE:
        return ("<div class='empty-state enrich-state'><div class='empty-mark'>✓</div>"
                "<h2>Contacts enriched</h2>"
                f"<p>{completed} profiles ready</p>{progress_bar}"
                "<button class='button button-primary' data-complete='enrich'>Continue</button></div>")
    # free_pending: the render already started-or-joined the free job; show work.
    if enrichment.get("status") in {"failed", "completed_with_errors"}:
        failed = max(0, int(counts.get("failed") or 0))
        return ("<div class='empty-state enrich-state'><div class='empty-mark'>!</div>"
                "<h2>Enrichment paused</h2>"
                f"<p>{failed} failed · {completed} complete · reload to retry</p>"
                f"{progress_bar}</div>")
    return ("<div class='empty-state enrich-state'>"
            "<h2>Preparing enrichment</h2>"
            f"<p>Preparing {progress['lookup_ready']} approved "
            f"contact{'s' if progress['lookup_ready'] != 1 else ''}</p>"
            f"{agent_handoff_bar}</div>")


def _step(number: int, label: str, active: bool, complete: bool, count: int = 0,
          href: str = "") -> str:
    state = " active" if active else (" complete" if complete else "")
    marker = "✓" if complete else str(number)
    count_html = f"<small>{count} left</small>" if count and not complete else ""
    current = " aria-current='step'" if active else ""
    content = f"<span>{marker}</span><div>{esc(label)}{count_html}</div>"
    if href:
        return f"<a class='step{state}' href='{esc(href)}'{current}>{content}</a>"
    return f"<div class='step{state}'{current}>{content}</div>"


def _carousel_nav() -> str:
    """Debug-only Prev/Next arrows flanking the queue card (browse, no decisions)."""
    return ("<button class='carousel-nav carousel-prev' type='button' "
            "data-carousel='prev' aria-label='Previous card'>&#8249;</button>"
            "<button class='carousel-nav carousel-next' type='button' "
            "data-carousel='next' aria-label='Next card'>&#8250;</button>")


def worth_review_body(parents: list[dict[str, Any]], progress: dict[str, int],
                      parents_dir: Path, dossier_dir: Path, *,
                      debug: bool = False, index: int = 0,
                      profile_cache_dir: Path = PROFILE_CACHE_DIR,
                      exclude: frozenset[str] | None = None) -> str:
    """The Review tab's current item: the next queue card, or the stage-complete
    state. Shared by page_html and /api/worth-card so a decision click can swap
    in the next card client-side without a full page reload. ``debug`` adds the
    browse-only carousel; ``index`` picks a queue position (default unchanged).
    ``exclude`` skips worth keys whose decision POST is still in flight, so the
    client can prefetch the FOLLOWING card without waiting for the save."""
    queue = [parent for parent in parents if needs_worth_review(parent)]
    if exclude:
        queue = [parent for parent in queue
                 if str(_worth_key(parent) or "").strip().lower() not in exclude]
    if queue:
        queue.sort(key=lambda parent: str(parent.get("name") or "").lower())
        index = index % len(queue)
        card = render_worth_card(queue[index], parents_dir, dossier_dir, profile_cache_dir)
        if debug:
            return (f"<div class='carousel-shell' data-queue-index='{index}' "
                    f"data-queue-total='{len(queue)}'>{_carousel_nav()}{card}</div>")
        return card
    return ("<div class='empty-state phase-finish'><div class='empty-mark'>✓</div>"
            "<h2>Decisions ready</h2>"
            f"<p>{progress['lookup_ready']} people will be enriched</p>"
            "<button class='button button-primary' data-complete='worth'>Continue</button></div>")


def _enrichment_note(enrichment: dict[str, Any] | None) -> str:
    """Passive status line for the LinkedIn stage while enrichment is incomplete.

    Purely informational — it never obscures or disables the review cards, so
    the user can keep reviewing (or debugging) even if enrichment broke."""
    status = str((enrichment or {}).get("status") or "not_started")
    copy = {
        "failed": "Enrichment failed — you can still review what's here.",
        "running": "Enrichment is still running — more people may appear here.",
    }.get(status, "Enrichment hasn't finished — more people may appear here.")
    return f"<p class='enrichment-note' role='status'>{esc(copy)}</p>"


def linkedin_review_queue(
    parents: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Stable queue of ONE entry per parent that still needs an identity decision,
    carrying ALL of that parent's pending candidates.

    A merged parent with N pending candidates (e.g. two synthetic profiles for the
    same person) is ONE card offering N options — the queue and the header/tab counts
    are per-parent, never per-candidate ("show the parent, not the children")."""
    queue: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    ordered = sorted(parents, key=lambda parent: str(parent.get("name") or "").lower())
    for parent in ordered:
        pending = pending_linkedin_candidates(parent)
        if pending:
            queue.append((parent, pending))
    return queue


GO_BACK_HTML = (
    "<p class='handoff-note'>Review complete — go back to Codex.</p>"
    "<div class='handoff-copy'>"
    "<code>Review complete proceed with enrichment</code>"
    "<button class='button button-outline' type='button' data-copy-continue "
    "data-phrase='Review complete proceed with enrichment' "
    "data-toast='Copied'>Copy</button></div>")


def linkedin_finished_body(progress: dict[str, int], *, linkedin_complete: bool) -> str:
    tail = (GO_BACK_HTML if linkedin_complete else
            "<button class='button button-primary' data-complete='linkedin'>Finish</button>")
    return ("<div class='empty-state phase-finish'><div class='empty-mark'>✓</div>"
            "<h2>LinkedIn profiles checked</h2>"
            f"<p>{progress['linkedin_done']} decisions saved</p>"
            f"{tail}</div>")


def linkedin_card_body(parents: list[dict[str, Any]], progress: dict[str, int], *,
                       linkedin_complete: bool, parents_dir: Path, dossier_dir: Path,
                       profile_cache_dir: Path = PROFILE_CACHE_DIR,
                       exclude: frozenset[str] | None = None) -> str:
    """The LinkedIn queue's current item: the next pending parent's card, or the
    stage-finished state — the linkedin twin of ``worth_review_body``. Shared by
    page_html and /api/linkedin-card so a decision click swaps in the next card
    client-side. ``exclude`` skips PARENT SLUGS whose decision POST is still in
    flight (the linkedin queue is parent-keyed), so the client can prefetch the
    FOLLOWING card without waiting for the save."""
    queue = linkedin_review_queue(parents)
    if exclude:
        queue = [(parent, pending) for parent, pending in queue
                 if str(parent.get("slug") or "").strip().lower() not in exclude]
    if queue:
        parent, pending = queue[0]
        return render_linkedin_card(parent, pending, parents_dir, dossier_dir,
                                    profile_cache_dir)
    return linkedin_finished_body(progress, linkedin_complete=linkedin_complete)


def linkedin_review_body(parents: list[dict[str, Any]], progress: dict[str, int], *,
                         enrichment_complete: bool, linkedin_complete: bool,
                         parents_dir: Path, dossier_dir: Path,
                         enrichment: dict[str, Any] | None = None,
                         profile_cache_dir: Path = PROFILE_CACHE_DIR,
                         debug: bool = False, index: int = 0) -> str:
    """Render the LinkedIn stage: one pending parent card inside the swap panel
    (the worth pattern), or the debug carousel."""
    note = "" if enrichment_complete else _enrichment_note(enrichment)
    queue = linkedin_review_queue(parents)

    if debug and queue:
        index %= len(queue)
        parent, pending = queue[index]
        body = render_linkedin_card(
            parent, pending, parents_dir, dossier_dir, profile_cache_dir)
        return (
            f"<div class='linkedin-stage' data-queue-index='{index}' "
            f"data-queue-total='{len(queue)}'>{note}{_carousel_nav()}{body}</div>"
        )

    card = linkedin_card_body(
        parents, progress, linkedin_complete=linkedin_complete,
        parents_dir=parents_dir, dossier_dir=dossier_dir,
        profile_cache_dir=profile_cache_dir)
    body = f"<div class='linkedin-panel' data-linkedin-panel>{card}</div>"
    return f"<div class='linkedin-stage'>{note}{body}</div>"


def page_html(parents: list[dict[str, Any]], params: dict[str, list[str]],
              review_path: Path, *, parents_dir: Path = PARENTS_DIR,
              dossier_dir: Path = DOSSIER_DIR,
              manifest_path: Path = REVIEW_MANIFEST,
              enrichment_manifest_path: Path = ENRICH_MANIFEST,
              profile_cache_dir: Path = PROFILE_CACHE_DIR,
              verdicts_path: Path = VERDICTS_JSONL,
              facts_dir: Path = FACTS_DIR,
              enrichment_state: dict[str, Any] | None = None,
              job_running: bool = False) -> bytes:
    """Render the complete review page from packaged shell and dynamic fragments."""
    progress = review_progress(parents)
    selection = worth_selection_from_parents(parents, manifest_path=manifest_path)
    enrichment = read_enrichment_manifest(enrichment_manifest_path, selection=selection)
    review_manifest = read_review_manifest(manifest_path)
    state_token = review_state_token(progress, selection, enrichment, review_manifest,
                                     job_running=job_running)
    view = _phase_view(params, progress, manifest_path)
    preview = str((params.get("preview") or [""])[0]).strip() == "1"
    debug = str((params.get("debug") or [""])[0]).strip() == "1"
    worth_complete = phase_is_completed("worth", progress, manifest_path) or not progress["worth_total"]
    enrichment_complete = (enrichment.get("status") == STATUS_COMPLETED
                           and enrichment.get("current"))
    enrichment_continued = enrichment_handoff_completed(manifest_path)
    linkedin_complete = (phase_is_completed("linkedin", progress, manifest_path)
                         or (view == "done" and not progress["linkedin_total"]))
    external_updates = (
        view in {"enrich", "done"}
        or (view == "linkedin" and not enrichment_complete)
    )
    people_active = view == "worth"
    enrich_active = view == "enrich"
    linkedin_active = view in {"linkedin", "done"}

    if view == "worth":
        decision_view = str((params.get("view") or ["review"])[0]).strip().lower()
        decision_view = decision_view if decision_view in {"review", "yes", "no"} else "review"
        tabs = render_decision_tabs(progress, decision_view, preview=preview)
        search = ""
        if decision_view == "review":
            body = worth_review_body(parents, progress, parents_dir, dossier_dir,
                                     debug=debug, profile_cache_dir=profile_cache_dir)
            # The typeahead lives OUTSIDE the swap panel so card swaps never
            # touch it; its pending queue is embedded once at page render.
            pending = worth_pending_entries(parents)
            search = worth_search_html("review", pending) if pending else ""
        else:
            # Legacy ?page= URLs still land here; the infinite-scroll list always
            # starts from the top and streams the rest via /api/decision-rows.
            body = render_decision_table(
                parents, decision_view, parents_dir=parents_dir, dossier_dir=dossier_dir)
        content = f"<div class='worth-stage'>{tabs}{search}<div class='worth-panel'>{body}</div></div>"
    elif view == "enrich":
        # The enrich page renders the DERIVED state (never a trusted persisted
        # status); the HTTP handler pre-derives it so its render matches the
        # free-job trigger decision it just made.
        if enrichment_state is None:
            enrichment_state = derive_enrichment_state(
                selection, verdicts_path=verdicts_path, review_path=review_path,
                facts_dir=facts_dir, manifest_path=enrichment_manifest_path)
        content = render_enrichment(enrichment_state, progress,
                                    worth_complete=bool(worth_complete))
    elif view == "linkedin":
        content = linkedin_review_body(
            parents, progress, enrichment_complete=bool(enrichment_complete),
            linkedin_complete=bool(linkedin_complete),
            parents_dir=parents_dir, dossier_dir=dossier_dir, enrichment=enrichment,
            profile_cache_dir=profile_cache_dir, debug=debug)
    else:
        content = ("<div class='empty-state done'><div class='empty-mark'>✓</div><h2>All set</h2>"
                   f"<p>{progress['linkedin_done']} identities checked · {progress['rejected']} rejected</p>"
                   f"{GO_BACK_HTML}</div>")

    stepper = (_step(1, "Review Decisions", people_active, worth_complete, progress["worth_pending"],
                     "/?stage=worth&preview=1")
               + "<i class='step-line'></i>"
               + _step(2, "Enrich Contacts", enrich_active, enrichment_complete,
                       int((enrichment.get("counts") or {}).get("pending") or 0),
                       "/?stage=enrich&preview=1")
               + "<i class='step-line'></i>"
               + _step(3, "Check LinkedIn", linkedin_active,
                       enrichment_complete and enrichment_continued and linkedin_complete,
                       progress["linkedin_pending"], "/?stage=linkedin&preview=1")
               )
    title = {"worth": "Add People", "enrich": "Enrich Contacts",
             "linkedin": "Check LinkedIn", "done": "All Set"}.get(view, "Add People")
    replacements = {
        "{{TITLE}}": esc(title),
        "{{STAGE}}": esc(view),
        "{{PREVIEW}}": "true" if preview else "false",
        "{{EXTERNAL_UPDATES}}": "true" if external_updates else "false",
        "{{STATE_TOKEN}}": esc(state_token),
        "{{ENRICHMENT_STATUS}}": esc(
            "approved" if enrichment.get("approval_current") else enrichment.get("status")
        ),
        "{{STEPPER}}": stepper,
        "{{CONTENT}}": content,
    }
    document = REVIEW_HTML.read_text(encoding="utf-8")
    for placeholder, value in replacements.items():
        document = document.replace(placeholder, value)
    return document.encode("utf-8")
