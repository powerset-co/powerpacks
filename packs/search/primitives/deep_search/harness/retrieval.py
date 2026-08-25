"""Which corpus a pond retrieves from, and the check that it is the approved one.

The backend comes from the run's recorded `decision.json`; the corpus identity
comes from `plan_binding.json`. Both must agree with what the caller asked for
before any pond compiles or runs.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

try:  # direct script execution
    from deep_search_loop import resolve_retrieval_identity
    from harness.artifacts import _read_json
except ImportError:  # pragma: no cover - module execution
    from ..deep_search_loop import resolve_retrieval_identity
    from .artifacts import _read_json

DEFAULT_LOCAL_DB = ".powerpacks/search-index/local-search.duckdb"


def _decision_backend(run_dir: Path, backend: str | None) -> str:
    recorded = _read_json(run_dir / "decision.json")
    value = str(recorded.get("backend") or "powerset")
    if backend and backend != value:
        raise ValueError(f"backend {backend!r} conflicts with decision.json backend {value!r}")
    return value


def _backend_args(backend: str, db: str) -> list[str]:
    return ["--backend", "local", "--db", db] if backend == "local" else []


def _approved_retrieval(run_dir: Path, plan: Mapping[str, Any], backend: str,
                        db: str) -> tuple[str | None, str]:
    approved = _read_json(run_dir / "plan_binding.json")["retrieval"]
    if approved.get("backend") != backend:
        raise ValueError("decision backend differs from the approved retrieval corpus")
    requested_db = str(approved.get("db_path") or db)
    identity, set_id, resolved_db = resolve_retrieval_identity(
        backend, dict(plan), approved.get("set_id"), requested_db)
    if identity != approved:
        raise ValueError("retrieval corpus differs from the corpus bound to this run")
    return set_id, resolved_db
