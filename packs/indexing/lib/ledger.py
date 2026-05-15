"""JSON ledger helpers for resumable local indexing runs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from packs.indexing.lib.io import append_jsonl, read_jsonl, write_json, read_json


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_ledger(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    ledger = read_json(p)
    ledger.setdefault("primitive", "build_processing_pipeline")
    ledger.setdefault("version", 1)
    ledger.setdefault("status", "pending")
    ledger.setdefault("steps", [])
    ledger.setdefault("artifacts", {})
    return ledger


def save_ledger(path: str | Path, ledger: dict[str, Any]) -> dict[str, Any]:
    write_json(path, ledger)
    return ledger


def mark_step(path: str | Path, ledger: dict[str, Any], step_id: str, status: str, **metadata: Any) -> dict[str, Any]:
    steps = ledger.setdefault("steps", [])
    step = next((s for s in steps if s.get("id") == step_id), None)
    if step is None:
        step = {"id": step_id}
        steps.append(step)
    step.update({"status": status, "updated_at": now_iso()})
    if metadata:
        step.update(metadata)
        if "artifacts" in metadata and isinstance(metadata["artifacts"], dict):
            ledger.setdefault("artifacts", {}).update(metadata["artifacts"])
    if all(s.get("status") == "completed" for s in steps):
        ledger["status"] = "completed"
    else:
        ledger["status"] = "running" if any(s.get("status") == "completed" for s in steps) else "pending"
    save_ledger(path, ledger)
    return ledger


def next_pending_step(ledger: dict[str, Any], steps: list[str] | tuple[str, ...]) -> str | None:
    by_id = {s.get("id"): s for s in ledger.get("steps", [])}
    for step_id in steps:
        if by_id.get(step_id, {}).get("status") != "completed":
            return step_id
    return None


@dataclass(frozen=True)
class LedgerEntry:
    key: str
    status: str
    payload_hash: str
    recorded_at: str
    metadata: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class IndexLedger:
    """Compatibility JSONL ledger used by older scaffold tests."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def entries(self) -> list[dict[str, Any]]:
        return read_jsonl(self.path)

    def latest_by_key(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for entry in self.entries():
            latest[str(entry.get("key", ""))] = entry
        latest.pop("", None)
        return latest

    def record(self, key: str, payload: dict[str, Any], *, status: str = "prepared", **metadata: Any) -> LedgerEntry:
        entry = LedgerEntry(key, status, stable_payload_hash(payload), now_iso(), metadata)
        append_jsonl(self.path, [entry.as_dict()])
        return entry

    def has_current(self, key: str, payload: dict[str, Any]) -> bool:
        latest = self.latest_by_key().get(key)
        return bool(latest and latest.get("payload_hash") == stable_payload_hash(payload))
