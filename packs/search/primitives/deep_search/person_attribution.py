#!/usr/bin/env python3
"""Save set-scoped network attribution alongside search results, without rescoring."""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from packs.ingestion.primitives.common.jsonio import write_json
import postgres_client as pg


class HydratePersonAttribution:
    def __init__(self, run_dir: Path, *, env_file: Path | None = None) -> None:
        self.run_dir = run_dir
        self.env_file = env_file

    def run(self) -> dict[str, Any]:
        path = self.run_dir / "results.json"
        results = json.loads(path.read_text(encoding="utf-8"))
        retrieval = results.get("retrieval") or {}
        if retrieval.get("backend") != "powerset":
            return {"status": "skipped", "reason": "Search does not use a Powerset set"}
        set_id = retrieval.get("set_id")
        if not set_id:
            return {"status": "failed", "error": "Saved search has no set ID"}

        person_ids: set[str] = set()
        for group in (results.get('summary', {}).get('groups') or {}).values():
            person_ids.update(row['person'] for row in group)
        for iteration in results.get("iterations", []):
            person_ids.update(row["person"] for row in iteration.get("shortlist_grades", []))
            artifact = (iteration.get("arm", {}).get("artifacts") or {}).get("jsonl")
            if not artifact:
                continue
            rows_path = Path(artifact)
            if not rows_path.is_absolute():
                rows_path = ROOT / rows_path
            opener = gzip.open if rows_path.suffix == ".gz" else open
            with opener(rows_path, "rt", encoding="utf-8") as handle:
                person_ids.update(json.loads(line)["person_id"] for line in handle if line.strip())

        saved = results.get("person_attribution") or {}
        missing = sorted(person_ids - saved.keys())
        if not missing:
            return {"status": "cached", "people": len(saved), "fetched": 0}
        try:
            scope = pg.fetch_set_operator_ids(set_id=set_id, env_file=self.env_file)
            rows = pg.fetch_network_attribution(missing, env_file=self.env_file,
                                                allowed_operator_ids=scope["operator_ids"])
        except Exception as exc:
            return {"status": "failed", "error": f"Network attribution Postgres lookup failed ({type(exc).__name__})"}

        fetched = {person_id: {"person_id": person_id, "sources": [], "operators": [],
                               "total_interactions": 0} for person_id in missing}
        fetched.update(rows)
        results["person_attribution"] = saved | fetched
        write_json(path, results)
        return {"status": "hydrated", "people": len(results["person_attribution"]),
                "fetched": len(fetched), "with_sources": sum(bool(row["sources"]) for row in fetched.values())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args(argv)
    payload = HydratePersonAttribution(args.run_dir, env_file=args.env_file).run()
    print(json.dumps(payload, indent=2))
    return 0 if payload["status"] in {"hydrated", "cached", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
