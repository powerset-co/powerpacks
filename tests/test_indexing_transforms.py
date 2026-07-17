import csv
import json
import tempfile
import unittest
from datetime import datetime, timezone

from packs.indexing.lib.identity import position_uuid, person_uuid
from pathlib import Path

from packs.indexing.lib.people import (
    PEOPLE_CSV_COLUMNS,
    _education_to_profile,
    build_people_records,
    build_roles,
    build_unified_profiles,
    epoch_seconds,
    flatten_people,
)
from packs.indexing.lib import location_normalization
from packs.indexing.lib.location_normalization import normalize_location_fields


class IndexingTransformTests(unittest.TestCase):
    def _people_csv(self, root: Path) -> Path:
        path = root / "people.csv"
        row = {column: "" for column in PEOPLE_CSV_COLUMNS}
        row.update(
            {
                "public_identifier": "jane-example",
                "linkedin_url": "https://www.linkedin.com/in/jane-example/?trk=people",
                "first_name": "Jane",
                "last_name": "Example",
                "full_name": "Jane Example",
                "headline": "Founder and CTO at Acme AI",
                "summary": "Builds developer tools.",
                "city": "San Francisco",
                "state": "CA",
                "country": "US",
                "location_raw": "San Francisco, CA",
                "profile_picture_url": "https://img.example/jane.jpg",
                "entity_urn": "urn:li:fsd_profile:abc",
                "source_channels": "linkedin_csv,gmail",
                "work_experiences": json.dumps(
                    [
                        {
                            "title": "Founder and CTO",
                            "company_name": "Acme AI",
                            "company_public_identifier": "acme-ai",
                            "company_linkedin_url": "https://www.linkedin.com/company/acme-ai",
                            "description": "Started the company.",
                            "starts_at": {"year": 2020, "month": 5, "day": 2},
                            "ends_at": None,
                            "is_current_position": True,
                        },
                        {
                            "title": "Senior Software Engineer",
                            "company_name": "OldCo",
                            "company_key": "rapidapi:oldco-1",
                            "start_date": "2016-01",
                            "end_date": "2019-12",
                            "is_current_position": False,
                        },
                    ]
                ),
                "education": json.dumps(
                    [
                        {
                            "school_name": "Stanford University",
                            "degree": "BS",
                            "field_of_study": "Computer Science",
                            "start_year": 2012,
                            "end_year": 2016,
                        }
                    ]
                ),
                "rapidapi_response": json.dumps(
                    {"follower_count": 1234, "connection_count": 500, "skills": ["python", "ml"]}
                ),
                "twitter_handle": "jane",
                "twitter_response": json.dumps({"followers_count": 77}),
            }
        )
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=PEOPLE_CSV_COLUMNS)
            writer.writeheader()
            writer.writerow(row)
        return path

    def test_flatten_people_uses_final_people_schema_and_stable_uuid5(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            people = flatten_people(self._people_csv(Path(td)))
        self.assertEqual(len(people), 1)
        person = people[0]
        self.assertEqual(person["id"], person_uuid("linkedin:jane-example"))
        explicit = [{**people[0]["raw"], "id": "person-legacy", "public_identifier": "", "linkedin_url": ""}]
        self.assertEqual(flatten_people(explicit)[0]["id"], person_uuid("id:person-legacy"))
        self.assertEqual(person["linkedin_url"], "https://www.linkedin.com/in/jane-example")
        self.assertEqual(person["public_profile_url"], "https://www.linkedin.com/in/jane-example")
        self.assertEqual(person["full_name"], "Jane Example")
        self.assertEqual(person["city"], "San Francisco")
        self.assertEqual(person["state"], "California")
        self.assertEqual(person["country"], "United States")
        self.assertEqual(person["macro_region"], "Americas")
        self.assertEqual(person["metro_areas"], ["San Francisco Bay Area"])
        self.assertEqual(len(person["work_experiences"]), 2)
        self.assertEqual(person["education"][0]["school_name"], "Stanford University")

    def test_location_normalization_ports_network_api_geo_rules(self) -> None:
        bay_area = normalize_location_fields(
            city="San Francisco Bay Area",
            country="United States",
            location_raw="San Francisco Bay Area",
        )
        self.assertEqual(bay_area["city"], "San Francisco")
        self.assertEqual(bay_area["state"], "California")
        self.assertEqual(bay_area["country"], "United States")
        self.assertEqual(bay_area["macro_region"], "Americas")
        self.assertEqual(bay_area["metro_areas"], ["San Francisco Bay Area"])

        raw_override = normalize_location_fields(location_raw="vancouver, bc, canada")
        self.assertEqual(raw_override["city"], "Vancouver")
        self.assertEqual(raw_override["state"], "British Columbia")
        self.assertEqual(raw_override["country"], "Canada")
        self.assertEqual(raw_override["macro_region"], "Americas")

    def test_city_to_metro_assigns_bay_area_metros_like_prod(self) -> None:
        # Prod bootstrap reference maps these Bay Area cities to the metro even
        # though the raw RapidAPI string never names a metro area.
        for city in ["Palo Alto", "Oakland", "San Jose", "Stanford", "Los Altos", "Redwood City"]:
            location = normalize_location_fields(
                city=city, state="California", country="United States"
            )
            self.assertEqual(
                location["metro_areas"], ["San Francisco Bay Area"], msg=city
            )

        # State abbreviations are expanded before the metro lookup.
        abbreviated = normalize_location_fields(city="Oakland", state="CA", country="US")
        self.assertEqual(abbreviated["metro_areas"], ["San Francisco Bay Area"])

        # San Francisco itself keeps its existing single-metro behavior.
        sf = normalize_location_fields(
            city="San Francisco", state="California", country="United States"
        )
        self.assertEqual(sf["city"], "San Francisco")
        self.assertEqual(sf["metro_areas"], ["San Francisco Bay Area"])

    def test_city_to_metro_is_conservative_for_ambiguous_cities(self) -> None:
        ln = location_normalization

        # Country-scoped: Vancouver only maps inside Canada, so a US Vancouver
        # (Vancouver, WA) never inherits the Canadian metro.
        usa = normalize_location_fields(city="Vancouver", country="United States")
        self.assertEqual(usa["metro_areas"], [])
        canada = normalize_location_fields(city="Vancouver", country="Canada")
        self.assertEqual(canada["metro_areas"], ["Vancouver Metropolitan Area"])

        # When the map has the same city in multiple states, a city without a
        # state must not pick either metro.
        original_mapping = ln._mapping_cache
        original_index = ln._city_metro_cache
        try:
            ln._mapping_cache = {
                "city_overrides": {},
                "metro_to_city": {},
                "city_to_metro": {
                    "springfield|illinois|united states": ["Springfield Metropolitan Area"],
                    "springfield|massachusetts|united states": ["Greater Springfield"],
                },
                "location_raw_overrides": {},
                "state_expansions": {},
            }
            ln._city_metro_cache = None
            ambiguous = normalize_location_fields(
                city="Springfield", country="United States"
            )
            self.assertEqual(ambiguous["metro_areas"], [])
            exact = normalize_location_fields(
                city="Springfield", state="Illinois", country="United States"
            )
            self.assertEqual(exact["metro_areas"], ["Springfield Metropolitan Area"])
        finally:
            ln._mapping_cache = original_mapping
            ln._city_metro_cache = original_index

    def test_city_to_metro_handles_non_us_cities(self) -> None:
        amsterdam = normalize_location_fields(
            city="Amsterdam", state="Noord-Holland", country="Netherlands"
        )
        self.assertEqual(amsterdam["city"], "Amsterdam")
        self.assertEqual(amsterdam["country"], "Netherlands")
        self.assertIn("Amsterdam Metropolitan Area", amsterdam["metro_areas"])
        self.assertEqual(amsterdam["macro_region"], "Western Europe")

    def test_raw_string_metro_detection_still_works(self) -> None:
        raw = normalize_location_fields(location_raw="San Francisco Bay Area")
        self.assertEqual(raw["city"], "San Francisco")
        self.assertEqual(raw["state"], "California")
        self.assertEqual(raw["metro_areas"], ["San Francisco Bay Area"])

        city_pattern = normalize_location_fields(
            city="New York City Metropolitan Area", country="United States"
        )
        self.assertEqual(city_pattern["city"], "New York")
        self.assertIn("New York Metropolitan Area", city_pattern["metro_areas"])

    def test_build_roles_emits_position_records_with_epoch_seconds_and_role_ids(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            people = flatten_people(self._people_csv(Path(td)))
        roles = build_roles(people)
        self.assertEqual(len(roles), 2)
        founder = roles[0]
        self.assertEqual(founder["base_id"], people[0]["id"])
        self.assertEqual(founder["id"], position_uuid(people[0]["id"], 0))
        self.assertEqual(founder["position_title"], "Founder and CTO")
        self.assertEqual(founder["start_date_epoch"], int(datetime(2020, 5, 2, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(founder["end_date_epoch"], 0)
        self.assertTrue(founder["is_current"])
        self.assertIn("founder", founder["role_ids"])
        self.assertIn("chief_technology_officer", founder["role_ids"])
        self.assertEqual(founder["seniority_band"], "owner")
        self.assertEqual(founder["macro_region"], "Americas")
        self.assertEqual(founder["metro_areas"], ["San Francisco Bay Area"])
        self.assertTrue(founder["company_id"])
        self.assertEqual(founder["x_twitter_followers"], 77)
        self.assertEqual(founder["linkedin_followers"], 1234)
        self.assertEqual(founder["linkedin_connections"], 500)
        old = roles[1]
        self.assertEqual(old["end_date_epoch"], int(datetime(2019, 12, 1, tzinfo=timezone.utc).timestamp()))
        self.assertFalse(old["is_current"])
        self.assertEqual(epoch_seconds("present", current_as_zero=True), 0)

    def test_build_people_records_matches_contract_without_profile_only_flag(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            people = flatten_people(self._people_csv(Path(td)))
        records = build_people_records(people)
        self.assertEqual(len(records), 2)
        record = records[0]
        from packs.indexing.lib.people import PEOPLE_NAMESPACE_COLUMNS
        self.assertEqual(set(record), set(PEOPLE_NAMESPACE_COLUMNS))
        self.assertNotIn("is_profile_only", record)
        self.assertEqual(record["position_title"], "Founder and CTO")
        self.assertEqual(record["base_id"], people[0]["id"])

    def test_education_to_profile_reads_camelcase_linkedin_keys(self) -> None:
        # Shape observed in .powerpacks/network-import/merged/people.csv education JSON.
        profile = _education_to_profile({
            "schoolName": "Cornell University",
            "school": "Cornell University",
            "degree": "Doctor of Philosophy - Ph.D.",
            "fieldOfStudy": "Psychology • Minor, Cognitive Science",
            "start": {"day": 0, "month": 8, "year": 2023},
            "end": {"day": 0, "month": 5, "year": 2028},
            "starts_at": {"year": 2023, "month": 8, "day": 0},
            "ends_at": {"year": 2028, "month": 5, "day": 0},
        })
        self.assertEqual(profile["school_name"], "Cornell University")
        self.assertEqual(profile["degree"], "Doctor of Philosophy - Ph.D.")
        self.assertEqual(profile["field_of_study"], "Psychology • Minor, Cognitive Science")
        self.assertEqual(profile["start_year"], 2023)
        self.assertEqual(profile["end_year"], 2028)

        # camelCase-only variant without starts_at/ends_at falls back to start/end.
        camel_only = _education_to_profile({
            "schoolName": "Duke University",
            "degreeName": "Bachelor of Science - B.S.",
            "fieldOfStudy": "Psychology",
            "start": {"day": 0, "month": 8, "year": 2017},
            "end": {"day": 0, "month": 12, "year": 2020},
        })
        self.assertEqual(camel_only["school_name"], "Duke University")
        self.assertEqual(camel_only["degree"], "Bachelor of Science - B.S.")
        self.assertEqual(camel_only["field_of_study"], "Psychology")
        self.assertEqual(camel_only["start_year"], 2017)
        self.assertEqual(camel_only["end_year"], 2020)

        # Zero years (LinkedIn "no date") normalize to None instead of 0.
        no_dates = _education_to_profile({
            "schoolName": "University of Konstanz",
            "fieldOfStudy": "Computational Science",
            "ends_at": {"year": 0, "month": 0, "day": 0},
            "end": {"day": 0, "month": 0, "year": 0},
        })
        self.assertIsNone(no_dates["start_year"])
        self.assertIsNone(no_dates["end_year"])

    def test_build_unified_profiles_hydrates_contract_profile_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            people = flatten_people(self._people_csv(Path(td)))
        profiles = build_unified_profiles(people)
        self.assertEqual(len(profiles), 1)
        profile = profiles[0]
        self.assertEqual(profile["person_id"], people[0]["id"])
        self.assertEqual(profile["name"], "Jane Example")
        self.assertEqual(profile["location"], "San Francisco, CA")
        self.assertEqual(profile["linkedin_url"], "https://www.linkedin.com/in/jane-example")
        self.assertEqual(profile["positions"][0]["role_track"], "engineering")
        self.assertEqual(profile["education"][0]["school_name"], "Stanford University")
        self.assertEqual(profile["tech_skills"], ["python", "ml"])
        self.assertEqual(profile["x_twitter_followers"], 77)
        self.assertEqual(profile["linkedin_followers"], 1234)
        self.assertEqual(profile["linkedin_connections"], 500)
        self.assertGreater(profile["years_of_experience"], 4)
        self.assertIn("linkedin_csv", profile["vertical_sources"])

    def test_build_unified_profiles_expose_inferred_birth_year_and_age(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            people = flatten_people(self._people_csv(Path(td)))

        # Without ages (no CSV value, no lookup) both fields stay None.
        profiles = build_unified_profiles(people)
        self.assertIsNone(profiles[0]["inferred_birth_year"])
        self.assertIsNone(profiles[0]["inferred_age"])

        # The inferred_ages artifact lookup populates both fields.
        profiles = build_unified_profiles(people, age_lookup={str(people[0]["id"]): 1997})
        self.assertEqual(profiles[0]["inferred_birth_year"], 1997)
        self.assertEqual(profiles[0]["inferred_age"], datetime.now().year - 1997)


if __name__ == "__main__":
    unittest.main()
