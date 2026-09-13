"""Tests for the shared probe_summaries contract (probe_artifacts.py).

Covers the shape-tolerant loader that ``merge_candidate_frontier
collect-probes`` relies on: a bare list, or a legacy ``{"probes": [...]}`` /
``{"probe_summaries": [...]}`` wrapper.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = ROOT / "packs/search/primitives/shared"

if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

from probe_artifacts import coerce_probe_list, load_probe_summaries  # noqa: E402


class TestCoerceProbeList(unittest.TestCase):
    def test_bare_list_passes_through(self) -> None:
        probes = [{"id": "p1"}, {"id": "p2"}]
        self.assertEqual(coerce_probe_list(probes), probes)

    def test_probes_wrapper_key(self) -> None:
        self.assertEqual(coerce_probe_list({"probes": [{"id": "p1"}]}), [{"id": "p1"}])

    def test_probe_summaries_wrapper_key(self) -> None:
        self.assertEqual(coerce_probe_list({"probe_summaries": [{"id": "p1"}]}), [{"id": "p1"}])

    def test_empty_dict_yields_empty_list(self) -> None:
        self.assertEqual(coerce_probe_list({}), [])

    def test_dict_with_non_list_value_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            coerce_probe_list({"probes": {"id": "p1"}})
        self.assertIn("must hold a list", str(ctx.exception))

    def test_non_dict_entries_raise(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            coerce_probe_list(["p1", "p2"])
        self.assertIn("entries must be objects", str(ctx.exception))
        self.assertIn("str", str(ctx.exception))

    def test_string_document_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            coerce_probe_list("probe_summaries")
        self.assertIn("must be a list or an object", str(ctx.exception))


class TestLoadProbeSummaries(unittest.TestCase):
    def test_load_bare_list_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            path = Path(tmp_str) / "probe_summaries.json"
            path.write_text(json.dumps([{"id": "p1", "status": "completed"}]))
            self.assertEqual(load_probe_summaries(path), [{"id": "p1", "status": "completed"}])

    def test_load_wrapper_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            path = Path(tmp_str) / "probe_summaries.json"
            path.write_text(json.dumps({"probes": [{"id": "p1"}]}))
            self.assertEqual(load_probe_summaries(path), [{"id": "p1"}])


if __name__ == "__main__":
    unittest.main()
