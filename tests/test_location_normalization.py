import json
import unittest

from packs.search.primitives.deep_search import location_scope as ls


class TestLocationNormalization(unittest.TestCase):
    def test_prefer_metro_area_filters_is_idempotent_and_all_or_nothing(self):
        nyc = {"cities": ["New York City"], "countries": ["US"]}
        preferred = {"metro_areas": ["New York Metropolitan Area"]}
        self.assertEqual(ls.prefer_metro_area_filters(nyc), preferred)
        self.assertEqual(ls.prefer_metro_area_filters(preferred), preferred)
        self.assertEqual(
            ls.prefer_metro_area_filters({
                "cities": ["San Francisco", "Raleigh"],
                "countries": ["United States"],
            }),
            {
                "cities": ["San Francisco", "Raleigh"],
                "countries": ["United States"],
            },
        )

    def test_aliases_are_canonicalized_without_changing_filter_family(self):
        cases = (
            ({"states": ["CA"], "countries": ["US"]},
             {"states": ["California"], "countries": ["United States"]}),
            ({"cities": ["New York City"], "countries": ["US"]},
             {"cities": ["New York"], "countries": ["United States"]}),
            ({"metro_areas": ["New York City metropolitan area"]},
             {"metro_areas": ["New York Metropolitan Area"]}),
            ({"metro_areas": ["London Metropolitan Area"]},
             {"metro_areas": ["London Metropolitan Area"]}),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(ls.canonicalize_location_filters(raw), expected)

    def test_every_local_canonical_metro_is_idempotent(self):
        mapping = json.loads(ls.LOCATION_MAPPING_FILE.read_text(encoding="utf-8"))
        metros = {
            value
            for values in mapping["city_to_metro"].values()
            for value in (values if isinstance(values, list) else [values])
        }
        for metro in sorted(metros):
            with self.subTest(metro=metro):
                self.assertEqual(
                    ls.canonicalize_location_filters({"metro_areas": [metro]}),
                    {"metro_areas": [metro]},
                )

    def test_city_canonicalization_is_field_aware(self):
        for city, country in (("Washington", "United States"), ("Victoria", "Canada")):
            with self.subTest(city=city):
                expected = {"cities": [city], "countries": [country]}
                self.assertEqual(ls.canonicalize_location_filters(expected), expected)

    def test_ambiguous_state_abbreviations_need_country_context(self):
        with self.assertRaisesRegex(ValueError, "country qualifier"):
            ls.canonicalize_location_filters({"states": ["WA"]})
        for country, state in (("United States", "Washington"), ("Australia", "Western Australia")):
            with self.subTest(country=country):
                self.assertEqual(
                    ls.canonicalize_location_filters({"states": ["WA"], "countries": [country]}),
                    {"states": [state], "countries": [country]},
                )

    def test_query_label_keeps_every_location_alternative(self):
        self.assertEqual(
            ls.query_location_label({"cities": ["San Francisco", "New York"], "countries": ["US"]}),
            "San Francisco Bay Area or New York Metropolitan Area",
        )
        self.assertEqual(
            ls.query_location_label({"countries": ["US", "Germany"]}),
            "United States or Germany",
        )
        self.assertEqual(
            ls.query_location_label({"macro_regions": ["Western Europe", "Eurasia"]}),
            "Europe",
        )
        self.assertEqual(ls.query_location_label({}), "")


if __name__ == "__main__":
    unittest.main()
