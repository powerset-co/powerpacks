"""Persisted JEV labels reach every review profile surface."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.deep_context.review_web import model, rendering


class ProfileLabelTests(unittest.TestCase):
    def test_badges_limit_three_and_reveal_remaining_on_focus(self):
        parent = {'labels': {'relationship_kind': 'colleague', 'is_founder': .9,
                             'is_investor': .86, 'is_professional': .9, 'is_classmate': .85}}
        markup = rendering._label_badges(parent)
        self.assertEqual(markup.count("class='person-label'"), 3)
        self.assertIn('tabindex=', markup)
        self.assertIn('role=', markup)
        self.assertIn('Work-related', markup)
        self.assertIn('>+1<', markup)
        self.assertEqual(rendering._label_badges({'labels': {}}), '')

    def test_badges_hide_scores_below_85_percent(self):
        labels = {'relationship_kind': 'unknown', 'relationship_kind_p': .95,
                  'evidence_incomplete': .88, 'real_relationship': .35,
                  'is_personal': .24, 'is_founder': .04}
        markup = rendering._label_badges({'labels': labels})
        visible = markup.split("class='person-label-more'")[0]
        self.assertEqual(visible.count("class='person-label'"), 1)
        self.assertIn('Limited context 88%', visible)
        self.assertNotIn('Direct contact', markup)
        self.assertNotIn('Personal', markup)
        self.assertNotIn('Founder', visible)
        self.assertNotIn('Unknown', markup)
        self.assertNotIn("class='person-label-more'", markup)

    def test_badges_escape_label_values(self):
        markup = rendering._label_badges({'labels': {'relationship_kind': '<script>alert(1)</script>'}})
        self.assertNotIn('<script>', markup)

    def test_annotation_reads_labels_for_the_worth_identity(self):
        with tempfile.TemporaryDirectory() as root:
            facts = Path(root)
            (facts / 'person-a.jsonl').write_text(json.dumps({'facts': {'labels': {'is_founder': .9}}})+'\n')
            parent = {'slug':'jordan','person_ids':['person-a'],'candidates':[]}
            row = {'parent_slug':'jordan','person_ids':['person-a'],'machine':{'decision':'yes'},'effective':'yes'}
            with patch.object(model.worth_view,'rows_from',return_value=[row]):
                model.annotate_worth([parent],{},facts)
            self.assertEqual(parent['labels']['is_founder'], .9)

    def test_profile_headers_share_badges(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            parent = {'name':'Jordan Bravo','slug':'jordan','person_ids':['person-a'],
                      'candidates':[], 'labels':{'is_founder':.9},
                      'worth':{'decision':'yes','reason':'Work contact'},
                      'machine_worth':{'decision':'yes','reason':'Work contact'}}
            for render in (rendering.render_worth_card, rendering.render_person_detail):
                with patch.object(rendering,'_recent_messages_html',return_value=''):
                    markup=render(parent,path,path,path)
                self.assertIn("class='person-label'",markup)
                self.assertLess(markup.index('<h2>'),markup.index("class='person-label'"))
            markup=rendering._decision_row_html(parent,'yes',path,path)
            self.assertIn('person-name-line',markup)
            self.assertIn('decision-expanded-profile',markup)
            self.assertIn("class='person-label'",markup)

    def test_linkedin_single_and_multiple_candidates_share_labels(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            parent = {'name': 'Jordan Bravo', 'slug': 'jordan', 'person_ids': ['person-a'],
                      'labels': {'is_founder': .9}, 'candidates': []}
            candidates = [{'pub': 'jordan-bravo', 'url': 'https://www.linkedin.com/in/jordan-bravo/',
                           'full_name': 'Jordan Bravo', 'has_profile': True},
                          {'pub': 'jordan-other', 'url': 'https://www.linkedin.com/in/jordan-other/',
                           'full_name': 'Jordan Bravo', 'has_profile': True}]
            for choices in (candidates[:1], candidates):
                with patch.object(rendering, '_hydrate_card_profile', return_value=False):
                    markup = rendering.render_linkedin_card(parent, choices, path, path, path)
                self.assertIn("class='person-label'", markup)

    def test_yes_no_badges_omit_percentages(self):
        parent = {'name':'Jordan Bravo','slug':'jordan','person_ids':['person-a'],
                  'candidates':[], 'labels':{'is_founder':.9},
                  'machine_worth':{'decision':'no','reason':'Limited relationship evidence'}}
        for decision in ('yes', 'no'):
            markup = rendering._decision_row_html(parent, decision, None, None)
            self.assertIn('Founder', markup)
            self.assertNotIn('90%', markup)
            summary = markup.split('</summary>')[0]
            self.assertNotIn('Limited relationship evidence', summary)
            self.assertIn('Limited relationship evidence', markup)

    def test_missing_linkedin_card_skip_uses_review_row_key(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            parent = {'name':'Jordan Bravo','slug':'jordan','person_ids':['identity-other']}
            candidate = {'pub':'', 'row_key':'message-linkedin:synthetic', 'url':''}
            markup = rendering.render_linkedin_card(parent, candidate, path, path, path)
            self.assertIn("data-pub='message-linkedin:synthetic'", markup)
            self.assertNotIn('Use this profile', markup)
