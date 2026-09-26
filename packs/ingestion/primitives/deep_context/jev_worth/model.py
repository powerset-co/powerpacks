"""Portable multinomial logistic mapping trained on original machine worth only."""
from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass

from packs.ingestion.primitives.deep_context.jev_worth.models import AnswerKind, WorthAnswer

from packs.ingestion.primitives.deep_context.jev_worth.questions import WORTH_SIGNALS

@dataclass(frozen=True)
class WorthModel:
    features: tuple[str, ...]
    mean: tuple[float, ...]
    scale: tuple[float, ...]
    intercept: tuple[float, ...]
    coefficients: tuple[tuple[float, ...], ...]
    classes: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: dict) -> WorthModel:
        return cls(tuple(payload['features']), tuple(payload['mean']), tuple(payload['scale']),
                   tuple(payload['intercept']), tuple(tuple(row) for row in payload['coefficients']), tuple(payload['classes']))


MODEL = WorthModel.from_payload(json.loads(Path(__file__).with_name('model.json').read_text()))


def features(answers: dict[str, WorthAnswer]) -> dict[str, float]:
    result = {}
    for name, answer in answers.items():
        prefix = 'worth:' if name == 'worth' or name in WORTH_SIGNALS else 'tag:'
        if answer.kind == AnswerKind.NOUL:
            result[prefix + name] = answer.noul
        else:
            result.update({prefix + name + '=' + option: value for option, value in answer.probabilities.items()})
    return result


def _scores(answers: dict[str, WorthAnswer], model: WorthModel) -> tuple[list[float], list[float]]:
    values = features(answers)
    normalized = [(values[name] - mean) / scale for name, mean, scale in zip(model.features, model.mean, model.scale)]
    scores = [intercept + sum(value * coefficient for value, coefficient in zip(normalized, coefficients))
              for intercept, coefficients in zip(model.intercept, model.coefficients)]
    return normalized, scores


def predict(answers: dict[str, WorthAnswer], *, model: WorthModel = MODEL) -> str:
    _, scores = _scores(answers, model)
    return model.classes[max(range(len(scores)), key=scores.__getitem__)]


def supporting_features(answers: dict[str, WorthAnswer], *, decision: str, model: WorthModel = MODEL) -> list[tuple[str, str | None, float]]:
    """Group standardized contributions to the decision over its nearest alternative.

    Return the strongest contributing option and its observed probability per
    question. The direct worth answer is excluded because it is circular prose.
    """
    normalized, scores = _scores(answers, model)
    winner = model.classes.index(decision)
    other = max((i for i in range(len(scores)) if i != winner), key=scores.__getitem__)
    values = features(answers)
    groups = {}
    for feature, value, weight, opposing in zip(
        model.features, normalized, model.coefficients[winner], model.coefficients[other],
    ):
        name, _, option = feature.split(':', 1)[1].partition('=')
        if name == 'worth':
            continue
        contribution = value * (weight - opposing)
        groups.setdefault(name, []).append((contribution, option or None, values[feature]))
    ranked = sorted(groups, key=lambda name: -sum(item[0] for item in groups[name]))
    return [(name, *max(groups[name], key=lambda item: item[0])[1:]) for name in ranked
            if sum(item[0] for item in groups[name]) > 0]
