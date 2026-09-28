"""Visible search status and compact summary controls."""
import unittest
from dataclasses import replace

from packs.search.primitives.deep_search.results_web.model import SearchCard
from packs.search.primitives.deep_search.results_web.rendering import _status_text, render_catalog


class SearchPolishTest(unittest.TestCase):
    def test_finished_pond_is_complete_to_the_reader(self):
        self.assertEqual(_status_text('awaiting_diagnosis'), 'Search Complete')
        self.assertEqual(_status_text('completed'), 'Search Complete')
        self.assertEqual(_status_text('ready_to_run'), 'Ready to run')

    def test_people_popover_contains_pins_and_score_distribution(self):
        card = SearchCard(run_id='example-role', title='Engineer', company='Example',
                          status='awaiting_diagnosis', created_at='', updated_at='',
                          search_version='3.1.0', candidates=23, ponds_run=1, cost_usd=1,
                          pinned=7, ce_scored=23, score_5=3, score_4=8, score_3=12)
        page = render_catalog((card,))
        self.assertIn("aria-describedby='counts-example-role'", page)
        self.assertIn('Pinned <b>7</b>', page)
        for score, count in ((5, 3), (4, 8), (3, 12)):
            self.assertIn(f'{score} / 5 <b>{count}</b>', page)
        self.assertIn('Search Complete', page)

    def test_routes_hide_pending_and_non_ce_searches(self):
        from pathlib import Path
        from unittest.mock import patch
        from packs.search.primitives.deep_search.results_web.server import search_routes
        card = SearchCard('example-role', 'Engineer', 'Example', 'completed', '', '', '3.1.0', 2, 1, 0,
                          ce_scored=2)
        with patch('packs.search.primitives.deep_search.results_web.server.load_catalog',
                   return_value=(card, replace(card, run_id='pending', ce_scored=0))):
            self.assertEqual(search_routes(Path('/unused')).catalog(), (card,))
