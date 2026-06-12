"""Tests for the shared probe_summaries contract (probe_artifacts.py).

Covers the shape-tolerant loader and the evaluator regression where a
Codex-authored ``{"probes": [...]}`` wrapper crashed
``evaluate_profile_candidates.collect_profiles`` with
``'str' object has no attribute 'get'``.

No network, no OpenAI calls — ``collect_profiles`` is exercised directly.
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = ROOT / "packs/search/primitives/shared"
EVALUATE_PY = ROOT / "packs/search/primitives/evaluate_profile_candidates/evaluate_profile_candidates.py"

if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

from probe_artifacts import coerce_probe_list, load_probe_summaries  # noqa: E402


def _load_module(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


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


class TestEvaluatorCollectProfilesRegression(unittest.TestCase):
    """Wrapper-shaped probe_summaries.json must not crash collect_profiles."""

    def _build_run_dir(self, tmp: Path, summaries_doc) -> tuple[Path, Path]:
        run_dir = tmp / "run"
        run_dir.mkdir()
        artifact_dir = tmp / "artifacts" / "probe-1"
        hydrate_dir = artifact_dir / "hydrate_people"
        hydrate_dir.mkdir(parents=True)
        with gzip.open(hydrate_dir / "profiles.jsonl.gz", "wt") as fh:
            fh.write(json.dumps({"person_id": "abc", "name": "Alice"}) + "\n")
        doc = summaries_doc(artifact_dir)
        (run_dir / "probe_summaries.json").write_text(json.dumps(doc))
        return run_dir, artifact_dir

    def test_collect_profiles_handles_wrapper_shape(self) -> None:
        module = _load_module("evaluate_profile_candidates_probe_artifacts_test", EVALUATE_PY)
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            run_dir, _ = self._build_run_dir(tmp, lambda artifact_dir: {
                "probes": [{
                    "id": "p1",
                    "status": "completed",
                    "artifact_dir": str(artifact_dir),
                }],
            })
            candidates = [{"candidate_id": "abc", "person_id": "abc"}]
            profiles = module.collect_profiles(candidates, run_dir)
            self.assertIn("abc", profiles)
            self.assertEqual(profiles["abc"]["name"], "Alice")

    def test_collect_profiles_handles_bare_list(self) -> None:
        module = _load_module("evaluate_profile_candidates_probe_artifacts_test", EVALUATE_PY)
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            run_dir, _ = self._build_run_dir(tmp, lambda artifact_dir: [{
                "id": "p1",
                "status": "completed",
                "artifact_dir": str(artifact_dir),
            }])
            candidates = [{"candidate_id": "abc", "person_id": "abc"}]
            profiles = module.collect_profiles(candidates, run_dir)
            self.assertIn("abc", profiles)


if __name__ == "__main__":
    unittest.main()
