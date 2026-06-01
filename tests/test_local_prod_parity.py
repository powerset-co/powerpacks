from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packs/search/evals/run_local_prod_parity.py"


def load_module():
    spec = importlib.util.spec_from_file_location("run_local_prod_parity", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


parity = load_module()


class LocalProdParityTests(unittest.TestCase):
    def test_local_filters_to_prod_filters_maps_schema_and_resolves_school(self) -> None:
        filters = {
            "semantic_query": "Software engineers who build production systems.",
            "bm25_queries": ["software engineer"],
            "education_names": ["Stanford University"],
            "metro_areas": ["San Francisco Bay Area"],
        }
        expanded = {
            "filters": {
                "education_ids": [
                    {
                        "id": "urn:harmonic:school:stanford",
                        "display_value": "Stanford University",
                    }
                ]
            }
        }

        out = parity.local_filters_to_prod_filters(filters, prod_expansion=expanded)

        self.assertNotIn("semantic_query", out)
        self.assertNotIn("bm25_queries", out)
        self.assertNotIn("education_names", out)
        self.assertEqual(out["role_semantic_query"], filters["semantic_query"])
        self.assertEqual(out["role_bm25_queries"], ["software engineer"])
        self.assertEqual(out["education_ids"], ["urn:harmonic:school:stanford"])
        self.assertEqual(out["education_op"], "or")
        self.assertNotIn("is_current", out)

    def test_choose_personal_set_prefers_alias_then_count(self) -> None:
        sets = [
            {"id": "wrong-big", "name": "Someone Else's Connections", "is_personal": True, "person_count": 9320},
            {"id": "right-zero", "name": "Jake Zeller's Connections", "is_personal": True, "person_count": 0},
            {"id": "right", "name": "Jake Zeller's Connections", "is_personal": True, "person_count": 9424},
            {"id": "non-personal", "name": "Jake Shared", "is_personal": False, "person_count": 9424},
        ]

        selected = parity.choose_personal_set(sets, slug="jake", aliases=["jake zeller"], local_count=9320)

        self.assertEqual(selected["id"], "right")
        self.assertEqual(selected["_selection_reason"], "alias_and_count")

    def test_prod_expansion_to_local_payload_uses_local_schema(self) -> None:
        expanded = {
            "original_query": "software engineers in sf that went to stanford",
            "traits": [
                {"meaning": "role", "temporal": "current", "value": "Software engineer"},
                {"meaning": "education", "temporal": "all", "value": "Stanford University"},
            ],
            "filters": {
                "role_semantic_query": "Software engineers build production systems.",
                "role_bm25_queries": ["software engineer"],
                "education_ids": [{"id": "urn:harmonic:school:stanford", "display_value": "Stanford University (Stanford)"}],
                "metro_areas": [{"id": "San Francisco Bay Area", "display_value": "San Francisco Bay Area"}],
                "seniority_bands": [{"id": "senior", "display_value": "Senior"}],
                "role_core_patterns": [{"regex": "ignored"}],
            },
        }
        fallback = {"role_search_filters": {"education_names": ["Stanford University"]}}

        payload = parity.prod_expansion_to_local_payload(
            expanded,
            fallback_payload=fallback,
            query="software engineers in sf that went to stanford",
        )
        filters = payload["role_search_filters"]

        self.assertEqual(filters["semantic_query"], "Software engineers build production systems.")
        self.assertEqual(filters["bm25_queries"], ["software engineer"])
        self.assertEqual(filters["education_names"], ["Stanford University"])
        self.assertEqual(filters["metro_areas"], ["San Francisco Bay Area"])
        self.assertEqual(filters["seniority_bands"], ["senior"])
        self.assertEqual(payload["traits"], expanded["traits"])
        self.assertNotIn("is_current_role", filters)
        self.assertNotIn("role_core_patterns", filters)

    def test_compare_ids_reports_precision_and_recall(self) -> None:
        comparison = parity.compare_ids(["a", "b", "c"], ["b", "c", "d", "e"])

        self.assertEqual(comparison["overlap_count"], 2)
        self.assertEqual(comparison["local_precision_vs_prod"], 0.6667)
        self.assertEqual(comparison["local_recall_vs_prod"], 0.5)
        self.assertEqual(comparison["prod_missing_local"], ["d", "e"])
        self.assertEqual(comparison["local_extra"], ["a"])


if __name__ == "__main__":
    unittest.main()
