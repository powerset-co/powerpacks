"""Fetch the profile of every LinkedIn person in merged/people.csv who has no work history.

Flow: merged/people.csv -> people with a slug, empty work_experiences and no
usable profile in profile_cache_v2 -> one gateway fetch per slug (cache-first,
recorded in the cache) -> counts. People from the LinkedIn import arrive with
their profile and are not fetched.
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
from packs.ingestion.primitives.enrich.profile_cache import profile_cache_path, read_usable_cached_profile
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
            and not read_usable_cached_profile(profile_cache_path(self.cache_dir, row["public_identifier"]))
        })
        counts = hydrate_profiles(missing, self.cache_dir) if missing else {}
        return {
            "primitive": "hydrate_merged",
            "status": "completed",
            "missing": len(missing),
            "fetched": counts.get("ok", 0),
            "no_profile": counts.get("failed", 0),
            "skipped_no_key": counts.get("skipped_no_key", 0),
        }


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    parser.add_argument("--profile-cache-dir", default=str(DEFAULT_PROFILE_CACHE_DIR))
    args = parser.parse_args()
    emit(HydrateMergedProfiles(people_csv=Path(args.people_csv), cache_dir=Path(args.profile_cache_dir)).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
