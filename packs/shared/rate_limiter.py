"""Shared client-side rate limiting for Powerpacks.

One :class:`StartRateLimiter` so every caller that paces an external API
(RapidAPI LinkedIn profiles, RapidAPI company details, ...) shares a single
tested implementation — fix it once, fix it everywhere. It paces request
*starts*, so a thread-pool fan-out sustains a steady requests-per-minute rate
instead of bursting and tripping the provider's 429s (each of which costs a
retry + backoff).

Usage::

    from packs.shared.rate_limiter import StartRateLimiter

    limiter = StartRateLimiter(max_rpm=300)   # module-level, shared by workers
    ...
    limiter.wait()                            # in each worker, before the call
"""
from __future__ import annotations

import threading
import time


class StartRateLimiter:
    """Thread-safe pacer: spaces request *starts* to at most ``max_rpm`` across
    all threads that share the instance.

    ``extra_sleep_seconds`` enforces an additional minimum gap between starts;
    the effective interval is the larger of ``60 / max_rpm`` and
    ``extra_sleep_seconds``. A non-positive ``max_rpm`` with no extra sleep
    disables pacing entirely (``wait()`` returns immediately).
    """

    def __init__(self, max_rpm: float, extra_sleep_seconds: float = 0.0) -> None:
        intervals = []
        if max_rpm and max_rpm > 0:
            intervals.append(60.0 / max_rpm)
        if extra_sleep_seconds and extra_sleep_seconds > 0:
            intervals.append(extra_sleep_seconds)
        self.interval = max(intervals) if intervals else 0.0
        self._lock = threading.Lock()
        self._next_start = 0.0

    def wait(self) -> None:
        """Block until this thread may start its next request."""
        if self.interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait_for = max(0.0, self._next_start - now)
            self._next_start = max(now, self._next_start) + self.interval
        if wait_for > 0:
            time.sleep(wait_for)
