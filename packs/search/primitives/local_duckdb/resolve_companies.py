#!/usr/bin/env python3
"""Resolve companies against an explicit local DuckDB backend."""

from _dispatch import dispatch


if __name__ == "__main__":
    dispatch("resolve_companies")
