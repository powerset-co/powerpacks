"""Dependency-free inference for the frozen Jev profile-capability model.

Changelog:
  2026-09-25: jev-profile-capability-v2 bundle: 31 features from seven profile questions,
    five JD-fold estimators, cutoff at 93% out-of-fold recall of Luna >= 3.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping

from packs.search.primitives.llm_rerank_candidates.jev.features import FEATURE_NAMES


MODEL_ASSET = Path(__file__).with_name("model.json")
FORMAT_VERSION = 1
MODEL_ID = "jev-1.13.0"
BUNDLE_ID = "jev-profile-capability-v2"
FEATURE_SCHEMA_VERSION = "jev-profile-features-v2"
QUESTION_VERSION = "jev-capability-v2-20260925"
PROMPT_VERSION = "jev-capability-v2-20260925"
# Out-of-fold cutoff at 93% recall of Luna >= 3 on the teacher set; must equal the bundle's cutoff.
F1_CUTOFF = 0.20825752133005385
ENSEMBLE_SIZE = 5
TREES_PER_ESTIMATOR = 100
NODE_WIDTH = 7

Node = tuple[int, float, int, int, float, int, int]
Tree = tuple[Node, ...]


@dataclass(frozen=True)
class Estimator:
    baseline: float
    trees: tuple[Tree, ...]


@dataclass(frozen=True)
class ModelBundle:
    feature_names: tuple[str, ...]
    cutoff: float
    estimators: tuple[Estimator, ...]


def _feature_schema_sha256(feature_names: tuple[str, ...]) -> str:
    encoded = json.dumps(feature_names, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _require_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _parse_tree(raw_tree: object, estimator_index: int, tree_index: int) -> Tree:
    if not isinstance(raw_tree, list) or not raw_tree:
        raise ValueError("Jev model tree must be a non-empty array")
    nodes = []
    for node_index, raw_node in enumerate(raw_tree):
        label = f"estimator {estimator_index} tree {tree_index} node {node_index}"
        if not isinstance(raw_node, list) or len(raw_node) != NODE_WIDTH:
            raise ValueError(f"{label} must be a {NODE_WIDTH}-value array")
        feature, threshold, left, right, value, leaf, missing_left = raw_node
        if any(
            isinstance(item, bool) or not isinstance(item, int) for item in (feature, left, right, leaf, missing_left)
        ):
            raise ValueError(f"{label} has invalid integer fields")
        if leaf not in (0, 1) or missing_left not in (0, 1):
            raise ValueError(f"{label} has invalid flags")
        threshold = _require_number(threshold, f"{label} threshold")
        value = _require_number(value, f"{label} value")
        if leaf:
            if feature != -1 or left != 0 or right != 0:
                raise ValueError(f"{label} has an invalid leaf")
        elif not 0 <= feature < len(FEATURE_NAMES):
            raise ValueError(f"{label} has an invalid feature index")
        elif not (node_index < left < len(raw_tree) and node_index < right < len(raw_tree)):
            raise ValueError(f"{label} has invalid child indices")
        nodes.append((feature, threshold, left, right, value, leaf, missing_left))
    return tuple(nodes)


def _parse_bundle(raw: object) -> ModelBundle:
    if not isinstance(raw, dict) or set(raw) != {
        "format_version",
        "manifest",
        "cutoff",
        "feature_names",
        "estimators",
        "provenance",
    }:
        raise ValueError("Jev model asset has unexpected fields")
    if raw["format_version"] != FORMAT_VERSION:
        raise ValueError("Jev model asset format is incompatible")

    manifest = raw["manifest"]
    expected_manifest = {
        "bundle_id": BUNDLE_ID,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_schema_sha256": _feature_schema_sha256(FEATURE_NAMES),
        "question_version": QUESTION_VERSION,
        "model_id": MODEL_ID,
        "prompt_version": PROMPT_VERSION,
    }
    if manifest != expected_manifest:
        raise ValueError("Jev model asset bindings are incompatible")
    if not isinstance(raw["feature_names"], list) or tuple(raw["feature_names"]) != FEATURE_NAMES:
        raise ValueError("Jev model feature schema is incompatible")

    provenance = raw["provenance"]
    if not isinstance(provenance, dict) or provenance.get("training_identifiers_included") is not False:
        raise ValueError("Jev model provenance is invalid")
    source_files = provenance.get("source_files")
    if not isinstance(source_files, list) or len(source_files) != ENSEMBLE_SIZE:
        raise ValueError("Jev model provenance has invalid source files")
    for source_file in source_files:
        if not isinstance(source_file, dict) or set(source_file) != {"file", "sha256"}:
            raise ValueError("Jev model provenance has an invalid source file")
        digest = source_file["sha256"]
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("Jev model provenance has an invalid source hash")

    cutoff = _require_number(raw["cutoff"], "Jev model cutoff")
    if cutoff != F1_CUTOFF:
        raise ValueError("Jev model cutoff is incompatible")
    raw_estimators = raw["estimators"]
    if not isinstance(raw_estimators, list) or len(raw_estimators) != ENSEMBLE_SIZE:
        raise ValueError(f"Jev model must contain {ENSEMBLE_SIZE} estimators")

    estimators = []
    for estimator_index, raw_estimator in enumerate(raw_estimators):
        if not isinstance(raw_estimator, dict) or set(raw_estimator) != {"baseline", "trees"}:
            raise ValueError("Jev model estimator has unexpected fields")
        raw_trees = raw_estimator["trees"]
        if not isinstance(raw_trees, list) or len(raw_trees) != TREES_PER_ESTIMATOR:
            raise ValueError(f"Jev model estimator must contain {TREES_PER_ESTIMATOR} trees")
        estimators.append(
            Estimator(
                baseline=_require_number(raw_estimator["baseline"], "Jev baseline"),
                trees=tuple(
                    _parse_tree(tree, estimator_index, tree_index) for tree_index, tree in enumerate(raw_trees)
                ),
            )
        )
    return ModelBundle(FEATURE_NAMES, cutoff, tuple(estimators))


@lru_cache(maxsize=None)
def load_model(path: Path | None = None) -> ModelBundle:
    """Load and validate a bundled or explicitly supplied portable model once."""
    model_path = MODEL_ASSET if path is None else Path(path)
    try:
        raw = json.loads(model_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot load Jev model asset: {model_path}") from exc
    return _parse_bundle(raw)


def _predict_tree(tree: Tree, values: tuple[float, ...]) -> float:
    index = 0
    while True:
        feature, threshold, left, right, value, leaf, missing_left = tree[index]
        if leaf:
            return value
        feature_value = values[feature]
        if math.isnan(feature_value):
            index = left if missing_left else right
        else:
            index = left if feature_value <= threshold else right


def _sigmoid(value: float) -> float:
    if value >= 0:
        inverse = math.exp(-value)
        return 1.0 / (1.0 + inverse)
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


def _predict_estimator(estimator: Estimator, values: tuple[float, ...]) -> float:
    logit = estimator.baseline + sum(_predict_tree(tree, values) for tree in estimator.trees)
    return _sigmoid(logit)


def predict(features: Mapping[str, float], model: ModelBundle | None = None) -> float:
    """Return the mean probability from the five frozen boosted ensembles."""
    bundle = model or load_model()
    if set(features) != set(bundle.feature_names):
        raise ValueError("Jev prediction features do not match the model schema")
    values = tuple(_require_number(features[name], f"Jev feature {name}") for name in bundle.feature_names)
    probabilities = tuple(_predict_estimator(estimator, values) for estimator in bundle.estimators)
    return sum(probabilities) / len(probabilities)
