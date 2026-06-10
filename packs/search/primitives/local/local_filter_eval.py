"""Local evaluator for TurboPuffer-style filter tuples.

The local DuckDB backend materializes candidate rows into Python dictionaries and
uses this module to apply the same lightweight filter tuples emitted by the
Powerpacks search primitives.  This file intentionally has no package-relative
imports so it can be loaded when ``packs/search/primitives/lib`` is placed
straight on ``sys.path``.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable
from typing import Any


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_LOGICAL_OPS = {"And", "Or"}
_COMPARISON_OPS = {
    "Eq",
    "NotEq",
    "In",
    "NotIn",
    "Gt",
    "Gte",
    "Lt",
    "Lte",
    "ContainsAny",
    "ContainsAllTokens",
    "IGlob",
}


def _row_get(row: Any, field: str) -> Any:
    if isinstance(row, dict):
        if field in row:
            return row[field]
        token_field = f"{field}_tokens"
        if token_field in row:
            return row[token_field]
        return None

    extra = getattr(row, "model_extra", None)
    if isinstance(extra, dict):
        if field in extra:
            return extra[field]
        token_field = f"{field}_tokens"
        if token_field in extra:
            return extra[token_field]

    if hasattr(row, field):
        return getattr(row, field)
    token_field = f"{field}_tokens"
    if hasattr(row, token_field):
        return getattr(row, token_field)
    return None


def _is_iterable_value(value: Any) -> bool:
    return isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray, dict))


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if _is_iterable_value(value):
        return list(value)
    return [value]


def _tokenize(value: Any) -> list[str]:
    if value is None:
        return []
    if _is_iterable_value(value):
        tokens: list[str] = []
        for item in value:
            tokens.extend(_TOKEN_RE.findall(str(item).lower()))
        return tokens
    return _TOKEN_RE.findall(str(value).lower())


def _compare_values(left: Any, right: Any, op: str) -> bool:
    if left is None:
        return False
    try:
        if op == "Gt":
            return left > right
        if op == "Gte":
            return left >= right
        if op == "Lt":
            return left < right
        if op == "Lte":
            return left <= right
    except TypeError:
        try:
            left_float = float(left)
            right_float = float(right)
        except (TypeError, ValueError):
            return False
        if op == "Gt":
            return left_float > right_float
        if op == "Gte":
            return left_float >= right_float
        if op == "Lt":
            return left_float < right_float
        if op == "Lte":
            return left_float <= right_float
    raise ValueError(f"unsupported comparison operator: {op!r}")


def _contains_any(row_value: Any, expected: Any) -> bool:
    row_values = _as_list(row_value)
    expected_values = _as_list(expected)
    row_set = {str(value) for value in row_values}
    expected_set = {str(value) for value in expected_values}
    return bool(row_set & expected_set)


def _contains_all_tokens(row_value: Any, expected: Any, options: dict[str, Any] | None = None) -> bool:
    query_tokens = _tokenize(expected)
    if not query_tokens:
        return True
    row_tokens = _tokenize(row_value)
    if not row_tokens:
        return False

    row_set = set(row_tokens)
    last_as_prefix = bool((options or {}).get("last_as_prefix"))
    exact_tokens = query_tokens[:-1] if last_as_prefix and query_tokens else query_tokens
    if any(token not in row_set for token in exact_tokens):
        return False
    if last_as_prefix:
        prefix = query_tokens[-1]
        return any(token.startswith(prefix) for token in row_tokens)
    return True


def _iglob(row_value: Any, pattern: Any) -> bool:
    pattern_text = str(pattern or "").lower()
    if not pattern_text:
        return False
    return any(fnmatch.fnmatch(str(value).lower(), pattern_text) for value in _as_list(row_value))


def filter_matches(row: Any, filters: Any) -> bool:
    """Return whether ``row`` satisfies a TurboPuffer-style filter tuple.

    ``filters=None`` matches every row.  Malformed tuples and unsupported
    operators raise ``ValueError`` rather than silently returning no matches.
    """

    if filters is None:
        return True
    if not isinstance(filters, (list, tuple)) or not filters:
        raise ValueError(f"invalid filter tuple: {filters!r}")

    op_or_field = filters[0]
    if op_or_field in _LOGICAL_OPS:
        if len(filters) != 2 or not isinstance(filters[1], (list, tuple)):
            raise ValueError(f"{op_or_field} filter must be a 2-tuple containing a list of clauses")
        clauses = list(filters[1])
        if op_or_field == "And":
            return all(filter_matches(row, clause) for clause in clauses)
        return any(filter_matches(row, clause) for clause in clauses)

    if len(filters) not in {3, 4}:
        raise ValueError(f"invalid comparison filter tuple: {filters!r}")
    field, operator, expected = filters[0], filters[1], filters[2]
    if not isinstance(field, str) or not isinstance(operator, str):
        raise ValueError(f"invalid comparison filter tuple: {filters!r}")
    if operator not in _COMPARISON_OPS:
        raise ValueError(f"unsupported filter operator: {operator!r}")
    if len(filters) == 4 and operator != "ContainsAllTokens":
        raise ValueError(f"operator {operator!r} does not accept filter options")

    row_value = _row_get(row, field)

    if operator == "Eq":
        return row_value == expected
    if operator == "NotEq":
        return row_value != expected
    if operator == "In":
        expected_values = set(str(value) for value in _as_list(expected))
        return any(str(value) in expected_values for value in _as_list(row_value))
    if operator == "NotIn":
        expected_values = set(str(value) for value in _as_list(expected))
        return all(str(value) not in expected_values for value in _as_list(row_value))
    if operator in {"Gt", "Gte", "Lt", "Lte"}:
        return _compare_values(row_value, expected, operator)
    if operator == "ContainsAny":
        return _contains_any(row_value, expected)
    if operator == "ContainsAllTokens":
        options = filters[3] if len(filters) == 4 else None
        if options is not None and not isinstance(options, dict):
            raise ValueError("ContainsAllTokens options must be a dict")
        return _contains_all_tokens(row_value, expected, options)
    if operator == "IGlob":
        return _iglob(row_value, expected)

    raise ValueError(f"unsupported filter operator: {operator!r}")


def filter_rows(rows: Iterable[Any], filters: Any) -> list[Any]:
    """Return all rows matching ``filters``."""

    return [row for row in rows if filter_matches(row, filters)]
