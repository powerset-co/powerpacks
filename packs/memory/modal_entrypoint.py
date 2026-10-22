"""Run generation, indexing and discovery against the memory database on Modal."""
from __future__ import annotations

import argparse
import json
import os
import threading
from dataclasses import asdict, replace
from pathlib import Path

from packs.memory import job
from packs.memory.job import Status
from packs.memory.runner import _run, provider


def _tail(root: Path, stopped: threading.Event) -> None:
    path = root / ".powerpacks/memory/job.json"
    previous = 0
    while not stopped.wait(0.1):
        with job.LOCK:
            modified = path.stat().st_mtime_ns
            if modified != previous:
                print(json.dumps(asdict(replace(job.read(root), failures=[]))), flush=True)
                previous = modified


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("discover", choices=("0", "1"))
    args = parser.parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        record = job.read(args.root)
        record.status, record.error = Status.FAILED, "OPENAI_API_KEY is not set in powerset-openai"
        job.write(args.root, record)
        print(json.dumps(asdict(replace(record, failures=[]))), flush=True)
        return 1
    stopped = threading.Event()
    tail = threading.Thread(target=_tail, args=(args.root, stopped), daemon=True)
    tail.start()
    try:
        _run(args.root, True, None, provider(), args.discover == "1", steps=job.STEPS[1:])
    finally:
        stopped.set()
        tail.join()
        print(json.dumps(asdict(replace(job.read(args.root), failures=[]))), flush=True)
    return int(job.read(args.root).status == Status.FAILED)


if __name__ == "__main__":
    raise SystemExit(main())
