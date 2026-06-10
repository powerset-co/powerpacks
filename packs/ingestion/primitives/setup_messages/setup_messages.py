#!/usr/bin/env python3
"""Run the Messages (iMessage/WhatsApp) vertical end-to-end.

Stages:
  1. inspect    – Check iMessage/WhatsApp access
  2. discover   – Extract contacts from linked message sources
  3. llm_review – LLM decides who's worth enriching
  4. user_review – User reviews contacts (blocks for user action)
  5. enrich     – Parallel.ai LinkedIn resolution
  6. source_people – Write messages people.csv
  7. merge_network – Merge into unified people.csv
  8. network_duckdb – Prepare contact lookup database
  9. index_estimate – Estimate search updates
 10. index_records – Build searchable people records
 11. search_duckdb – Update local search database
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from packs.ingestion.primitives.discover_contacts_pipeline.common import read_json  # noqa: E402
from packs.ingestion.primitives.import_contacts_pipeline.common import (  # noqa: E402
    DEFAULT_ACCOUNTS,
    DEFAULT_BASE_DIR,
    DEFAULT_IMPORT_DIR,
)
from packs.indexing.primitives.index_contacts_pipeline import index_contacts_pipeline  # noqa: E402

VERTICAL = "messages"
RUN_ROOT = Path(".powerpacks/runs/setup-messages")
MESSAGES_STATE = Path(".powerpacks/messages")
IMPORT_LEDGER = MESSAGES_STATE / "import-run.setup-messages.json"
REVIEW_CSV = MESSAGES_STATE / "research_review.csv"
DISCOVER_DIR = DEFAULT_BASE_DIR / "discover" / "messages"

STAGES = [
    {"id": "inspect", "label": "Check message sources"},
    {"id": "discover", "label": "Discover message contacts"},
    {"id": "llm_review", "label": "AI contact review"},
    {"id": "user_review", "label": "Review contacts"},
    {"id": "enrich", "label": "Enrich message contacts"},
    {"id": "source_people", "label": "Save message people file"},
    {"id": "merge_network", "label": "Merge contact sources"},
    {"id": "network_duckdb", "label": "Prepare contact lookup database"},
    {"id": "index_estimate", "label": "Estimate search updates"},
    {"id": "index_records", "label": "Build searchable people records"},
    {"id": "search_duckdb", "label": "Update local search database"},
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, default=str), flush=True)


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    tmp.rename(path)


def valid_run_id(run_id: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_:-]{0,127}", run_id))


@dataclass
class RunContext:
    run_id: str
    state_path: Path
    events_path: Path
    status: dict[str, Any]

    def update(self, **fields: Any) -> None:
        self.status.update(fields)
        self.status["updated_at"] = now_iso()
        atomic_write_json(self.state_path, self.status)

    def event(self, stage_id: str, message: str, *, status: str = "running", progress: float | None = None, payload: dict[str, Any] | None = None) -> None:
        stage_index = next((idx + 1 for idx, stage in enumerate(STAGES) if stage["id"] == stage_id), 0)
        stage_label = next((stage["label"] for stage in STAGES if stage["id"] == stage_id), stage_id)
        if progress is None:
            if status == "completed":
                progress = stage_index / len(STAGES) if stage_index else 0
            else:
                progress = max(stage_index - 1, 0) / len(STAGES) if stage_index else 0
        event = {
            "run_id": self.run_id,
            "vertical": VERTICAL,
            "status": status,
            "stage": stage_id,
            "stage_label": stage_label,
            "stage_index": stage_index,
            "stage_total": len(STAGES),
            "message": message,
            "progress": progress,
            "updated_at": now_iso(),
            "payload": payload or {},
        }
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
        print(f"setup: {stage_label} ({stage_index}/{len(STAGES)})", flush=True)
        emit_json({"event": "progress", **event})
        stages = dict(self.status.get("stages") or {})
        stages[stage_id] = {"status": status, "label": stage_label, "message": message, "updated_at": event["updated_at"], "payload": payload or {}}
        if status in {"failed", "blocked_approval", "blocked_user_action"}:
            overall_status = status
        elif status == "completed" and stage_index == len(STAGES):
            overall_status = "completed"
        else:
            overall_status = "running"
        self.update(status=overall_status, current_stage=stage_id, progress=event["progress"], stages=stages)


def _completed_stages(ctx: RunContext) -> set[str]:
    stages = ctx.status.get("stages") or {}
    return {sid for sid, detail in stages.items() if isinstance(detail, dict) and detail.get("status") == "completed"}


def make_context(*, resume: bool = False) -> RunContext:
    rid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    state_path = RUN_ROOT / "status.json"
    events_path = RUN_ROOT / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    if resume and state_path.exists():
        try:
            status = json.loads(state_path.read_text(encoding="utf-8"))
            status["status"] = "running"
            status["updated_at"] = now_iso()
        except (json.JSONDecodeError, OSError):
            status = None
    else:
        status = None
    if status is None:
        status = {
            "schema_version": 1,
            "vertical": VERTICAL,
            "run_id": rid,
            "status": "running",
            "started_at": now_iso(),
            "updated_at": now_iso(),
            "progress": 0,
            "stages": {},
            "stage_order": STAGES,
        }
        events_path.write_text("", encoding="utf-8")
    ctx = RunContext(rid, state_path, events_path, status)
    ctx.update()
    return ctx


def args_for_index(operator_id: str) -> argparse.Namespace:
    return argparse.Namespace(
        people=str(index_contacts_pipeline.DEFAULT_PEOPLE_CSV),
        output=str(index_contacts_pipeline.DEFAULT_OUTPUT_DIR),
        manifest=str(index_contacts_pipeline.DEFAULT_MANIFEST),
        openai_usage_tier=os.getenv("POWERPACKS_OPENAI_USAGE_TIER") or None,
        input=[],
        include_existing_artifacts=True,
    )


def _check_imessage_access() -> dict[str, Any]:
    """Check if iMessage chat.db is readable."""
    try:
        result = subprocess.run(
            ["uv", "run", "--project", ".", "python",
             "packs/messages/primitives/extract_imessage_contacts/extract_imessage_contacts.py",
             "check"],
            capture_output=True, text=True, timeout=15, cwd=str(ROOT),
        )
        output = result.stdout + result.stderr
        payload = {}
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    pass
        readable = payload.get("readable", False) or "readable" in output.lower()
        return {"available": True, "readable": readable, "payload": payload}
    except (subprocess.TimeoutExpired, OSError):
        return {"available": False, "readable": False}


def _run_messages_discovery(accounts_path: Path) -> dict[str, Any]:
    """Run the messages import pipeline in discovery-only mode."""
    cmd = [
        "uv", "run", "--project", ".", "python",
        "packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py",
        "run",
        "--ledger", str(IMPORT_LEDGER),
        "--reuse-existing-artifacts",
        "--include-imessage",
        "--include-whatsapp",
        "--include-contact-merge",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=10 * 60, cwd=str(ROOT),
    )
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    return {"status": "failed", "error": result.stderr or "no output", "code": result.returncode}


def _run_llm_review() -> dict[str, Any]:
    """Run the LLM review step (powerset candidates + local match + llm review)."""
    cmd = [
        "uv", "run", "--project", ".", "python",
        "packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py",
        "continue",
        "--ledger", str(IMPORT_LEDGER),
        "--reuse-existing-artifacts",
        "--include-imessage",
        "--include-whatsapp",
        "--include-contact-merge",
        "--include-powerset-candidates",
        "--include-local-match",
        "--include-llm-review",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=30 * 60, cwd=str(ROOT),
    )
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    return {"status": "failed", "error": result.stderr or "no output", "code": result.returncode}


def _run_deep_research(approve_spend: bool = False) -> dict[str, Any]:
    """Run Parallel.ai deep research on the research queue."""
    cmd = [
        "uv", "run", "--project", ".", "python",
        "packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py",
        "continue",
        "--ledger", str(IMPORT_LEDGER),
        "--reuse-existing-artifacts",
        "--include-imessage",
        "--include-whatsapp",
        "--include-contact-merge",
        "--include-powerset-candidates",
        "--include-local-match",
        "--include-llm-review",
        "--include-research",
    ]
    if approve_spend:
        cmd.append("--approve-parallel-spend")
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=60 * 60, cwd=str(ROOT),
    )
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    return {"status": "failed", "error": result.stderr or "no output", "code": result.returncode}


def _count_review_rows() -> dict[str, Any]:
    """Count rows in research_review.csv by status."""
    if not REVIEW_CSV.exists():
        return {"total": 0, "exists": False}
    try:
        from packs.ingestion.primitives.discover_contacts_pipeline.common import read_csv_rows
        _, rows = read_csv_rows(REVIEW_CSV)
        from packs.ingestion.primitives.discover_contacts_pipeline.messages import (
            explicitly_approved_message_review_row,
            messages_review_row_rejected,
            messages_review_row_in_network,
        )
        approved = sum(1 for r in rows if explicitly_approved_message_review_row(r))
        rejected = sum(1 for r in rows if messages_review_row_rejected(r))
        in_network = sum(1 for r in rows if messages_review_row_in_network(r))
        return {"total": len(rows), "approved": approved, "rejected": rejected, "in_network": in_network, "exists": True}
    except Exception:
        return {"total": 0, "exists": True, "error": "failed to parse"}


def _run_messages_import(operator_id: str, accounts_path: Path) -> dict[str, Any]:
    """Run the messages import/enrichment after review is done."""
    from packs.ingestion.primitives.import_contacts_pipeline import messages as messages_import
    ns = argparse.Namespace(
        command="run",
        accounts=accounts_path,
        operator_id=operator_id,
        confirm_import=True,
    )
    return messages_import.run(ns)


def run(args: argparse.Namespace) -> dict[str, Any]:
    continuing = getattr(args, "continue_run", False)
    ctx = make_context(resume=continuing)
    accounts_path = Path(str(args.accounts)).expanduser()
    done = _completed_stages(ctx) if continuing else set()
    try:
        # --- inspect ---
        if "inspect" not in done:
            ctx.event("inspect", "Checking message sources")
            imessage = _check_imessage_access()
            sources = []
            if imessage.get("readable"):
                sources.append("iMessage")
            # Check WhatsApp from accounts.json (linking actions write back to it)
            accounts = read_json(accounts_path, {}) or {}
            messages_cfg = (accounts.get("accounts", {}).get("messages", {}).get("config", {}) or {})
            whatsapp_cfg = messages_cfg.get("whatsapp", {}) or {}
            if whatsapp_cfg.get("authenticated") or whatsapp_cfg.get("status") in ("authenticated", "linked"):
                sources.append("WhatsApp")
            if not sources:
                ctx.event("inspect", "No message sources available (iMessage needs Full Disk Access, WhatsApp needs linking)", status="failed")
                ctx.update(status="failed", error="no_message_sources", completed_at=now_iso())
                return {"status": "failed", "vertical": VERTICAL, "error": "no_message_sources"}

            # Write source status back to accounts.json so downstream primitives see messages as linked
            has_whatsapp = "WhatsApp" in sources
            messages_record = accounts.get("accounts", {}).get("messages", {})
            config = messages_record.get("config", {}) if isinstance(messages_record.get("config"), dict) else {}
            config["imessage"] = {"readable": imessage.get("readable", False), "status": "ready" if imessage.get("readable") else "not_ready"}
            if has_whatsapp:
                wa = config.get("whatsapp", {}) if isinstance(config.get("whatsapp"), dict) else {}
                wa["authenticated"] = True
                wa["status"] = "authenticated"
                config["whatsapp"] = wa
            messages_record["config"] = config
            messages_record["linked"] = True
            messages_record["skipped"] = False
            accounts.setdefault("accounts", {})["messages"] = messages_record
            accounts["updated_at"] = now_iso()
            accounts_path.write_text(json.dumps(accounts, indent=2) + "\n")

            ctx.event("inspect", f"Message sources ready: {', '.join(sources)}", status="completed",
                       payload={"sources": sources, "imessage": imessage})

        # --- discover ---
        if "discover" not in done:
            ctx.event("discover", "Extracting contacts from message sources")
            discovery_payload = _run_messages_discovery(accounts_path)
            status = discovery_payload.get("status", "")
            if status == "failed":
                raise RuntimeError(discovery_payload.get("error") or "Messages discovery failed")
            contacts_csv = DISCOVER_DIR / "contacts.csv"
            contact_count = 0
            if contacts_csv.exists():
                contact_count = sum(1 for _ in open(contacts_csv)) - 1
            ctx.event("discover", f"Discovered {contact_count} message contacts", status="completed",
                       payload={"contacts": contact_count})

        # --- llm_review ---
        if "llm_review" not in done:
            ctx.event("llm_review", "Running AI review to identify contacts worth enriching")
            llm_payload = _run_llm_review()
            status = llm_payload.get("status", "")
            if status == "failed":
                raise RuntimeError(llm_payload.get("error") or "LLM review failed")
            ctx.event("llm_review", "AI review complete", status="completed", payload={"llm": llm_payload.get("status")})

        # --- user_review ---
        if "user_review" not in done:
            review_counts = _count_review_rows()
            if review_counts.get("total", 0) > 0:
                ctx.event("user_review", f"Review {review_counts['total']} contacts — approve or reject before enrichment",
                           status="blocked_user_action",
                           payload={"review_counts": review_counts, "review_csv": str(REVIEW_CSV)})
                ctx.update(status="blocked_user_action", completed_at=now_iso())
                return {
                    "status": "blocked_user_action",
                    "vertical": VERTICAL,
                    "review_counts": review_counts,
                    "review_csv": str(REVIEW_CSV),
                }
            else:
                ctx.event("user_review", "No contacts to review", status="completed")

        # --- enrich ---
        if "enrich" not in done:
            approve_spend = getattr(args, "approve_spend", False)
            review_counts = _count_review_rows()
            approved = review_counts.get("approved", 0)

            if approved > 0 and not approve_spend:
                cost_usd = round(approved * 0.05, 2)
                ctx.event("enrich", f"Approve ~${cost_usd:.2f} Parallel.ai spend for {approved} contacts?",
                           status="blocked_approval",
                           payload={"parallel_spend_estimate": {"pending_contacts": approved, "estimated_usd": cost_usd}})
                ctx.update(status="blocked_approval", completed_at=now_iso())
                return {
                    "status": "blocked_approval",
                    "vertical": VERTICAL,
                    "parallel_spend_estimate": {"pending_contacts": approved, "estimated_usd": cost_usd},
                }

            ctx.event("enrich", f"Importing and enriching {approved} reviewed contacts")
            import_payload = _run_messages_import(args.operator_id, accounts_path)
            import_status = import_payload.get("status")
            if import_status == "blocked_approval":
                ctx.event("enrich", "Messages enrichment needs approval", status="blocked_approval", payload=import_payload)
                ctx.update(status="blocked_approval", completed_at=now_iso())
                return {"status": "blocked_approval", "vertical": VERTICAL, "import": import_payload}
            if import_status not in ("completed", "skipped"):
                raise RuntimeError(import_payload.get("reason") or import_payload.get("error") or f"Messages import failed: {import_status}")
            people_count = (import_payload.get("stats") or {}).get("people", 0)
            ctx.event("enrich", f"Enriched {people_count} message contacts", status="completed",
                       payload={"people": people_count, "import": import_payload.get("status")})

        # --- source_people ---
        people_csv = str(DEFAULT_IMPORT_DIR / "messages" / "people.csv")
        ctx.event("source_people", "Messages people file is ready", status="completed", payload={"people_csv": people_csv})

        # --- index ---
        def index_progress(stage_id: str, message: str, status: str, payload: dict[str, Any] | None) -> None:
            ctx.event(stage_id, message, status=status, payload=payload or {})

        index_payload, index_code = index_contacts_pipeline.run_pipeline(args_for_index(args.operator_id), progress_callback=index_progress)
        if index_code != 0:
            raise RuntimeError(index_payload.get("error") or "Indexing failed")

        result = {
            "status": "completed",
            "vertical": VERTICAL,
            "outputs": {
                "people_csv": people_csv,
                "merged_people_csv": str(index_contacts_pipeline.DEFAULT_PEOPLE_CSV),
                "duckdb": index_payload.get("duckdb") or str(index_contacts_pipeline.DEFAULT_OUTPUT_DIR / "local-search.duckdb"),
            },
        }
        ctx.event("search_duckdb", "Message contacts are searchable", status="completed", progress=1.0, payload=result["outputs"])
        ctx.update(status="completed", progress=1.0, result=result, completed_at=now_iso())
        return result
    except Exception as exc:
        payload = {"status": "failed", "vertical": VERTICAL, "error": str(exc)}
        ctx.event(str(ctx.status.get("current_stage") or "inspect"), str(exc), status="failed", payload=payload)
        ctx.update(status="failed", error=str(exc), completed_at=now_iso())
        return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Messages onboarding v2 vertical")
    sub = parser.add_subparsers(dest="command", required=True)
    for cmd in ["dry-run", "run"]:
        s = sub.add_parser(cmd)
        s.add_argument("--operator-id", default="local")
        s.add_argument("--accounts", default=str(DEFAULT_ACCOUNTS))
        s.add_argument("--approve-spend", action="store_true", default=False)
        s.add_argument("--max-enrich", type=int, default=0)
        s.add_argument("--continue", dest="continue_run", action="store_true", default=False)
    status = sub.add_parser("status")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "status":
        path = RUN_ROOT / "status.json"
        if path.exists():
            emit_json(json.loads(path.read_text(encoding="utf-8")))
        else:
            emit_json({"status": "missing", "vertical": VERTICAL})
        return 0
    if args.command == "dry-run":
        review_counts = _count_review_rows()
        emit_json({"status": "ok", "vertical": VERTICAL, "review_counts": review_counts})
        return 0
    payload = run(args)
    emit_json(payload)
    return 0 if payload.get("status") == "completed" else 21 if payload.get("status") == "blocked_user_action" else 20 if payload.get("status") == "blocked_approval" else 1


if __name__ == "__main__":
    raise SystemExit(main())
