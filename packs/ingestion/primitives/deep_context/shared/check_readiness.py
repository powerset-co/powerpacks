"""Report source readiness and projected Deep Context counts without spend.

Scoped deliberately: per AGENTS.md's health-check policy, `$deep-context` runs
this narrow probe (msgvault/Gmail, chat.db/iMessage, wacli/WhatsApp, people.csv,
OPENAI_API_KEY, TYPESAFE_API_KEY, owner.json, the canonical SQLite db) on every
invocation instead
of the full `bin/doctor`, which is broader and reserved for concrete setup
failures, not routine readiness checks.

`next_command` is the first unmet step, first rule wins: ensure-parents (no
store, or a store with no people) → seed (legacy artifacts beside a store that
has not carried them over) → owner (no owner profile; synthesis requires one)
→ none.

Changelog:
- 2026-09-25: a legacy install routes to ensure-parents then seed; migrate-sqlite
  is no longer a next command. ensure-parents creates a missing store.
- 2026-09-25: next_command routes an empty store to ensure-parents and a
  missing owner profile to the owner command; owner.json is reported
  `absent`, not `absent_optional`.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from packs.ingestion.primitives.deep_context.collection import context_sources
from packs.ingestion.primitives.deep_context.collection.models import ChatDbProbe
from packs.ingestion.primitives.deep_context.collection.planning import projected_bundles
from packs.ingestion.primitives.deep_context.shared.common import (
    CANONICAL_DB,
    DEFAULT_PEOPLE_CSV,
    OWNER_JSON,
    emit,
    load_env,
)
from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.models import MESSAGE_CHANNELS
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import (
    ImportedPerson,
    read_imported_people,
)
from packs.ingestion.primitives.deep_context.migration.seed import (
    carried_over_at,
    legacy_decisions_present,
)
from packs.ingestion.primitives.deep_context.shared.readiness_models import (
    CandidateCounts,
    ChatDbCheck,
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
from packs.ingestion.primitives.common.legacy import scrub_august_deep_context_store

ENSURE_PARENTS_COMMAND = "bin/deep-context ensure-parents"
SEED_COMMAND = "bin/deep-context seed"
OWNER_COMMAND = "bin/deep-context owner --linkedin-url <url> --email <email>"

# Paired positionally with the `check_statuses` tuple built in run() — same
# order (imessage, msgvault, openai key, typesafe key), not matched
# by name.
# Reordering one without the other silently attaches the wrong advice line.
ADVICE_RULES: tuple[tuple[str, str], ...] = (
    (
        "unreadable_full_disk_access",
        "iMessage blocked: grant Full Disk Access to your terminal and run in it (not via the Claude Code Bash tool).",
    ),
    ("missing", "No msgvault.db — run $import-email/$msgvault to sync Gmail, or proceed with messages only."),
    ("missing", "OPENAI_API_KEY missing from environment/.env — synthesis cannot run."),
    ("missing", "TYPESAFE_API_KEY missing from environment/.env — worth labels and the merge judge cannot run."),
)


def _next_command(*, has_people: bool, seed_required: bool, has_owner: bool, owner_command: str) -> str | None:
    if not has_people:
        return ENSURE_PARENTS_COMMAND
    if seed_required:
        return SEED_COMMAND
    if not has_owner:
        return owner_command
    return None


def _import_counts(
    people: tuple[ImportedPerson, ...],
    db: Db | None,
) -> ImportReadinessCounts:
    message_people = sum(
        bool(MESSAGE_CHANNELS.intersection(row.source_channels)) and bool(row.emails or row.phones) for row in people
    )
    projected_people = queries.people(db) if db is not None else ()
    projected_facts = queries.facts(db) if db is not None else ()
    parent_by_person = {row.person_id: row.parent_id for row in projected_people}
    fact_people = {row.person_id for row in projected_facts if row.person_id}
    fact_parents = {row.parent_id for row in projected_facts}
    per_source: dict[str, int] = {}
    with_dossiers = 0
    candidates = [row for row in people if row.person_id.startswith("candidate:")]
    for row in candidates:
        source = next(iter(row.source_channels), "unknown")
        per_source[source] = per_source.get(source, 0) + 1
        # A candidate counts as having a dossier either way: a fact recorded directly
        # against its own person_id, or one recorded at the parent it was merged into.
        if row.person_id in fact_people or parent_by_person.get(row.person_id) in fact_parents:
            with_dossiers += 1
    return ImportReadinessCounts(
        message_people,
        CandidateCounts(len(candidates), source_counts(per_source), with_dossiers),
    )


def sqlite_counts(db: Db) -> ProjectedReadinessCounts:
    """Project message/candidate/owner counts straight from SQLite.

    run() only reads .messages/.has_owner/.owner_path off the result; the
    .message_people/.candidates computation below runs on every readiness
    check regardless (a test asserts on them — see test_deep_context_owner_projection.py).
    """
    projected_people = queries.people(db)
    projected_facts = queries.facts(db)
    sources_by_person: dict[str, list[str]] = {}
    for row in queries.sources(db):
        sources_by_person.setdefault(row.person_id, []).append(row.source)
    message_people = sum(
        bool(MESSAGE_CHANNELS.intersection(sources_by_person.get(row.person_id, []))) for row in projected_people
    )
    fact_people = {row.person_id for row in projected_facts if row.person_id}
    per_source: dict[str, int] = {}
    total = with_dossiers = 0
    for row in projected_people:
        if not row.person_id.startswith("candidate:"):
            continue
        total += 1
        source = next(iter(sources_by_person.get(row.person_id, [])), "unknown")
        per_source[source] = per_source.get(source, 0) + 1
        if row.person_id in fact_people:
            with_dossiers += 1
    channel_counts: dict[str, int] = {}
    for bundle in projected_bundles(db).values():
        for message in bundle.messages:
            channel = message.channel or "unknown"
            channel_counts[channel] = channel_counts.get(channel, 0) + 1
    return ProjectedReadinessCounts(
        message_people=message_people,
        candidates=CandidateCounts(total, source_counts(per_source), with_dossiers),
        messages=MessageCounts(sum(channel_counts.values()), source_counts(channel_counts)),
        has_owner=queries.owner_profile(db) is not None,
        owner_path=queries.owner_path(db) or "",
    )


class CheckReadiness:
    """Probe source stores while reading all downstream state from SQLite."""

    # Readiness accepts a path because reporting a missing canonical database is
    # its job; unlike a processing stage, it must not require the store to exist.
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
        scrub_august_deep_context_store(self.db_path)
        load_env()
        chat: ChatDbProbe = context_sources.probe_chat_db(self.chat_db)
        database_exists = self.db is not None or self.db_path.is_file()
        db: Db | None = self.db or Db(self.db_path) if database_exists else None
        has_people = bool(db is not None and any(not row.is_owner for row in queries.people(db)))
        legacy_present = legacy_decisions_present(self.db_path.parent.parent)
        seed_required = legacy_present and has_people and carried_over_at(db) is None
        # Two different questions, two different sources: imported_counts answers
        # "what did we import" from people.csv (below, message_people/candidates on
        # the report); projected answers "what did we actually collect" from SQLite
        # (only its .messages/.has_owner/.owner_path are used — see sqlite_counts).
        imported = read_imported_people(self.people_csv)
        imported_counts = _import_counts(imported, db)
        # Before the store exists, owner.json has not been imported yet: read the file.
        owner_json = self.db_path.parent / OWNER_JSON.name
        owner_command = "bin/deep-context owner" if owner_json.is_file() else OWNER_COMMAND
        projected = (
            sqlite_counts(db)
            if db is not None
            else (
                ProjectedReadinessCounts(
                    message_people=0,
                    candidates=CandidateCounts(0, (), 0),
                    messages=MessageCounts(0, ()),
                    has_owner=owner_json.is_file(),
                    owner_path=str(owner_json) if owner_json.is_file() else "",
                )
            )
        )
        has_key = bool(os.getenv("OPENAI_API_KEY"))
        has_typesafe_key = bool(os.getenv("TYPESAFE_API_KEY"))
        checks = ReadinessChecks(
            msgvault_gmail=PathCheck(
                "ok" if self.msgvault_db.exists() else "missing",
                str(self.msgvault_db),
            ),
            imessage_chat_db=ChatDbCheck(
                chat.status,
                chat.messages,
                chat.error,
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
                "present" if projected.has_owner else "absent",
                projected.owner_path,
            ),
            openai_api_key=StatusCheck("present" if has_key else "missing"),
            typesafe_api_key=StatusCheck("present" if has_typesafe_key else "missing"),
            canonical_sqlite=PathCheck(
                (
                    "seed_required"
                    if seed_required
                    else "ok"
                    if has_people
                    else "empty"
                    if database_exists
                    else "missing"
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
            and has_typesafe_key
            and has_people
            and not seed_required
            and projected.has_owner
        )
        # Order must track ADVICE_RULES above exactly — see the comment there.
        check_statuses = (
            checks.imessage_chat_db.status,
            checks.msgvault_gmail.status,
            checks.openai_api_key.status,
            checks.typesafe_api_key.status,
        )
        advice = [
            text
            for status, (prefix, text) in zip(check_statuses, ADVICE_RULES, strict=True)
            if status.startswith(prefix)
        ]
        if not projected.has_owner:
            advice.append(f"No owner profile — synthesis requires one: run {owner_command}.")
        if seed_required:
            advice.append(f"Legacy Deep Context artifacts are not carried over yet: run {SEED_COMMAND}.")
        elif not database_exists:
            advice.append(f"No Deep Context database yet: {ENSURE_PARENTS_COMMAND} creates it.")

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
            next_command=_next_command(
                has_people=has_people, seed_required=seed_required, has_owner=projected.has_owner,
                owner_command=owner_command,
            ),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Readiness check for the deep-context pipeline.")
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    parser.add_argument("--msgvault-db", default=str(context_sources.gni.DEFAULT_MSGVAULT_DB))
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
