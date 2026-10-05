"""Insufficient identity evidence neither joins nor separates people."""
import unittest

from packs.ingestion.primitives.deep_context.merge_candidates.candidate_pairs import accepted_edges


class UncertainMergeTests(unittest.TestCase):
    def test_uncertain_pair_does_not_block_two_proven_connections(self):
        self.assertEqual(accepted_edges([
            ('a', 'b', True, .99), ('b', 'c', True, .98), ('a', 'c', None, .9),
        ]), [('a', 'b'), ('b', 'c')])

    def test_different_pair_blocks_transitive_merge(self):
        self.assertEqual(accepted_edges([
            ('a', 'b', True, .99), ('b', 'c', True, .98), ('a', 'c', False, .9),
        ]), [('a', 'b')])

    def test_uncertain_pair_does_not_merge(self):
        self.assertEqual(accepted_edges([('a', 'b', None, .99)]), [])
