"""Explanations use decision contributions, not the highest raw probabilities."""
import unittest
from unittest.mock import patch
from packs.ingestion.primitives.deep_context.jev_worth import model, runner


class JevWorthReasonTests(unittest.TestCase):
    def test_low_work_evidence_can_outweigh_high_relationship_score(self):
        answers = {'work_signal': {'type':'noul','noul':.1},
                   'real_relationship': {'type':'noul','noul':.95},
                   'transactional_only': {'type':'noul','noul':.8}}
        fitted = {'classes':['no','yes','maybe'],
                  'features':['worth:work_signal','worth:real_relationship','worth:transactional_only'],
                  'mean':[.5,.5,.5], 'scale':[1,1,1], 'intercept':[0,0,-10],
                  'coefficients':[[-5,-1,3],[0,0,0],[0,0,0]]}
        with patch.dict(model.MODEL,fitted,clear=True):
            self.assertEqual(model.predict(answers),'no')
            ranked = model.supporting_features(answers,decision='no')
            self.assertEqual([x[0] for x in ranked],['work_signal','transactional_only'])
            reason = runner._reason(answers,decision='no')
        self.assertIn('little evidence of work-related context',reason)
        self.assertIn('mainly transactional',reason)
        self.assertNotIn('direct correspondence',reason)

    def test_choice_explains_supporting_option_not_argmax(self):
        answers={'relationship_kind':{'type':'choice','probabilities':{'friend':.8,'stranger':.2}}}
        fitted={'classes':['no','yes','maybe'],
                'features':['tag:relationship_kind=friend','tag:relationship_kind=stranger'],
                'mean':[.9,.1], 'scale':[1,1], 'intercept':[0,0,-10],
                'coefficients':[[0,10],[0,0],[0,0]]}
        with patch.dict(model.MODEL,fitted,clear=True):
            reason=runner._reason(answers,decision='no')
        self.assertNotIn('friend',reason)
        self.assertIn('little evidence of unsolicited contact',reason)

    def test_direct_verdict_is_never_an_explanation(self):
        answers={'worth':{'type':'choice','probabilities':{'no':.9,'yes':.1}}}
        fitted={'classes':['no','yes','maybe'],'features':['worth:worth=no','worth:worth=yes'],
                'mean':[0,0],'scale':[1,1],'intercept':[0,0,-10],
                'coefficients':[[10,0],[0,0],[0,0]]}
        with patch.dict(model.MODEL,fitted,clear=True):
            reason=runner._reason(answers,decision='no')
        self.assertIn('no clear explanation',reason)
        self.assertNotIn('worth answer',reason)

    def test_templates_never_dump_scores(self):
        with patch.object(runner,'supporting_features',return_value=[('noise',None,.94),('is_stranger',None,.9),('work_signal',None,.05)]):
            reason=runner._reason({},decision='no')
        self.assertIn('unsolicited outreach',reason)
        for token in ('%', 'JEV:', '_', 'cutoff'):
            self.assertNotIn(token,reason)

    def test_repeated_absences_are_combined_and_outreach_deduplicated(self):
        features = [('is_stranger', None, .1), ('noise', None, .1), ('is_founder', None, .1)]
        with patch.object(runner, 'supporting_features', return_value=features):
            reason = runner._reason({}, decision='yes')
        self.assertEqual(reason, "There's little evidence of unsolicited outreach or broadcasts, or a founder background.")

    def test_mixed_strengths_form_separate_sentences(self):
        features = [('transactional_only', None, .9), ('relationship_kind', 'service_provider', .8),
                    ('work_signal', None, .1)]
        with patch.object(runner, 'supporting_features', return_value=features):
            reason = runner._reason({}, decision='no')
        self.assertEqual(reason, "Looks like mainly transactional contact and a service-provider relationship. "
                         "There's little evidence of work-related context.")
