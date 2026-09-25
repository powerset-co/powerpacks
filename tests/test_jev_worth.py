"""JEV worth request privacy, reduction, and frozen machine-label mapping."""
import asyncio
import json
from types import SimpleNamespace

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from importlib.util import find_spec

from packs.ingestion.primitives.deep_context.jev_worth import model, questions, runner


def _answers():
    result = {}
    for name, question in questions.build_questions({}).items():
        if question['type'] == 'noul':
            result[name] = {'type': 'noul', 'noul': 0.6}
        else:
            options = list(question['criteria']) if question['type'] == 'choice' else list(map(str, range(len(question['criteria']))))
            result[name] = {'type': question['type'], 'probabilities': {option: 1 / len(options) for option in options}}
    return result


class JevWorthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp_path = Path(temporary.name)

    def test_request_excludes_prior_verdict_labels_and_raw_messages(self):
        facts = {'canonical_name': 'Jordan Bravo', 'network_worth': {'decision': 'no', 'reason': 'OLD VERDICT'},
                 'labels': {'noise': 1}, 'owned_identifiers': {'emails': ['owner@example.com']}, 'title': 'Engineer'}
        bundle = {'messages': [{'body': 'RAW SECRET', 'channel': 'gmail', 'direction': 'from_me', 'at': '2026-01-01'}],
                  'profile': {'headline': 'ENRICHED SECRET'}, 'source_channels': ['gmail']}
        request = questions.build_request(facts=facts, bundle=bundle, owner={'name': 'Casey', 'emails': ['OWNER SECRET']}, reference_date='2026-01-01')
        serialized = json.dumps(request['state'])
        for hidden in ('network_worth', 'labels', 'RAW SECRET', 'OLD VERDICT', 'ENRICHED SECRET', 'OWNER SECRET', 'owner@example.com'):
            assert hidden not in serialized
        assert len(request['questions']) == 41
        assert request['state']['channels']['from_me'] == 1
        assert request['state']['profile']['title'] == 'Engineer'
        assert 'email-backed' in request['questions']['worth']['instructions']
        assert facts['network_worth']['decision'] == 'no'


    def test_source_policy(self):
        for channels, expected in [(['gmail_msgvault'], 'email-backed'), (['email'], 'email-backed'), (['whatsapp'], 'phone-message-backed'), (['email', 'whatsapp'], 'both email and phone'), ([], 'source is unclear')]:
            assert expected in questions.build_questions({'source_channels': channels})['worth']['instructions']


    def test_model_and_labels_cover_joint_questions(self):
        answers = _answers()
        features = model.features(answers)
        assert set(features) == set(model.MODEL['features'])
        assert model.predict(answers) in ('yes', 'no', 'maybe')
        labels = runner._labels(answers)
        assert labels['noise'] == 0.6
        assert type(labels['warmth']) is float
        assert type(labels['relationship_kind']) is str
        assert type(labels['relationship_kind_p']) is float


    def test_classify_uses_shared_cache_and_keeps_usage(self):
        tmp_path = self.tmp_path
        seen = []
        async def answer_requests(requests, **kwargs):
            seen.append(kwargs)
            return {key: SimpleNamespace(response={'answers': _answers(), 'usage': {'input_tokens': 123, 'output_tokens': 0}}, cached=True) for key in requests}
        with patch.object(runner, 'answer_requests', answer_requests):
            result = asyncio.run(runner.classify(facts={}, bundle={}, owner={}, reference_date='2026-01-01', output_dir=tmp_path))
        assert result['network_worth']['decision'] in ('yes', 'no', 'maybe')
        assert result['usage'] == {'input_tokens': 123, 'output_tokens': 0, 'cached': True}
        assert seen[0]['output_dir'] == tmp_path


    def test_notable_title_rule(self):
        for headline in ("CEO @ AngelList", "Co-Founder & CTO", "Managing Director, Growth",
                         "Chief Revenue Officer at Harmonic", "General Partner, Example Ventures"):
            assert runner.notable_title(headline), headline
        for headline in ("Partnerships Manager", "VP Engineering", "Software Engineer", "Chief of Staff", ""):
            assert not runner.notable_title(headline), headline


    def test_classify_notable_headline_is_yes_without_a_new_request(self):
        tmp_path = self.tmp_path
        seen = []
        async def answer_requests(requests, **kwargs):
            seen.extend(requests.values())
            return {key: SimpleNamespace(response={'answers': _answers(), 'usage': {'input_tokens': 5, 'output_tokens': 0}}, cached=True) for key in requests}
        with patch.object(runner, 'answer_requests', answer_requests), patch.object(runner, 'predict', return_value='maybe'):
            plain = asyncio.run(runner.classify(facts={}, bundle={}, owner={}, reference_date='2026-01-01', output_dir=tmp_path))
            notable = asyncio.run(runner.classify(facts={}, bundle={}, owner={}, reference_date='2026-01-01', output_dir=tmp_path, headline='CEO, AngelList'))
        assert plain['network_worth']['decision'] == 'maybe'
        assert notable['network_worth'] == {'decision': 'yes', 'reason': runner.NOTABLE_REASON_PREFIX + 'CEO, AngelList'}
        assert notable['labels'] == plain['labels']
        # The headline never enters the JEV request, so the cache key is unchanged.
        assert seen[0] == seen[1]


    def test_cached_estimate_costs_nothing(self):
        tmp_path = self.tmp_path
        request = questions.build_request(facts={}, bundle={}, owner={}, reference_date='2026-01-01')
        path = runner.cache_path(tmp_path, runner.request_digest(request))
        path.parent.mkdir()
        path.write_text('{}')
        assert runner.estimate(request, output_dir=tmp_path) == {'input_tokens': 0, 'cost_usd': 0.0, 'cached': True}


    def test_portable_mapping_matches_sklearn_without_human_or_heldout_labels(self):
        if find_spec('sklearn') is None:
            self.skipTest('scikit-learn is only needed to verify the offline exporter')
        import copy
        import numpy as np
        from packs.ingestion.primitives.deep_context.jev_worth.export_model import fit

        records = []
        answers = []
        for index in range(36):
            answer = _answers()
            actual = ('yes', 'no', 'maybe')[index % 3]
            answer['worth']['probabilities'] = {label: 0.8 if label == actual else 0.1 for label in ('yes', 'no', 'maybe')}
            answers.append(answer)
            records.append({'features': model.features(answer), 'machine': actual,
                            'human': 'no', 'split': 'test' if index >= 30 else 'train'})
        exported, classifier = fit(records)
        expected = classifier.predict(np.array([[row['features'][name] for name in exported['features']] for row in records]))
        assert [model.predict(answer, model=exported) for answer in answers] == list(expected)
        altered = copy.deepcopy(records)
        for row in altered:
            row['human'] = 'yes'
            if row['split'] == 'test':
                row['machine'] = 'no'
        exported_again, _ = fit(altered)
        assert exported_again == exported
        assert exported['human_labels_used'] is False
        assert exported['training_rows'] == 30
        assert exported['heldout_rows'] == 6
