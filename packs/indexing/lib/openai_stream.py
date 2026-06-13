"""Stream paid OpenAI calls through a fixed-size pool, handling results as they complete.

Prod parity (aleph combined_enrichment.py): calls flow continuously through
concurrency slots, so a slow call occupies one slot while the rest keep
moving — the latency tail costs wall time once at the end of the pool, never
once per wave. This replaces the gather-a-wave-then-wait pattern, whose wall
time was set by the slowest call of every wave.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable


class StopStreaming(Exception):
    """Raised by on_result to halt the pool early; carries the caller's payload."""

    def __init__(self, payload: Any):
        super().__init__("streaming stopped")
        self.payload = payload


async def drain_pool(coros: list[Awaitable[Any]], on_result: Callable[[Any], None]) -> Any | None:
    """Run coroutines concurrently and call on_result(result) as each completes.

    on_result may raise StopStreaming to cancel outstanding work; its payload
    is returned. Any other exception (including a failed call) cancels the
    pool and propagates.
    """
    tasks = [asyncio.ensure_future(coro) for coro in coros]
    try:
        for fut in asyncio.as_completed(tasks):
            on_result(await fut)
    except StopStreaming as stop:
        return stop.payload
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return None
