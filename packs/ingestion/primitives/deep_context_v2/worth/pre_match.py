"""The pre-match: the LinkedIn connections whose name matches every source name of a family under
the one name rule.

Free, derived on every run, nothing stored. Worth calls it to say yes to a family already in the
network; enrich calls it to propose that connection's URL.

Created: 2026-10-06
"""
from __future__ import annotations

from packs.ingestion.primitives.deep_context_v2.db.queries_worth import Connection
from packs.ingestion.primitives.deep_context_v2.names import Name, parse_name, same_person_name


def pre_match(source_names: list[str], connections: list[Connection]) -> list[Connection]:
    """The connections whose name matches every one of the family's source names. A family with no
    source name matches nothing."""
    names: list[Name] = []
    for text in source_names:
        names.append(parse_name(text))
    matches: list[Connection] = []
    if not names:
        return matches
    for connection in connections:
        connection_name: Name = parse_name(connection.name)
        every_name: bool = True
        for name in names:
            if not same_person_name(name, connection_name):
                every_name = False
                break
        if every_name:
            matches.append(connection)
    return matches
