"""$logbook orchestrator + CLI — raw verbatim message archive.

Subcommands (all local, no spend):

  check    --csv F            per-channel reachability + how deep each store goes
  estimate --csv F            cheap COUNT-only sizing + a wall-clock estimate
  deepen   --csv F [--run]    show / run the FREE local backfill syncs
  export   --csv F            full (re)build: stream every message -> markdown
  sync     --csv F            incremental + APPEND-ONLY (never overwrites)

``build_logbook(people, ...)`` is the typed entry both CLI builds and the People
page call: it builds only the given entries, keeps every other entry in the
manifest and index, moves a rebuilt entry's prior files to ``<slug>.bkup-<utc>/``,
and reports each channel as ok / missing / unreadable. Export builds each entry
in ``.building-<utc>/`` and swaps it in only when its streams finished, so a
failing reader leaves that entry and the catalog as they were.

``sync`` appends past each channel's watermark, which is store insertion order,
not message time: history backfilled after a sync lands at the end of the file
out of date order. ``export`` rebuilds in date order.

Output (one fixed dir, gitignored):
  .powerpacks/logbook/<slug>/<channel>/<thread|dm|group>.md
  .powerpacks/logbook/index.md         catalog
  .powerpacks/logbook/manifest.json    counts + per-container stable-id watermarks

Memory: one output file open at a time. Gmail streams one message at a time;
iMessage and WhatsApp hold one conversation's rows while writing it, so peak
memory follows the largest single conversation, not the whole corpus.

Changelog:
  2026-09-30: export stages each entry and swaps it in after its streams finish;
  the catalog is written even when a build fails; person slugs of the batch are
  reserved from group slugs; an unreadable chat.db reports ``unreadable``.
  2026-09-30: ``build_logbook`` typed entry. Export keeps unrelated catalog entries
  (it used to reset the manifest), sets prior files aside instead of deleting them,
  leaves missing/unreadable channels untouched, and keeps each group chat's slug
  across builds. CLI export/sync print the build result.
  2026-07-23 (audit dedup): now_iso, write_json import from common.jsonio instead of deep_context.shared.common (deduped there); no behavior change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sqlite3
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterator

from packs.ingestion.primitives.common.person import Person
from packs.ingestion.primitives.discover.messages import chatdb
from packs.ingestion.primitives.common.jsonio import now_iso, write_json
from packs.ingestion.primitives.discover.messages.wacli import store_db as wacli_store
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


def _cmd(argv: list[str], comment: str = "") -> str:
    text = shlex.join([str(part) for part in argv])
    return f"{text}   # {comment}" if comment else text


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

    On ``export`` it writes fresh files; on ``sync`` it appends to existing container
    files (resumed via ``prior``: (channel, container_id) -> {rel_path, last_year})
    and only creates a file for genuinely new containers. Appended rows are newer
    by store id, which is usually but not always newer by date (see module doc).

    The open-file identity is keyed by ``(channel, container_id)``, NOT container_id
    alone: the iMessage DM and the WhatsApp DM both use container_id "dm", so keying
    on container_id alone makes the WhatsApp DM stream append into the still-open
    iMessage dm.md instead of opening its own whatsapp/dm.md.
    """

    def __init__(self, root: Path, entry_slug: str, *, append: bool, prior: dict[str, Any]):
        self.root = root
        self.entry_slug = entry_slug
        self.append = append
        self.prior = prior or {}
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
            path = self.root / rel_path
            new_file = not path.exists()
            self._last_year = resumed.get("last_year")
        else:
            rel_path = f"{self.entry_slug}/{_container_filename(channel, kind, row)}"
            path = self.root / rel_path
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


def _drain(writer: EntryWriter, stream: Iterator[dict[str, Any]], written: Counter[str]) -> None:
    for row in stream:
        writer.write(row)
        written[row["channel"]] += 1


# --- the typed build result ---------------------------------------------------


class ChannelStatus(StrEnum):
    """Whether a build could read a channel's local store."""

    OK = "ok"
    MISSING = "missing"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class ChannelCoverage:
    """One channel of a build: how far its store reaches and what the build wrote from it."""

    channel: str
    status: ChannelStatus
    messages: int
    earliest: str | None
    latest: str | None


@dataclass(frozen=True)
class LogbookBuild:
    """What one build wrote: the entries it built, their totals, and per-channel coverage."""

    root: Path
    entries: tuple[str, ...]
    messages: int
    files: int
    channels: tuple[ChannelCoverage, ...]
    elapsed_seconds: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "entries": list(self.entries),
            "messages": self.messages,
            "files": self.files,
            "channels": [
                {
                    "channel": row.channel,
                    "status": row.status.value,
                    "messages": row.messages,
                    "earliest": row.earliest,
                    "latest": row.latest,
                }
                for row in self.channels
            ],
            "elapsed_seconds": self.elapsed_seconds,
        }


# --- store openers / readiness ---------------------------------------------


def emit(payload: dict[str, Any]) -> None:
    """One compact JSON line on stdout: the manifest the skill reads back."""
    print(json.dumps(payload, ensure_ascii=False))


def _store_depth(channel: str, db: Path) -> dict[str, Any]:
    info: dict[str, Any] = {"channel": channel, "path": str(db), "exists": db.exists()}
    if not db.exists():
        info["status"] = "missing"
        return info
    try:
        if channel == "imessage":
            probe = chatdb.probe_message_counts(db)
            # Unreadable is no Full Disk Access OR a corrupt / foreign file; the probe can't tell.
            info["status"] = "ok" if probe["readable"] else "unreadable"
            info["messages"] = probe["messages"]
            if probe["readable"]:
                con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
                try:
                    raw = con.execute("SELECT MIN(date), MAX(date) FROM message").fetchone()
                    info["earliest"] = (chatdb.apple_timestamp_to_iso(raw[0]) or "") if raw and raw[0] else None
                    info["latest"] = (chatdb.apple_timestamp_to_iso(raw[1]) or "") if raw and raw[1] else None
                finally:
                    con.close()
            return info
        uri = f"file:{db}?mode=ro" + ("&immutable=1" if channel == "imessage" else "")
        con = sqlite3.connect(uri, uri=True)
        try:
            if channel == "gmail":
                row = con.execute(
                    "SELECT COUNT(*), MIN(COALESCE(sent_at,received_at,internal_date)), MAX(COALESCE(sent_at,received_at,internal_date)) FROM messages WHERE message_type='email'"
                ).fetchone()
                info["messages"], info["earliest"], info["latest"] = int(row[0] or 0), row[1], row[2]
                info["accounts"] = [
                    r[0] for r in con.execute("SELECT identifier FROM sources WHERE source_type='gmail'")
                ]
            else:  # whatsapp
                row = con.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM messages").fetchone()
                info["messages"] = int(row[0] or 0)
                info["earliest"] = wacli_store.whatsapp_epoch_to_iso(row[1])
                info["latest"] = wacli_store.whatsapp_epoch_to_iso(row[2])
            info["status"] = "ok"
        finally:
            con.close()
    except sqlite3.Error as exc:
        info["status"] = f"error: {type(exc).__name__}"
    return info


def _channel_status(depth: dict[str, Any]) -> ChannelStatus:
    if not depth["exists"]:
        return ChannelStatus.MISSING
    return ChannelStatus.OK if depth["status"] == "ok" else ChannelStatus.UNREADABLE


def default_paths() -> dict[str, Path]:
    """The local message stores Logbook reads, at their default locations."""
    return {
        "gmail": Path(DEFAULT_MSGVAULT_DB).expanduser(),
        "imessage": Path(DEFAULT_CHAT_DB).expanduser(),
        "whatsapp": Path(DEFAULT_WACLI_DB).expanduser(),
    }


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
    ready = all(depth[ch].get("status") == "ok" for ch in channels if ch != "imessage") and (
        depth.get("imessage", {}).get("status") in (None, "ok") or "imessage" not in channels
    )
    return {
        "command": "check",
        "csv": str(args.csv),
        "people": len(people),
        "groups": len(groups),
        "channels": channels,
        "store_depth": depth,
        "ready": ready,
        "generated_at": now_iso(),
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
        "command": "estimate",
        "csv": str(args.csv),
        "channels": channels,
        "people": len(people),
        "named_groups": len(groups),
        "totals": totals,
        "estimated_seconds": round(seconds, 1),
        "estimated_minutes": round(seconds / 60, 2),
        "note": "COUNT-only, no body reads, no spend",
        "per_person": per_person,
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
                cmds.append(
                    _cmd(
                        ["msgvault", "sync-full", acct, "--after", today, "--noresume"],
                        "PHASE 1 auth/refresh token (fast, ~0 msgs)",
                    )
                )
            for acct in gmail_accounts:
                cmds.append(
                    _cmd(
                        [
                            "uv",
                            "run",
                            "--project",
                            ".",
                            "python",
                            "packs/ingestion/primitives/discover/gmail/discover.py",
                            "discover",
                            "--account-email",
                            acct,
                            "--fresh",
                        ],
                        "PHASE 2 deep sync",
                    )
                )
            caveats.append(
                f"Gmail auth-first: {len(gmail_accounts)} account(s) ({', '.join(gmail_accounts)}). "
                "Approve each Google login window in PHASE 1 (they appear up front), then PHASE 2 "
                "syncing runs unattended. Note: msgvault applies the account's linked category filter "
                "(promotions/social/forums/updates are excluded by design)."
            )
        else:
            cmds.append(
                "uv run --project . python packs/ingestion/primitives/discover/gmail/discover.py discover --account-email <account-email> --fresh"
            )

    # --- WhatsApp: refresh + scoped backfill (after gmail auth). ----------------
    if "whatsapp" in channels:
        store = paths["whatsapp"].parent
        # wacli `sync` only refreshes the group LIST + pulls recent/live messages going
        # forward (--max-messages is a DB-size cap, not a depth knob). OLDER history is
        # requested with `history backfill` (on-demand sync from your PRIMARY PHONE).
        # We SCOPE the backfill to just the chats that matter (the CSV people's DMs +
        # their groups) via --chat <jid>, instead of the user's entire WhatsApp.
        cmds.append(_cmd(["wacli", "--store", str(store), "sync", "--once", "--refresh-contacts", "--refresh-groups"]))
        names = [g.name for g in group_targets if g.channel == "whatsapp"]
        for person in people:
            wa_jids.extend(src.whatsapp_target_jids(paths["whatsapp"], person, names))
        wa_jids = list(dict.fromkeys(wa_jids))  # dedupe, preserve order
        if wa_jids:
            request_count = max(1, args.rounds)
            for jid in wa_jids:
                cmds.append(
                    _cmd(
                        [
                            "wacli",
                            "--store",
                            str(store),
                            "history",
                            "backfill",
                            "--chat",
                            jid,
                            "--requests",
                            str(request_count),
                            "--count",
                            "50",
                            "--wait",
                            "1m",
                            "--idle-exit",
                            "5s",
                        ],
                        f"scoped backfill for {jid} ({request_count} request(s))",
                    )
                )
        else:
            caveats.append(
                "No CSV-people WhatsApp chats found locally to scope backfill — "
                "either these people aren't in your WhatsApp, or run a plain `sync` first. "
                "`wacli history backfill` requires an explicit --chat JID, so logbook will not "
                "fall back to whole-store backfill."
            )
        caveats.append(
            "WhatsApp is the shallowest channel: `sync` is recent-forward only; scoped "
            "`history backfill --chat <jid>` backfills just the target conversations, but "
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
        "command": "deepen",
        "channels": channels,
        "current_depth": depth,
        "whatsapp_target_chats": wa_jids,
        "rounds": args.rounds,
        "recommended_commands": cmds,
        "ran": ran if args.run else None,
        "caveats": caveats,
        "note": "FREE syncs (no per-message cost). WhatsApp backfill is SCOPED to the CSV "
        "people's chats and connects to your phone live. Re-run estimate after.",
        "generated_at": now_iso(),
    }


EntryStreams = Callable[[dict[str, int]], list[Iterator[dict[str, Any]]]]


def build_logbook(
    people: list[Person],
    group_targets: list[GroupTarget] | None = None,
    *,
    paths: dict[str, Path],
    channels: list[str] | None = None,
    root: Path = LOGBOOK_ROOT,
    include_groups: bool = True,
    append: bool = False,
    input_csv: str = "",
) -> LogbookBuild:
    """Archive every message the local stores hold for ``people`` and their groups.

    Export (the default) rebuilds each selected entry from the stores after moving its
    prior files aside; sync appends past each entry's watermark. Only the entries built
    here change: the manifest and index keep every other entry. A channel whose store is
    missing or unreadable is reported, and its archive is left as it was.
    """
    channels = list(CHANNEL_DIR) if channels is None else channels
    depth = {channel: _store_depth(channel, paths[channel]) for channel in channels}
    statuses = {channel: _channel_status(depth[channel]) for channel in channels}
    readable = [channel for channel in channels if statuses[channel] is ChannelStatus.OK]
    manifest_path = root / MANIFEST_JSON.name
    prior_entries: dict[str, Any] = (
        json.loads(manifest_path.read_text(encoding="utf-8")).get("entries", {}) if manifest_path.exists() else {}
    )
    entries = dict(prior_entries)
    built: dict[str, dict[str, dict[str, Any]]] = {}
    written: Counter[str] = Counter()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

    # Export writes each entry here first and swaps it in only after every stream read
    # cleanly, so a reader failure leaves the archive and the catalog as they were.
    staging = root / f".building-{stamp}"

    def build_entry(slug: str, name: str, kind: str, entry_channels: list[str], streams: EntryStreams) -> None:
        prior = prior_entries.get(slug, {}) if append else {}
        resumed = {(c["channel"], c["container_id"]): c for c in prior.get("containers", {}).values()}
        writer = EntryWriter(root if append else staging, slug, append=append, prior=resumed)
        try:
            for stream in streams(prior.get("watermark", {})):
                _drain(writer, stream, written)
        finally:
            containers = writer.close()
        if not append:
            _set_aside(root, slug, entry_channels, stamp)
            _swap_in(staging, root, slug)
        built[slug] = containers
        _record_entry(entries, slug, name, kind, containers, replaced=[] if append else entry_channels)

    gmail_con = src.open_msgvault(paths["gmail"]) if "gmail" in readable else None
    t0 = time.time()
    completed = False
    try:
        # 1) People: gmail threads + imessage/whatsapp DMs under the person slug.
        for person in people:
            build_entry(person.slug, person.full_name, "person", readable,
                        _person_streams(person, readable, paths, gmail_con))

        # 2) Groups: each named/discovered group is its own top-level slug.
        for gjid, gtitle, channel, gslug in _resolve_group_entries(
            group_targets or [], people, paths, readable, include_groups, prior_entries
        ):
            build_entry(gslug, gtitle, "group", [channel], _group_streams(gjid, gtitle, channel, paths))
        completed = True
    finally:
        if gmail_con is not None:
            gmail_con.close()
        # The catalog lists exactly what is on disk: the entries swapped in before a failure.
        elapsed = round(time.time() - t0, 1)
        _write_manifest(root, channels, entries, sum(written.values()), elapsed, append, input_csv,
                        status="completed" if completed else "failed")
        _write_index(root, entries)
        if staging.exists() and not any(staging.iterdir()):
            staging.rmdir()

    return LogbookBuild(
        root=root,
        entries=tuple(slug for slug in built if slug in entries),
        messages=sum(written.values()),
        files=sum(len(containers) for containers in built.values()),
        channels=tuple(
            ChannelCoverage(channel, statuses[channel], written[channel],
                            depth[channel].get("earliest"), depth[channel].get("latest"))
            for channel in channels
        ),
        elapsed_seconds=elapsed,
    )


def _person_streams(person: Person, readable: list[str], paths: dict[str, Path], gmail_con: Any) -> EntryStreams:
    def streams(watermark: dict[str, int]) -> list[Iterator[dict[str, Any]]]:
        out = []
        if gmail_con is not None:
            out.append(src.stream_gmail(person, gmail_con, since_id=watermark.get("gmail", 0)))
        if "imessage" in readable:
            out.append(src.stream_imessage_dm(person, paths["imessage"], since_rowid=watermark.get("imessage", 0)))
        if "whatsapp" in readable:
            out.append(src.stream_whatsapp_dm(person, paths["whatsapp"], since_rowid=watermark.get("whatsapp", 0)))
        return out

    return streams


def _group_streams(gjid: Any, gtitle: str, channel: str, paths: dict[str, Path]) -> EntryStreams:
    def streams(watermark: dict[str, int]) -> list[Iterator[dict[str, Any]]]:
        since = watermark.get(channel, 0)
        if channel == "whatsapp":
            return [src.stream_whatsapp_group(paths["whatsapp"], gjid, gtitle, since_rowid=since)]
        return [src.stream_imessage_group(paths["imessage"], gjid[0], gtitle, gjid[1], since_rowid=since)]

    return streams


def _set_aside(root: Path, slug: str, channels: list[str], stamp: str) -> None:
    """Move an entry's channel folders aside before a rebuild: the archive may hold
    messages the stores no longer do, so nothing is deleted."""
    for channel in channels:
        folder = root / slug / CHANNEL_DIR[channel]
        if folder.exists():
            backup = root / f"{slug}.bkup-{stamp}"
            backup.mkdir(parents=True, exist_ok=True)
            folder.rename(backup / CHANNEL_DIR[channel])


def _swap_in(staging: Path, root: Path, slug: str) -> None:
    """Move a fully built entry's channel folders from staging into the archive."""
    built = staging / slug
    if not built.exists():
        return
    (root / slug).mkdir(parents=True, exist_ok=True)
    for folder in built.iterdir():
        folder.rename(root / slug / folder.name)
    built.rmdir()


def _record_entry(
    entries: dict[str, Any],
    slug: str,
    name: str,
    kind: str,
    containers: dict[str, dict[str, Any]],
    *,
    replaced: list[str],
) -> None:
    """Fold one built entry into the catalog: the containers it wrote, plus every prior
    container of a channel this build did not rewrite (``replaced``)."""
    kept = {
        rel_path: meta
        for rel_path, meta in entries.get(slug, {}).get("containers", {}).items()
        if meta.get("channel") not in replaced
    }
    merged = kept | containers
    if not merged:
        entries.pop(slug, None)
        return
    watermark: dict[str, int] = {}
    for meta in merged.values():
        channel = meta["channel"]
        watermark[channel] = max(watermark.get(channel, 0), int(meta.get("watermark", 0)))
    entries[slug] = {
        "slug": slug,
        "name": name,
        "kind": kind,
        "messages": sum(int(meta.get("messages", 0)) for meta in merged.values()),
        "files": len(merged),
        "watermark": watermark,
        "containers": merged,
    }


def _resolve_group_entries(
    group_targets: list[GroupTarget],
    people: list[Person],
    paths: dict[str, Path],
    channels: list[str],
    include_groups: bool,
    prior_entries: dict[str, Any],
):
    """Yield (jid_or_(rowid,guid), title, channel, slug) for every group entry to build.

    A group keeps the slug the catalog already gave its chat; a new chat whose name
    slug is taken by another entry gets a short id suffix."""
    known = {
        container["container_id"]: slug
        for slug, entry in prior_entries.items()
        if entry.get("kind") == "group"
        for container in entry.get("containers", {}).values()
    }
    # Person slugs of this batch count as taken too: a group must never share a person's folder.
    taken: set[str] = set(prior_entries) | {person.slug for person in people}
    seen_ids: set[str] = set()  # dedupe by container id (jid/guid), across all passes

    def _emit(container_key: str, target, title: str, channel: str):
        if container_key in seen_ids:
            return None  # same group already emitted (e.g. CSV-named AND membership)
        gslug = known.get(container_key)
        if gslug is None:
            base = group_slug(title)
            gslug = base if base not in taken else f"{base}-{_short(container_key)}"
        seen_ids.add(container_key)
        taken.add(gslug)
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


def _write_manifest(
    root: Path,
    channels: list[str],
    entries: dict[str, Any],
    total_msgs: int,
    seconds: float,
    append: bool,
    input_csv: str,
    *,
    status: str,
) -> None:
    manifest = {
        "source": "logbook",
        "status": status,
        "mode": "sync" if append else "export",
        "input_csv": input_csv,
        "channels": channels,
        "privacy": {
            "reads_bodies": True,
            "persists_verbatim": True,
            "scope": "Gmail threads + iMessage/WhatsApp DMs + named/membership groups",
        },
        "totals": {
            "entries": len(entries),
            "files": sum(e.get("files", 0) for e in entries.values()),
            "messages_written": total_msgs,
        },
        "elapsed_seconds": seconds,
        "entries": entries,
        "generated_at": now_iso(),
    }
    write_json(root / MANIFEST_JSON.name, manifest)


def _write_index(root: Path, entries: dict[str, Any]) -> None:
    lines = [
        "# Logbook index",
        "",
        f"_Generated {now_iso()}_",
        "",
        "| Entry | Kind | Files | Messages | Channels |",
        "|---|---|---|---|---|",
    ]
    for slug in sorted(entries):
        e = entries[slug]
        chans = sorted({m.get("channel") for m in e.get("containers", {}).values() if m.get("channel")})
        lines.append(
            f"| [{e.get('name') or slug}]({slug}/) | {e.get('kind')} | {e.get('files', 0)} | {e.get('messages', 0)} | {', '.join(chans)} |"
        )
    index = root / INDEX_MD.name
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _cli_build(args: argparse.Namespace, *, append: bool) -> dict[str, Any]:
    people, group_targets = load_people_from_csv(Path(args.csv), limit=args.limit, slug=args.slug)
    build = build_logbook(
        people,
        group_targets,
        paths=_paths(args),
        channels=_channels(args),
        include_groups=not args.no_groups,
        append=append,
        input_csv=str(args.csv),
    )
    return {"command": "sync" if append else "export", **build.to_payload(), "generated_at": now_iso()}


def cmd_export(args: argparse.Namespace) -> dict[str, Any]:
    return _cli_build(args, append=False)


def cmd_sync(args: argparse.Namespace) -> dict[str, Any]:
    return _cli_build(args, append=True)


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
            p.add_argument(
                "--no-groups",
                action="store_true",
                help="skip group chats (default: include every group a target is in)",
            )
        if name == "deepen":
            p.add_argument("--run", action="store_true", help="actually run the free local syncs (default: print only)")
            p.add_argument("--rounds", type=int, default=1, help="WhatsApp history backfill requests per chat")
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], dict[str, Any]] = {
        "check": cmd_check,
        "estimate": cmd_estimate,
        "deepen": cmd_deepen,
        "export": cmd_export,
        "sync": cmd_sync,
    }[args.command]
    emit(handler(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
