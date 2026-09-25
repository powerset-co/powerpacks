"""Export the frozen worth mapping from the two cached JEV experiments.

Run locally with scikit-learn; this never calls an API or reads human decisions.
The output contains coefficients and aggregate counts, never contact identifiers.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

# deep-context keeps every import at the module boundary (see
# tests/test_deep_context_import_hygiene.py), so the offline exporter's
# numpy/scikit-learn dependency lives here rather than inside fit(). Install
# that extra before importing this module (the module is unused at runtime).
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


def fit(records: list[dict]) -> tuple[dict, object]:
    names = sorted(records[0]['features'])
    train = [record for record in records if record['split'] == 'train']
    matrix = np.array([[record['features'][name] for name in names] for record in train])
    labels = [record['machine'] for record in train]
    classifier = make_pipeline(StandardScaler(), LogisticRegression(C=1, max_iter=2000, random_state=24))
    classifier.fit(matrix, labels)
    scaler, logistic = classifier.steps[0][1], classifier.steps[1][1]
    return {
        'target': 'original_machine_worth', 'human_labels_used': False,
        'training_rows': len(train), 'heldout_rows': len(records) - len(train),
        'features': names, 'mean': scaler.mean_.tolist(), 'scale': scaler.scale_.tolist(),
        'coefficients': logistic.coef_.tolist(), 'intercept': logistic.intercept_.tolist(),
        'classes': logistic.classes_.tolist(),
    }, classifier


def _features(path: Path, prefix: str) -> dict[str, float]:
    answers = json.loads(json.loads(path.read_text())['raw_response'])['answers']
    result = {}
    for name, answer in answers.items():
        if answer['type'] == 'noul':
            result[prefix + name] = answer['noul']
        else:
            result.update({prefix + name + '=' + option: value for option, value in answer['probabilities'].items()})
    return result


def export(analysis_dir: Path, output: Path) -> dict:
    prior = analysis_dir.parent / 'jev-worth'
    records = []
    for line in (analysis_dir / 'requests.jsonl').read_text().splitlines():
        row = json.loads(line)
        records.append({
            'machine': row['machine'], 'split': row['split'],
            'features': {
                **_features(analysis_dir / 'jev' / f"{row['request_sha256']}.json", 'worth:'),
                **_features(prior / 'jev' / f"{row['prior_request_sha256']}.json", 'tag:'),
            },
        })
    model, _ = fit(records)
    output.write_text(json.dumps(model, separators=(',', ':')) + '\n')
    return {'training_rows': model['training_rows'], 'heldout_rows': model['heldout_rows'], 'features': len(model['features'])}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--analysis-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path(__file__).with_name('model.json'))
    args = parser.parse_args()
    print(json.dumps(export(args.analysis_dir, args.output)))
