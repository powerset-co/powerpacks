"""Readiness check for the deep-context pipeline (read-only, no spend).

Probes every connection the pipeline needs and reports per-source status so the
orchestrator can decide what's collectable before spending anything:

  - msgvault.db (Gmail bodies)        ok | missing
  - chat.db (iMessage DMs)            ok | unreadable (Full Disk Access) | missing
  - wacli.db (WhatsApp DMs)           ok | missing (optional)
  - merged people.csv                 ok (+ count of message-channel people) | missing
  - owner.json (shared-context)       present | absent (optional)
  - OPENAI_API_KEY (synthesis)        present | missing

Flow: probe each source -> one `checks` entry per source -> `ready` (people.csv
plus at least one source plus a key) -> `advice`, which is exactly the
`ADVICE_RULES` table below filtered by those statuses, in table order.

Exit status is always 0; read `ready` (all required sources usable) in the JSON.

Changelog:
  2026-07-30 (house style): `run(args)` became the construct-and-run
    `CheckReadiness` class and `main()` a thin argparse entry; the advice
    if-ladder became the `ADVICE_RULES` table and the chat.db ternary the
    first-rule-wins `chat_db_status`. Same `ready` logic, same checks key order,
    same status spellings, same advice sentences in the same order; no behavior
    change. The default chat.db is the `default_chat_db()` callable resolved per
    call (the constructor / the parser default), not a `Path.home()` frozen at
    import — matching the pre-class behavior.
  2026-07-23 (audit dedup): now_iso import from common.jsonio instead of deep_context.common (deduped there); no behavior change.
"""
from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context import sources
from packs.ingestion.primitives.deep_context.candidates import (
    candidate_person_id,
    iter_candidate_rows,
)
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    FACTS_DIR,
    GMAIL_CHANNEL,
    IMESSAGE_CHANNEL,
    OWNER_JSON,
    WHATSAPP_CHANNEL,
    emit,
    load_env,
)
from packs.ingestion.primitives.common.jsonio import now_iso


def default_chat_db() -> Path:
    """The macOS iMessage store under the CURRENT home.

    A callable, not a module constant: `Path.home()` reads `$HOME`, and resolving
    it at import time froze whatever the interpreter started with — so a caller
    (or a test) that sets HOME afterwards was silently ignored.
    """
    return Path.home() / "Library" / "Messages" / "chat.db"

# The whole advice policy, in output order: a rule fires when the named check's
# status starts with its prefix ("absent" covers "absent_optional"), and every
# rule that fires appends its sentence. Nothing else may add advice.
ADVICE_RULES: tuple[tuple[str, str, str], ...] = (
    ("imessage_chat_db", "unreadable_full_disk_access",
     "iMessage blocked: grant Full Disk Access to your terminal and run in it (not via the Claude Code Bash tool)."),
    ("msgvault_gmail", "missing",
     "No msgvault.db — run $import-email/$msgvault to sync Gmail, or proceed with messages only."),
    ("openai_api_key", "missing",
     "OPENAI_API_KEY missing from environment/.env — synthesis cannot run."),
    ("owner_json", "absent",
     "No owner.json — add one to enable shared-context (school/employer overlap) inference."),
)


def chat_db_status(probe: dict[str, Any]) -> str:
    """First rule wins: opened and read -> ok, no file -> missing, otherwise the
    file is there but sqlite could not read it, i.e. a Full Disk Access denial."""
    if probe["readable"]:
        return "ok"
    if not probe["exists"]:
        return "missing"
    return "unreadable_full_disk_access"


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


def count_candidates(facts_dir: Path = FACTS_DIR) -> dict[str, Any]:
    """Import-candidate pool scope: per-source counts + how many already have facts."""
    per_source: dict[str, int] = {}
    total = with_dossiers = 0
    for row in iter_candidate_rows():
        total += 1
        source = str(row.get("source") or "unknown").strip().lower() or "unknown"
        per_source[source] = per_source.get(source, 0) + 1
        pid = candidate_person_id(str(row.get("candidate_key") or "").strip())
        if (facts_dir / f"{pid}.jsonl").exists():
            with_dossiers += 1
    return {"total": total, "per_source": per_source, "with_dossiers": with_dossiers}


class CheckReadiness:
    """Probes the four stores plus owner.json and the synthesis key.

    Read-only everywhere: it opens chat.db to prove readability and stats the
    other paths. Only msgvault_db and chat_db are user-expanded, matching the
    paths those tools themselves accept.
    """

    def __init__(
        self,
        *,
        people_csv: Path = DEFAULT_PEOPLE_CSV,
        msgvault_db: Path = sources.gni.DEFAULT_MSGVAULT_DB,
        chat_db: Path | None = None,
        wacli_db: Path = sources.DEFAULT_WACLI_DB,
    ) -> None:
        self.people_csv = Path(people_csv)
        self.msgvault_db = Path(msgvault_db).expanduser()
        self.chat_db = Path(chat_db or default_chat_db()).expanduser()
        self.wacli_db = Path(wacli_db)

    def run(self) -> dict[str, Any]:
        load_env()
        chat = sources.probe_chat_db(self.chat_db)
        people_n = count_message_people(self.people_csv)
        has_key = bool(os.getenv("OPENAI_API_KEY"))

        checks = {
            "msgvault_gmail": {"status": "ok" if self.msgvault_db.exists() else "missing", "path": str(self.msgvault_db)},
            "imessage_chat_db": {"status": chat_db_status(chat), "messages": chat.get("messages", 0), "error": chat.get("error")},
            "whatsapp_wacli": {"status": "ok" if self.wacli_db.exists() else "missing_optional", "path": str(self.wacli_db)},
            "people_csv": {"status": "ok" if self.people_csv.exists() else "missing", "message_people": people_n},
            "owner_json": {"status": "present" if OWNER_JSON.exists() else "absent_optional", "path": str(OWNER_JSON)},
            "openai_api_key": {"status": "present" if has_key else "missing"},
        }
        # Required to do anything useful: people.csv + at least one source + an API key.
        any_source = any(checks[k]["status"] == "ok"
                         for k in ("msgvault_gmail", "imessage_chat_db", "whatsapp_wacli"))
        ready = checks["people_csv"]["status"] == "ok" and any_source and has_key
        advice = [text for check, prefix, text in ADVICE_RULES
                  if checks[check]["status"].startswith(prefix)]

        return {
            "source": "check_readiness",
            "status": "completed",
            "ready": ready,
            "message_people": people_n,
            "candidates": count_candidates(),
            "checks": checks,
            "advice": advice,
            "updated_at": now_iso(),
        }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Readiness check for the deep-context pipeline.")
    p.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    p.add_argument("--msgvault-db", default=str(sources.gni.DEFAULT_MSGVAULT_DB))
    p.add_argument("--chat-db", default=str(default_chat_db()))
    p.add_argument("--wacli-db", default=str(sources.DEFAULT_WACLI_DB))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    emit(CheckReadiness(
        people_csv=Path(args.people_csv),
        msgvault_db=Path(args.msgvault_db),
        chat_db=Path(args.chat_db),
        wacli_db=Path(args.wacli_db),
    ).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
