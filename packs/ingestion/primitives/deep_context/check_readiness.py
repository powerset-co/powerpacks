"""Readiness check for the deep-context pipeline (read-only, no spend).

Probes every connection the pipeline needs and reports per-source status so the
orchestrator can decide what's collectable before spending anything:

  - msgvault.db (Gmail bodies)        ok | missing
  - chat.db (iMessage DMs)            ok | unreadable (Full Disk Access) | missing
  - wacli.db (WhatsApp DMs)           ok | missing (optional)
  - merged people.csv                 ok (+ count of message-channel people) | missing
  - owner.json (shared-context)       present | absent (optional)
  - OPENAI_API_KEY (synthesis)        present | missing

Exit status is always 0; read `ready` (all required sources usable) in the JSON.
"""
from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context import sources
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    GMAIL_CHANNEL,
    IMESSAGE_CHANNEL,
    OWNER_JSON,
    WHATSAPP_CHANNEL,
    emit,
    load_env,
    now_iso,
)


def count_message_people(people_csv: Path) -> int:
    if not people_csv.exists():
        return 0
    msg_channels = {GMAIL_CHANNEL, IMESSAGE_CHANNEL, WHATSAPP_CHANNEL}
    n = 0
    with people_csv.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            channels = {c.strip() for c in str(row.get("source_channels") or "").split(",")}
            if channels & msg_channels:
                n += 1
    return n


def run(args: argparse.Namespace) -> dict[str, Any]:
    load_env()
    msgvault_db = Path(args.msgvault_db).expanduser()
    chat_db = Path(args.chat_db).expanduser()
    wacli_db = Path(args.wacli_db)
    people_csv = Path(args.people_csv)

    chat = sources.probe_chat_db(chat_db)
    chat_status = "ok" if chat["readable"] else ("missing" if not chat["exists"] else "unreadable_full_disk_access")
    people_n = count_message_people(people_csv)
    has_key = bool(os.getenv("OPENAI_API_KEY"))

    checks = {
        "msgvault_gmail": {"status": "ok" if msgvault_db.exists() else "missing", "path": str(msgvault_db)},
        "imessage_chat_db": {"status": chat_status, "messages": chat.get("messages", 0), "error": chat.get("error")},
        "whatsapp_wacli": {"status": "ok" if wacli_db.exists() else "missing_optional", "path": str(wacli_db)},
        "people_csv": {"status": "ok" if people_csv.exists() else "missing", "message_people": people_n},
        "owner_json": {"status": "present" if OWNER_JSON.exists() else "absent_optional", "path": str(OWNER_JSON)},
        "openai_api_key": {"status": "present" if has_key else "missing"},
    }
    # Required to do anything useful: people.csv + at least one source + an API key.
    any_source = checks["msgvault_gmail"]["status"] == "ok" or chat_status == "ok" or checks["whatsapp_wacli"]["status"] == "ok"
    ready = checks["people_csv"]["status"] == "ok" and any_source and has_key

    advice = []
    if chat_status == "unreadable_full_disk_access":
        advice.append("iMessage blocked: grant Full Disk Access to your terminal and run in it (not via the Claude Code Bash tool).")
    if checks["msgvault_gmail"]["status"] == "missing":
        advice.append("No msgvault.db — run $import-email/$msgvault to sync Gmail, or proceed with messages only.")
    if not has_key:
        advice.append("OPENAI_API_KEY missing from environment/.env — synthesis cannot run.")
    if checks["owner_json"]["status"].startswith("absent"):
        advice.append("No owner.json — add one to enable shared-context (school/employer overlap) inference.")

    return {
        "source": "check_readiness",
        "status": "completed",
        "ready": ready,
        "message_people": people_n,
        "checks": checks,
        "advice": advice,
        "updated_at": now_iso(),
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Readiness check for the deep-context pipeline.")
    p.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    p.add_argument("--msgvault-db", default=str(sources.gni.DEFAULT_MSGVAULT_DB))
    p.add_argument("--chat-db", default=str(Path.home() / "Library" / "Messages" / "chat.db"))
    p.add_argument("--wacli-db", default=str(sources.DEFAULT_WACLI_DB))
    return p


def main(argv: list[str] | None = None) -> int:
    emit(run(build_parser().parse_args(argv)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
