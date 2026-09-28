"""Fetch the profile of every LinkedIn person in merged/people.csv who has no work history.

Flow: merged/people.csv -> people with a slug and empty work_experiences ->
the shared profile door per slug (cache hit, recorded empty, or one gateway
fetch recorded in profile_cache_v2) -> counts. People from the LinkedIn import
arrive with their profile and are not looked up. An unreachable gateway or a
missing key fails the step: realize stops rather than build without them.
`bin/deep-context realize` merges, runs this, then merges again, so the people
fetched here carry their work history into merged/people.csv.

Changelog:
  2026-09-28: created. Gmail/messages people resolved to a LinkedIn had no
    profile step between resolution and the merge.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.paths import DEFAULT_PROFILE_CACHE_DIR
from packs.ingestion.primitives.common.jsonio import emit
from packs.ingestion.primitives.common.paths import DEFAULT_BASE_DIR
from packs.ingestion.primitives.deep_context.shared.common import load_env
from packs.ingestion.primitives.enrich.rapidapi_client import hydrate_profiles
from packs.shared.csv_io import CsvIO

DEFAULT_PEOPLE_CSV = DEFAULT_BASE_DIR / "merged" / "people.csv"


class HydrateMergedProfiles:
    def __init__(self, *, people_csv: Path = DEFAULT_PEOPLE_CSV, cache_dir: Path = DEFAULT_PROFILE_CACHE_DIR) -> None:
        self.people_csv = Path(people_csv)
        self.cache_dir = Path(cache_dir)

    def run(self) -> dict[str, Any]:
        missing = sorted({
            (row["public_identifier"], row["linkedin_url"])
            for row in CsvIO.read_dict_rows(self.people_csv)
            if row["public_identifier"] and row["work_experiences"] in ("", "[]")
        })
        counts = hydrate_profiles(missing, self.cache_dir) if missing else {}
        unreachable = counts.get("skipped_no_key", 0)
        return {
            "primitive": "hydrate_merged",
            "status": "failed" if unreachable else "completed",
            "missing": len(missing),
            "fetched": counts.get("ok", 0),
            "no_profile": counts.get("failed", 0),
            "unreachable": unreachable,
        }


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    parser.add_argument("--profile-cache-dir", default=str(DEFAULT_PROFILE_CACHE_DIR))
    args = parser.parse_args()
    payload = HydrateMergedProfiles(people_csv=Path(args.people_csv), cache_dir=Path(args.profile_cache_dir)).run()
    emit(payload)
    return 0 if payload["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
