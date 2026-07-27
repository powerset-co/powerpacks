"""[1/4] Collect per-person message context from Gmail and chat DMs.

For each person in the merged people.csv who has any message channel, stream a
recent, adaptively-sampled window of their actual message BODIES into one
ephemeral JSON bundle per person. Only people with >= 1 message produce a bundle;
zero-interaction contacts are skipped. A full run removes bundles whose people
left the current merged input; scoped runs never prune unrelated bundles.

Reads message bodies - that deep inspection is the whole point. iMessage and
WhatsApp read DMs by default. The explicit ``--include-groups`` option also reads
small iMessage group-chat bodies; WhatsApp groups remain excluded. Bundles live under
``.powerpacks/deep-context/raw/`` (gitignored, ephemeral, purgeable); dossiers keep
synthesized facts, not verbatim text.

Memory: one person's recent window at a time (every source query is per-person
``LIMIT``-bounded), so RSS stays flat regardless of archive size.

Outputs (fixed dir, overwrite in place):
  <out-dir>/<person_id>.json   one bundle per person with >=1 message
  <out-dir>/manifest.json      counts/status/privacy

Changelog:
  2026-07-27 (declared contract): `CollectPersonContext` is a
    `pipeline/contract.py:Node` named `deep_collect`. It declares merged/people.csv
    plus the three external message stores (msgvault.db, chat.db, wacli.db) as
    inputs and the `{person_id}` raw-bundle template as its output; the final
    manifest write moved into the Node template (same keys, plus the declared
    `fingerprints` block). `build(args)` became `execute()`. `--dry-run` BYPASSES
    `run()` — main() calls `execute()` directly — because a dry run writes no
    manifest today and an estimate must never overwrite a completed raw manifest.
    Same flags, same emitted payload, same exit codes.
  2026-07-27: full collection removes raw bundles absent from the current people input.
  2026-07-23 (audit dedup): now_iso, write_json import from common.jsonio instead of deep_context.common (deduped there); no behavior change.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from packs.ingestion.primitives.deep_context import sources
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    RAW_BUNDLE_TEMPLATE,
    RAW_DIR,
    RAW_MANIFEST,
    Person,
    emit,
    load_people,
)
from packs.ingestion.primitives.common.jsonio import now_iso, write_json
from packs.ingestion.primitives.common.paths import DEFAULT_MSGVAULT_DB
from packs.ingestion.primitives.discover.messages.extract_imessage import DEFAULT_CHAT_DB
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, StageManifest

# Each channel is its own vertical with this deep cap: Gmail, iMessage, and WhatsApp
# each pool up to DEFAULT_DEEP_CAP recent messages independently, then they're
# concatenated (so no channel crowds out another). The incremental synthesizer groks
# the blended pool newest-first and stops on saturation/max-batches, so spend is bounded
# regardless of pool size. A char cap guards memory (raised to fit ~3 full verticals).
DEFAULT_DEEP_CAP = 1600
SAFETY_CHAR_CAP = 1_800_000


def _load_bundle(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _bundle_matches_policy(
    bundle: dict[str, Any],
    *,
    deep_cap: int,
    include_groups: bool,
    max_group_size: int,
) -> bool:
    policy = bundle.get("collection_policy")
    if not isinstance(policy, dict):
        return False
    return (
        policy.get("deep_cap") == deep_cap
        and policy.get("include_groups") is bool(include_groups)
        and (
            not include_groups
            or policy.get("max_group_size") == max_group_size
        )
    )


def _validate_people_csv(path: Path) -> None:
    with path.open(newline="", encoding="utf-8") as fh:
        fields = set(csv.DictReader(fh).fieldnames or [])
    missing = {"id", "source_channels"} - fields
    if missing:
        raise ValueError(f"people CSV missing required columns: {', '.join(sorted(missing))}")


def _purge_group_scoped_or_untrusted_bundles(out_dir: Path, *, partial: bool) -> int:
    """Discard unsafe raw bundles without deserializing their message bodies."""
    bundle_paths = [path for path in sorted(out_dir.glob("*.json")) if path.name != "manifest.json"]
    if not bundle_paths:
        return 0
    manifest = _load_bundle(out_dir / "manifest.json")
    privacy = manifest.get("privacy")
    safe_to_reuse = (
        manifest.get("privacy_schema_version") == 2
        and isinstance(privacy, dict)
        and privacy.get("group_bodies_present") is False
    )
    if safe_to_reuse:
        return 0
    if partial:
        raise ValueError(
            "existing raw bundles have group-enabled or legacy privacy scope; "
            "run a full default collection without --person/--limit to rebuild them safely"
        )
    for path in bundle_paths:
        path.unlink()
    return len(bundle_paths)


def _retained_group_policy(out_dir: Path) -> tuple[int, int]:
    """Return retained group-message count and the largest known group-size cap."""
    message_count = 0
    max_group_size = 0
    for path in sorted(out_dir.glob("*.json")):
        if path.name == "manifest.json":
            continue
        bundle = _load_bundle(path)
        messages = bundle.get("messages")
        if not isinstance(messages, list):
            continue
        groups = [
            message for message in messages
            if isinstance(message, dict) and message.get("channel") == "imessage_group"
        ]
        if not groups:
            continue
        message_count += len(groups)
        policy = bundle.get("collection_policy")
        if isinstance(policy, dict) and isinstance(policy.get("max_group_size"), int):
            max_group_size = max(max_group_size, policy["max_group_size"])
    return message_count, max_group_size


def collect_one(
    person: Person,
    *,
    store: "sources.gni.MsgvaultStore | None",
    accounts: set[str],
    chat_db: Path,
    wacli_db: Path,
    deep_cap: int,
    include_groups: bool = False,
    max_group_size: int = 25,
) -> tuple[list[dict[str, Any]], int]:
    """Gather a deep, recency-first pool of one person's messages across sources.

    Returns ``(pool, available)`` where ``available`` is the TRUE total in the
    sources (e.g. all 102k iMessage DMs, or every poolable email), so
    ``capped = available > len(pool)`` is honest. Each channel is its own vertical,
    already capped at ``deep_cap`` by its reader; they're concatenated by priority
    (identity-dense email, then DM bodies newest-first, then — only with
    ``include_groups`` — group-chat bodies), bounded only by the char safety cap."""
    gmail: list[dict[str, Any]] = []
    gmail_total = 0
    if store is not None and person.emails:
        gmail = sources.read_gmail(person, store, accounts, cap=deep_cap)
        gmail_total = sources.count_gmail(person, store, accounts)
    dm_chat: list[dict[str, Any]] = []
    group_chat: list[dict[str, Any]] = []
    true_chat_total = 0
    if person.phones:
        whatsapp = sources.read_whatsapp(person, wacli_db, cap=deep_cap)
        dm_chat.extend(sources.read_imessage(person, chat_db, cap=deep_cap))
        dm_chat.extend(whatsapp)
        # Reuse the WhatsApp pull for the honest total instead of re-querying it.
        # (len(whatsapp) is post-cap; a count_whatsapp_dms() is a clean follow-up.)
        true_chat_total = sources.count_imessage_dms(person, chat_db) + len(whatsapp)
        if include_groups:
            group_chat = sources.read_imessage_group_messages(
                person, chat_db, max_group_size=max_group_size, cap=deep_cap)

    # No shared message cap: each vertical already pooled up to deep_cap, so an
    # email-rich contact and a text-rich contact each keep their full vertical. The
    # char cap is the only cross-channel bound (RAM guard). Priority order keeps the
    # identity-dense email first, then DMs newest-first, then group bodies.
    ordered = list(gmail) \
        + sorted(dm_chat, key=lambda m: m.get("at") or "", reverse=True) \
        + sorted(group_chat, key=lambda m: m.get("at") or "", reverse=True)
    pool: list[dict[str, Any]] = []
    used = 0
    for msg in ordered:
        text = msg.get("text") or ""
        if not text:
            continue
        if used + len(text) > SAFETY_CHAR_CAP and pool:
            break
        pool.append(msg)
        used += len(text)
    pool.sort(key=lambda m: m.get("at") or "")
    available = gmail_total + true_chat_total + len(group_chat)
    return pool, available


class CollectPrivacy(BaseModel):
    """The manifest's `privacy` block — same keys as the raw dict it replaces."""
    message_bodies_read: bool = True
    dms_only: bool = True
    group_body_access_requested: bool = False
    group_bodies_present: bool = False
    group_body_messages_present: int = 0
    groups_read: bool = False
    group_source: str = ""
    max_group_size: int = 0
    network_called: bool = False
    local_only: bool = True


class CollectPersonContextManifest(StageManifest):
    """The stage's typed manifest payload — same keys as the raw dict it replaces.
    `updated_at` is stamped in `execute()` so the dry-run bypass (which skips the
    manifest writer) still emits it, exactly as the raw dict did."""
    source: str = "collect_person_context"
    privacy_schema_version: int = 2
    dry_run: bool = False
    people_total: int = 0
    people_with_context: int = 0
    people_skipped_existing: int = 0
    total_messages_sampled: int = 0
    people_capped: int = 0
    channel_message_counts: dict[str, int] = Field(default_factory=dict)
    contacts_per_sec: float = 0.0
    messages_per_sec: float = 0.0
    # `float | int`: the raw dict emitted the literal int 0 when no people were
    # selected and a rounded float otherwise; the union preserves both verbatim.
    ms_per_contact: float | int = 0
    deep_cap_per_person: int = 0
    groups_included: bool = False
    max_group_size: int = 0
    bundles_purged_for_scope: int = 0
    orphan_bundles_removed: int = 0
    msgvault_available: bool = False
    chat_db_available: bool = False
    chat_db_probe: dict[str, Any] = Field(default_factory=dict)
    wacli_available: bool = False
    out_dir: str = ""
    elapsed_ms: int = 0
    updated_at: str = ""
    privacy: CollectPrivacy = Field(default_factory=CollectPrivacy)


class CollectPersonContext(Node):
    """Streams each merged person's recent message bodies into one raw bundle.

    Free and local: reads the merged people.csv plus the external message stores,
    writes only raw bundles + the stage manifest. Construct with explicit paths
    and call `run()` — except `--dry-run`, where the caller invokes `execute()`
    directly so the counting pass never writes the stage manifest."""

    name = "deep_collect"
    # people.csv is produced in-graph (merge_people). It stays `required=False`
    # even though absence is a hard failure: `_validate_people_csv` raises exactly
    # as before (traceback + exit 1, now also a typed Failed manifest), whereas a
    # required-input NotReady would exit 0 — and under `bin/deep-context rejudge`
    # (set -e) that would let the paid synthesize step run against stale bundles.
    # The three message stores are genuinely external: the msgvault and wacli
    # binaries and macOS own them; no node writes them.
    inputs = (
        Artifact(path=str(DEFAULT_PEOPLE_CSV), required=False),
        Artifact(path=str(DEFAULT_MSGVAULT_DB), external=True, required=False),
        Artifact(path=str(DEFAULT_CHAT_DB), external=True, required=False),
        Artifact(path=str(sources.DEFAULT_WACLI_DB), external=True, required=False),
    )
    outputs = (
        Artifact(path=RAW_BUNDLE_TEMPLATE, required=False),
    )
    payload = CollectPersonContextManifest
    manifest = str(RAW_MANIFEST)

    def __init__(
        self,
        *,
        people_csv: Path | None = None,
        out_dir: Path | None = None,
        msgvault_db: Path | None = None,
        chat_db: Path | None = None,
        wacli_db: Path | None = None,
        deep_cap: int = DEFAULT_DEEP_CAP,
        include_groups: bool = False,
        max_group_size: int = 25,
        limit: int = 0,
        person: str = "",
        force: bool = False,
        dry_run: bool = False,
    ) -> None:
        self.people_csv = Path(people_csv or DEFAULT_PEOPLE_CSV)
        self.out_dir = Path(out_dir or RAW_DIR)
        self.msgvault_db = Path(msgvault_db or DEFAULT_MSGVAULT_DB).expanduser()
        self.chat_db = Path(chat_db or DEFAULT_CHAT_DB).expanduser()
        self.wacli_db = Path(wacli_db or sources.DEFAULT_WACLI_DB)
        self.deep_cap = deep_cap
        self.include_groups = include_groups
        self.max_group_size = max_group_size
        self.limit = limit
        self.person = person
        self.force = force
        self.dry_run = dry_run

    def bindings(self) -> dict[str, str]:
        return {
            str(DEFAULT_PEOPLE_CSV): str(self.people_csv),
            str(DEFAULT_MSGVAULT_DB): str(self.msgvault_db),
            str(DEFAULT_CHAT_DB): str(self.chat_db),
            str(sources.DEFAULT_WACLI_DB): str(self.wacli_db),
            RAW_BUNDLE_TEMPLATE: str(self.out_dir / "{person_id}.json"),
            self.manifest: str(self.out_dir / "manifest.json"),
        }

    def execute(self) -> CollectPersonContextManifest:
        started = time.monotonic()
        _validate_people_csv(self.people_csv)

        store: "sources.gni.MsgvaultStore | None" = None
        accounts: set[str] = set()
        if self.msgvault_db.exists():
            store = sources.gni.MsgvaultStore(self.msgvault_db)
            try:
                store.connect()
                store.require_schema()
                accounts = store.account_emails()
            except Exception:
                store.close()
                store = None

        # One-time chat.db readability probe so a Full Disk Access denial is loud,
        # not silently swallowed as "0 iMessage messages".
        chat_probe = sources.probe_chat_db(self.chat_db)
        if chat_probe["exists"] and not chat_probe["readable"]:
            print(
                f"[collect] WARNING: chat.db exists but is unreadable — iMessage will be EMPTY. "
                f"Likely Full Disk Access. error={chat_probe['error']}",
                file=sys.stderr, flush=True,
            )

        if not self.dry_run:
            self.out_dir.mkdir(parents=True, exist_ok=True)

        bundles_purged_for_scope = 0
        if not self.dry_run and not self.include_groups:
            bundles_purged_for_scope = _purge_group_scoped_or_untrusted_bundles(
                self.out_dir,
                partial=bool(self.person or self.limit),
            )

        people_total = 0
        with_context = 0
        capped = 0
        skipped_existing = 0
        selected_person_ids: set[str] = set()
        channel_counts = {"gmail": 0, "imessage": 0, "whatsapp": 0}
        total_messages = 0
        try:
            # Every keyable merged person is collectable, including candidate:<key> rows.
            for person in load_people(self.people_csv, limit=self.limit, person_id=self.person):
                people_total += 1
                selected_person_ids.add(person.person_id)
                bundle_path = self.out_dir / f"{person.person_id}.json"
                if bundle_path.exists() and not self.force and not self.dry_run:
                    existing = _load_bundle(bundle_path)
                    if _bundle_matches_policy(
                        existing,
                        deep_cap=self.deep_cap,
                        include_groups=self.include_groups,
                        max_group_size=self.max_group_size,
                    ):
                        skipped_existing += 1
                        with_context += 1
                        continue
                messages, available = collect_one(
                    person,
                    store=store,
                    accounts=accounts,
                    chat_db=self.chat_db,
                    wacli_db=self.wacli_db,
                    deep_cap=self.deep_cap,
                    include_groups=self.include_groups,
                    max_group_size=self.max_group_size,
                )
                groups = sources.read_imessage_groups(person, self.chat_db) if person.phones else []
                thread_participants = (sources.gmail_thread_participants(person, store)
                                       if store is not None and person.emails else [])
                if not messages and not groups:
                    if not self.dry_run:
                        bundle_path.unlink(missing_ok=True)
                    continue
                with_context += 1
                total_messages += len(messages)
                if available > len(messages):
                    capped += 1
                for msg in messages:
                    channel_counts[msg["channel"]] = channel_counts.get(msg["channel"], 0) + 1
                if self.dry_run:
                    continue
                write_json(bundle_path, {
                    "person_id": person.person_id,
                    "full_name": person.full_name,
                    "emails": person.emails,
                    "phones": person.phones,
                    "source_channels": person.source_channels,
                    "groups": groups,
                    "thread_participants": thread_participants,
                    "messages": messages,
                    "messages_available": available,
                    "capped": available > len(messages),
                    "collection_policy": {
                        "deep_cap": self.deep_cap,
                        "include_groups": bool(self.include_groups),
                        "max_group_size": self.max_group_size if self.include_groups else 0,
                    },
                    "collected_at": now_iso(),
                })
                if with_context % 25 == 0:
                    print(f"[collect] {with_context} bundles written", file=sys.stderr, flush=True)
        finally:
            if store is not None:
                store.close()

        # Only a full run sees the whole people input, so only a full run may
        # drop bundles for people who left it; scoped/limited/dry runs never do.
        orphan_bundles_removed = 0
        if not self.dry_run and not self.person and not self.limit:
            for bundle_path in self.out_dir.glob("*.json"):
                if bundle_path.name == "manifest.json" or bundle_path.stem in selected_person_ids:
                    continue
                bundle_path.unlink()
                orphan_bundles_removed += 1

        elapsed_s = max(time.monotonic() - started, 1e-6)
        retained_group_messages, retained_max_group_size = _retained_group_policy(self.out_dir)
        group_access_requested = bool(self.include_groups)
        group_bodies_present = retained_group_messages > 0
        return CollectPersonContextManifest(
            status="completed",
            privacy_schema_version=2,
            dry_run=bool(self.dry_run),
            people_total=people_total,
            people_with_context=with_context,
            people_skipped_existing=skipped_existing,
            total_messages_sampled=total_messages,
            people_capped=capped,
            channel_message_counts=channel_counts,
            contacts_per_sec=round(people_total / elapsed_s, 1),
            messages_per_sec=round(total_messages / elapsed_s, 1),
            ms_per_contact=round(elapsed_s / people_total * 1000, 2) if people_total else 0,
            deep_cap_per_person=self.deep_cap,
            groups_included=bool(self.include_groups),
            max_group_size=self.max_group_size,
            bundles_purged_for_scope=bundles_purged_for_scope,
            orphan_bundles_removed=orphan_bundles_removed,
            msgvault_available=store is not None or self.msgvault_db.exists(),
            chat_db_available=self.chat_db.exists(),
            chat_db_probe=chat_probe,
            wacli_available=self.wacli_db.exists(),
            out_dir=str(self.out_dir),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            updated_at=now_iso(),
            privacy=CollectPrivacy(
                message_bodies_read=True,
                dms_only=not (group_access_requested or group_bodies_present),
                group_body_access_requested=group_access_requested,
                group_bodies_present=group_bodies_present,
                group_body_messages_present=retained_group_messages,
                groups_read=group_access_requested or group_bodies_present,
                group_source="imessage" if group_access_requested or group_bodies_present else "",
                max_group_size=(
                    self.max_group_size if group_access_requested else retained_max_group_size
                ),
                network_called=False,
                local_only=True,
            ),
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Collect per-person message bodies (Gmail + chat DMs; optional small iMessage groups).")
    p.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    p.add_argument("--out-dir", default=str(RAW_DIR))
    p.add_argument("--msgvault-db", default=str(DEFAULT_MSGVAULT_DB))
    p.add_argument("--chat-db", default=str(DEFAULT_CHAT_DB))
    p.add_argument("--wacli-db", default=str(sources.DEFAULT_WACLI_DB))
    p.add_argument("--deep-cap", type=int, default=DEFAULT_DEEP_CAP, help="Max messages pooled per person (raise = costs more at synthesis)")
    p.add_argument("--include-groups", action="store_true", help="Opt-in: also read iMessage GROUP bodies from small shared groups (costs more)")
    p.add_argument("--max-group-size", type=int, default=25, help="Skip groups larger than this many participants")
    p.add_argument("--limit", type=int, default=0, help="Limit people (0 = all)")
    p.add_argument("--person", default="", help="Only this person id (candidate:<key> selects a candidate)")
    p.add_argument("--force", action="store_true", help="Rebuild bundles even if present")
    p.add_argument("--dry-run", action="store_true", help="Count messages, write nothing")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    node = CollectPersonContext(
        people_csv=Path(args.people_csv),
        out_dir=Path(args.out_dir),
        msgvault_db=Path(args.msgvault_db),
        chat_db=Path(args.chat_db),
        wacli_db=Path(args.wacli_db),
        deep_cap=args.deep_cap,
        include_groups=args.include_groups,
        max_group_size=args.max_group_size,
        limit=args.limit,
        person=args.person,
        force=args.force,
        dry_run=args.dry_run,
    )
    # A dry run writes nothing today — bypass the run template so the counting
    # pass can never overwrite a completed raw manifest with its estimate.
    payload = node.execute() if args.dry_run else node.run()
    emit(payload.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
