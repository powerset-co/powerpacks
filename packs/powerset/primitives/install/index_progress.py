"""Read current indexing progress from the Modal dispatcher's existing fixed files."""

import json
import math
from datetime import datetime
from pathlib import Path

_PROGRESS_PATHS = (
    ".powerpacks/runs/setup-linkedin-modal/status.json",
    ".powerpacks/runs/setup-gmail-modal/status.json",
)


def _timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("progress timestamp has no timezone")
    return timestamp


def read_index_progress(root: Path, started_at: str) -> dict | None:
    """Ignore previous runs; preserve the newest current producer's status and counts."""
    newest = _timestamp(started_at)
    selected = None
    for relative in _PROGRESS_PATHS:
        path = root / relative
        if not path.is_file():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            candidate_started = _timestamp(record["started_at"])
            if candidate_started < newest:
                continue
            status, stage, progress = record["status"], record["current_stage"], record["progress"]
            if status not in {"running", "failed", "completed"}:
                raise ValueError("unknown indexing status")
            if type(progress) not in (int, float) or not math.isfinite(progress) or not 0 <= progress <= 1:
                raise ValueError("indexing progress must be between zero and one")
            current = record["stages"][stage] if stage in record["stages"] else None
            if current is None:
                message = next(item["label"] for item in record["stage_order"] if item["id"] == stage)
                payload = {}
            else:
                message, payload = current["message"], current["payload"]
            if not isinstance(stage, str) or not isinstance(message, str) or not isinstance(payload, dict):
                raise ValueError("invalid indexing stage")
            selected = {"status": status, "current_stage": stage, "message": message,
                        "progress": progress, "payload": payload}
            newest = candidate_started
        except (OSError, ValueError, TypeError, KeyError, AttributeError, StopIteration) as error:
            return {"status": "failed", "message": "Search progress could not be read from the indexing status file.",
                    "payload": {"error": str(error), "status_path": str(path)}}
    return selected
