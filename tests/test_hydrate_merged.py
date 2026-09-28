"""realize fetches the profiles the merge found missing, and only those."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.enrich import hydrate_merged
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS
from packs.shared.csv_io import CsvIO


def _row(**fields) -> dict[str, str]:
    return {**{column: "" for column in PEOPLE_SCHEMA_COLUMNS}, **fields}


class HydrateMergedTests(unittest.TestCase):
    def test_only_slugs_without_a_usable_cached_profile_are_fetched(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cache = base / "profile_cache_v2"
            cache.mkdir()
            (cache / "casey-delta.json").write_text(json.dumps({
                "raw_response": {"full_name": "Casey Delta"}, "normalized_profile": {"success": True}}))
            people = base / "people.csv"
            CsvIO.write_dict_rows(people, PEOPLE_SCHEMA_COLUMNS, [
                _row(id="a", public_identifier="casey-delta", linkedin_url="https://www.linkedin.com/in/casey-delta"),
                _row(id="b", public_identifier="riley-echo", linkedin_url="https://www.linkedin.com/in/riley-echo"),
                _row(id="candidate:email:jordan@example.com", primary_email="jordan@example.com"),
                # From the LinkedIn import: its profile came with it.
                _row(id="c", public_identifier="morgan-fox", work_experiences='[{"title": "CEO"}]'),
            ])
            counts = {"wanted": 1, "ok": 1, "failed": 0, "skipped_no_key": 0}
            with mock.patch.object(hydrate_merged, "hydrate_profiles", return_value=counts) as fetch:
                payload = hydrate_merged.HydrateMergedProfiles(people_csv=people, cache_dir=cache).run()
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[0], [("riley-echo", "https://www.linkedin.com/in/riley-echo")])
        self.assertEqual((payload["status"], payload["missing"], payload["fetched"]), ("completed", 1, 1))

    def test_nothing_missing_fetches_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            people = Path(td) / "people.csv"
            CsvIO.write_dict_rows(people, PEOPLE_SCHEMA_COLUMNS, [_row(id="c", primary_email="jordan@example.com")])
            with mock.patch.object(hydrate_merged, "hydrate_profiles") as fetch:
                payload = hydrate_merged.HydrateMergedProfiles(people_csv=people, cache_dir=Path(td)).run()
        fetch.assert_not_called()
        self.assertEqual(payload["missing"], 0)


if __name__ == "__main__":
    unittest.main()
