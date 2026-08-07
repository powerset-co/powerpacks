"""Report source readiness and projected Deep Context counts without spend."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from packs.ingestion.primitives.deep_context import context_sources
from packs.ingestion.primitives.deep_context.collection.state import projected_bundles
from packs.ingestion.primitives.deep_context.common import (
    CANONICAL_DB,
    DEFAULT_PEOPLE_CSV,
    GMAIL_CHANNEL,
    IMESSAGE_CHANNEL,
    WHATSAPP_CHANNEL,
    emit,
    load_env,
)
from packs.ingestion.primitives.deep_context.db.models import CanonicalSnapshot
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.imported_people import (
    ImportedPerson,
    read_imported_people,
)
from packs.ingestion.primitives.deep_context.migrate_sqlite import (
    legacy_artifacts_present,
)
from packs.ingestion.primitives.deep_context.readiness_models import (
    CandidateCounts,
    ChatDbCheck,
    ChatDbProbe,
    ImportReadinessCounts,
    MessageCounts,
    PathCheck,
    PeopleCsvCheck,
    ProjectedReadinessCounts,
    ReadinessChecks,
    ReadinessReport,
    StatusCheck,
    readiness_payload,
    source_counts,
)
from packs.ingestion.primitives.common.jsonio import now_iso


ADVICE_RULES: tuple[tuple[str, str], ...] = (
    ("unreadable_full_disk_access", "iMessage blocked: grant Full Disk Access to your terminal and run in it (not via the Claude Code Bash tool)."),
    ("missing", "No msgvault.db — run $import-email/$msgvault to sync Gmail, or proceed with messages only."),
    ("missing", "OPENAI_API_KEY missing from environment/.env — synthesis cannot run."),
    ("absent", "No owner.json — add one to enable shared-context (school/employer overlap) inference."),
)


def _import_counts(
    people: tuple[ImportedPerson, ...], db: Db | None,
) -> ImportReadinessCounts:
    msg_channels = {GMAIL_CHANNEL, IMESSAGE_CHANNEL, WHATSAPP_CHANNEL}
    message_people = sum(
        bool(msg_channels.intersection(row.source_channels))
        and bool(row.emails or row.phones)
        for row in people
    )
    snapshot: CanonicalSnapshot | None = (
        canonical_snapshot(db) if db is not None else None
    )
    parent_by_person = {
        row.person_id: row.parent_id for row in snapshot.people
    } if snapshot is not None else {}
    fact_people = {
        row.person_id for row in snapshot.facts if row.person_id
    } if snapshot is not None else set()
    fact_parents = {
        row.parent_id for row in snapshot.facts
    } if snapshot is not None else set()
    per_source: dict[str, int] = {}
    with_dossiers = 0
    candidates = [row for row in people if row.person_id.startswith("candidate:")]
    for row in candidates:
        source = next(iter(row.source_channels), "unknown")
        per_source[source] = per_source.get(source, 0) + 1
        if row.person_id in fact_people or parent_by_person.get(row.person_id) in fact_parents:
            with_dossiers += 1
    return ImportReadinessCounts(
        message_people,
        CandidateCounts(len(candidates), source_counts(per_source), with_dossiers),
    )


def sqlite_counts(db: Db) -> ProjectedReadinessCounts:
    snapshot = canonical_snapshot(db)
    msg_channels = {GMAIL_CHANNEL, IMESSAGE_CHANNEL, WHATSAPP_CHANNEL}
    sources_by_person: dict[str, list[str]] = {}
    for row in snapshot.sources:
        sources_by_person.setdefault(row.person_id, []).append(row.source)
    message_people = sum(
        bool(msg_channels.intersection(sources_by_person.get(row.person_id, [])))
        for row in snapshot.people
    )
    fact_people = {row.person_id for row in snapshot.facts if row.person_id}
    per_source: dict[str, int] = {}
    total = with_dossiers = 0
    for row in snapshot.people:
        if not row.person_id.startswith("candidate:"):
            continue
        total += 1
        source = next(iter(sources_by_person.get(row.person_id, [])), "unknown")
        per_source[source] = per_source.get(source, 0) + 1
        if row.person_id in fact_people:
            with_dossiers += 1
    channel_counts: dict[str, int] = {}
    for bundle in projected_bundles(snapshot).values():
        for message in bundle.messages:
            channel = message.channel or "unknown"
            channel_counts[channel] = channel_counts.get(channel, 0) + 1
    return ProjectedReadinessCounts(
        message_people=message_people,
        candidates=CandidateCounts(total, source_counts(per_source), with_dossiers),
        messages=MessageCounts(
            sum(channel_counts.values()), source_counts(channel_counts)
        ),
        has_owner=snapshot.owner is not None,
        owner_path=snapshot.owner_path or "",
    )


class CheckReadiness:
    """Probe source stores while reading all downstream state from SQLite."""

    def __init__(
        self,
        *,
        db: Db | None = None,
        db_path: Path = CANONICAL_DB,
        people_csv: Path = DEFAULT_PEOPLE_CSV,
        msgvault_db: Path = context_sources.gni.DEFAULT_MSGVAULT_DB,
        chat_db: Path | None = None,
        wacli_db: Path = context_sources.DEFAULT_WACLI_DB,
    ) -> None:
        self.db = db
        self.db_path = db.db_path if db is not None else Path(db_path)
        self.people_csv = Path(people_csv)
        self.msgvault_db = Path(msgvault_db).expanduser()
        self.chat_db = Path(chat_db or Path.home() / "Library/Messages/chat.db").expanduser()
        self.wacli_db = Path(wacli_db)

    def run(self) -> ReadinessReport:
        load_env()
        chat: ChatDbProbe = ChatDbProbe.from_payload(
            context_sources.probe_chat_db(self.chat_db)
        )
        database_exists = self.db is not None or self.db_path.is_file()
        db: Db | None = self.db or Db(self.db_path) if database_exists else None
        snapshot: CanonicalSnapshot | None = (
            canonical_snapshot(db) if db is not None else None
        )
        has_people = bool(
            snapshot and any(not row.is_owner for row in snapshot.people)
        )
        legacy_present = legacy_artifacts_present(
            self.db_path.parent,
            self.db_path.parent.parent / "network-import/overrides/review.csv",
        )
        migration_required = legacy_present and not has_people
        imported = read_imported_people(self.people_csv)
        imported_counts = _import_counts(imported, db)
        projected = sqlite_counts(db) if db is not None else (
            ProjectedReadinessCounts(
                message_people=0,
                candidates=CandidateCounts(0, (), 0),
                messages=MessageCounts(0, ()),
                has_owner=False,
                owner_path="",
            )
        )
        has_key = bool(os.getenv("OPENAI_API_KEY"))
        checks = ReadinessChecks(
            msgvault_gmail=PathCheck(
                "ok" if self.msgvault_db.exists() else "missing",
                str(self.msgvault_db),
            ),
            imessage_chat_db=ChatDbCheck(
                chat.status, chat.messages, chat.error,
            ),
            whatsapp_wacli=PathCheck(
                "ok" if self.wacli_db.exists() else "missing_optional",
                str(self.wacli_db),
            ),
            people_csv=PeopleCsvCheck(
                "ok" if self.people_csv.is_file() else "missing",
                str(self.people_csv),
                imported_counts.message_people,
            ),
            owner_json=PathCheck(
                "present" if projected.has_owner else "absent_optional",
                projected.owner_path,
            ),
            openai_api_key=StatusCheck("present" if has_key else "missing"),
            canonical_sqlite=PathCheck(
                (
                    "migration_required" if migration_required else
                    "ok" if has_people else
                    "empty" if database_exists else "missing"
                ),
                str(self.db_path),
            ),
        )
        any_source = any(
            status == "ok"
            for status in (
                checks.msgvault_gmail.status,
                checks.imessage_chat_db.status,
                checks.whatsapp_wacli.status,
            )
        )
        ready = (
            checks.people_csv.status == "ok"
            and any_source
            and has_key
            and not migration_required
        )
        check_statuses = (
            checks.imessage_chat_db.status,
            checks.msgvault_gmail.status,
            checks.openai_api_key.status,
            checks.owner_json.status,
        )
        advice = [
            text
            for status, (prefix, text) in zip(
                check_statuses, ADVICE_RULES, strict=True
            )
            if status.startswith(prefix)
        ]
        if migration_required:
            advice.append(
                "Legacy Deep Context artifacts need one SQLite import before processing."
            )

        return ReadinessReport(
            source="check_readiness",
            status="completed",
            ready=ready,
            message_people=imported_counts.message_people,
            candidates=imported_counts.candidates,
            messages=projected.messages,
            checks=checks,
            advice=tuple(advice),
            updated_at=now_iso(),
            next_command=(
                "bin/deep-context migrate-sqlite" if migration_required else None
            ),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Readiness check for the deep-context pipeline.")
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    parser.add_argument(
        "--msgvault-db", default=str(context_sources.gni.DEFAULT_MSGVAULT_DB)
    )
    parser.add_argument("--chat-db", default=str(Path.home() / "Library/Messages/chat.db"))
    parser.add_argument("--wacli-db", default=str(context_sources.DEFAULT_WACLI_DB))
    args = parser.parse_args(argv)
    result = CheckReadiness(
        db_path=Path(args.db),
        people_csv=Path(args.people_csv),
        msgvault_db=Path(args.msgvault_db),
        chat_db=Path(args.chat_db),
        wacli_db=Path(args.wacli_db),
    ).run()
    emit(readiness_payload(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
