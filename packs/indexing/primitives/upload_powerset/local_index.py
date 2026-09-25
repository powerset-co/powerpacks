"""Read local DuckDB profiles and namespace documents for Powerset upload.

Flow: read person profiles and linked entity ids -> select contract columns
present in the local and live schemas -> build documents for namespace writes.

Changelog:
  2026-09-24: created.
"""

from __future__ import annotations

from typing import Any

from packs.indexing.lib.contracts import contract_attribute_names, load_search_contract, vector_metadata
from packs.indexing.primitives.upload_powerset.models import Namespace, PersonProfile
from packs.indexing.primitives.upload_powerset.turbopuffer_writer import NAMESPACE_BY_LOGICAL


def entity_ids_by_person(con: Any, logical: str, person_ids: list[str]) -> dict[str, tuple[str, ...]]:
    if logical == "companies":
        sql = """
            SELECT p.base_id, p.company_id FROM local_people_positions p
            JOIN local_companies c ON c.id = p.company_id
            WHERE p.base_id = ANY(?)
        """
    else:
        sql = """
            SELECT e.base_id, e.canonical_education_id FROM local_people_education e
            JOIN local_education s ON s.id = e.canonical_education_id
            WHERE e.base_id = ANY(?)
        """
    by_person: dict[str, set[str]] = {}
    for person_id, entity_id in con.execute(sql, [person_ids]).fetchall():
        by_person.setdefault(str(person_id), set()).add(str(entity_id))
    return {person_id: tuple(sorted(ids)) for person_id, ids in by_person.items()}


def person_profiles(con: Any, person_ids: tuple[str, ...]) -> list[PersonProfile]:
    rows = con.execute(
        "SELECT * FROM local_person_profiles WHERE person_id = ANY(?) ORDER BY person_id",
        [list(person_ids)],
    )
    columns = [column[0] for column in rows.description]
    return [PersonProfile.from_db_row(dict(zip(columns, row))) for row in rows.fetchall()]


def namespace_rows(con: Any, logical: str, ids: tuple[str, ...],
                   allowed: dict[str, tuple[str, ...]], operator_id: str,
                   live: frozenset[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    namespace = NAMESPACE_BY_LOGICAL[logical]
    columns = doc_columns(con, namespace, live)
    select = ", ".join(f't."{column}"' for column in columns)
    key = "base_id" if namespace.person_grain else "id"
    person_key = f', t."{key}" AS "_person_key"' if namespace.person_grain else ""
    rows = con.execute(
        f'SELECT {select}{person_key} FROM {namespace.table} t WHERE t."{key}" = ANY(?) ORDER BY t."id"',
        [list(ids)],
    )
    names = [column[0] for column in rows.description]
    docs = []
    for row in rows.fetchall():
        doc = dict(zip(names, row))
        if namespace.person_grain:
            person_id = str(doc.pop("_person_key"))
        # TurboPuffer writes only present values; NULL would erase a live attribute.
        doc = {name: value for name, value in doc.items() if value is not None}
        if namespace.person_grain:
            doc["allowed_operator_ids"] = list(allowed.get(person_id, ()))
        elif logical == "companies":
            # Companies have allowed_operator_ids in WRITE_SCHEMA; schools do not.
            doc["allowed_operator_ids"] = [operator_id]
        docs.append(doc)
    return docs


def doc_columns(con: Any, namespace: Namespace, live: frozenset[str]) -> list[str]:
    """Contract columns present in the local table and live namespace."""
    contract = load_search_contract(f"turbopuffer/{namespace.logical}.namespace.json")
    names = ["id"] + [name for name in contract_attribute_names(contract) if name != "id"]
    if vector_metadata(contract) is not None:
        names.append("vector")
    columns = {row[0] for row in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [namespace.table]).fetchall()}
    allowed = columns if not live else columns & (live | {"id"})
    return [name for name in names if name in allowed]
