"""Build per-person email context from the local msgvault archive.

For every contact that the Gmail flow would send to Parallel for LinkedIn
resolution, pull their most recent email subjects + snippets straight from the
local msgvault SQLite store and write one combined, reviewable payload.

Why this exists
---------------
Today the Parallel lookup (``resolve_linkedin_queue.py``) receives almost no
context per person -- just ``{full_name, company, email}`` where ``company`` is
merely guessed from the email domain (and blank for personal domains). That
thin signal is why a bare name can resolve to the wrong LinkedIn profile. This
primitive assembles the local-only context we *could* attach (what threads this
person actually appears in) so a human can review it BEFORE any LLM/Parallel
step is wired up.

Candidate set fidelity
----------------------
The candidate emails are re-derived exactly the way ``gmail_network_import``
builds its ``linkedin_resolution_queue``: aggregate msgvault metadata, drop
automated senders, keep only round-trip contacts (both sent AND received), then
take the same queue rows. We reuse those helpers directly so this stays 1:1
with "the people we would send to Parallel".

Privacy note (deliberate, local-only)
-------------------------------------
The standard Gmail import path is metadata-only -- it never reads subjects or
snippets. This primitive INTENTIONALLY reads ``messages.subject`` and
``messages.snippet`` from the local msgvault DB, purely to build a local review
artifact. It performs NO network calls, NO LLM calls, and sends nothing
anywhere. Subjects/snippets never leave the local machine via this primitive.

Outputs (one fixed directory, overwrite in place -- manifest + outputs only):
  <out-dir>/email_context.jsonl   one JSON record per person (full fidelity)
  <out-dir>/email_context.csv     flat, one row per person (easy spreadsheet review)
  <out-dir>/manifest.json         counts/status/timing
"""
import argparse
import csv
import html
import itertools
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

# Reuse the exact candidate-derivation + msgvault helpers from the Gmail import
# primitive, and the canonical role/service-address detector from the Parallel
# resolution path, so this stays faithful to "who we send to Parallel".
_PRIMITIVES_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PRIMITIVES_DIR / "gmail_network_import"))
sys.path.insert(0, str(_PRIMITIVES_DIR / "resolve_linkedin_queue"))

import gmail_network_import as gni  # noqa: E402
from resolve_linkedin_queue import is_generic_or_non_person  # noqa: E402

DEFAULT_OUT_DIR = Path(".powerpacks/network-import/discover/email-context")
# Emails read per contact (most recent, sender = contact or you). More = richer
# identity signal at a small linear cost; tunable via --per-person (e.g. 50).
DEFAULT_PER_PERSON = 20
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

# Markers of a quoted reply / forwarded history block. We cut the body at the first
# such marker so we keep only the new message text (and signature), not the thread.
_QUOTE_CUT = re.compile(
    r"(?im)^\s*(on .{0,120}wrote:|-+\s*original message\s*-+|-+\s*forwarded message\s*-+|"
    r"from:\s.+|sent from my .+|get outlook for .+)\s*$"
)

# Resolve a contact's participant id(s) up front so the message lookups can use
# the sender_id / message_recipients.participant_id indexes directly. The old
# `sender = ? OR EXISTS(recipients…)` shape made SQLite scan every email per
# contact (idx_messages_type) and run a correlated subquery per row — minutes
# per all-contact run on a large archive. Credit: Jake Zeller diagnosed this.
PARTICIPANT_IDS_SQL = "SELECT id FROM participants WHERE LOWER(email_address) = ?"

_RECENT_EMAILS_SELECT = """
SELECT
    COALESCE(m.sent_at, m.received_at, m.internal_date) AS at,
    m.conversation_id,
    LOWER(sp.email_address) AS sender_email,
    m.subject,
    m.snippet,
    mb.body_text
"""
# Emails the contact SENT — index-direct on messages.sender_id.
RECENT_EMAILS_FROM_SENDER_SQL = _RECENT_EMAILS_SELECT + """
FROM messages m
LEFT JOIN participants sp ON sp.id = m.sender_id
LEFT JOIN message_bodies mb ON mb.message_id = m.id
WHERE m.message_type = 'email'
  AND (m.deleted_at IS NULL OR m.deleted_at = '')
  AND (m.deleted_from_source_at IS NULL OR m.deleted_from_source_at = '')
  AND m.sender_id = ?1
ORDER BY at DESC
LIMIT ?2
"""
# Emails the contact RECEIVED — index-direct on message_recipients.participant_id.
RECENT_EMAILS_TO_RECIPIENT_SQL = _RECENT_EMAILS_SELECT + """
FROM message_recipients mr
JOIN messages m ON m.id = mr.message_id
LEFT JOIN participants sp ON sp.id = m.sender_id
LEFT JOIN message_bodies mb ON mb.message_id = m.id
WHERE m.message_type = 'email'
  AND (m.deleted_at IS NULL OR m.deleted_at = '')
  AND (m.deleted_from_source_at IS NULL OR m.deleted_from_source_at = '')
  AND mr.participant_id = ?1
ORDER BY at DESC
LIMIT ?2
"""


def fetch_recent_rows(con: sqlite3.Connection, email: str, fetch_limit: int) -> list[sqlite3.Row]:
    """Recent sender+recipient messages for a contact, via participant-id indexes.

    Per-contact path, kept for ``recent_emails_for`` and the unit tests. The
    all-contacts build path uses ``stream_contact_groups`` (one windowed query)
    instead — see below.
    """
    ids = [r["id"] for r in con.execute(PARTICIPANT_IDS_SQL, (email.lower(),)).fetchall()]
    if not ids:
        return []
    rows: list[sqlite3.Row] = []
    for pid in ids:
        rows.extend(con.execute(RECENT_EMAILS_FROM_SENDER_SQL, (pid, fetch_limit)).fetchall())
        rows.extend(con.execute(RECENT_EMAILS_TO_RECIPIENT_SQL, (pid, fetch_limit)).fetchall())
    rows.sort(key=lambda row: str(row["at"] or ""), reverse=True)
    return rows[:fetch_limit]


# --- All-contacts fast path: one windowed query, streamed contact-by-contact ---
#
# The per-contact loop above runs ~2 indexed queries per contact. On a large
# archive that per-contact overhead dominates wall-clock. Instead we resolve all
# candidate emails to participant ids once (a temp `cand_pid` table), then run a
# SINGLE windowed query that takes each contact's most-recent `fetch_limit`
# messages via ROW_NUMBER() OVER (PARTITION BY contact ORDER BY at DESC).
#
# Memory stays low two ways:
#   1. The window/sort carries only lightweight columns (subject + snippet, no
#      body blobs); `body_text` is LEFT JOINed only AFTER the rn<=K filter, so
#      bodies are read for the kept rows only — not for every message in the sort.
#   2. The result is ORDER BY contact, so we stream the cursor with
#      itertools.groupby and hold just one contact's ~fetch_limit rows at a time.
def create_candidate_pid_table(con: sqlite3.Connection, emails: Iterable[str]) -> int:
    """(Re)build temp tables mapping each candidate email -> participant id(s).

    One O(participants) scan up front, instead of a per-contact id lookup. Returns
    the number of (email, pid) rows mapped."""
    con.execute("DROP TABLE IF EXISTS cand_pid")
    con.execute("DROP TABLE IF EXISTS cand_email")
    con.execute("CREATE TEMP TABLE cand_email(email TEXT PRIMARY KEY)")
    con.executemany(
        "INSERT OR IGNORE INTO cand_email(email) VALUES (?)",
        [(e.strip().lower(),) for e in emails if e and e.strip()],
    )
    con.execute(
        """
        CREATE TEMP TABLE cand_pid AS
        SELECT ce.email AS cemail, p.id AS pid
        FROM cand_email ce
        JOIN participants p ON LOWER(p.email_address) = ce.email
        """
    )
    con.execute("CREATE INDEX cand_pid_pid ON cand_pid(pid)")
    return con.execute("SELECT COUNT(*) AS n FROM cand_pid").fetchone()["n"]


# Window on lightweight columns only (subject/snippet); join body_text AFTER the
# rn<=K filter so the big blobs are read for kept rows only. UNION (not UNION ALL)
# dedupes a message that matches a contact on both the sender and recipient side.
WINDOWED_CONTEXT_SQL = """
WITH assoc AS (
    SELECT cp.cemail AS cemail, m.id AS mid,
           COALESCE(m.sent_at, m.received_at, m.internal_date) AS at,
           m.conversation_id AS conversation_id, m.sender_id AS sender_id,
           m.subject AS subject, m.snippet AS snippet
    FROM cand_pid cp
    JOIN messages m ON m.sender_id = cp.pid
    WHERE m.message_type = 'email'
      AND (m.deleted_at IS NULL OR m.deleted_at = '')
      AND (m.deleted_from_source_at IS NULL OR m.deleted_from_source_at = '')
    UNION
    SELECT cp.cemail, m.id,
           COALESCE(m.sent_at, m.received_at, m.internal_date),
           m.conversation_id, m.sender_id, m.subject, m.snippet
    FROM cand_pid cp
    JOIN message_recipients mr ON mr.participant_id = cp.pid
    JOIN messages m ON m.id = mr.message_id
    WHERE m.message_type = 'email'
      AND (m.deleted_at IS NULL OR m.deleted_at = '')
      AND (m.deleted_from_source_at IS NULL OR m.deleted_from_source_at = '')
),
ranked AS (
    SELECT assoc.*,
           ROW_NUMBER() OVER (PARTITION BY cemail ORDER BY at DESC, mid DESC) AS rn
    FROM assoc
)
SELECT r.cemail AS cemail,
       r.at AS at,
       r.conversation_id AS conversation_id,
       LOWER(sp.email_address) AS sender_email,
       r.subject AS subject,
       r.snippet AS snippet,
       mb.body_text AS body_text
FROM ranked r
LEFT JOIN participants sp ON sp.id = r.sender_id
LEFT JOIN message_bodies mb ON mb.message_id = r.mid
WHERE r.rn <= ?
ORDER BY r.cemail, r.at DESC, r.mid DESC
"""


def stream_contact_groups(
    con: sqlite3.Connection, fetch_limit: int
) -> Iterator[tuple[str, list[sqlite3.Row]]]:
    """Yield ``(contact_email, recent_rows)`` from the windowed query, one contact
    at a time. Requires ``create_candidate_pid_table`` to have run first. Only one
    contact's rows are materialized at a time, so memory stays bounded."""
    cur = con.execute(WINDOWED_CONTEXT_SQL, (fetch_limit,))
    for cemail, group in itertools.groupby(cur, key=lambda r: r["cemail"]):
        yield cemail, list(group)


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
    (re.compile(r"\+?\d[\d().\-  ]{7,}\d"), 3),                                  # phone number
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


def account_emails(con: sqlite3.Connection) -> set[str]:
    """Lowercased synced account addresses, used to infer message direction.

    msgvault's Gmail sync leaves ``is_from_me`` = 0 for every row, so direction
    is derived from whether the sender is one of the synced accounts instead.
    """
    rows = con.execute("SELECT LOWER(identifier) AS ident FROM sources").fetchall()
    return {str(r["ident"]).strip() for r in rows if str(r["ident"] or "").strip()}


def select_emails_from_rows(
    rows: Iterable[sqlite3.Row],
    email: str,
    per_person: int,
    snippet_chars: int,
    accounts: set[str],
    source: str = "snippet",
    head_chars: int = DEFAULT_HEAD_CHARS,
    tail_chars: int = DEFAULT_TAIL_CHARS,
) -> tuple[list[dict[str, Any]], int]:
    """Pick a contact's context emails from already-fetched recent ``rows``.

    Shared by both fetch paths (per-contact ``recent_emails_for`` and the
    all-contacts ``stream_contact_groups``) so selection semantics are identical.

    Only keep messages whose sender is the contact themselves (``from_role`` =
    "contact" -> their own words) or the account owner (``from_role`` = "me" ->
    my words directed at them). Messages sent by a third party where the contact
    is merely a co-recipient are dropped: that content belongs to the sender, not
    the contact, and attributing it would contaminate their markers.

    ``source`` = "snippet" uses Gmail's ~200-char snippet; "body" reads the full
    local body and keeps its head + tail (substance + signature), falling back to
    the snippet when no body is stored.

    Selection is signal-dense, not just most-recent: one email per thread
    (deduped by ``conversation_id``) so the slots are distinct conversations, and
    within each thread we keep the message with the highest deterministic
    ``signal_score`` (signature / bio features), tie-broken toward the contact's
    own email then recency. Threads are then ordered by that signal so the
    richest emails fill the ``per_person`` slots.
    """
    dropped = 0
    by_thread: dict[Any, tuple[Any, dict[str, Any]]] = {}
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
        cur = by_thread.get(key)
        if cur is None or rank > cur[0]:
            by_thread[key] = (rank, entry)
    # Signal-densest threads first, then greedily drop near-duplicate content
    # (boilerplate / repeated chat blurbs / same quoted thread) so the slots are
    # genuinely distinct — keeps the higher-signal of any near-dup pair.
    ranked = sorted(by_thread.values(), key=lambda kv: kv[0], reverse=True)
    kept: list[dict[str, Any]] = []
    kept_shingles: list[frozenset[str]] = []
    for _, entry in ranked:
        if len(kept) >= per_person:
            break
        sh = shingles(entry["snippet"])
        if any(jaccard(sh, prev) >= NEARDUP_THRESHOLD for prev in kept_shingles):
            continue
        kept.append(entry)
        kept_shingles.append(sh)
    return kept, dropped


def recent_emails_for(
    con: sqlite3.Connection,
    email: str,
    per_person: int,
    snippet_chars: int,
    accounts: set[str],
    source: str = "snippet",
    head_chars: int = DEFAULT_HEAD_CHARS,
    tail_chars: int = DEFAULT_TAIL_CHARS,
) -> tuple[list[dict[str, Any]], int]:
    """Per-contact wrapper: fetch this contact's recent rows then select from them.

    The all-contacts build path uses ``stream_contact_groups`` +
    ``select_emails_from_rows`` directly; this wrapper preserves the single-contact
    API used by callers and tests."""
    rows = fetch_recent_rows(con, email, per_person * FETCH_MULTIPLIER)
    return select_emails_from_rows(
        rows, email, per_person, snippet_chars, accounts,
        source=source, head_chars=head_chars, tail_chars=tail_chars,
    )


def derive_candidates(
    con: sqlite3.Connection,
    account_email: str,
    exclude_labels: Iterable[str] | None,
    include_automated: bool,
    include_role_mailboxes: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Re-derive the Parallel resolution queue exactly like gmail_network_import,
    then drop role/service mailboxes (support@, info@, careers@, …) using the same
    detector the Parallel resolution path uses. Returns (queue, role_dropped)."""
    aggregated = gni.aggregate_msgvault_contacts(con, account_email, exclude_labels)
    non_automated = [r for r in aggregated if include_automated or not r.get("automated_filtered")]
    filtered = [r for r in non_automated if gni.has_round_trip_interaction(r)]
    queue = gni.linkedin_resolution_queue_rows(filtered)
    if include_role_mailboxes:
        return queue, 0
    kept = []
    role_dropped = 0
    for q in queue:
        email = str(q.get("primary_email") or q.get("handle") or "")
        if is_generic_or_non_person(email):
            role_dropped += 1
        else:
            kept.append(q)
    return kept, role_dropped


def format_email_cell(entry: dict[str, Any]) -> str:
    """One recent email as a single compact, human-scannable cell."""
    date = (entry.get("at") or "")[:10]
    subject = entry.get("subject") or "(no subject)"
    snippet = entry.get("snippet") or ""
    who = "from:them" if entry.get("from_role") == "contact" else "from:me"
    head = f"[{who} {date}] {subject}".strip()
    return f"{head} :: {snippet}" if snippet else head


def write_review_csv(records: list[dict[str, Any]], out_dir: Path, per_person: int) -> Path:
    """Flat one-row-per-person CSV: thin Parallel context + recent emails as msg1..msgN."""
    msg_cols = [f"msg{i}" for i in range(1, per_person + 1)]
    header = [
        "email", "full_name", "company_guess", "primary_email_type",
        "total_messages", "thread_count", "last_interaction", "recent_count",
    ] + msg_cols
    csv_path = out_dir / "email_context.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()
        for rec in records:
            recent = rec.get("recent_emails") or []
            row = {
                "email": rec.get("email", ""),
                "full_name": rec.get("full_name", ""),
                "company_guess": rec.get("company_guess", ""),
                "primary_email_type": rec.get("primary_email_type", ""),
                "total_messages": rec.get("total_messages", ""),
                "thread_count": rec.get("thread_count", ""),
                "last_interaction": rec.get("last_interaction", ""),
                "recent_count": len(recent),
            }
            for i, col in enumerate(msg_cols):
                row[col] = format_email_cell(recent[i]) if i < len(recent) else ""
            writer.writerow(row)
    return csv_path


def build_context(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    db_path = Path(args.msgvault_db).expanduser()
    out_dir = Path(args.out_dir)

    con = gni.connect_msgvault(db_path)
    try:
        gni.require_msgvault_schema(con)
        con.row_factory = sqlite3.Row
        exclude_labels = gni.default_excluded_labels(args.include_category_mail)
        queue, role_mailboxes_dropped = derive_candidates(
            con, args.account_email, exclude_labels, args.include_automated, args.include_role_mailboxes
        )
        if args.limit and args.limit > 0:
            queue = queue[: args.limit]

        accounts = account_emails(con)

        # Candidate emails in queue order (deduped), so output ordering is stable.
        emails_in_order: list[str] = []
        entry_by_email: dict[str, dict[str, Any]] = {}
        for entry in queue:
            email = str(entry.get("primary_email") or entry.get("handle") or "").strip().lower()
            if not email or email in entry_by_email:
                continue
            emails_in_order.append(email)
            entry_by_email[email] = entry

        total = len(emails_in_order)
        print(f"[build_email_context] building context for {total} contacts…", file=sys.stderr, flush=True)

        # One windowed query over all contacts; stream it contact-by-contact so
        # only one contact's rows are in memory at a time.
        create_candidate_pid_table(con, emails_in_order)
        fetch_limit = args.per_person * FETCH_MULTIPLIER
        recent_by_email: dict[str, list[dict[str, Any]]] = {}
        dropped_third_party = 0
        processed = 0
        for cemail, rows in stream_contact_groups(con, fetch_limit):
            if cemail not in entry_by_email:
                continue
            recent, dropped = select_emails_from_rows(
                rows, cemail, args.per_person, args.snippet_chars, accounts,
                source=args.source, head_chars=args.head_chars, tail_chars=args.tail_chars,
            )
            recent_by_email[cemail] = recent
            dropped_third_party += dropped
            processed += 1
            if processed % 50 == 0:
                print(f"[build_email_context] {processed}/{total} contacts processed", file=sys.stderr, flush=True)
        print(f"[build_email_context] {total}/{total} contacts processed", file=sys.stderr, flush=True)

        records: list[dict[str, Any]] = []
        with_context = 0
        for email in emails_in_order:
            entry = entry_by_email[email]
            recent = recent_by_email.get(email, [])
            if recent:
                with_context += 1
            records.append({
                # what Parallel currently receives (thin context)
                "email": email,
                "full_name": entry.get("full_name", ""),
                "company_guess": entry.get("company_guess", ""),
                "primary_email_type": entry.get("primary_email_type", ""),
                "total_messages": entry.get("total_messages", ""),
                "thread_count": entry.get("thread_count", ""),
                "last_interaction": entry.get("last_interaction", ""),
                # the new local context we could attach
                "recent_emails": recent,
            })
    finally:
        con.close()

    out_dir.mkdir(parents=True, exist_ok=True)
    payload_path = out_dir / "email_context.jsonl"
    with payload_path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    csv_path = write_review_csv(records, out_dir, args.per_person)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    manifest = {
        "source": "build_email_context",
        "status": "completed",
        "msgvault_db": str(db_path),
        "account_email": args.account_email or "(all)",
        "per_person": args.per_person,
        "source": args.source,
        "snippet_chars": args.snippet_chars,
        "head_chars": args.head_chars,
        "tail_chars": args.tail_chars,
        "include_automated": bool(args.include_automated),
        "include_category_mail": bool(args.include_category_mail),
        "role_mailboxes_dropped": role_mailboxes_dropped,
        "people_total": len(records),
        "people_with_context": with_context,
        "people_without_context": len(records) - with_context,
        "third_party_messages_dropped": dropped_third_party,
        "sender_filter": "contact_or_account_owner",
        "output": str(payload_path),
        "output_csv": str(csv_path),
        "elapsed_ms": elapsed_ms,
        "updated_at": gni.now_iso(),
        "privacy": {
            "reads_subjects_snippets": True,
            "network_called": False,
            "llm_called": False,
            "local_only": True,
        },
    }
    manifest_path = out_dir / "manifest.json"
    gni.write_json(manifest_path, manifest)
    manifest["manifest"] = str(manifest_path)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build per-person email context from local msgvault (read-only).")
    parser.add_argument("--msgvault-db", default=str(gni.DEFAULT_MSGVAULT_DB), help="Path to msgvault.db")
    parser.add_argument("--account-email", default="", help="Limit to one synced Gmail account (default: all)")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Output directory")
    parser.add_argument("--per-person", type=int, default=DEFAULT_PER_PERSON, help="Recent emails per person")
    parser.add_argument("--source", choices=["snippet", "body"], default="body", help="Use local full body (head+tail) or Gmail snippet")
    parser.add_argument("--snippet-chars", type=int, default=DEFAULT_SNIPPET_CHARS, help="Max snippet characters (snippet mode)")
    parser.add_argument("--head-chars", type=int, default=DEFAULT_HEAD_CHARS, help="Body head chars kept (body mode)")
    parser.add_argument("--tail-chars", type=int, default=DEFAULT_TAIL_CHARS, help="Body tail chars kept (body mode)")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of people (0 = all)")
    parser.add_argument("--include-automated", action="store_true", help="Include automated/no-reply senders")
    parser.add_argument("--include-category-mail", action="store_true", help="Include CATEGORY_* labelled mail")
    parser.add_argument("--include-role-mailboxes", action="store_true", help="Include role/service addresses (support@, info@, …)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = build_context(args)
    gni.emit(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
