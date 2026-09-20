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
    answers = {
        "function_match": _score({"0": 0.1, "1": 0.2, "2": 0.3, "3": 0.4}),
        "direct_execution": _noul(0.6),
        "coverage": _score({"0": 0.1, "1": 0.2, "2": 0.3, "3": 0.3, "4": 0.1}),
        "specialty": _choice({"direct": 0.4, "transferable": 0.3, "missing": 0.1, "unknown": 0.1, "not_required": 0.1}),
        "transfer": _score({"0": 0.1, "1": 0.2, "2": 0.3, "3": 0.4}),
        "evidence_basis": _choice(
            {"description": 0.4, "summary": 0.2, "repeated_roles": 0.2, "isolated_title": 0.1, "none": 0.1}
        ),
        "continuity": _choice(
            {"current": 0.3, "recent": 0.2, "senior_adjacent": 0.1, "stale_switch": 0.1, "unknown": 0.2, "none": 0.1}
        ),
        "historical_match": _score({"0": 0.1, "1": 0.2, "2": 0.3, "3": 0.4}),
        "repeated_practice": _noul(0.7),
        "relevant_leadership": _noul(0.3),
        "scope": _score({"0": 0.1, "1": 0.2, "2": 0.3, "3": 0.4}),
        "company_domain": _score({"0": 0.1, "1": 0.2, "2": 0.3, "3": 0.4}),
        "company_quality": _choice({"strong": 0.6, "ordinary": 0.2, "weak": 0.1, "unknown": 0.1}),
        "environment_fit": _choice({"comparable": 0.4, "transferable": 0.3, "mismatch": 0.1, "unknown": 0.2}),
        "funding_context": _choice({"supported": 0.5, "adverse": 0.1, "unknown": 0.4}),
        "independent_execution_quality": _noul(0.8),
        "education_relevance": _choice({"relevant": 0.3, "unrelated": 0.2, "unknown": 0.5}),
        "school_signal": _choice({"strong": 0.9, "ordinary": 0.05, "unknown": 0.05}),
        "wrong_function": _noul(0.2),
        "overall_rating": _choice({"1": 0.0, "2": 0.1, "3": 0.2, "4": 0.3, "5": 0.4}),
        "role_0_function": _noul(0.8),
        "role_0_execution": _noul(0.5),
        "role_0_quality": _choice({"strong": 0.7, "ordinary": 0.2, "weak": 0.05, "unknown": 0.05}),
        "role_1_function": _noul(0.4),
        "role_1_execution": _noul(0.25),
        "role_1_quality": _choice({"strong": 0.1, "ordinary": 0.2, "weak": 0.6, "unknown": 0.1}),
    }
    return answers


class JevFeatureTests(unittest.TestCase):
    def test_builds_the_frozen_company_schema_and_interactions(self) -> None:
        roles = [
            {"dates": {"recency": "current", "years_in_role": 2}},
            {"dates": {"recency": "ended_within_5years", "years_in_role": 3}},
        ]
        features = build_features(roles, _answers())

        self.assertEqual(set(features), set(FEATURE_NAMES))
        self.assertEqual(len(features), 87)
        self.assertAlmostEqual(features["role_match_max"], 0.8)
        self.assertAlmostEqual(features["role_substantive_max"], 0.4)
        self.assertAlmostEqual(features["role_repeated_support"], 0.6)
        self.assertAlmostEqual(features["company_relevant_strong"], 0.5)
        self.assertAlmostEqual(features["company_role_year_share_strong"], 0.34)
        self.assertAlmostEqual(features["company_current_relevant_strong"], 0.56)
        self.assertAlmostEqual(features["company_current_strong_prior_weak"], 0.2128)
        self.assertAlmostEqual(features["direct_x_continuity"], 0.36)
        self.assertAlmostEqual(features["function_x_company_quality"], 0.4)
        self.assertAlmostEqual(features["stale_x_historical_direct"], 0.04)
        self.assertAlmostEqual(features["stale_x_historical_adjacent"], 0.03)

    def test_overall_rating_and_school_cannot_influence_features(self) -> None:
        roles = [{"dates": {"recency": "unknown", "years_in_role": None}}]
        answers = _answers()
        original = build_features(roles, answers)
        changed = copy.deepcopy(answers)
        changed["overall_rating"]["probabilities"] = {"1": 1.0, "2": 0.0, "3": 0.0, "4": 0.0, "5": 0.0}
        changed["school_signal"]["probabilities"] = {"strong": 0.0, "ordinary": 0.0, "unknown": 1.0}
        self.assertEqual(build_features(roles, changed), original)


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
