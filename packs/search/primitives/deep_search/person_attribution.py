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


def local_network_attribution(db_path: Path, person_ids: list[str]) -> dict[str, Any]:
    """Read saved source counts for the one owner of a local index."""
    import duckdb
    from dataclasses import asdict
    from packs.indexing.primitives.upload_powerset.models import CLOUD_SOURCE_CHANNEL
    from packs.search.primitives.deep_search.results_web.model import NetworkOperator, NetworkSource, PersonAttribution

    manifest_path = db_path.with_name("manifest.json")
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    operator_id = manifest.get("operator_id") or "local"
    owner_path = db_path.parent.parent / "deep-context" / "owner.json"
    owner = json.loads(owner_path.read_text()) if owner_path.exists() else {}
    operator_name = owner.get("name") or "Local network"
    with duckdb.connect(str(db_path), read_only=True) as conn:
        rows = conn.execute(
            "SELECT person_id, source_channels, interaction_counts, total_interactions "
            "FROM local_person_profiles WHERE person_id IN (SELECT unnest(?))", [person_ids],
        ).fetchall()
    result = {}
    for person_id, labels, raw_counts, total in rows:
        counts = json.loads(raw_counts) if raw_counts else {}
        channels = sorted({CLOUD_SOURCE_CHANNEL.get(label, label) for label in labels or []} | counts.keys())
        sources = tuple(sorted(
            (NetworkSource(channel, int(counts.get(channel) or 0), 1) for channel in channels),
            key=lambda source: (-source.total_interactions, source.channel),
        ))
        operator = NetworkOperator(
            operator_id=operator_id, operator_name=operator_name, channels=tuple(channels),
            gmail_interactions=int(counts.get("gmail") or 0),
            message_interactions=sum(int(counts.get(channel) or 0) for channel in ("imessage", "whatsapp")),
        )
        result[person_id] = asdict(PersonAttribution(
            person_id=person_id, sources=sources, operators=(operator,),
            total_interactions=int(total or sum(source.total_interactions for source in sources)),
        ))
    return result


class HydratePersonAttribution:
    def __init__(self, run_dir: Path, *, env_file: Path | None = None) -> None:
        self.run_dir = run_dir
        self.env_file = env_file

    def run(self, *, refresh: bool = False) -> dict[str, Any]:
        path = self.run_dir / "results.json"
        results = json.loads(path.read_text(encoding="utf-8"))
        retrieval = results.get("retrieval") or {}
        local = retrieval.get("backend") == "local"
        set_id = retrieval.get("set_id")
        if not local and not set_id:
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
        missing = sorted(person_ids if refresh else person_ids - saved.keys())
        if not missing:
            return {"status": "cached", "people": len(saved), "fetched": 0}
        try:
            if local:
                rows = local_network_attribution(Path(retrieval["db_path"]), missing)
            else:
                scope = pg.fetch_set_operator_ids(set_id=set_id, env_file=self.env_file)
                rows = pg.fetch_network_attribution(missing, env_file=self.env_file,
                                                    allowed_operator_ids=scope["operator_ids"])
        except Exception as exc:
            return {"status": "failed", "error": f"Network attribution lookup failed ({type(exc).__name__})"}

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
    parser.add_argument("--refresh", action="store_true", help="Refresh saved attribution without rescoring")
    args = parser.parse_args(argv)
    payload = HydratePersonAttribution(args.run_dir, env_file=args.env_file).run(refresh=args.refresh)
    print(json.dumps(payload, indent=2))
    return 0 if payload["status"] in {"hydrated", "cached", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
