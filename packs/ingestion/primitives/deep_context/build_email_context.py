"""Select bounded, signal-dense Gmail context from ``MsgvaultStore`` rows.

The Deep Context collector uses this local-only selector to keep conversation
breadth, useful signature/bio evidence, and non-duplicated thread depth.  All
msgvault SQLite access stays in ``discover/gmail/msgvault/store.py``; this file
contains only text cleanup, ranking, deduplication, and the one fetch wrapper.
"""

import html
import re
import sys
from pathlib import Path
from typing import Any, Iterable

# Reuse the exact candidate-derivation + msgvault access from the Gmail import
# primitive, and the canonical role/service-address detector from the Parallel
# resolution path, so this stays faithful to "who we send to Parallel".
# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.discover.gmail.msgvault import store as gni  # noqa: E402
DEFAULT_SNIPPET_CHARS = 200
# In --source body mode we keep the head + tail of the cleaned body: the head
# carries the substance ("I'm a founder of…"), the tail carries the signature /
# footer (title, company, phone, links). Bodies are read locally from
# message_bodies; the quoted reply chain is stripped first so the tail is the
# contact's own signature, not the bottom of a forwarded thread.
DEFAULT_HEAD_CHARS = 300
DEFAULT_TAIL_CHARS = 300
# Fetch this many recent candidate messages before sender-filtering, so we still
# end up with per_person after dropping third-party-sent threads.
FETCH_MULTIPLIER = 8
# Depth mode (max_per_thread != 1) keeps multiple messages per thread, so a smaller
# over-fetch already fills the budget — keeps the raw row pull sane when per_person
# is large (the 1600-message Gmail vertical would otherwise fetch 12.8k rows).
DEPTH_FETCH_MULTIPLIER = 3

# Markers of a quoted reply / forwarded history block. We cut the body at the first
# such marker so we keep only the new message text (and signature), not the thread.
_QUOTE_CUT = re.compile(
    r"(?im)^\s*(on .{0,120}wrote:|-+\s*original message\s*-+|-+\s*forwarded message\s*-+|"
    r"from:\s.+|sent from my .+|get outlook for .+)\s*$"
)


def clean_text(value: Any, limit: int | None = None) -> str:
    """Unescape HTML entities, collapse whitespace, optionally truncate."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if limit is not None and limit > 0 and len(text) > limit:
        text = text[:limit]
    return text


def clean_body(value: Any, head_chars: int, tail_chars: int) -> str:
    """Strip the quoted reply/forward chain, then keep the head + tail of the
    contact's own message: head = substance, tail = signature/footer."""
    text = html.unescape(str(value or ""))
    cut = _QUOTE_CUT.search(text)
    if cut:
        text = text[: cut.start()]
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    text = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if not text:
        return ""
    if len(text) <= head_chars + tail_chars:
        return text
    return f"{text[:head_chars].strip()} … {text[-tail_chars:].strip()}"


# Deterministic "this message carries identity signal" features — the stuff a
# signature block / intro bio contains. Used to pick the best email per thread
# (no LLM): a message with a phone + title + license outscores a "thanks!".
_SIGNAL_FEATURES = [
    (re.compile(r"\+?\d[\d().\-  ]{7,}\d"), 3),                                  # phone number
    (re.compile(r"https?://|www\.|linkedin\.com/in/|github\.com/|[a-z0-9-]+\.(?:com|io|co|org|net)\b", re.I), 2),  # url / domain
    (re.compile(r"(?<![\w.])@[A-Za-z0-9_]{2,}"), 1),                                 # social handle
    (re.compile(r"\b(?:co-?founder|founder|ceo|cto|coo|cfo|vp|head of|director|principal|"
                r"engineer|developer|manager|realtor|broker|partner|associate|analyst|"
                r"consultant|professor|lecturer|recruiter|designer|attorney|architect|scientist)\b", re.I), 2),  # title
    (re.compile(r"\b(?:DRE|CalBRE|NMLS|License|Lic\.?)\s*#?\s*\d", re.I), 3),         # license / id
    (re.compile(r"\b(?:at|@)\s+[A-Z][A-Za-z0-9&.\-]+(?:\s+[A-Z][A-Za-z0-9&.\-]+)*"), 1),  # "… at SomeCompany"
]


def signal_score(text: str) -> int:
    """Deterministic identity-signal score for a message body/snippet (no LLM).

    Rewards signature/bio features (phone, url, title, license, company) plus a
    small length bonus (intros/signatures are longer than one-liners)."""
    text = text or ""
    score = sum(weight for pat, weight in _SIGNAL_FEATURES if pat.search(text))
    return score + min(len(text) // 200, 3)


# Near-duplicate threshold: two emails whose word-shingle sets overlap at least
# this much are treated as the same content (boilerplate, repeated chat blurbs,
# the same quoted thread). Greedy filtering keeps the higher-signal one.
NEARDUP_THRESHOLD = 0.6


def shingles(text: str, k: int = 3) -> frozenset[str]:
    """Word k-shingle set for Jaccard near-dup detection (exact MinHash)."""
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    if len(tokens) < k:
        return frozenset(tokens)
    return frozenset(" ".join(tokens[i:i + k]) for i in range(len(tokens) - k + 1))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def select_emails_from_rows(
    rows: Iterable[Any],
    email: str,
    per_person: int,
    snippet_chars: int,
    accounts: set[str],
    source: str = "snippet",
    head_chars: int = DEFAULT_HEAD_CHARS,
    tail_chars: int = DEFAULT_TAIL_CHARS,
    max_per_thread: int | None = 1,
) -> tuple[list[dict[str, Any]], int]:
    """Pick a contact's context emails from already-fetched recent ``rows``.

    Shared by both fetch paths (per-contact ``recent_emails_for`` and the
    all-contacts ``MsgvaultStore.stream_contact_groups``) so selection semantics
    are identical.

    Only keep messages whose sender is the contact themselves (``from_role`` =
    "contact" -> their own words) or the account owner (``from_role`` = "me" ->
    my words directed at them). Messages sent by a third party where the contact
    is merely a co-recipient are dropped: that content belongs to the sender, not
    the contact, and attributing it would contaminate their markers.

    ``source`` = "snippet" uses Gmail's ~200-char snippet; "body" reads the full
    local body and keeps its head + tail (substance + signature), falling back to
    the snippet when no body is stored.

    ``max_per_thread`` controls thread depth. The default ``1`` keeps the old
    behavior exactly — one signal-densest message per thread — so the marker-review
    build path is unchanged. ``None`` (or >1) keeps the back-and-forth: deep-context
    passes ``None`` so synthesis sees the whole conversation, not one line of it.

    Selection is signal-dense, not just most-recent: within each thread messages are
    ranked by ``signal_score`` (signature / bio features), tie-broken toward the
    contact's own email then recency, and truncated to ``max_per_thread``. We then
    fill the ``per_person`` message budget BREADTH-first (every thread's best message,
    threads ordered by that best signal) and only then DEPTH (each thread's remaining
    messages) — so coverage across conversations degrades gracefully before any one
    thread is allowed to contribute extra messages.
    """
    dropped = 0
    by_thread: dict[Any, list[tuple[Any, dict[str, Any]]]] = {}
    for idx, row in enumerate(rows):
        sender = str(row["sender_email"] or "").strip()
        if sender == email:
            from_role = "contact"
        elif sender and sender in accounts:
            from_role = "me"
        else:
            dropped += 1
            continue
        if source == "body":
            text = clean_body(row["body_text"], head_chars, tail_chars) or clean_text(row["snippet"], snippet_chars)
        else:
            text = clean_text(row["snippet"], snippet_chars)
        at = str(row["at"] or "").strip()
        # Rank within a thread: most identity signal, then the contact's own email,
        # then most recent (ISO `at` sorts chronologically).
        rank = (signal_score(text), 1 if from_role == "contact" else 0, at)
        entry = {"at": at, "from": sender, "from_role": from_role, "subject": clean_text(row["subject"]), "snippet": text}
        cid = row["conversation_id"]
        key = ("thread", cid) if cid not in (None, "", "None") else ("msg", idx)
        by_thread.setdefault(key, []).append((rank, entry))
    # Within each thread: signal-densest first; keep up to max_per_thread (None = all).
    # Stable sort + ">" replacement parity: on a rank tie the first-seen message wins,
    # matching the old single-rep behavior when max_per_thread == 1.
    for msgs in by_thread.values():
        msgs.sort(key=lambda re: re[0], reverse=True)
        if max_per_thread is not None:
            del msgs[max_per_thread:]
    # Breadth pass = each thread's best message (threads ordered by that best rank);
    # depth pass = the remaining per-thread messages, globally rank-ordered. With
    # max_per_thread == 1 the depth pass is empty, so this is identical to before.
    leaders = sorted((msgs[0] for msgs in by_thread.values()), key=lambda re: re[0], reverse=True)
    rest = sorted((m for msgs in by_thread.values() for m in msgs[1:]), key=lambda re: re[0], reverse=True)
    # Then greedily drop near-duplicate content (boilerplate / repeated chat blurbs /
    # same quoted thread) so the slots are genuinely distinct.
    kept: list[dict[str, Any]] = []
    kept_shingles: list[frozenset[str]] = []
    for _, entry in leaders + rest:
        if len(kept) >= per_person:
            break
        sh = shingles(entry["snippet"])
        if any(jaccard(sh, prev) >= NEARDUP_THRESHOLD for prev in kept_shingles):
            continue
        kept.append(entry)
        kept_shingles.append(sh)
    return kept, dropped


def recent_emails_for(
    store: gni.MsgvaultStore,
    email: str,
    per_person: int,
    snippet_chars: int,
    accounts: set[str],
    source: str = "snippet",
    head_chars: int = DEFAULT_HEAD_CHARS,
    tail_chars: int = DEFAULT_TAIL_CHARS,
    max_per_thread: int | None = 1,
) -> tuple[list[dict[str, Any]], int]:
    """Per-contact wrapper: fetch this contact's recent rows via ``store`` then
    select from them.

    The all-contacts build path uses ``store.stream_contact_groups`` +
    ``select_emails_from_rows`` directly; this wrapper preserves the single-contact
    API used by callers and tests."""
    # Breadth mode (max_per_thread == 1) over-fetches 8x so we still see many distinct
    # threads after dropping third-party-sent ones. Depth mode keeps multiple messages
    # per thread, so a smaller multiple already fills the budget — avoid pulling
    # per_person*8 rows when per_person is large (e.g. the 1600 Gmail vertical).
    mult = FETCH_MULTIPLIER if max_per_thread == 1 else DEPTH_FETCH_MULTIPLIER
    rows = store.fetch_recent_rows(email, per_person * mult)
    return select_emails_from_rows(
        rows, email, per_person, snippet_chars, accounts,
        source=source, head_chars=head_chars, tail_chars=tail_chars,
        max_per_thread=max_per_thread,
    )
