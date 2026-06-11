import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packs.indexing.lib.company_locations import (  # noqa: E402
    backfill_company_locations_from_rapidapi,
    load_company_hq_from_cache,
    normalize_company_hq,
)


def write_payload(cache_dir: Path, rid: str, payload: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{rid}.json").write_text(json.dumps(payload), encoding="utf-8")


def corpus_row(urn: str, **overrides) -> dict:
    row = {
        "company_urn": urn,
        "company_name": overrides.pop("company_name", "Company " + urn),
        "city": "",
        "state": "",
        "country": "",
        "metro_area": "",
        "macro_region": "",
    }
    row.update(overrides)
    return row


class CompanyLocationsFromRapidapiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_dir = Path(self.tmp.name) / "rapidapi-company-cache"
        # Real RapidAPI payload shape: data.headquarter with raw LinkedIn codes.
        write_payload(self.cache_dir, "101", {
            "data": {
                "name": "Roblox",
                "headquarter": {"city": "San Mateo", "geographicArea": "CA", "country": "US"},
            },
        })
        write_payload(self.cache_dir, "202", {
            "data": {
                "name": "Acme Robotics",
                "headquarter": {"city": "Berlin", "country": "DE"},
            },
        })
        write_payload(self.cache_dir, "303", {"data": {"name": "No HQ Co"}})
        write_payload(self.cache_dir, "505", {
            "data": {
                "name": "Worldwide Co",
                "headquarter": {"city": "Worldwide", "country": "OO"},
            },
        })
        # RapidAPI sentinel payload for unresolvable companies.
        write_payload(self.cache_dir, "0", {"data": None, "success": False})

    def test_extracts_and_normalizes_rapidapi_payload(self) -> None:
        lookup = load_company_hq_from_cache(["101"], cache_dir=self.cache_dir)
        self.assertEqual(lookup["101"], {
            "city": "San Mateo",
            "state": "California",
            "country": "United States",
            "metro_area": "San Francisco Bay Area",
            "macro_region": "Americas",
        })

    def test_country_code_normalized_to_full_name(self) -> None:
        location = normalize_company_hq("Berlin", "", "DE")
        self.assertEqual(location["country"], "Germany")
        self.assertEqual(location["macro_region"], "Western Europe")
        self.assertEqual(location["city"], "Berlin")

    def test_backfill_fills_empty_fields_from_cache(self) -> None:
        rows = [corpus_row("urn-roblox", company_name="Roblox")]
        stats = backfill_company_locations_from_rapidapi(rows, {"urn-roblox": "101"}, cache_dir=self.cache_dir)
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["companies_filled"], 1)
        self.assertEqual(rows[0]["city"], "San Mateo")
        self.assertEqual(rows[0]["state"], "California")
        self.assertEqual(rows[0]["country"], "United States")
        self.assertEqual(rows[0]["metro_area"], "San Francisco Bay Area")
        self.assertEqual(rows[0]["macro_region"], "Americas")

    def test_backfill_never_overwrites_non_empty_fields(self) -> None:
        rows = [corpus_row(
            "urn-roblox",
            city="Existing City",
            country="Existing Country",
            macro_region="Existing Region",
        )]
        backfill_company_locations_from_rapidapi(rows, {"urn-roblox": "101"}, cache_dir=self.cache_dir)
        self.assertEqual(rows[0]["city"], "Existing City")
        self.assertEqual(rows[0]["country"], "Existing Country")
        self.assertEqual(rows[0]["macro_region"], "Existing Region")
        # Empty fields are still filled from the cached payload.
        self.assertEqual(rows[0]["state"], "California")
        self.assertEqual(rows[0]["metro_area"], "San Francisco Bay Area")

    def test_cache_miss_leaves_row_untouched(self) -> None:
        rows = [corpus_row("urn-miss"), corpus_row("urn-no-rid")]
        stats = backfill_company_locations_from_rapidapi(
            rows, {"urn-miss": "99999"}, cache_dir=self.cache_dir,
        )
        self.assertEqual(stats["matched"], 0)
        self.assertEqual(stats["companies_filled"], 0)
        for row in rows:
            self.assertEqual(row["city"], "")
            self.assertEqual(row["macro_region"], "")

    def test_payload_without_hq_leaves_row_untouched(self) -> None:
        rows = [corpus_row("urn-nohq")]
        stats = backfill_company_locations_from_rapidapi(rows, {"urn-nohq": "303"}, cache_dir=self.cache_dir)
        self.assertEqual(stats["matched"], 0)
        self.assertEqual(rows[0]["city"], "")

    def test_worldwide_placeholder_hq_leaves_row_untouched(self) -> None:
        rows = [corpus_row("urn-worldwide")]
        stats = backfill_company_locations_from_rapidapi(rows, {"urn-worldwide": "505"}, cache_dir=self.cache_dir)
        self.assertEqual(stats["matched"], 0)
        self.assertEqual(rows[0]["city"], "")
        self.assertEqual(rows[0]["country"], "")

    def test_rapidapi_sentinel_id_is_never_joined(self) -> None:
        rows = [corpus_row("urn-sentinel")]
        stats = backfill_company_locations_from_rapidapi(rows, {"urn-sentinel": "0"}, cache_dir=self.cache_dir)
        self.assertEqual(stats["companies_with_rapidapi_id"], 0)
        self.assertEqual(stats["matched"], 0)
        self.assertEqual(rows[0]["city"], "")

    def test_macro_region_derived_for_rows_with_existing_country(self) -> None:
        rows = [corpus_row("urn-no-rid", country="United States")]
        stats = backfill_company_locations_from_rapidapi(rows, {}, cache_dir=self.cache_dir)
        self.assertEqual(rows[0]["macro_region"], "Americas")
        self.assertEqual(stats["fields_filled"], 1)

    def test_backfill_is_cache_only_never_network(self) -> None:
        rows = [
            corpus_row("urn-roblox"),
            corpus_row("urn-miss"),
        ]
        with mock.patch(
            "http.client.HTTPSConnection",
            side_effect=AssertionError("network call attempted during cache-only backfill"),
        ):
            stats = backfill_company_locations_from_rapidapi(
                rows,
                {"urn-roblox": "101", "urn-miss": "99999"},
                cache_dir=self.cache_dir,
            )
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(rows[0]["country"], "United States")
        self.assertEqual(rows[1]["city"], "")


if __name__ == "__main__":
    unittest.main()
