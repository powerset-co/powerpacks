"""Per-item progress shared by the CLI and the background memory runner."""
from __future__ import annotations

from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeVar


class Step(StrEnum):
    IDLE = "idle"
    IMPORT = "import"
    GENERATE = "generate"
    INDEX = "index"
    DISCOVER = "discover"
    DONE = "done"


@dataclass(frozen=True)
class Progress:
    step: Step
    done: int
    total: int
    key: str
    error: str = ""


class _Report(Protocol):
    failures: tuple[tuple[str, str], ...]


T = TypeVar("T")
R = TypeVar("R", bound=_Report)


def retry_first(items: Callable[[], Iterator[T]], key: Callable[[T], str], first: tuple[str, ...]) -> Iterator[T]:
    keys = set(first)
    if keys:
        for item in items():
            if key(item) in keys:
                yield item
    for item in items():
        if key(item) not in keys:
            yield item


def consume(events: Generator[Progress, None, R]) -> R:
    failures = []
    while True:
        try:
            event = next(events)
            if event.error:
                failures.append((event.key, event.error))
        except StopIteration as finished:
            result: R = finished.value
            result.failures = tuple(failures)
            return result
