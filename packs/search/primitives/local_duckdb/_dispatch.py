"""Dispatch helpers for explicit local DuckDB primitive entrypoints."""

from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path


PRIMITIVES_DIR = Path(__file__).resolve().parents[1]
LIB_DIR = PRIMITIVES_DIR / "lib"
SHARED_DIR = PRIMITIVES_DIR / "shared"
LOCAL_DIR = PRIMITIVES_DIR / "local"
TURBOPUFFER_DIR = PRIMITIVES_DIR / "turbopuffer"


def dispatch(target: str, *, add_local_db_arg: bool = False, target_file: Path | None = None) -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--db", required=True)
    local_args, rest = parser.parse_known_args()

    for _path in [LIB_DIR, SHARED_DIR, LOCAL_DIR, TURBOPUFFER_DIR]:
        sys.path.insert(0, str(_path))
    import local_search_backend as local_backend  # type: ignore

    local_backend.configure_local_backend(local_args.db)
    if add_local_db_arg:
        rest.extend(["--local-db", local_args.db])

    target_path = target_file if target_file is not None else PRIMITIVES_DIR / target / f"{target}.py"
    sys.path.insert(0, str(target_path.parent))
    sys.argv = [str(target_path), *rest]
    runpy.run_path(str(target_path), run_name="__main__")
