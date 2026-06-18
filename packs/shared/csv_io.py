"""Central CSV I/O for Powerpacks.

Every CSV read in this repo should go through :class:`CsvIO` so a single fix
applies to all call sites. The reason this exists: rows can carry very large
embedded JSON fields (``rapidapi_response``, ``harmonic_response``,
``work_experiences``) that blow past Python's default 131072-byte
``csv.field_size_limit`` and raise ``_csv.Error: field larger than field
limit``. Routing reads through here raises that limit once, idempotently, for
the whole process — so we never have to remember to do it per file again.

Drop-in usage (mirrors the stdlib signatures exactly)::

    from packs.shared.csv_io import CsvIO

    with open(path, newline="") as f:
        for row in CsvIO.dict_reader(f):
            ...

    rows = list(CsvIO.reader(handle))
"""
from __future__ import annotations

import csv
import sys
from typing import Any


class CsvIO:
    """Process-wide CSV facade. All methods are classmethods; never instantiated."""

    _limit_raised = False

    @classmethod
    def ensure_field_limit(cls) -> None:
        """Raise ``csv.field_size_limit`` to the platform maximum, once per process."""
        if cls._limit_raised:
            return
        limit = sys.maxsize
        while True:
            try:
                csv.field_size_limit(limit)
            except OverflowError:
                limit //= 2
                continue
            cls._limit_raised = True
            return

    @classmethod
    def dict_reader(cls, f: Any, *args: Any, **kwargs: Any) -> "csv.DictReader":
        """Drop-in for ``csv.DictReader`` with the field-size guard applied."""
        cls.ensure_field_limit()
        return csv.DictReader(f, *args, **kwargs)

    @classmethod
    def reader(cls, f: Any, *args: Any, **kwargs: Any):
        """Drop-in for ``csv.reader`` with the field-size guard applied."""
        cls.ensure_field_limit()
        return csv.reader(f, *args, **kwargs)

    @classmethod
    def dict_writer(cls, f: Any, *args: Any, **kwargs: Any) -> "csv.DictWriter":
        """Drop-in for ``csv.DictWriter`` (writes are unaffected by the limit;
        provided so all CSV traffic can share one facade)."""
        return csv.DictWriter(f, *args, **kwargs)

    @classmethod
    def writer(cls, f: Any, *args: Any, **kwargs: Any):
        """Drop-in for ``csv.writer``."""
        return csv.writer(f, *args, **kwargs)


# Raise the limit at import time too, so merely importing this module protects
# any process that pulls it in (directly or transitively), even before the first
# reader is constructed.
CsvIO.ensure_field_limit()
