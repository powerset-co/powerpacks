"""Collect bounded Gmail, iMessage, and WhatsApp context per SQLite parent.

Each parent is processed independently: its messages are pooled, written to a
fixed raw JSON bundle, and projected into SQLite before the next parent
starts, so no message body is retained past the parent being processed —
only a scalar set of parent ids carries across the loop, for the
after-the-fact orphan sweep. The stage writes one display-only manifest;
downstream stages read the SQLite projection.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from packs.ingestion.primitives.deep_context.collection import context_sources, planning
from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle, MessageChannel
from packs.ingestion.primitives.deep_context.collection.normalization import normalize_cached_bundles
from packs.ingestion.primitives.deep_context.shared.common import (
    CANONICAL_DB,
    RAW_BUNDLE_TEMPLATE,
    RAW_DIR,
    RAW_MANIFEST,
    emit,
)
from packs.ingestion.primitives.deep_context.db.context_queries import (
    collection_bundle_group_message_count,
    collection_bundle_parent_ids,
    existing_parent_ids,
)
from packs.ingestion.primitives.deep_context.db.projectors import project_parent_source_bundle
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.manifests.collect_person_context_manifest import (
    CollectPersonContextManifest,
)
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
        max_group_size: int = 25,
        dry_run: bool = False,
    ) -> None:
        self.db = db
        self.out_dir = Path(out_dir or RAW_DIR)
        self.msgvault_db = Path(msgvault_db or DEFAULT_MSGVAULT_DB).expanduser()
        self.chat_db = Path(chat_db or DEFAULT_CHAT_DB).expanduser()
        self.wacli_db = Path(wacli_db or context_sources.DEFAULT_WACLI_DB)
        self.deep_cap = deep_cap
        self.max_group_size = max_group_size
        self.dry_run = dry_run
        self.sources = context_sources.ContextSources(
            store=context_sources.gni.MsgvaultStore(self.msgvault_db),
            chat_db=self.chat_db,
            wacli_db=self.wacli_db,
            deep_cap=self.deep_cap,
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
            # No-op on a clean install: works from cached artifact payloads only,
            # opens no message store, so it never re-bills.
            normalize_cached_bundles(db, self.out_dir)
        people = planning.source_parents(db)
        bundle_ids = set(collection_bundle_parent_ids(db))

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

        people_total = len(people)
        with_context = 0
        capped = 0
        # Seeded from the enum so every channel_counts key is a plain str: a
        # channel absent from this seed would enter later as a MessageChannel
        # key via .get()'s default, splitting the dict across two key types.
        channel_counts: dict[str, int] = {channel.value: 0 for channel in MessageChannel}
        total_messages = 0
        try:
            for person in people:
                bundle_path = self.out_dir / f"{person.person_id}.json"
                messages, available = self.sources.collect_person(person)
                groups = self.sources.imessage_groups(person)
                thread_participants = self.sources.thread_participants(person)
                if not messages and not groups:
                    if not self.dry_run:
                        bundle_path.unlink(missing_ok=True)
                        # Absent path -> projector deletes the SQLite row (see
                        # projectors.py), clearing any earlier bundle for this parent.
                        project_parent_source_bundle(db, bundle_path, person.person_id)
                        bundle_ids.discard(person.person_id)
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
                bundle = CollectionBundle.of(
                    person,
                    messages=messages,
                    groups=groups,
                    thread_participants=thread_participants,
                    available=available,
                )
                payload = bundle.to_payload()
                write_json(bundle_path, payload)
                project_parent_source_bundle(db, bundle_path, person.person_id)
                bundle_ids.add(person.person_id)
                if with_context % 25 == 0:
                    print(f"[collect] {with_context} bundles written", file=sys.stderr, flush=True)
        finally:
            self.sources.close()

        orphan_person_ids: set[str] = set()
        if not self.dry_run:
            # A bundle is orphaned when its parent row is gone, never merely
            # because this run's message-channel selection (source_parents)
            # happened to skip it — narrowing selection must not be destructive.
            orphan_person_ids = bundle_ids - existing_parent_ids(db)
            for parent_id in orphan_person_ids:
                path = self.out_dir / f"{parent_id}.json"
                path.unlink(missing_ok=True)
                project_parent_source_bundle(db, path, parent_id)

        retained_group_messages = collection_bundle_group_message_count(db)
        group_bodies_present = retained_group_messages > 0
        # a run too fast to measure must not divide by zero
        elapsed_s = max(time.monotonic() - started, 1e-6)
        return CollectPersonContextManifest(
            status="completed",
            privacy_schema_version=2,
            dry_run=bool(self.dry_run),
            people_total=people_total,
            people_with_context=with_context,
            total_messages_sampled=total_messages,
            people_capped=capped,
            channel_message_counts=channel_counts,
            contacts_per_sec=round(people_total / elapsed_s, 1),
            messages_per_sec=round(total_messages / elapsed_s, 1),
            ms_per_contact=round(elapsed_s / people_total * 1000, 2) if people_total else 0,
            deep_cap_per_person=self.deep_cap,
            max_group_size=self.max_group_size,
            orphan_bundles_removed=len(orphan_person_ids),
            msgvault_available=self.msgvault_db.exists(),
            chat_db_available=self.chat_db.exists(),
            chat_db_probe=chat_probe_payload,
            wacli_available=self.wacli_db.exists(),
            out_dir=str(self.out_dir),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            updated_at=now_iso(),
            # dms_only=False / group_body_access_requested=True are the receipt for
            # standing owner authorization (AGENTS.md) to read iMessage group bodies.
            # network_called=False / local_only=True are invariants this stage guarantees.
            privacy={
                "message_bodies_read": True,
                "dms_only": False,
                "group_body_access_requested": True,
                "group_bodies_present": group_bodies_present,
                "group_body_messages_present": retained_group_messages,
                "groups_read": True,
                "group_source": "imessage",
                "max_group_size": self.max_group_size,
                "network_called": False,
                "local_only": True,
            },
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Collect per-parent message bodies (Gmail + chat DMs + small iMessage groups)."
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
        help=(
            "Max messages pooled per channel, not per person — Gmail multiplies "
            "this by the contact's address count (raise = costs more at synthesis)"
        ),
    )
    p.add_argument("--max-group-size", type=int, default=25, help="Skip groups larger than this many participants")
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
        max_group_size=args.max_group_size,
        dry_run=args.dry_run,
    )
    # run() is the Node template (writes the manifest, records artifacts); execute()
    # is the bare body, so --dry-run counts without leaving a receipt.
    payload = node.execute() if args.dry_run else node.run()
    emit(payload.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
