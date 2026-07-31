#!/usr/bin/env python3
"""Capture a read-only Reflect corpus snapshot through the selected typed runner."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packs.search.pipeline.models import Backend, LocalCorpus, SearchSpec


def selected_runner(spec: SearchSpec):
    if spec.backend == Backend.LOCAL:
        from packs.search.backends.local.runner import LocalSearchRunner

        assert isinstance(spec.corpus, LocalCorpus)
        return LocalSearchRunner(spec.corpus.db_path)
    from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner

    return TurboPufferSearchRunner(spec.corpus)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument(
        "--evidence-person-ids",
        type=Path,
        required=True,
        help="Private JSON array containing the complete review/labeled pool",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    reflect_root = (ROOT / ".powerpacks" / "reflect").resolve()
    output = args.out.resolve()
    if not output.is_relative_to(reflect_root):
        raise SystemExit(f"--out must remain under {reflect_root}")
    spec = SearchSpec.from_dict(json.loads(args.spec.read_text()))
    person_ids = json.loads(args.evidence_person_ids.read_text())
    if not isinstance(person_ids, list) or any(not isinstance(value, str) for value in person_ids):
        raise SystemExit("--evidence-person-ids must contain a JSON string array")
    snapshot = selected_runner(spec).snapshot_corpus(args.scope, tuple(dict.fromkeys(person_ids)))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
