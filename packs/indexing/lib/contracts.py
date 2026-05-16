"""Search contract loading, normalization, and local people.csv validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS
from packs.indexing.lib.identity import canonical_person_key
from packs.indexing.lib.io import csv_header, read_csv, read_jsonl

ROOT = Path(__file__).resolve().parents[3]
CONTRACT_ROOT = ROOT / "packs/search/contracts"
CANONICAL_PEOPLE_CSV = Path(".powerpacks/network-import/merged/people.csv")
MERGE_COLUMNS = ["merge_key", "merge_confidence", "merge_sources", "merged_row_count", "needs_review"]
CANONICAL_PEOPLE_COLUMNS = PEOPLE_SCHEMA_COLUMNS + MERGE_COLUMNS


@dataclass(frozen=True)
class ContractResult:
    path: str
    ok: bool
    row_count: int
    errors: list[str]
    warnings: list[str]
    columns: list[str]

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def load_search_contract(relative_path: str | Path) -> dict[str, Any]:
    path = Path(relative_path)
    if not path.is_absolute():
        candidate = CONTRACT_ROOT / path
        path = candidate if candidate.exists() else ROOT / path
    return json.loads(path.read_text(encoding="utf-8"))


def _attrs(contract: dict[str, Any]) -> list[dict[str, Any]]:
    return list(contract.get("attributes") or [])


def attribute_names(contract: dict[str, Any]) -> set[str]:
    return {str(attr.get("name")) for attr in _attrs(contract) if attr.get("name")}


def required_attribute_names(contract: dict[str, Any]) -> set[str]:
    return {str(attr.get("name")) for attr in _attrs(contract) if attr.get("name") and attr.get("required") is True}


def vector_metadata(contract: dict[str, Any]) -> dict[str, Any] | None:
    meta = contract.get("vector")
    return meta if isinstance(meta, dict) else None


def allowed_record_names(contract: dict[str, Any]) -> set[str]:
    allowed = attribute_names(contract) | {"id"}
    if vector_metadata(contract) is not None:
        allowed.add("vector")
    return allowed


def _default_for_type(type_name: str) -> Any:
    if type_name.endswith("[]"):
        return []
    if type_name in {"integer", "number"}:
        return 0
    if type_name == "boolean":
        return False
    return ""


def _coerce(value: Any, type_name: str) -> Any:
    if value is None or value == "":
        return _default_for_type(type_name)
    if type_name.endswith("[]"):
        return value if isinstance(value, list) else [value]
    if type_name == "integer":
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    if type_name == "number":
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0
    if type_name == "boolean":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "y"}
    return str(value) if not isinstance(value, (dict, list)) else value


def normalize_record_for_contract(record: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    required = required_attribute_names(contract)
    for attr in _attrs(contract):
        name = str(attr["name"])
        if name in record or name in required:
            normalized[name] = _coerce(record.get(name), str(attr.get("type", "string")))
    if "id" in record and "id" not in normalized:
        normalized["id"] = str(record["id"])
    if vector_metadata(contract) is not None and "vector" in record:
        normalized["vector"] = record["vector"]
    return normalized


def count_defaulted_numeric(records: list[dict[str, Any]], contract: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for attr in contract.get("attributes") or []:
        name = str(attr.get("name"))
        if attr.get("type") in {"integer", "number"}:
            counts[name] = sum(1 for row in records if row.get(name) in (None, ""))
    return {key: value for key, value in counts.items() if value}

def _validate_vector(record: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    meta = vector_metadata(contract)
    if meta is None or "vector" not in record:
        return []
    vector = record.get("vector")
    if not isinstance(vector, list):
        return ["vector must be a list"]
    dimension = meta.get("dimension")
    if dimension is not None and len(vector) != int(dimension):
        return [f"vector dimension {len(vector)} != {dimension}"]
    return []


def validate_record(record: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    allowed = allowed_record_names(contract)
    required = required_attribute_names(contract)
    missing = sorted(name for name in required if name not in record)
    extra = sorted(name for name in record if name not in allowed)
    errors = _validate_vector(record, contract)
    return {"ok": not missing and not extra and not errors, "missing": missing, "extra": extra, "errors": errors}


def validate_jsonl(path: str | Path, contract_path: str | Path) -> dict[str, Any]:
    contract = load_search_contract(contract_path)
    errors: list[dict[str, Any]] = []
    count = 0
    for count, row in enumerate(read_jsonl(path), start=1):
        result = validate_record(row, contract)
        if not result["ok"]:
            errors.append({"line": count, **result})
    return {"ok": not errors, "path": str(path), "row_count": count, "errors": errors}


def validate_people_csv(path: str | Path = CANONICAL_PEOPLE_CSV, *, require_rows: bool = True) -> ContractResult:
    p = Path(path)
    errors: list[str] = []
    warnings: list[str] = []
    columns: list[str] = []
    rows: list[dict[str, str]] = []
    if not p.exists():
        return ContractResult(str(p), False, 0, [f"missing people csv: {p}"], warnings, columns)
    columns = csv_header(p)
    missing = [col for col in PEOPLE_SCHEMA_COLUMNS if col not in columns]
    if missing:
        errors.append("missing required people schema columns: " + ", ".join(missing))
    missing_merge = [col for col in MERGE_COLUMNS if col not in columns]
    if missing_merge:
        warnings.append("missing merge bookkeeping columns: " + ", ".join(missing_merge))
    rows = read_csv(p)
    if require_rows and not rows:
        errors.append("people csv has no rows")
    missing_identity = 0
    for row in rows:
        if canonical_person_key(row).startswith("person:") and not (row.get("full_name") or row.get("first_name") or row.get("last_name")):
            missing_identity += 1
    if missing_identity:
        warnings.append(f"rows with weak generated identity: {missing_identity}")
    return ContractResult(str(p), not errors, len(rows), errors, warnings, columns)


def assert_people_csv(path: str | Path = CANONICAL_PEOPLE_CSV) -> ContractResult:
    result = validate_people_csv(path)
    if not result.ok:
        raise ValueError("; ".join(result.errors))
    return result
