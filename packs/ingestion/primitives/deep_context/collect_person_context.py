"""Collect bounded Gmail, iMessage, and WhatsApp context per SQLite parent.

Each parent is processed independently, so memory stays flat. The stage writes a
fixed raw JSON bundle, projects its full payload immediately, and writes one
display-only manifest. Downstream stages read the SQLite projection.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from packs.ingestion.primitives.deep_context.collection import context_sources, planning
from packs.ingestion.primitives.deep_context.collection.models import (
    CollectionBundle,
    CollectPersonContextManifest,
)
from packs.ingestion.primitives.deep_context.collection.normalization import normalize_cached_bundles
from packs.ingestion.primitives.deep_context.common import (
    CANONICAL_DB,
    RAW_BUNDLE_TEMPLATE,
    RAW_DIR,
    RAW_MANIFEST,
    emit,
)
from packs.ingestion.primitives.deep_context.db.projectors import project_parent_source_bundle
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.common.jsonio import now_iso, write_json
from packs.ingestion.primitives.common.paths import DEFAULT_MSGVAULT_DB
from packs.ingestion.primitives.discover.messages.extract_imessage import DEFAULT_CHAT_DB
from packs.ingestion.primitives.pipeline.contract import Artifact, Node

DEFAULT_DEEP_CAP = context_sources.CHAT_MESSAGE_CAP


class CollectPersonContext(Node):
    """Write and project one bounded message bundle per SQLite parent."""

    name = "deep_collect"
    inputs = (
        Artifact(path=str(CANONICAL_DB), external=True, required=False),
        Artifact(path=str(DEFAULT_MSGVAULT_DB), external=True, required=False),
        Artifact(path=str(DEFAULT_CHAT_DB), external=True, required=False),
        Artifact(path=str(context_sources.DEFAULT_WACLI_DB), external=True, required=False),
    )
    outputs = (Artifact(path=RAW_BUNDLE_TEMPLATE, required=False),)
    payload = CollectPersonContextManifest
    manifest = str(RAW_MANIFEST)

    def __init__(
        self,
        *,
        db: Db,
        out_dir: Path | None = None,
        msgvault_db: Path | None = None,
        chat_db: Path | None = None,
        wacli_db: Path | None = None,
        deep_cap: int = DEFAULT_DEEP_CAP,
        include_groups: bool = False,
        max_group_size: int = 25,
        limit: int | None = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> None:
        self.db = db
        self.out_dir = Path(out_dir or RAW_DIR)
        self.msgvault_db = Path(msgvault_db or DEFAULT_MSGVAULT_DB).expanduser()
        self.chat_db = Path(chat_db or DEFAULT_CHAT_DB).expanduser()
        self.wacli_db = Path(wacli_db or context_sources.DEFAULT_WACLI_DB)
        self.deep_cap = deep_cap
        self.include_groups = include_groups
        self.max_group_size = max_group_size
        self.limit = limit
        self.force = force
        self.dry_run = dry_run
        self.sources = context_sources.ContextSources(
            store=context_sources.gni.MsgvaultStore(self.msgvault_db),
            chat_db=self.chat_db,
            wacli_db=self.wacli_db,
            deep_cap=self.deep_cap,
            include_groups=self.include_groups,
            max_group_size=self.max_group_size,
        )

    def bindings(self) -> dict[str, str]:
        return {
            str(CANONICAL_DB): str(self.db.db_path),
            str(DEFAULT_MSGVAULT_DB): str(self.msgvault_db),
            str(DEFAULT_CHAT_DB): str(self.chat_db),
            str(context_sources.DEFAULT_WACLI_DB): str(self.wacli_db),
            RAW_BUNDLE_TEMPLATE: str(self.out_dir / "{parent_id}.json"),
            self.manifest: str(self.out_dir / "manifest.json"),
        }

    def execute(self) -> CollectPersonContextManifest:
        started = time.monotonic()
        db = self.db
        if not self.dry_run:
            normalize_cached_bundles(db, self.out_dir)
        people = planning.source_parents(db, limit=self.limit)
        bundles = planning.projected_bundles(db)

        readiness = self.sources.readiness()
        chat_probe = readiness.chat_db
        chat_probe_payload = chat_probe.to_payload()
        if chat_probe.exists and not chat_probe.readable:
            print(
                f"[collect] WARNING: chat.db exists but is unreadable — iMessage will be EMPTY. "
                f"Likely Full Disk Access. error={chat_probe.error}",
                file=sys.stderr,
                flush=True,
            )

        if not self.dry_run:
            self.out_dir.mkdir(parents=True, exist_ok=True)

        purged_person_ids: set[str] = set()
        if not self.dry_run and not self.include_groups:
            purged_person_ids = planning.purge_group_scope(
                bundles,
                limited=bool(self.limit),
            )
            for parent_id in purged_person_ids:
                path = self.out_dir / f"{parent_id}.json"
                path.unlink(missing_ok=True)
                project_parent_source_bundle(db, path, parent_id)
                bundles.pop(parent_id, None)

        people_total = len(people)
        with_context = 0
        capped = 0
        skipped_existing = 0
        selected_person_ids: set[str] = set()
        channel_counts = {"gmail": 0, "imessage": 0, "whatsapp": 0}
        total_messages = 0
        try:
            for person in people:
                selected_person_ids.add(person.person_id)
                bundle_path = self.out_dir / f"{person.person_id}.json"
                existing: CollectionBundle | None = bundles.get(person.person_id)
                if existing and not self.force and not self.dry_run:
                    if planning.bundle_matches_policy(
                        existing,
                        person,
                        deep_cap=self.deep_cap,
                        include_groups=self.include_groups,
                        max_group_size=self.max_group_size,
                    ):
                        skipped_existing += 1
                        with_context += 1
                        continue
                messages, available = self.sources.collect_person(person)
                groups = self.sources.imessage_groups(person)
                thread_participants = self.sources.thread_participants(person)
                if not messages and not groups:
                    if not self.dry_run:
                        bundle_path.unlink(missing_ok=True)
                        project_parent_source_bundle(db, bundle_path, person.person_id)
                        bundles.pop(person.person_id, None)
                    continue
                with_context += 1
                total_messages += len(messages)
                if available > len(messages):
                    capped += 1
                for msg in messages:
                    channel = msg.channel or "unknown"
                    channel_counts[channel] = channel_counts.get(channel, 0) + 1
                if self.dry_run:
                    continue
                bundle = planning.build_bundle(
                    person,
                    messages=messages,
                    groups=groups,
                    thread_participants=thread_participants,
                    available=available,
                    deep_cap=self.deep_cap,
                    include_groups=self.include_groups,
                    max_group_size=self.max_group_size,
                    collected_at=now_iso(),
                )
                payload = bundle.to_payload()
                write_json(bundle_path, payload)
                project_parent_source_bundle(db, bundle_path, person.person_id)
                bundles[person.person_id] = bundle
                if with_context % 25 == 0:
                    print(f"[collect] {with_context} bundles written", file=sys.stderr, flush=True)
        finally:
            self.sources.close()

        orphan_person_ids: set[str] = set()
        if not self.dry_run and not self.limit:
            orphan_person_ids = set(bundles) - selected_person_ids
            for parent_id in orphan_person_ids:
                path = self.out_dir / f"{parent_id}.json"
                path.unlink(missing_ok=True)
                project_parent_source_bundle(db, path, parent_id)
                bundles.pop(parent_id, None)

        retained_group_messages, retained_max_group_size = planning.retained_group_policy(
            bundles,
        )
        group_access_requested = bool(self.include_groups)
        group_bodies_present = retained_group_messages > 0
        elapsed_s = max(time.monotonic() - started, 1e-6)
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
            bundles_purged_for_scope=len(purged_person_ids),
            orphan_bundles_removed=len(orphan_person_ids),
            msgvault_available=self.msgvault_db.exists(),
            chat_db_available=self.chat_db.exists(),
            chat_db_probe=chat_probe_payload,
            wacli_available=self.wacli_db.exists(),
            out_dir=str(self.out_dir),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            updated_at=now_iso(),
            privacy={
                "message_bodies_read": True,
                "dms_only": not (group_access_requested or group_bodies_present),
                "group_body_access_requested": group_access_requested,
                "group_bodies_present": group_bodies_present,
                "group_body_messages_present": retained_group_messages,
                "groups_read": group_access_requested or group_bodies_present,
                "group_source": "imessage" if group_access_requested or group_bodies_present else "",
                "max_group_size": self.max_group_size if group_access_requested else retained_max_group_size,
                "network_called": False,
                "local_only": True,
            },
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Collect per-parent message bodies (Gmail + chat DMs; optional small iMessage groups)."
    )
    p.add_argument("--db", default=str(CANONICAL_DB))
    p.add_argument("--out-dir", default=str(RAW_DIR))
    p.add_argument("--msgvault-db", default=str(DEFAULT_MSGVAULT_DB))
    p.add_argument("--chat-db", default=str(DEFAULT_CHAT_DB))
    p.add_argument("--wacli-db", default=str(context_sources.DEFAULT_WACLI_DB))
    p.add_argument(
        "--deep-cap",
        type=int,
        default=DEFAULT_DEEP_CAP,
        help="Max messages pooled per person (raise = costs more at synthesis)",
    )
    p.add_argument(
        "--include-groups",
        action="store_true",
        help="Opt-in: also read iMessage GROUP bodies from small shared groups (costs more)",
    )
    p.add_argument("--max-group-size", type=int, default=25, help="Skip groups larger than this many participants")
    p.add_argument("--limit", type=int, default=None, help="Limit parents")
    p.add_argument("--force", action="store_true", help="Rebuild bundles even if present")
    p.add_argument("--dry-run", action="store_true", help="Count messages, write nothing")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    node = CollectPersonContext(
        db=open_existing_db(Path(args.db)),
        out_dir=Path(args.out_dir),
        msgvault_db=Path(args.msgvault_db),
        chat_db=Path(args.chat_db),
        wacli_db=Path(args.wacli_db),
        deep_cap=args.deep_cap,
        include_groups=args.include_groups,
        max_group_size=args.max_group_size,
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
    )
    payload = node.execute() if args.dry_run else node.run()
    emit(payload.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
