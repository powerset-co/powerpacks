"""One imported person as the logbook reads them, with the two identity helpers it keys on.

Moved here whole from v1 deep-context's `shared/common.py` when that package was deleted; the
logbook is the one remaining reader.

Created: 2026-10-07 (moved from deep_context/shared/common.py)
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


def phone_digits(raw: str) -> str:
    """Comparable digit key, dropping a US country code so +1NXX == NXX."""
    digits = re.sub(r"[^\d]", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


def slugify(name: str, person_id: str) -> str:
    """Stable filename stem: name-slug + short id suffix (collision-proof)."""
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "person"
    pid = (person_id or "").lower()
    if pid.startswith("candidate:"):
        suffix = hashlib.sha1(pid.encode("utf-8")).hexdigest()[:8]
    elif pid.startswith("parent-"):
        suffix = re.sub(r"[^a-z0-9]+", "", pid.removeprefix("parent-"))[:8]
    else:
        suffix = re.sub(r"[^a-z0-9]+", "", pid)[:8] or "unknown"
    return f"{base}-{suffix}"


@dataclass
class Person:
    # The logbook uses the raw people.csv row id.
    person_id: str
    full_name: str
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    source_channels: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return slugify(self.full_name, self.person_id)
