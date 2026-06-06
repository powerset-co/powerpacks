#!/usr/bin/env python3
"""Hydrate candidates from an explicit local DuckDB backend."""

from _dispatch import dispatch


if __name__ == "__main__":
    dispatch("hydrate_people", add_local_db_arg=True)
