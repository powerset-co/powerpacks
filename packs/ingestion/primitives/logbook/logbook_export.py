"""$logbook orchestrator + CLI — raw verbatim message archive.

Subcommands (all local, no spend):

  check    --csv F            per-channel reachability + how deep each store goes
  estimate --csv F            cheap COUNT-only sizing + a wall-clock estimate
  deepen   --csv F [--run]    show / run the FREE local backfill syncs
  export   --csv F            full (re)build: stream every message -> markdown
  sync     --csv F            incremental + APPEND-ONLY (never overwrites)

Output (one fixed dir, gitignored):
  .powerpacks/logbook/<slug>/<channel>/<thread|dm|group>.md
  .powerpacks/logbook/index.md         catalog
  .powerpacks/logbook/manifest.json    counts + per-container stable-id watermarks

Memory: one ordered cursor per (entry, channel), iterated row-by-row; one output
file open at a time; only the current container's small stat buffer in RAM. Peak
RSS is bounded by the work, not the corpus.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterator

from packs.ingestion.primitives.deep_context import sources as dcs
from packs.ingestion.primitives.deep_context.common import Person, emit, now_iso, write_json
from packs.ingestion.primitives.logbook import logbook_sources as src
from packs.ingestion.primitives.logbook.logbook_common import (
    DEFAULT_CHAT_DB,
    DEFAULT_MSGVAULT_DB,
    DEFAULT_WACLI_DB,
    INDEX_MD,
    LOGBOOK_ROOT,
    MANIFEST_JSON,
    GroupTarget,
    group_slug,
    load_people_from_csv,
)

# Rough throughput constants (msgs/sec) for the time estimate — gmail reads big
# bodies, chat stores are tiny one-liners. Measured ballpark on Apple silicon.
GMAIL_MSGS_PER_SEC = 3000
CHAT_MSGS_PER_SEC = 8000

CHANNEL_DIR = {"gmail": "gmail", "imessage": "imessage", "whatsapp": "whatsapp"}


# --- filename / slug helpers ------------------------------------------------

def _subject_slug(subject: str) -> str:
    base = re.sub(r"^\s*(re|fwd?|fw)\s*:\s*", "", (subject or "").strip(), flags=re.I)
    base = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    return (base[:60].strip("-")) or "no-subject"


def _short(value: str) -> str:
    return hashlib.sha1((value or "").encode("utf-8")).hexdigest()[:6]


def _thread_filename(channel: str, first_row: dict[str, Any]) -> str:
    year = first_row.get("year") or "undated"
    return f"{CHANNEL_DIR[channel]}/{year}-{_subject_slug(first_row.get('subject', ''))}-{_short(first_row['container_id'])}.md"


def _container_filename(channel: str, kind: str, first_row: dict[str, Any]) -> str:
    if kind == "thread":
        return _thread_filename(channel, first_row)
    if kind == "group":
        return f"{CHANNEL_DIR[channel]}/group.md"
    return f"{CHANNEL_DIR[channel]}/dm.md"


def _fmt_time(at: str) -> str:
    return (at[:16].replace("T", " ")) if at else "unknown-date"


def _format_message(row: dict[str, Any]) -> str:
    sender = row.get("sender") or "unknown"
    header = f"**{_fmt_time(row.get('at', ''))} · {sender}:**"
    text = (row.get("text") or "").strip()
    if "\n" in text or len(text) > 180:
        return f"{header}\n\n{text}\n"
    return f"{header} {text}\n"


# --- the streaming markdown writer ------------------------------------------

class EntryWriter:
    """Routes a per-(entry, channel) row stream into one file per container.

    On ``export`` it overwrites; on ``sync`` it appends to existing container
    files (resumed via ``prior``: (channel, container_id) -> {rel_path, last_year})
    and only creates a file for genuinely new containers. New messages are always
    chronologically later than what's stored, so appending is correct.

    The open-file identity is keyed by ``(channel, container_id)``, NOT container_id
    alone: the iMessage DM and the WhatsApp DM both use container_id "dm", so keying
    on container_id alone makes the WhatsApp DM stream append into the still-open
    iMessage dm.md instead of opening its own whatsapp/dm.md.
    """

    def __init__(self, entry_slug: str, *, append: bool, prior: dict[str, Any]):
        self.entry_slug = entry_slug
        self.append = append
        self.prior = prior or {}
        self.dir = LOGBOOK_ROOT / entry_slug
        self.containers: dict[str, dict[str, Any]] = {}
        self._fh = None
        self._cur_key: tuple[str, str] | None = None
        self._cur_meta: dict[str, Any] | None = None
        self._last_year: int | None = None

    def _open_container(self, row: dict[str, Any]) -> None:
        cid = row["container_id"]
        channel, kind = row["channel"], row["kind"]
        key = (channel, cid)
        resumed = self.prior.get(key)
        if resumed and self.append:
            rel_path = resumed["rel_path"]
            path = LOGBOOK_ROOT / rel_path
            new_file = not path.exists()
            self._last_year = resumed.get("last_year")
        else:
            rel_path = f"{self.entry_slug}/{_container_filename(channel, kind, row)}"
            path = LOGBOOK_ROOT / rel_path
            new_file = True
            self._last_year = None
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if (self.append and not new_file) else "w"
        self._fh = path.open(mode, encoding="utf-8")
        if mode == "w":
            self._write_frontmatter(row, channel, kind)
            self._last_year = None
        self._cur_key = key
        self._cur_meta = {
            "container_id": cid,
            "container_title": row.get("container_title") or "",
            "channel": channel,
            "kind": kind,
            "rel_path": rel_path,
            "messages": (resumed.get("messages", 0) if (resumed and self.append) else 0),
            "first_at": (resumed.get("first_at") if (resumed and self.append) else row.get("at")),
            "last_at": row.get("at"),
            "watermark": (resumed.get("watermark", 0) if (resumed and self.append) else 0),
        }

    def _write_frontmatter(self, row: dict[str, Any], channel: str, kind: str) -> None:
        title = (row.get("container_title") or row.get("subject") or "").strip()
        heading = title or ("Direct messages" if kind == "dm" else "Conversation")
        fm = [
            "---",
            f"entry: {self.entry_slug}",
            f"channel: {channel}",
            f"kind: {kind}",
            f'container_id: "{row.get("container_id")}"',
            f'title: "{title.replace(chr(34), chr(39))}"',
            f"created_at: {now_iso()}",
            "---",
            "",
            f"# {heading}",
            "",
        ]
        self._fh.write("\n".join(fm))

    def write(self, row: dict[str, Any]) -> None:
        if (row["channel"], row["container_id"]) != self._cur_key:
            self._finalize()
            self._open_container(row)
        year = row.get("year")
        if year is not None and year != self._last_year:
            self._fh.write(f"\n## {year}\n\n")
            self._last_year = year
        self._fh.write(_format_message(row))
        meta = self._cur_meta
        meta["messages"] += 1
        meta["last_at"] = row.get("at") or meta["last_at"]
        if not meta.get("first_at"):
            meta["first_at"] = row.get("at")
        wm = int(row.get("watermark") or 0)
        if wm > meta["watermark"]:
            meta["watermark"] = wm

    def _finalize(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        if self._cur_meta is not None:
            meta = dict(self._cur_meta)
            meta["last_year"] = self._last_year
            self.containers[meta["rel_path"]] = meta
            self._cur_meta = None
            self._cur_key = None

    def close(self) -> dict[str, dict[str, Any]]:
        self._finalize()
        return self.containers


def _drain(writer: EntryWriter, stream: Iterator[dict[str, Any]]) -> int:
    n = 0
    for row in stream:
        writer.write(row)
        n += 1
    return n


# --- store openers / readiness ---------------------------------------------

def _store_depth(channel: str, db: Path) -> dict[str, Any]:
    info: dict[str, Any] = {"channel": channel, "path": str(db), "exists": db.exists()}
    if not db.exists():
        info["status"] = "missing"
        return info
    try:
        if channel == "imessage":
            probe = dcs.probe_chat_db(db)
            info["status"] = "ok" if probe["readable"] else "unreadable_full_disk_access"
            info["messages"] = probe.get("messages")
            if probe["readable"]:
                con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
                try:
                    raw = con.execute("SELECT MIN(date), MAX(date) FROM message").fetchone()
                    info["earliest"] = dcs.bec_apple_iso(raw[0]) if raw and raw[0] else None
                    info["latest"] = dcs.bec_apple_iso(raw[1]) if raw and raw[1] else None
                finally:
                    con.close()
            return info
        uri = f"file:{db}?mode=ro" + ("&immutable=1" if channel == "imessage" else "")
        con = sqlite3.connect(uri, uri=True)
        try:
            if channel == "gmail":
                row = con.execute("SELECT COUNT(*), MIN(COALESCE(sent_at,received_at,internal_date)), MAX(COALESCE(sent_at,received_at,internal_date)) FROM messages WHERE message_type='email'").fetchone()
                info["messages"], info["earliest"], info["latest"] = int(row[0] or 0), row[1], row[2]
                info["accounts"] = [r[0] for r in con.execute("SELECT identifier FROM sources WHERE source_type='gmail'")]
            else:  # whatsapp
                row = con.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM messages").fetchone()
                info["messages"] = int(row[0] or 0)
                info["earliest"] = src._whatsapp_iso(row[1])
                info["latest"] = src._whatsapp_iso(row[2])
            info["status"] = "ok"
        finally:
            con.close()
    except sqlite3.Error as exc:
        info["status"] = f"error: {type(exc).__name__}"
    return info


def _paths(args: argparse.Namespace) -> dict[str, Path]:
    return {
        "gmail": Path(args.msgvault_db).expanduser(),
        "imessage": Path(args.chat_db).expanduser(),
        "whatsapp": Path(args.wacli_db).expanduser(),
    }


def _channels(args: argparse.Namespace) -> list[str]:
    return [c.strip() for c in str(args.channels).split(",") if c.strip() in CHANNEL_DIR]


# --- subcommands ------------------------------------------------------------

def cmd_check(args: argparse.Namespace) -> dict[str, Any]:
    paths = _paths(args)
    channels = _channels(args)
    depth = {ch: _store_depth(ch, paths[ch]) for ch in channels}
    people, groups = load_people_from_csv(Path(args.csv), limit=args.limit, slug=args.slug)
    ready = all(depth[ch].get("status") == "ok" for ch in channels if ch != "imessage") and \
        (depth.get("imessage", {}).get("status") in (None, "ok") or "imessage" not in channels)
    return {
        "command": "check", "csv": str(args.csv), "people": len(people), "groups": len(groups),
        "channels": channels, "store_depth": depth, "ready": ready, "generated_at": now_iso(),
    }


def cmd_estimate(args: argparse.Namespace) -> dict[str, Any]:
    paths = _paths(args)
    channels = _channels(args)
    people, groups = load_people_from_csv(Path(args.csv), limit=args.limit, slug=args.slug)
    gmail_con = src.open_msgvault(paths["gmail"]) if "gmail" in channels and paths["gmail"].exists() else None
    totals = {"messages": 0, "threads": 0, "containers": 0}
    per_person: list[dict[str, Any]] = []
    try:
        for person in people:
            row: dict[str, Any] = {"slug": person.slug, "name": person.full_name}
            if gmail_con is not None:
                m, t = src.count_gmail(person, gmail_con)
                row["gmail_messages"], row["gmail_threads"] = m, t
                totals["messages"] += m
                totals["threads"] += t
                totals["containers"] += t
            if "imessage" in channels:
                m, c = src.count_imessage_dm(person, paths["imessage"])
                row["imessage_messages"] = m
                totals["messages"] += m
                totals["containers"] += c
            if "whatsapp" in channels:
                m, c = src.count_whatsapp_dm(person, paths["whatsapp"])
                row["whatsapp_messages"] = m
                totals["messages"] += m
                totals["containers"] += c
            per_person.append(row)
    finally:
        if gmail_con is not None:
            gmail_con.close()
    gmail_msgs = sum(r.get("gmail_messages", 0) for r in per_person)
    chat_msgs = totals["messages"] - gmail_msgs
    seconds = gmail_msgs / GMAIL_MSGS_PER_SEC + chat_msgs / CHAT_MSGS_PER_SEC
    return {
        "command": "estimate", "csv": str(args.csv), "channels": channels,
        "people": len(people), "named_groups": len(groups), "totals": totals,
        "estimated_seconds": round(seconds, 1), "estimated_minutes": round(seconds / 60, 2),
        "note": "COUNT-only, no body reads, no spend", "per_person": per_person,
        "generated_at": now_iso(),
    }


def cmd_deepen(args: argparse.Namespace) -> dict[str, Any]:
    paths = _paths(args)
    channels = _channels(args)
    depth = {ch: _store_depth(ch, paths[ch]) for ch in channels}
    people, group_targets = load_people_from_csv(Path(args.csv), limit=args.limit, slug=args.slug)
    cmds: list[str] = []
    caveats: list[str] = []
    wa_jids: list[str] = []

    # --- Gmail: AUTH all accounts up front, THEN deep-sync (one login pass). ----
    # msgvault has no standalone auth command — OAuth fires lazily on sync when a
    # token is expired. Running a fast, ~0-message sync per account FIRST front-loads
    # every Google login so the user clicks through them once at the start, instead of
    # being interrupted mid-run per account (what the unscoped flow did).
    gmail_accounts = depth.get("gmail", {}).get("accounts", []) if "gmail" in channels else []
    if "gmail" in channels:
        today = date.today().isoformat()
        if gmail_accounts:
            for acct in gmail_accounts:
                cmds.append(f"msgvault sync-full {acct} --after {today} --noresume   # PHASE 1 auth/refresh token (fast, ~0 msgs)")
            for acct in gmail_accounts:
                cmds.append(f"uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/gmail.py discover --account-email {acct} --fresh   # PHASE 2 deep sync")
            caveats.append(
                f"Gmail auth-first: {len(gmail_accounts)} account(s) ({', '.join(gmail_accounts)}). "
                "Approve each Google login window in PHASE 1 (they appear up front), then PHASE 2 "
                "syncing runs unattended. Note: msgvault applies the account's linked category filter "
                "(promotions/social/forums/updates are excluded by design)."
            )
        else:
            cmds.append("uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/gmail.py discover --account-email <account-email> --fresh")

    # --- WhatsApp: refresh + scoped backfill (after gmail auth). ----------------
    if "whatsapp" in channels:
        store = paths["whatsapp"].parent
        # wacli `sync` only refreshes the group LIST + pulls recent/live messages going
        # forward (--max-messages is a DB-size cap, not a depth knob). OLDER history is
        # requested with `history fill`/`backfill` (on-demand sync from your PRIMARY PHONE).
        # We SCOPE the backfill to just the chats that matter (the CSV people's DMs +
        # their groups) via --chat <jid>, instead of the user's entire WhatsApp.
        cmds.append(f"wacli --store {store} sync --once --refresh-contacts --refresh-groups")
        names = [g.name for g in group_targets if g.channel == "whatsapp"]
        for person in people:
            wa_jids.extend(src.whatsapp_target_jids(paths["whatsapp"], person, names))
        wa_jids = list(dict.fromkeys(wa_jids))  # dedupe, preserve order
        if wa_jids:
            chat_flags = " ".join(f"--chat {j}" for j in wa_jids)
            for r in range(max(1, args.rounds)):
                cmds.append(f"wacli --store {store} history fill {chat_flags}   # round {r + 1}/{args.rounds} (re-run digs deeper)")
        else:
            cmds.append(f"wacli --store {store} history fill   # no target chats resolved locally → whole-store backfill")
            caveats.append("No CSV-people WhatsApp chats found locally to scope backfill — "
                           "either these people aren't in your WhatsApp, or run a plain `sync` first.")
        caveats.append(
            "WhatsApp is the shallowest channel: `sync` is recent-forward only; scoped "
            "`history fill --chat <jid>` backfills just the target conversations, but "
            "on-demand sync only serves what your primary phone still has — full history "
            "is not guaranteed. Inspect with `wacli --store " + str(store) + " history coverage`."
        )
    # iMessage chat.db is already complete locally — nothing to deepen.
    ran: list[dict[str, Any]] = []
    if args.run:
        for cmd in cmds:
            bare = cmd.split("#", 1)[0].strip()
            proc = subprocess.run(bare, shell=True, capture_output=True, text=True)
            ran.append({"cmd": bare, "returncode": proc.returncode, "stderr_tail": (proc.stderr or "")[-400:]})
    return {
        "command": "deepen", "channels": channels, "current_depth": depth,
        "whatsapp_target_chats": wa_jids, "rounds": args.rounds,
        "recommended_commands": cmds, "ran": ran if args.run else None,
        "caveats": caveats,
        "note": "FREE syncs (no per-message cost). WhatsApp backfill is SCOPED to the CSV "
                "people's chats and connects to your phone live. Re-run estimate after.",
        "generated_at": now_iso(),
    }


def _run_build(args: argparse.Namespace, *, append: bool) -> dict[str, Any]:
    paths = _paths(args)
    channels = _channels(args)
    include_groups = not getattr(args, "no_groups", False)
    people, group_targets = load_people_from_csv(Path(args.csv), limit=args.limit, slug=args.slug)

    prior_manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8")) if (append and MANIFEST_JSON.exists()) else {}
    prior_entries = prior_manifest.get("entries", {}) if isinstance(prior_manifest, dict) else {}
    entries: dict[str, dict[str, Any]] = dict(prior_entries) if append else {}

    gmail_con = src.open_msgvault(paths["gmail"]) if "gmail" in channels and paths["gmail"].exists() else None
    t0 = time.time()
    total_msgs = 0
    try:
        # 1) People: gmail threads + imessage/whatsapp DMs under the person slug.
        for person in people:
            prior_e = prior_entries.get(person.slug, {})
            wm = prior_e.get("watermark", {}) if append else {}
            containers_prior = prior_e.get("containers", {}) if append else {}
            prior_by_cid = {(c["channel"], c["container_id"]): c for c in containers_prior.values()}
            if not append:
                _clear_entry(person.slug, channels)
            writer = EntryWriter(person.slug, append=append, prior=prior_by_cid)
            if gmail_con is not None:
                total_msgs += _drain(writer, src.stream_gmail(person, gmail_con, since_id=wm.get("gmail", 0)))
            if "imessage" in channels:
                total_msgs += _drain(writer, src.stream_imessage_dm(person, paths["imessage"], since_rowid=wm.get("imessage", 0)))
            if "whatsapp" in channels:
                total_msgs += _drain(writer, src.stream_whatsapp_dm(person, paths["whatsapp"], since_rowid=wm.get("whatsapp", 0)))
            containers = writer.close()
            _record_entry(entries, person.slug, person.full_name, "person", containers, append)

        # 2) Groups: each named/discovered group is its own top-level slug.
        for gjid, gtitle, channel, gslug in _resolve_group_entries(group_targets, people, paths, channels, include_groups):
            prior_e = prior_entries.get(gslug, {})
            wm = prior_e.get("watermark", {}) if append else {}
            prior_by_cid = {(c["channel"], c["container_id"]): c for c in (prior_e.get("containers", {}) if append else {}).values()}
            if not append:
                _clear_entry(gslug, [channel])
            writer = EntryWriter(gslug, append=append, prior=prior_by_cid)
            since = wm.get(channel, 0)
            if channel == "whatsapp":
                total_msgs += _drain(writer, src.stream_whatsapp_group(paths["whatsapp"], gjid, gtitle, since_rowid=since))
            else:
                total_msgs += _drain(writer, src.stream_imessage_group(paths["imessage"], gjid[0], gtitle, gjid[1], since_rowid=since))
            containers = writer.close()
            _record_entry(entries, gslug, gtitle, "group", containers, append)
    finally:
        if gmail_con is not None:
            gmail_con.close()

    manifest = _write_manifest(args, channels, entries, total_msgs, round(time.time() - t0, 1), append)
    _write_index(entries)
    return manifest


def _clear_entry(slug: str, channels: list[str]) -> None:
    for ch in channels:
        d = LOGBOOK_ROOT / slug / CHANNEL_DIR[ch]
        if d.exists():
            shutil.rmtree(d)


def _record_entry(entries: dict[str, Any], slug: str, name: str, kind: str,
                  containers: dict[str, dict[str, Any]], append: bool) -> None:
    if not containers and not (append and slug in entries):
        return
    existing = entries.get(slug, {}) if append else {}
    merged = dict(existing.get("containers", {}))
    merged.update(containers)
    watermark: dict[str, int] = dict(existing.get("watermark", {}))
    msgs = 0
    for meta in merged.values():
        msgs += int(meta.get("messages", 0))
        ch = meta.get("channel")
        if ch:
            watermark[ch] = max(watermark.get(ch, 0), int(meta.get("watermark", 0)))
    entries[slug] = {
        "slug": slug, "name": name, "kind": kind,
        "messages": msgs, "files": len(merged),
        "watermark": watermark, "containers": merged,
    }


def _resolve_group_entries(group_targets: list[GroupTarget], people: list[Person],
                           paths: dict[str, Path], channels: list[str], include_groups: bool):
    """Yield (jid_or_(rowid,guid), title, channel, slug) for every group entry to build."""
    seen_ids: set[str] = set()    # dedupe by container id (jid/guid), across all passes
    seen_slugs: set[str] = set()  # disambiguate only genuinely-distinct groups w/ same name

    def _emit(container_key: str, target, title: str, channel: str):
        if container_key in seen_ids:
            return None  # same group already emitted (e.g. CSV-named AND membership)
        base = group_slug(title)
        gslug = base if base not in seen_slugs else f"{base}-{_short(container_key)}"
        seen_ids.add(container_key)
        seen_slugs.add(gslug)
        return (target, title, channel, gslug)

    # CSV-named WhatsApp groups are extracted by default (user listed them explicitly).
    if "whatsapp" in channels:
        names = [g.name for g in group_targets if g.channel == "whatsapp"]
        for g in src.resolve_whatsapp_groups(paths["whatsapp"], names):
            row = _emit(g["jid"], g["jid"], g["title"], "whatsapp")
            if row:
                yield row
    if not include_groups:
        return

    # Name unnamed iMessage groups by their participants (resolved across stores).
    name_map = src.build_imessage_name_map(paths["whatsapp"], paths["gmail"], people) if "imessage" in channels else {}
    # --include-groups: archive every group each target person participates in,
    # SYMMETRICALLY across both chat channels (membership-based).
    for person in people:
        if "whatsapp" in channels:
            for g in src.resolve_whatsapp_groups(paths["whatsapp"], [], person=person):
                row = _emit(g["jid"], g["jid"], g["title"], "whatsapp")
                if row:
                    yield row
        if "imessage" in channels:
            for g in src.resolve_imessage_groups(person, paths["imessage"], name_map=name_map):
                row = _emit(str(g["guid"]), (g["chat_rowid"], g["guid"]), g["title"], "imessage")
                if row:
                    yield row


def _write_manifest(args, channels, entries, total_msgs, seconds, append) -> dict[str, Any]:
    total_files = sum(e.get("files", 0) for e in entries.values())
    manifest = {
        "source": "logbook", "status": "completed", "mode": "sync" if append else "export",
        "input_csv": str(args.csv), "channels": channels,
        "privacy": {"reads_bodies": True, "persists_verbatim": True,
                    "scope": "Gmail threads + iMessage/WhatsApp DMs + named/membership groups"},
        "totals": {"entries": len(entries), "files": total_files, "messages_written": total_msgs},
        "elapsed_seconds": seconds, "entries": entries, "generated_at": now_iso(),
    }
    write_json(MANIFEST_JSON, manifest)
    return {k: v for k, v in manifest.items() if k != "entries"} | {"entries_count": len(entries)}


def _write_index(entries: dict[str, Any]) -> None:
    lines = ["# Logbook index", "", f"_Generated {now_iso()}_", "",
             "| Entry | Kind | Files | Messages | Channels |", "|---|---|---|---|---|"]
    for slug in sorted(entries):
        e = entries[slug]
        chans = sorted({m.get("channel") for m in e.get("containers", {}).values() if m.get("channel")})
        lines.append(f"| [{e.get('name') or slug}]({slug}/) | {e.get('kind')} | {e.get('files',0)} | {e.get('messages',0)} | {', '.join(chans)} |")
    INDEX_MD.parent.mkdir(parents=True, exist_ok=True)
    INDEX_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_export(args: argparse.Namespace) -> dict[str, Any]:
    return _run_build(args, append=False)


def cmd_sync(args: argparse.Namespace) -> dict[str, Any]:
    return _run_build(args, append=True)


# --- CLI --------------------------------------------------------------------

def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--csv", required=True, help="people CSV (founder shape or merged people.csv)")
    p.add_argument("--channels", default="gmail,imessage,whatsapp")
    p.add_argument("--msgvault-db", default=DEFAULT_MSGVAULT_DB)
    p.add_argument("--chat-db", default=DEFAULT_CHAT_DB)
    p.add_argument("--wacli-db", default=DEFAULT_WACLI_DB)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--slug", default="", help="restrict to one person/group slug")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="$logbook — raw verbatim message archive")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "estimate", "deepen", "export", "sync"):
        p = sub.add_parser(name)
        _add_common(p)
        if name in ("export", "sync"):
            p.add_argument("--no-groups", action="store_true", help="skip group chats (default: include every group a target is in)")
        if name == "deepen":
            p.add_argument("--run", action="store_true", help="actually run the free local syncs (default: print only)")
            p.add_argument("--rounds", type=int, default=1, help="WhatsApp history fill rounds per chat (each digs deeper)")
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], dict[str, Any]] = {
        "check": cmd_check, "estimate": cmd_estimate, "deepen": cmd_deepen,
        "export": cmd_export, "sync": cmd_sync,
    }[args.command]
    emit(handler(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
