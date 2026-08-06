"""Select bounded, signal-dense Gmail context from shared msgvault rows."""

import html
import re
from typing import Any, Iterable

from packs.ingestion.primitives.discover.gmail.msgvault import store as gni

DEFAULT_SNIPPET_CHARS = 200
DEFAULT_HEAD_CHARS = 300
DEFAULT_TAIL_CHARS = 300
FETCH_MULTIPLIER = 8
DEPTH_FETCH_MULTIPLIER = 3
NEARDUP_THRESHOLD = 0.6
_QUOTE_CUT = re.compile(
    r"(?im)^\s*(on .{0,120}wrote:|-+\s*original message\s*-+|-+\s*forwarded message\s*-+|"
    r"from:\s.+|sent from my .+|get outlook for .+)\s*$"
)


def clean_text(value: Any, limit: int | None = None) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if limit is not None and 0 < limit < len(text):
        text = text[:limit]
    return text


def clean_body(value: Any, head_chars: int, tail_chars: int) -> str:
    """Keep new-message substance and its signature, excluding quoted history."""
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


_SIGNAL_FEATURES = [
    (re.compile(r"\+?\d[\d().\-  ]{7,}\d"), 3),
    (re.compile(r"https?://|www\.|linkedin\.com/in/|github\.com/|[a-z0-9-]+\.(?:com|io|co|org|net)\b", re.I), 2),
    (re.compile(r"(?<![\w.])@[A-Za-z0-9_]{2,}"), 1),
    (re.compile(r"\b(?:co-?founder|founder|ceo|cto|coo|cfo|vp|head of|director|principal|"
                r"engineer|developer|manager|realtor|broker|partner|associate|analyst|"
                r"consultant|professor|lecturer|recruiter|designer|attorney|architect|scientist)\b", re.I), 2),
    (re.compile(r"\b(?:DRE|CalBRE|NMLS|License|Lic\.?)\s*#?\s*\d", re.I), 3),
    (re.compile(r"\b(?:at|@)\s+[A-Z][A-Za-z0-9&.\-]+(?:\s+[A-Z][A-Za-z0-9&.\-]+)*"), 1),
]


def signal_score(text: str) -> int:
    """Score signature/bio features plus a small length bonus."""
    text = text or ""
    score = sum(weight for pat, weight in _SIGNAL_FEATURES if pat.search(text))
    return score + min(len(text) // 200, 3)


def shingles(text: str, k: int = 3) -> frozenset[str]:
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    if len(tokens) < k:
        return frozenset(tokens)
    return frozenset(" ".join(tokens[i:i + k]) for i in range(len(tokens) - k + 1))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


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
    """Select contact/owner mail breadth-first, then depth, with near-dup removal."""
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
        rank = (signal_score(text), 1 if from_role == "contact" else 0, at)
        entry = {"at": at, "from": sender, "from_role": from_role, "subject": clean_text(row["subject"]), "snippet": text}
        cid = row["conversation_id"]
        key = ("thread", cid) if cid not in (None, "", "None") else ("msg", idx)
        by_thread.setdefault(key, []).append((rank, entry))
    for msgs in by_thread.values():
        msgs.sort(key=lambda re: re[0], reverse=True)
        if max_per_thread is not None:
            del msgs[max_per_thread:]
    leaders = sorted((msgs[0] for msgs in by_thread.values()), key=lambda re: re[0], reverse=True)
    rest = sorted((m for msgs in by_thread.values() for m in msgs[1:]), key=lambda re: re[0], reverse=True)
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
    """Fetch one contact through the shared store, then apply the selector."""
    mult = FETCH_MULTIPLIER if max_per_thread == 1 else DEPTH_FETCH_MULTIPLIER
    rows = store.fetch_recent_rows(email, per_person * mult)
    return select_emails_from_rows(
        rows, email, per_person, snippet_chars, accounts,
        source=source, head_chars=head_chars, tail_chars=tail_chars,
        max_per_thread=max_per_thread,
    )
