"""One persisted memory job, shared by the runner and the local page."""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.memory.progress import Step

LOCK = threading.RLock()
STEPS = (Step.IMPORT, Step.GENERATE, Step.INDEX, Step.DISCOVER)


class Status(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Counts:
    done: int = 0
    total: int = 0


@dataclass(frozen=True)
class Failure:
    step: Step
    key: str
    error: str
    at: str


@dataclass
class Job:
    enabled: bool = False
    keep_updated: bool = True
    step: Step = Step.IDLE
    status: Status = Status.IDLE
    steps: dict[str, Counts] = field(default_factory=lambda: {step: Counts() for step in STEPS})
    current_item: str = ""
    people_indexed: int = 0
    started_at: str = ""
    updated_at: str = ""
    approved_spend_usd: float = 0
    spent_usd: float | None = 0
    tokens: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0, "embedding": 0})
    failures: list[Failure] = field(default_factory=list)
    error: str = ""
    stopped: str = ""
    pid: int = 0


def database(root: Path) -> Path:
    return root / ".powerpacks/memory/memory.sqlite"


def read(root: Path) -> Job:
    path = root / ".powerpacks/memory/job.json"
    with LOCK:
        if not path.exists():
            return Job()
        record = json.loads(path.read_text())
    record["step"] = Step(record["step"])
    record["status"] = Status(record["status"])
    record["steps"] = {step: Counts(**counts) for step, counts in record["steps"].items()}
    record["failures"] = [Failure(Step(item["step"]), item["key"], item["error"], item["at"])
                          for item in record["failures"]]
    return Job(**record)


def write(root: Path, record: Job) -> None:
    with LOCK:
        record.updated_at = now_iso()
        path = root / ".powerpacks/memory/job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(record), indent=2) + "\n")
        temporary.replace(path)
