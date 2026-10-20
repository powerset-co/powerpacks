"""Per-item progress shared by the CLI and the background memory runner."""
from __future__ import annotations

from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar


class Step(StrEnum):
    IDLE = "idle"
    IMPORT = "import"
    GENERATE = "generate"
    DISCOVER = "discover"
    INDEX = "index"
    DONE = "done"


@dataclass(frozen=True)
class Progress:
    step: Step
    done: int
    total: int
    key: str
    error: str = ""


T = TypeVar("T")


def retry_first(items: Callable[[], Iterator[T]], key: Callable[[T], str], first: tuple[str, ...]) -> Iterator[T]:
    keys = set(first)
    if keys:
        for item in items():
            if key(item) in keys:
                yield item
    for item in items():
        if key(item) not in keys:
            yield item


def consume(events: Generator[Progress, None, T]) -> T:
    while True:
        try:
            next(events)
        except StopIteration as finished:
            result: T = finished.value
            return result
