import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "packs/search/primitives/expand_search_request/parallel_extractors.py"


def load_module():
    spec = importlib.util.spec_from_file_location("parallel_extractors_test", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class ExpandSearchRequestTests(unittest.TestCase):
    def test_sf_in_phrase_adds_person_city(self):
        mod = load_module()
        filters = mod._merge(
            {"semantic_query": "software engineering work", "bm25_queries": ["software engineer"]},
            {},
            {},
            {},
            {},
            {},
            {},
            "swe in sf",
        )

        self.assertEqual(filters["cities"], ["San Francisco"])
        self.assertNotIn("company_cities", filters)

    def test_sf_company_phrase_adds_company_city(self):
        mod = load_module()
        filters = mod._merge(
            {"semantic_query": "software engineering work", "bm25_queries": ["software engineer"]},
            {},
            {},
            {},
            {},
            {},
            {},
            "software engineers at sf companies",
        )

        self.assertEqual(filters["company_cities"], ["San Francisco"])
        self.assertNotIn("cities", filters)


if __name__ == "__main__":
    unittest.main()
