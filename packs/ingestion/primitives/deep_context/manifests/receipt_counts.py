"""Progress counts shared by Deep Context research receipts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReceiptCounts:
    total: int
    completed: int
    pending: int
    failed: int

    @classmethod
    def create(
        cls, *, total: int, completed: int = 0, failed: int = 0,
    ) -> ReceiptCounts:
        total = max(0, total)
        completed = min(max(0, completed), total)
        failed = min(max(0, failed), total - completed)
        return cls(total, completed, total - completed - failed, failed)
