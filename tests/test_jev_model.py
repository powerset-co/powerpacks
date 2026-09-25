from __future__ import annotations

import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from packs.search.primitives.llm_rerank_candidates.jev.features import (
    FEATURE_NAMES,
    build_features,
)
from packs.search.primitives.llm_rerank_candidates.jev.model import (
    Estimator,
    F1_CUTOFF,
    MODEL_ASSET,
    ModelBundle,
    _predict_tree,
    load_model,
    predict,
)


def _choice(probabilities: dict[str, float]) -> dict:
    return {"type": "choice", "probabilities": probabilities}


def _score(probabilities: dict[str, float]) -> dict:
    return {"type": "score", "probabilities": probabilities}


def _noul(value: float) -> dict:
    return {"type": "noul", "noul": value}


def _answers() -> dict:
    return {
        "transfer": _score({"0": 0.1, "1": 0.2, "2": 0.3, "3": 0.4}),
        "continuity": _choice(
            {"current": 0.3, "recent": 0.2, "senior_adjacent": 0.1, "stale_switch": 0.1, "unknown": 0.2, "none": 0.1}
        ),
        "independent_execution_quality": _noul(0.8),
        "evidence_basis": _choice(
            {"description": 0.4, "summary": 0.2, "repeated_roles": 0.2, "isolated_title": 0.1, "none": 0.1}
        ),
        "company_quality": _choice({"strong": 0.6, "ordinary": 0.2, "weak": 0.1, "unknown": 0.1}),
        "specialty": _choice({"direct": 0.4, "transferable": 0.3, "missing": 0.1, "unknown": 0.1, "not_required": 0.1}),
        "historical_match": _score({"0": 0.1, "1": 0.2, "2": 0.3, "3": 0.4}),
    }


class JevFeatureTests(unittest.TestCase):
    def test_builds_the_frozen_profile_schema_and_interactions(self) -> None:
        features = build_features(_answers())

        self.assertEqual(tuple(features), FEATURE_NAMES)
        self.assertEqual(len(features), 31)
        self.assertAlmostEqual(features["transfer::3"], 0.4)
        self.assertAlmostEqual(features["continuity::stale_switch"], 0.1)
        self.assertAlmostEqual(features["independent_execution_quality"], 0.8)
        self.assertAlmostEqual(features["company_quality::strong"], 0.6)
        self.assertAlmostEqual(features["stale_x_historical_direct"], 0.04)
        self.assertAlmostEqual(features["stale_x_historical_adjacent"], 0.03)

    def test_extra_answers_are_ignored_and_missing_ones_fail(self) -> None:
        answers = _answers()
        with_extra = copy.deepcopy(answers)
        with_extra["role_0_function"] = _noul(0.8)
        self.assertEqual(build_features(with_extra), build_features(answers))
        self.assertEqual(predict(build_features(with_extra)), predict(build_features(answers)))
        del answers["transfer"]
        with self.assertRaises(KeyError):
            build_features(answers)


class JevModelTests(unittest.TestCase):
    def test_bundled_model_is_bound_and_cached(self) -> None:
        first = load_model()
        second = load_model()
        self.assertIs(first, second)
        self.assertEqual(first.feature_names, FEATURE_NAMES)
        self.assertEqual(first.cutoff, F1_CUTOFF)
        self.assertEqual(len(first.estimators), 5)
        self.assertTrue(all(len(estimator.trees) == 100 for estimator in first.estimators))

    def test_exact_threshold_routes_left(self) -> None:
        tree = (
            (0, 0.5, 1, 2, 0.0, 0, 0),
            (-1, 0.0, 0, 0, -2.0, 1, 0),
            (-1, 0.0, 0, 0, 3.0, 1, 0),
        )
        self.assertEqual(_predict_tree(tree, (0.5,)), -2.0)
        self.assertEqual(_predict_tree(tree, (math.nextafter(0.5, math.inf),)), 3.0)

    def test_ensemble_averages_probabilities_instead_of_logits(self) -> None:
        bundle = ModelBundle(
            feature_names=("x",),
            cutoff=0.5,
            estimators=(Estimator(2.0, ()), Estimator(-1.0, ())),
        )
        expected = ((1 / (1 + math.exp(-2))) + (1 / (1 + math.exp(1)))) / 2
        self.assertAlmostEqual(predict({"x": 0.0}, bundle), expected)
        self.assertNotAlmostEqual(predict({"x": 0.0}, bundle), 1 / (1 + math.exp(-0.5)))

    def test_nonfinite_and_incompatible_features_fail_closed(self) -> None:
        features = dict.fromkeys(FEATURE_NAMES, 0.0)
        features[FEATURE_NAMES[0]] = math.inf
        with self.assertRaisesRegex(ValueError, "must be finite"):
            predict(features)
        features[FEATURE_NAMES[0]] = math.nan
        with self.assertRaisesRegex(ValueError, "must be finite"):
            predict(features)
        features.pop(FEATURE_NAMES[0])
        with self.assertRaisesRegex(ValueError, "schema"):
            predict(features)

    def test_malformed_and_incompatible_assets_fail_closed(self) -> None:
        original = json.loads(MODEL_ASSET.read_text())
        cases = []
        incompatible = copy.deepcopy(original)
        incompatible["format_version"] = 2
        cases.append(incompatible)
        malformed = copy.deepcopy(original)
        malformed["estimators"][0]["trees"][0][0] = [0, 0.5]
        cases.append(malformed)
        unbound = copy.deepcopy(original)
        unbound["manifest"]["prompt_version"] = "different"
        cases.append(unbound)

        with tempfile.TemporaryDirectory() as directory:
            for index, case in enumerate(cases):
                path = Path(directory) / f"model-{index}.json"
                path.write_text(json.dumps(case))
                with self.assertRaises(ValueError):
                    load_model(path)


if __name__ == "__main__":
    unittest.main()
