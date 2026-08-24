#!/usr/bin/env python3
"""Build the operator-scoped TurboPuffer investor resolver namespace."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[4]
PRIMITIVES = ROOT / "packs/search/primitives"
for _path in (PRIMITIVES / "lib", PRIMITIVES / "shared", PRIMITIVES / "turbopuffer"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packs.indexing.lib.contracts import (  # noqa: E402
    load_search_contract,
    normalize_record_for_contract,
    validate_record,
)
from packs.shared.csv_io import CsvIO  # noqa: E402
from turbopuffer_search_backend import load_env_file, namespace, namespace_name  # noqa: E402


TOKEN_RE = re.compile(r"[a-z0-9]+")
ALIASES = {
    "a16z": "Andreessen Horowitz",
    "yc": "Y Combinator",
    "sequoia": "Sequoia Capital",
}


def normalize_tokens(value: str) -> str:
    return " ".join(TOKEN_RE.findall(value.lower()))


def read_rows(path: Path, operator_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    scoped_operator_ids = tuple(
        sorted(dict.fromkeys(str(value).strip() for value in operator_ids if str(value).strip()))
    )
    if not scoped_operator_ids:
        raise ValueError("investor index rebuild requires at least one operator_id")
    rows: list[dict[str, Any]] = []
    with path.open(newline="") as handle:
        reader = CsvIO.dict_reader(handle)
        missing = {"urn", "name", "type", "investment_count"} - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(f"investor CSV missing required columns: {sorted(missing)}")
        for raw in reader:
            urn = (raw.get("urn") or "").strip()
            name = (raw.get("name") or "").strip()
            if not urn or not name:
                continue
            rows.append(
                {
                    "id": urn,
                    "investor_name": name,
                    "canonical_name": name,
                    "investor_name_tokens": normalize_tokens(name),
                    "investor_type": (raw.get("type") or "").strip() or None,
                    "investment_count": int(float(raw.get("investment_count") or 0)),
                    "canonical_urn": urn,
                    "allowed_operator_ids": list(scoped_operator_ids),
                }
            )

    by_name = {row["investor_name"].lower(): row for row in rows}
    for alias, canonical in ALIASES.items():
        target = by_name.get(canonical.lower())
        if target:
            rows.append(
                {
                    **target,
                    "id": f"{target['id']}#alias:{alias}",
                    "investor_name": alias,
                    "investor_name_tokens": normalize_tokens(alias),
                }
            )
    contract = load_search_contract("turbopuffer/investors.namespace.json")
    normalized = sorted(
        (normalize_record_for_contract(row, contract) for row in rows),
        key=lambda row: str(row["id"]),
    )
    failures = [result for row in normalized if not (result := validate_record(row, contract))["ok"]]
    if failures:
        raise RuntimeError(json.dumps({"contract_errors": failures}, sort_keys=True))
    return normalized


def rebuild(
    csv_path: Path,
    operator_ids: tuple[str, ...],
    *,
    namespace_factory: Callable[[str], Any] = namespace,
) -> dict[str, Any]:
    scoped_operator_ids = tuple(
        sorted(dict.fromkeys(str(value).strip() for value in operator_ids if str(value).strip()))
    )
    rows = read_rows(csv_path, scoped_operator_ids)
    if not rows:
        raise ValueError("investor index rebuild requires at least one valid canonical investor row")
    ns = namespace_factory("investors")
    contract = load_search_contract("turbopuffer/investors.namespace.json")
    turbopuffer_types = {
        "string": "string",
        "string[]": "[]string",
        "integer": "uint",
        "number": "float",
        "boolean": "bool",
    }
    schema = {
        row["name"]: {
            "type": turbopuffer_types[row["type"]],
            **({"filterable": True} if row["name"] in {"investor_name", "allowed_operator_ids"} else {}),
            **({"full_text_search": True} if row["name"] == "investor_name_tokens" else {}),
        }
        for row in contract["attributes"]
        if row["name"] != "id"
    }
    ns.write(
        delete_by_filter=("id", "NotEq", ""),
        delete_by_filter_allow_partial=False,
        upsert_rows=rows,
        schema=schema,
    )
    return {
        "namespace": namespace_name("investors"),
        "rows": len(rows),
        "canonical_rows": len({row["canonical_urn"] for row in rows}),
        "operator_ids": list(scoped_operator_ids),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Powerpacks investor namespace")
    parser.add_argument("--csv", required=True)
    parser.add_argument("--operator-id", action="append", required=True)
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    load_env_file(Path(args.env_file) if args.env_file else None)
    result = rebuild(
        Path(args.csv).expanduser(),
        tuple(dict.fromkeys(args.operator_id)),
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
