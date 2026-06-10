#!/usr/bin/env python3
"""Resolve companies against an explicit local DuckDB backend."""

from _dispatch import PRIMITIVES_DIR, dispatch


if __name__ == "__main__":
    dispatch(
        "resolve_companies",
        target_file=PRIMITIVES_DIR / "local" / "local_resolve_companies.py",
    )
