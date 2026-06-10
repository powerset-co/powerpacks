"""Backend-neutral search backend mode detection.

This tiny module is intentionally not local- or TurboPuffer-specific.  Shared
query/filter helpers use it only to avoid remote-only defaults, while concrete
pipelines import their backend modules directly.
"""

from __future__ import annotations

import os


_EXPLICIT_LOCAL_SEARCH_DB: str | None = None


def configure_local_backend(db_path: str | os.PathLike[str] | None) -> None:
    global _EXPLICIT_LOCAL_SEARCH_DB
    _EXPLICIT_LOCAL_SEARCH_DB = str(db_path) if db_path else None


def explicit_local_backend_path() -> str | None:
    if _EXPLICIT_LOCAL_SEARCH_DB:
        return _EXPLICIT_LOCAL_SEARCH_DB
    value = os.getenv("POWERPACKS_LOCAL_SEARCH_DB")
    return value if value else None


def is_local_backend_configured() -> bool:
    return bool(explicit_local_backend_path())
