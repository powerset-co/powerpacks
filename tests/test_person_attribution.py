from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.search.primitives.deep_search import person_attribution as attribution


class PersonAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.run_dir = Path(temporary.name)
        self.results_path = self.run_dir / "results.json"
        self.rows_path = self.run_dir / "rows.jsonl"
        self.rows_path.write_text(json.dumps({"person_id": "p1"}) + "\n" +
                                  json.dumps({"person_id": "p2"}) + "\n")
        self.results = {
            "retrieval": {"backend": "powerset", "set_id": "reviewed-set"},
            "iterations": [{"shortlist_grades": [{"person": "p1", "score": .91}],
                            "arm": {"artifacts": {"jsonl": str(self.rows_path)}}}],
        }
        self.results_path.write_text(json.dumps(self.results))
        self.hydrator = attribution.HydratePersonAttribution(self.run_dir)

    def test_direct_postgres_hydration_uses_exact_saved_set_without_auth0(self) -> None:
        response = {"p1": {
            "person_id": "p1", "total_interactions": 12,
            "sources": [{"channel": "gmail", "total_interactions": 10, "operator_count": 1},
                        {"channel": "imessage", "total_interactions": 2, "operator_count": 1}],
            "operators": [{"operator_id": "op1", "operator_name": "Jordan Bravo",
                           "channels": ["gmail", "imessage"], "gmail_interactions": 10}],
        }}
        with mock.patch.object(attribution.pg, "fetch_set_operator_ids",
                               return_value={"operator_ids": ["op1"]}) as scope, \
                mock.patch.object(attribution.pg, "fetch_network_attribution", return_value=response) as fetch:
            output = self.hydrator.run()
        self.assertEqual(output, {"status": "hydrated", "people": 2, "fetched": 2, "with_sources": 1})
        scope.assert_called_once_with(set_id="reviewed-set", env_file=None)
        fetch.assert_called_once_with(["p1", "p2"], env_file=None, allowed_operator_ids=["op1"])
        saved = json.loads(self.results_path.read_text())
        network = saved.pop("person_attribution")
        self.assertEqual(network["p1"], response["p1"])
        self.assertEqual(network["p2"]["total_interactions"], 0)
        self.assertEqual(saved, self.results)
        with mock.patch.object(attribution.pg, "fetch_set_operator_ids") as scope:
            self.assertEqual(self.hydrator.run()["status"], "cached")
            scope.assert_not_called()

    def test_database_failure_preserves_results_without_caching_zero_counts(self) -> None:
        before = self.results_path.read_bytes()
        with mock.patch.object(attribution.pg, "fetch_set_operator_ids", return_value={"operator_ids": ["op1"]}), \
                mock.patch.object(attribution.pg, "fetch_network_attribution", side_effect=RuntimeError("offline")):
            self.assertEqual(self.hydrator.run()["status"], "failed")
        self.assertEqual(self.results_path.read_bytes(), before)

    def test_refresh_replaces_saved_attribution_without_touching_scores(self) -> None:
        self.results['person_attribution'] = {'p1': {'person_id': 'p1', 'sources': [],
                                                  'operators': [], 'total_interactions': 0}}
        self.results_path.write_text(json.dumps(self.results))
        response = {'p1': {'person_id': 'p1', 'sources': [], 'operators': [], 'total_interactions': 9}}
        with mock.patch.object(attribution.pg, 'fetch_set_operator_ids', return_value={'operator_ids': ['op1']}), \
                mock.patch.object(attribution.pg, 'fetch_network_attribution', return_value=response) as fetch:
            self.hydrator.run(refresh=True)
        self.assertEqual(fetch.call_args.args[0], ['p1', 'p2'])
        saved = json.loads(self.results_path.read_text())
        self.assertEqual(saved['person_attribution']['p1'], response['p1'])
        self.assertEqual(saved['iterations'], self.results['iterations'])

    def test_hydrates_people_only_present_in_combined_summary(self) -> None:
        self.results['summary'] = {'groups': {'qualified': [{'person': 'p3'}]}}
        self.results_path.write_text(json.dumps(self.results))
        with mock.patch.object(attribution.pg, 'fetch_set_operator_ids', return_value={'operator_ids': ['op1']}), \
                mock.patch.object(attribution.pg, 'fetch_network_attribution', return_value={}) as fetch:
            self.hydrator.run()
        self.assertEqual(fetch.call_args.args[0], ['p1', 'p2', 'p3'])

    def test_local_backend_never_reads_remote_attribution(self) -> None:
        self.results["retrieval"] = {"backend": "local"}
        self.results_path.write_text(json.dumps(self.results))
        with mock.patch.object(attribution.pg, "fetch_set_operator_ids") as scope:
            self.assertEqual(self.hydrator.run()["status"], "skipped")
            scope.assert_not_called()

    def test_incremental_pond_requests_only_uncached_people(self) -> None:
        self.results["person_attribution"] = {"p1": {"person_id": "p1", "sources": [],
                                                    "operators": [], "total_interactions": 0}}
        self.results_path.write_text(json.dumps(self.results))
        with mock.patch.object(attribution.pg, "fetch_set_operator_ids", return_value={"operator_ids": ["op1"]}), \
                mock.patch.object(attribution.pg, "fetch_network_attribution", return_value={}) as fetch:
            self.assertEqual(self.hydrator.run()["fetched"], 1)
        self.assertEqual(fetch.call_args.args[0], ["p2"])


if __name__ == "__main__":
    unittest.main()
