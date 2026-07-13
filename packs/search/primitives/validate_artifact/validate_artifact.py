#!/usr/bin/env python3
"""Validate a JSON artifact against a repo schema.

Agent-authored artifacts (plan.json etc.) must conform to the schemas in
packs/search/schemas/. This is the one supported way to check them — do not
hand-roll jsonschema imports in ad-hoc scripts.

Usage:
    uv run --project . python packs/search/primitives/validate_artifact/validate_artifact.py \
        --schema search-network-jd-plan --file .powerpacks/deep-search/<run>/plan.json

    ... --list-schemas
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[4]
SCHEMAS_DIR = ROOT / "packs/search/schemas"


def resolve_schema_path(name_or_path: str) -> Path:
    candidate = Path(name_or_path)
    if candidate.exists():
        return candidate
    bare = name_or_path.removesuffix(".schema.json").removesuffix(".json")
    resolved = SCHEMAS_DIR / f"{bare}.schema.json"
    if resolved.exists():
        return resolved
    raise SystemExit(f"error: schema not found: {name_or_path} (looked in {SCHEMAS_DIR})")


def validate_file(name_or_path: str, artifact_path: Path):
    """Validate one JSON file and return its document; raise ValueError on invalid input."""
    schema_path = resolve_schema_path(name_or_path)
    if not artifact_path.exists():
        raise ValueError(f"file not found: {artifact_path}")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        document = json.loads(artifact_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{artifact_path} is not valid JSON: {exc}") from exc
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    if errors:
        details = []
        for error in errors:
            pointer = "/" + "/".join(str(part) for part in error.absolute_path)
            details.append(f"{pointer or '/'}: {error.message}")
        raise ValueError("; ".join(details))
    return document


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", help="schema name (e.g. search-network-jd-plan) or path")
    parser.add_argument("--file", help="JSON artifact to validate")
    parser.add_argument("--list-schemas", action="store_true", help="list available schema names")
    args = parser.parse_args()

    if args.list_schemas:
        for path in sorted(SCHEMAS_DIR.glob("*.schema.json")):
            print(path.name.removesuffix(".schema.json"))
        return
    if not args.schema or not args.file:
        parser.error("--schema and --file are required (or use --list-schemas)")

    artifact_path = Path(args.file)
    try:
        validate_file(args.schema, artifact_path)
    except ValueError as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        raise SystemExit(1)
    schema_path = resolve_schema_path(args.schema)
    print(f"ok: {artifact_path} conforms to {schema_path.name}")


if __name__ == "__main__":
    main()
