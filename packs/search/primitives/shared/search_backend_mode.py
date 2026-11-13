"""Backend-neutral search backend mode detection.

This tiny module is intentionally not local- or TurboPuffer-specific.  Shared
query/filter helpers use it only to avoid remote-only defaults, while concrete
pipelines import their backend modules directly.

Local mode is only ever configured by the process itself, from its own `--db` /
`--local-db` argument. Nothing is read from the environment or `.env`.
"""

from __future__ import annotations

import os


_EXPLICIT_LOCAL_SEARCH_DB: str | None = None


def configure_local_backend(db_path: str | os.PathLike[str] | None) -> None:
    global _EXPLICIT_LOCAL_SEARCH_DB
    _EXPLICIT_LOCAL_SEARCH_DB = str(db_path) if db_path else None


def explicit_local_backend_path() -> str | None:
    return _EXPLICIT_LOCAL_SEARCH_DB or None


def is_local_backend_configured() -> bool:
    return bool(explicit_local_backend_path())
