"""Report source readiness and projected Deep Context counts without spend."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context import sources
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
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.imported_people import (
    ImportedPerson,
    read_imported_people,
)
from packs.ingestion.primitives.deep_context.migrate_sqlite import (
    legacy_artifacts_present,
)
from packs.ingestion.primitives.common.jsonio import now_iso


ADVICE_RULES: tuple[tuple[str, str, str], ...] = (
    ("imessage_chat_db", "unreadable_full_disk_access", "iMessage blocked: grant Full Disk Access to your terminal and run in it (not via the Claude Code Bash tool)."),
    ("msgvault_gmail", "missing", "No msgvault.db — run $import-email/$msgvault to sync Gmail, or proceed with messages only."),
    ("openai_api_key", "missing", "OPENAI_API_KEY missing from environment/.env — synthesis cannot run."),
    ("owner_json", "absent", "No owner.json — add one to enable shared-context (school/employer overlap) inference."),
)


def _import_counts(
    people: tuple[ImportedPerson, ...], db: Db | None,
) -> tuple[int, dict[str, Any]]:
    msg_channels = {GMAIL_CHANNEL, IMESSAGE_CHANNEL, WHATSAPP_CHANNEL}
    message_people = sum(
        bool(msg_channels.intersection(row.source_channels))
        and bool(row.emails or row.phones)
        for row in people
    )
    snapshot = canonical_snapshot(db) if db is not None else None
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
    return message_people, {
        "total": len(candidates),
        "per_source": per_source,
        "with_dossiers": with_dossiers,
    }


def sqlite_counts(db: Db) -> tuple[int, dict[str, Any], dict[str, Any], bool, str]:
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
        for message in bundle.get("messages") or []:
            if not isinstance(message, dict):
                continue
            channel = str(message.get("channel") or "unknown")
            channel_counts[channel] = channel_counts.get(channel, 0) + 1
    return (
        message_people,
        {"total": total, "per_source": per_source, "with_dossiers": with_dossiers},
        {"total": sum(channel_counts.values()), "per_source": channel_counts},
        snapshot.owner is not None,
        snapshot.owner_path or "",
    )


class CheckReadiness:
    """Probe source stores while reading all downstream state from SQLite."""

    def __init__(
        self,
        *,
        db: Db | None = None,
        db_path: Path = CANONICAL_DB,
        people_csv: Path = DEFAULT_PEOPLE_CSV,
        msgvault_db: Path = sources.gni.DEFAULT_MSGVAULT_DB,
        chat_db: Path | None = None,
        wacli_db: Path = sources.DEFAULT_WACLI_DB,
    ) -> None:
        self.db = db
        self.db_path = db.db_path if db is not None else Path(db_path)
        self.people_csv = Path(people_csv)
        self.msgvault_db = Path(msgvault_db).expanduser()
        self.chat_db = Path(chat_db or Path.home() / "Library/Messages/chat.db").expanduser()
        self.wacli_db = Path(wacli_db)

    def run(self) -> dict[str, Any]:
        load_env()
        chat = sources.probe_chat_db(self.chat_db)
        database_exists = self.db is not None or self.db_path.is_file()
        db = self.db or Db(self.db_path) if database_exists else None
        snapshot = canonical_snapshot(db) if db is not None else None
        has_people = bool(
            snapshot and any(not row.is_owner for row in snapshot.people)
        )
        legacy_present = legacy_artifacts_present(
            self.db_path.parent,
            self.db_path.parent.parent / "network-import/overrides/review.csv",
        )
        migration_required = legacy_present and not has_people
        imported = read_imported_people(self.people_csv)
        people_n, candidates = _import_counts(imported, db)
        projected = sqlite_counts(db) if db is not None else (
            0,
            {"total": 0, "per_source": {}, "with_dossiers": 0},
            {"total": 0, "per_source": {}},
            False,
            "",
        )
        _, _, messages, has_owner, owner_path = projected
        has_key = bool(os.getenv("OPENAI_API_KEY"))
        chat_status = (
            "ok" if chat["readable"] else
            "missing" if not chat["exists"] else
            "unreadable_full_disk_access"
        )

        checks = {
            "msgvault_gmail": {"status": "ok" if self.msgvault_db.exists() else "missing", "path": str(self.msgvault_db)},
            "imessage_chat_db": {"status": chat_status, "messages": chat.get("messages", 0), "error": chat.get("error")},
            "whatsapp_wacli": {"status": "ok" if self.wacli_db.exists() else "missing_optional", "path": str(self.wacli_db)},
            "people_csv": {
                "status": "ok" if self.people_csv.is_file() else "missing",
                "path": str(self.people_csv),
                "message_people": people_n,
            },
            "owner_json": {"status": "present" if has_owner else "absent_optional", "path": owner_path},
            "openai_api_key": {"status": "present" if has_key else "missing"},
            "canonical_sqlite": {
                "status": (
                    "migration_required" if migration_required else
                    "ok" if has_people else
                    "empty" if database_exists else "missing"
                ),
                "path": str(self.db_path),
            },
        }
        any_source = any(checks[k]["status"] == "ok"
                         for k in ("msgvault_gmail", "imessage_chat_db", "whatsapp_wacli"))
        ready = (
            checks["people_csv"]["status"] == "ok"
            and any_source
            and has_key
            and not migration_required
        )
        advice = [text for check, prefix, text in ADVICE_RULES
                  if checks[check]["status"].startswith(prefix)]
        if migration_required:
            advice.append(
                "Legacy Deep Context artifacts need one SQLite import before processing."
            )

        return {
            "source": "check_readiness", "status": "completed", "ready": ready,
            "message_people": people_n, "candidates": candidates, "messages": messages,
            "checks": checks, "advice": advice, "updated_at": now_iso(),
            "next_command": "bin/deep-context migrate-sqlite" if migration_required else None,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Readiness check for the deep-context pipeline.")
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    parser.add_argument("--msgvault-db", default=str(sources.gni.DEFAULT_MSGVAULT_DB))
    parser.add_argument("--chat-db", default=str(Path.home() / "Library/Messages/chat.db"))
    parser.add_argument("--wacli-db", default=str(sources.DEFAULT_WACLI_DB))
    args = parser.parse_args(argv)
    result = CheckReadiness(
        db_path=Path(args.db),
        people_csv=Path(args.people_csv),
        msgvault_db=Path(args.msgvault_db),
        chat_db=Path(args.chat_db),
        wacli_db=Path(args.wacli_db),
    ).run()
    emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
