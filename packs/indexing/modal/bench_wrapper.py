#!/usr/bin/env python3
"""Run a command and record wall time + peak RSS. Stdlib only; runs inside the Modal sandbox.

Usage: bench_wrapper.py <report.json> <command> [args...]

Samples the child's /proc/<pid>/status VmRSS once per second and records
resource.getrusage(RUSAGE_CHILDREN).ru_maxrss after exit. Exits with the
child's exit code.
"""
from __future__ import annotations

import json
import resource
import subprocess
import sys
import threading
import time
from pathlib import Path


def read_rss_kb(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def main() -> int:
    report_path = Path(sys.argv[1])
    command = sys.argv[2:]
    samples: list[dict[str, float]] = []
    start = time.monotonic()
    proc = subprocess.Popen(command)

    def poll() -> None:
        while proc.poll() is None:
            rss_kb = read_rss_kb(proc.pid)
            if rss_kb:
                samples.append({"t": round(time.monotonic() - start, 1), "rss_mb": round(rss_kb / 1024, 1)})
            time.sleep(1.0)

    poller = threading.Thread(target=poll, daemon=True)
    poller.start()
    exit_code = proc.wait()
    poller.join(timeout=3)
    wall = time.monotonic() - start
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    report = {
        "command": command,
        "exit_code": exit_code,
        "wall_seconds": round(wall, 1),
        "max_rss_mb": round(ru.ru_maxrss / 1024, 1),  # linux ru_maxrss is KB
        "sampled_peak_rss_mb": max((s["rss_mb"] for s in samples), default=0.0),
        "samples": samples,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2))
    print(f"[bench] exit={exit_code} wall={wall:.0f}s max_rss={report['max_rss_mb']:.0f}MB", flush=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
