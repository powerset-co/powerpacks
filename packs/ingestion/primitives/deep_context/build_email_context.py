"""Build per-person email context from the local msgvault archive.

For every contact that the Gmail flow would send to Parallel for LinkedIn
resolution, pull their most recent email subjects + snippets straight from the
local msgvault SQLite store and write one combined, reviewable payload.

Why this exists
---------------
Today the Parallel lookup (``gmail/resolve_queue.py``) receives almost no
context per person -- just ``{full_name, company, email}`` where ``company`` is
merely guessed from the email domain (and blank for personal domains). That
thin signal is why a bare name can resolve to the wrong LinkedIn profile. This
primitive assembles the local-only context we *could* attach (what threads this
person actually appears in) so a human can review it BEFORE any LLM/Parallel
step is wired up.

Candidate set fidelity
----------------------
The candidate emails are re-derived exactly the way ``gmail/discover_engine``
builds its ``linkedin_resolution_queue``: aggregate msgvault metadata
(``MsgvaultStore.aggregate_contacts``), drop automated senders, keep only
round-trip contacts (both sent AND received), then take the same queue rows. We
reuse those helpers directly so this stays 1:1 with "the people we would send to
Parallel".

Division of labor
-----------------
All msgvault SQLite access (connection, schema, the recent-emails-with-bodies
SQL, the windowed all-contacts stream, owner/account derivation, honest counts)
lives on ``MsgvaultStore`` in ``gmail/msgvault_store.py``. This module keeps only
the non-DB logic: HTML/body cleaning, the deterministic identity-signal score,
near-dup shingle detection, the per-thread email SELECTION
(``select_emails_from_rows``), the ``recent_emails_for`` fetch+select wrapper,
the candidate-derivation orchestration, and the CSV/JSONL/manifest writing + CLI.

Privacy note (deliberate, local-only)
-------------------------------------
The standard Gmail import path is metadata-only -- it never reads subjects or
snippets. This primitive INTENTIONALLY reads ``messages.subject`` and
``messages.snippet`` (and, in body mode, ``message_bodies.body_text``) from the
local msgvault DB, purely to build a local review artifact. It performs NO
network calls, NO LLM calls, and sends nothing anywhere. Subjects/snippets/bodies
never leave the local machine via this primitive.

Outputs (one fixed directory, overwrite in place -- manifest + outputs only):
  <out-dir>/email_context.jsonl   one JSON record per person (full fidelity)
  <out-dir>/email_context.csv     flat, one row per person (easy spreadsheet review)
  <out-dir>/manifest.json         counts/status/timing

Changelog:
  2026-07-23 (audit batch 19): folded this module's second msgvault SQLite layer
    into ``MsgvaultStore`` (gmail/msgvault_store.py). The recent-emails SQL +
    ``fetch_recent_rows``, ``create_candidate_pid_table``,
    ``stream_contact_groups``, ``account_emails``, ``owner_identity``, and
    ``count_messages_for`` now live there as methods; this module does all DB
    work through a ``MsgvaultStore``. ``recent_emails_for`` and
    ``derive_candidates`` now take a store instead of a raw connection; the pure
    selection (``select_emails_from_rows``) and signal/near-dup helpers stay here.
  2026-07-23 (audit batch 17): the retired gmail/network_import.py split;
    ``gni`` now aliases the concrete ``gmail/msgvault_store`` module,
    linkedin_resolution_queue_rows comes from ``gmail/discover_engine``, and
    emit/now_iso/write_json come from the discover stage's ``common``.
"""
import argparse
import csv
import html
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterable

# Reuse the exact candidate-derivation + msgvault access from the Gmail import
# primitive, and the canonical role/service-address detector from the Parallel
# resolution path, so this stays faithful to "who we send to Parallel".
# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.discover_contacts_pipeline.common import (  # noqa: E402
    emit,
    now_iso,
    write_json,
)
from packs.ingestion.primitives.discover_contacts_pipeline.gmail import msgvault_store as gni  # noqa: E402
from packs.ingestion.primitives.discover_contacts_pipeline.gmail.discover_engine import (  # noqa: E402
    linkedin_resolution_queue_rows,
)
from packs.ingestion.primitives.discover_contacts_pipeline.gmail.resolve_queue import (  # noqa: E402
    is_generic_or_non_person,
)

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
    rows: Iterable[sqlite3.Row],
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


def derive_candidates(
    store: gni.MsgvaultStore,
    account_email: str,
    exclude_labels: Iterable[str] | None,
    include_automated: bool,
    include_role_mailboxes: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Re-derive the Parallel resolution queue exactly like gmail/discover_engine,
    then drop role/service mailboxes (support@, info@, careers@, …) using the same
    detector the Parallel resolution path uses. Returns (queue, role_dropped)."""
    aggregated = store.aggregate_contacts(account_email, exclude_labels)
    non_automated = [r for r in aggregated if include_automated or not r.get("automated_filtered")]
    filtered = [r for r in non_automated if gni.has_round_trip_interaction(r)]
    queue = linkedin_resolution_queue_rows(filtered)
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

    with gni.MsgvaultStore(db_path) as store:
        store.require_schema()
        exclude_labels = gni.default_excluded_labels(args.include_category_mail)
        queue, role_mailboxes_dropped = derive_candidates(
            store, args.account_email, exclude_labels, args.include_automated, args.include_role_mailboxes
        )
        if args.limit and args.limit > 0:
            queue = queue[: args.limit]

        accounts = store.account_emails()
        owner = store.owner_identity()

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
        store.create_candidate_pid_table(emails_in_order)
        fetch_limit = args.per_person * FETCH_MULTIPLIER
        recent_by_email: dict[str, list[dict[str, Any]]] = {}
        dropped_third_party = 0
        processed = 0
        for cemail, rows in store.stream_contact_groups(fetch_limit):
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
            # A domain heuristic on a free provider (gmail.com -> "Gmail") is noise,
            # not a real employer — only keep the guess for work domains.
            email_type = entry.get("primary_email_type", "")
            company_guess = entry.get("company_guess", "") if email_type == "work" else ""
            records.append({
                # what Parallel currently receives (thin context)
                "email": email,
                "full_name": entry.get("full_name", ""),
                "company_guess": company_guess,
                "primary_email_type": email_type,
                "total_messages": entry.get("total_messages", ""),
                "thread_count": entry.get("thread_count", ""),
                "last_interaction": entry.get("last_interaction", ""),
                # the new local context we could attach
                "recent_emails": recent,
            })

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
        "owner": owner,
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
        "updated_at": now_iso(),
        "privacy": {
            "reads_subjects_snippets": True,
            "network_called": False,
            "llm_called": False,
            "local_only": True,
        },
    }
    manifest_path = out_dir / "manifest.json"
    write_json(manifest_path, manifest)
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
    emit(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
