from __future__ import annotations

from copy import deepcopy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import duckdb

from packs.search.primitives.deep_search import network_floors


def _plan() -> dict:
    return {
        "candidate_populations": [
            {"population": "Executive Assistant with demanding principal support",
             "hint_kind": "stated-background"},
            {"population": "Operations professional", "hint_kind": "capability-adjacent"},
            {"population": "high-growth experience", "hint_kind": "ranking-boost"},
        ],
        "search_scope": {
            "location": "Stockholm, Sweden",
            "filters": {"cities": ["Stockholm"], "countries": ["Sweden"]},
        },
    }


class FakePowersetNamespace:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def multi_query(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(results=[
            SimpleNamespace(aggregation_groups=[{"base_id": "p1"}, {"base_id": "p2"}]),
            SimpleNamespace(aggregation_groups=[]),
        ])


class NetworkFloorTests(unittest.TestCase):
    def test_binding_tracks_set_population_menu_and_geography(self) -> None:
        plan = _plan()
        identity = {"backend": "powerset", "set_id": "set-1"}
        binding = network_floors.floor_binding(plan, "powerset", identity)

        self.assertNotEqual(
            binding,
            network_floors.floor_binding(
                plan, "powerset", {"backend": "powerset", "set_id": "set-2"}),
        )
        changed_menu = deepcopy(plan)
        changed_menu["candidate_populations"].append({
            "population": "Chief of Staff", "hint_kind": "capability-adjacent",
        })
        self.assertNotEqual(
            binding, network_floors.floor_binding(changed_menu, "powerset", identity))
        changed_geo = deepcopy(plan)
        changed_geo["search_scope"] = {
            "location": "Europe", "filters": {"macro_regions": ["Western Europe", "Eurasia"]},
        }
        self.assertNotEqual(
            binding, network_floors.floor_binding(changed_geo, "powerset", identity))

    def test_fake_powerset_probe_is_current_deduped_and_set_scoped(self) -> None:
        namespace = FakePowersetNamespace()
        artifact = network_floors.probe_populations(
            _plan(),
            backend="powerset",
            retrieval_identity={"backend": "powerset", "set_id": "set-1"},
            powerset_namespace=namespace,
            resolved_operator_ids=["op-1", "op-2"],
            observed_at="2026-08-25T12:00:00Z",
        )

        self.assertEqual(len(namespace.calls), 1)
        call = namespace.calls[0]
        self.assertEqual(call["timeout"], 2.0)
        self.assertEqual(len(call["queries"]), 2)
        for query in call["queries"]:
            self.assertEqual(query["group_by"], ["base_id"])
            self.assertEqual(query["limit"], 10_000)
            self.assertIn(["is_current", "Eq", True], query["filters"][1])
            self.assertIn(
                ["allowed_operator_ids", "ContainsAny", ["op-1", "op-2"]],
                query["filters"][1],
            )
        self.assertEqual([row["count"] for row in artifact["floors"]], [2, 0])
        self.assertEqual(artifact["binding"]["populations"], [
            "Executive Assistant with demanding principal support",
            "Operations professional",
        ])
        self.assertTrue(artifact["provenance"]["namespace"].startswith("aleph_people_v1"))
        self.assertEqual(artifact["provenance"]["set_id"], "set-1")
        self.assertIn("in this set at 2026-08-25T12:00:00Z", artifact["floors"][0]["label"])

    def test_local_probe_uses_same_filter_and_dedupes_current_people(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            db_path = Path(raw) / "local.duckdb"
            conn = duckdb.connect(str(db_path))
            conn.execute("""
                create table local_people_positions (
                    id varchar, base_id varchar, person_id varchar, position_title varchar,
                    word_tokens varchar[], is_current boolean, city varchar, country varchar
                )
            """)
            conn.executemany(
                "insert into local_people_positions values (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("r1", "p1", "p1", "Executive Assistant", ["executive", "assistant"], True, "Stockholm", "Sweden"),
                    ("r2", "p1", "p1", "Executive Assistant", ["executive", "assistant"], True, "Stockholm", "Sweden"),
                    ("r3", "p2", "p2", "Executive Assistant", ["executive", "assistant"], False, "Stockholm", "Sweden"),
                    ("r4", "p3", "p3", "Executive Assistant", ["executive", "assistant"], True, "London", "United Kingdom"),
                    ("r5", "p4", "p4", "Operations Professional", ["operations", "professional"], True, "Stockholm", "Sweden"),
                ],
            )
            conn.close()
            stat = db_path.stat()
            identity = {
                "backend": "local", "db_path": str(db_path),
                "db_size": stat.st_size, "db_mtime_ns": stat.st_mtime_ns,
            }

            artifact = network_floors.probe_populations(
                _plan(), backend="local", retrieval_identity=identity,
                observed_at="2026-08-25T12:00:00Z")

        self.assertEqual([row["count"] for row in artifact["floors"]], [1, 1])
        self.assertEqual(artifact["floors"][0]["title_headline_terms"],
                         ["executive", "assistant"])
        self.assertEqual(artifact["provenance"]["db_path"], str(db_path))
        self.assertNotIn("allowed_operator_ids", str(artifact["floors"][0]["filter_expression"]))
        self.assertIn("in this local index at 2026-08-25T12:00:00Z",
                      artifact["floors"][0]["label"])


if __name__ == "__main__":
    unittest.main()
