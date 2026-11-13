"""Run only v2 unit tests, with no network and a five-minute execution budget."""
from __future__ import annotations

import os
import signal
import sys
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _deadline(_signal: int, _frame: object) -> None:
    print("v2 unit tests exceeded 290 seconds", file=sys.stderr, flush=True)
    # unittest catches SystemExit as a test error and continues. Terminate the runner,
    # including worker threads, rather than running more tests after the one-shot alarm.
    os._exit(1)


def main() -> int:
    signal.signal(signal.SIGALRM, _deadline)
    signal.alarm(290)
    started = time.perf_counter()
    try:
        with ExitStack() as guard:
            for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection", "socket.getaddrinfo"):
                guard.enter_context(patch(target, side_effect=AssertionError("v2 unit tests must not use the network")))
            suite = unittest.defaultTestLoader.discover(
                str(ROOT / "tests" / "deep_context_v2"), top_level_dir=str(ROOT)
            )
            result = unittest.TextTestRunner(verbosity=1).run(suite)
        print(f"v2 unit lane: {result.testsRun} tests, {time.perf_counter() - started:.2f}s, network disabled")
        return 0 if result.wasSuccessful() else 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())
