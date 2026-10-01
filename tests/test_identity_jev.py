"""The cheap identity judge accepts supported matches and escalates disagreement."""
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import IdentityTask, JudgeProfile
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence
from packs.search.primitives.llm_rerank_candidates.jev import client as jev
from tests.test_jev_client import _Client, _Response, _payload

from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import jev_judge


class IdentityJevTest(unittest.TestCase):
    def test_supported_match_is_accepted_but_conflicting_identity_escalates(self):
        yes = {
            'competing_identity': {'noul': 0.01}, 'concrete_conflict': {'noul': 0.01},
            'identity': {'probabilities': {'confirmed': .98, 'needs_review': .01, 'wrong_person': .01}},
            'positive_connection': {'noul': .99},
        }
        association = {
            'association': {'probabilities': {'yes': .98, 'no': .01, 'review': .01}},
            'source_conflict': {'noul': .01}, 'specific_connection': {'noul': .99},
        }
        self.assertGreater(jev_judge._probability(yes, association), .5)
        yes['competing_identity']['noul'] = .99
        yes['concrete_conflict']['noul'] = .99
        yes['positive_connection']['noul'] = .01
        yes['identity']['probabilities'] = {'confirmed': .01, 'needs_review': .01, 'wrong_person': .98}
        association['association']['probabilities'] = {'yes': .01, 'no': .98, 'review': .01}
        association['source_conflict']['noul'] = .99
        association['specific_connection']['noul'] = .01
        self.assertLess(jev_judge._probability(yes, association), .5)

    def test_explicit_rejections_escalate_despite_positive_model_score(self):
        network = {'competing_identity': {'noul': .25}, 'concrete_conflict': {'noul': .8},
            'identity': {'choice': 'wrong_person',
                'probabilities': {'confirmed': .05, 'needs_review': .15, 'wrong_person': .8}},
            'positive_connection': {'noul': .1}}
        association = {'association': {'choice': 'no',
                'probabilities': {'yes': .02, 'review': .08, 'no': .9}},
            'source_conflict': {'noul': .8}, 'specific_connection': {'noul': .1}}
        probability = jev_judge._probability(network, association)
        self.assertGreater(probability, .5)
        verdict, reason = jev_judge._classify(probability, network, association)
        self.assertEqual(verdict, 'needs_review')
        self.assertIn('views', reason)

    def test_both_views_must_support_the_match(self):
        for identity, association in [('confirmed', 'no'), ('needs_review', 'yes'),
                                      ('confirmed', 'review'), ('needs_review', 'review')]:
            verdict, _ = jev_judge._classify(.99,
                {'identity': {'probabilities': {identity: 1.0}}},
                {'association': {'probabilities': {association: 1.0}}})
            self.assertEqual(verdict, 'needs_review')
        self.assertEqual(jev_judge._classify(.9,
            {'identity': {'probabilities': {'confirmed': 1.0}}},
            {'association': {'probabilities': {'yes': 1.0}}})[0], 'confirmed')
        verdict, reason = jev_judge._classify(.49,
            {'identity': {'probabilities': {'confirmed': 1.0}}},
            {'association': {'probabilities': {'yes': 1.0}}})
        self.assertEqual(verdict, 'needs_review')
        self.assertIn('model', reason)

    def test_classification_uses_validated_top_probability_not_choice(self):
        network = {'identity': {'choice': 'wrong_person',
            'probabilities': {'confirmed': .9, 'wrong_person': .1}}}
        association = {'association': {'choice': 'no',
            'probabilities': {'yes': .9, 'no': .1}}}
        self.assertEqual(jev_judge._classify(.9, network, association)[0], 'confirmed')
        del network['identity']['choice']
        del association['association']['choice']
        self.assertEqual(jev_judge._classify(.9, network, association)[0], 'confirmed')
        association['association']['probabilities'] = {'yes': .1, 'no': .9}
        self.assertEqual(jev_judge._classify(.9, network, association)[0], 'needs_review')

    def test_feature_order_does_not_depend_on_provider_json_order(self):
        network = {'competing_identity': {'noul': .2}, 'concrete_conflict': {'noul': .1},
                   'identity': {'probabilities': {'wrong_person': .1, 'confirmed': .8, 'needs_review': .1}},
                   'positive_connection': {'noul': .8}}
        association = {'source_conflict': {'noul': .1}, 'specific_connection': {'noul': .8},
                       'association': {'probabilities': {'review': .1, 'yes': .8, 'no': .1}}}
        self.assertEqual(jev_judge._probability(network, association),
                         jev_judge._probability(dict(reversed(list(network.items()))),
                                                dict(reversed(list(association.items())))))


    def test_paid_answers_resume_from_exact_cache_without_an_api_key(self):
        task = IdentityTask(DossierEvidence(name="Jordan Bravo"), JudgeProfile.from_payload({
            "full_name": "Jordan Bravo", "linkedin_url": "https://www.linkedin.com/in/jordan-bravo",
            "has_profile": True,
        }))
        requests = jev_judge._requests(task, (), "2026-10-01")
        client = _Client(*(_Response(200, _payload(request)) for request in requests.values()))
        with tempfile.TemporaryDirectory() as directory:
            kwargs = dict(imported_urls=[()], output_dir=Path(directory), reference_date="2026-10-01")
            with patch.dict("os.environ", {"TYPESAFE_API_KEY": "synthetic-test-key"}), \
                 patch.object(jev.httpx, "AsyncClient", return_value=client), \
                 patch.object(jev, "append_usage_row"):
                first = jev_judge.judge_batch([task], **kwargs)
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(first[0].usage.input_tokens, 4000)
            with patch.object(jev_judge, "load_env"), \
                 patch.dict("os.environ", {}, clear=True), \
                 patch.object(jev.httpx, "AsyncClient", side_effect=AssertionError("cache must avoid network")):
                second = jev_judge.judge_batch([task], **kwargs)
            self.assertEqual(first[0].fingerprint, second[0].fingerprint)
            self.assertEqual(first[0].verdict, second[0].verdict)
            self.assertEqual(second[0].usage.input_tokens, 0)
