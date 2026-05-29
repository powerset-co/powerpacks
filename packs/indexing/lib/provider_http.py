from __future__ import annotations

import json
import signal
import threading
import urllib.request
from contextlib import contextmanager
from typing import Any


class ProviderRequestTimeout(TimeoutError):
    pass


@contextmanager
def wall_clock_timeout(seconds: int | float | None):
    if not seconds or seconds <= 0 or threading.current_thread() is not threading.main_thread():
        yield
        return

    def _timeout(_signum: int, _frame: Any) -> None:
        raise ProviderRequestTimeout(f"provider request timed out after {seconds}s")

    old_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _timeout)
    old_timer = signal.setitimer(signal.ITIMER_REAL, float(seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, old_timer[0], old_timer[1])


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str], *, timeout: int = 60) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with wall_clock_timeout(timeout):
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - explicit paid provider path
            body = response.read().decode("utf-8")
    result = json.loads(body)
    if not isinstance(result, dict):
        raise RuntimeError("provider returned non-object JSON response")
    return result
