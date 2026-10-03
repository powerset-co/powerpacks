"""Every source member constrains a merge, including transitive joins."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from packs.ingestion.primitives.deep_context.db.models import MergeVerdictRow, ParentRow, PersonRow
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.merge_candidates import build_parents, receipts
from packs.ingestion.primitives.deep_context.merge_candidates.cluster_merge_candidates import ClusterMergeCandidates
from packs.ingestion.primitives.deep_context.merge_candidates.candidate_pairs import generate_pairs, slam_dunk_verdict
from packs.ingestion.primitives.deep_context.merge_candidates.models import MergeDecision, MergePairVerdict, MergePerson
from packs.ingestion.primitives.pipeline.contract import PeopleRow


def person(key, name, source_names=None):
    return MergePerson(key, key, name, name.lower(), parent_id=key,
                        member_person_ids=(key,), emails=('shared@example.com',),
                        source_names=tuple(source_names if source_names is not None else (name,)))


def positive(left, right, confidence):
    return MergePairVerdict(left, right, 'synthetic', MergeDecision(True, confidence, True, '', 'llm'))


class SourceMemberMergeTests(unittest.TestCase):
    def test_internal_child_negative_blocks_every_further_parent_join(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Db(Path(directory) / 'context.sqlite')
            db.project_rows((ParentRow('a', 'a'), ParentRow('b', 'b'),
                             PersonRow('a', 'a'), PersonRow('a2', 'a'), PersonRow('b', 'b')))
            db.replace_imported_people(tuple(PeopleRow(id=key, full_name='Jordan Bravo') for key in ('a', 'a2', 'b')))
            db.replace_merge_verdicts((
                MergeVerdictRow('a', 'a2', 'a', 'a2', 'internal-no', 'llm', False, .1, True, '', False),
                MergeVerdictRow('a', 'b', 'a', 'b', 'external-yes', 'llm', True, .99, True, '', True),
            ))
            self.assertEqual(build_parents._accepted_components(db), ())
            a = replace(person('a', 'Jordan Bravo'), member_person_ids=('a', 'a2'))
            with patch.object(receipts, 'merge_people', return_value=[a, person('b', 'Jordan Bravo')]):
                self.assertEqual(receipts.survey_pairs(db).pairs, [])

    def test_missing_original_roster_entry_is_unknown_not_an_invisible_child(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Db(Path(directory) / 'context.sqlite')
            db.project_rows((ParentRow('a', 'a'), ParentRow('b', 'b'),
                             PersonRow('a', 'a'), PersonRow('missing', 'a'), PersonRow('b', 'b')))
            db.replace_imported_people(tuple(PeopleRow(id=key, full_name='Jordan Bravo') for key in ('a', 'b')))
            db.replace_merge_verdicts((MergeVerdictRow('a', 'b', 'a', 'b', 'yes', 'llm',
                                                     True, .99, True, '', True),))
            self.assertEqual(build_parents._accepted_components(db), ())

    def test_survey_output_respects_a_stored_child_negative_outside_its_pairs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / 'context.sqlite')
            db.project_rows(tuple(ParentRow(key, key) for key in ('a', 'b', 'c')) + tuple(
                PersonRow(child, parent) for child, parent in [('a', 'a'), ('a2', 'a'), ('b', 'b'), ('c', 'c'), ('c2', 'c')]
            ))
            db.replace_merge_verdicts((MergeVerdictRow('a2', 'c2', 'a2', 'c2', 'old-negative', 'llm',
                                                     False, .1, True, 'two people', False),))
            a, b, c = (person(key, 'Jordan Bravo') for key in ('a', 'b', 'c'))
            a = replace(a, member_person_ids=('a', 'a2'))
            c = replace(c, member_person_ids=('c', 'c2'))
            with patch.object(receipts, 'merge_people', return_value=[a, b, c]):
                node = ClusterMergeCandidates(db=db, dossier_dir=root,
                                              out_csv=root / 'pairs.csv', out_md=root / 'pairs.md')
                result = node.execute()
            self.assertEqual(result.candidate_pairs, 1)
            self.assertEqual([(row['person_a'], row['person_b']) for row in db.query(
                'SELECT person_a,person_b FROM merge_verdicts WHERE accepted=1 ORDER BY person_a,person_b'
            )], [('a', 'b')])

    def test_representative_cannot_hide_an_incompatible_source_child(self):
        family = person('a', 'Jordan Bravo', ('Jordan Bravo', 'Casey Delta'))
        other = person('b', 'Jordan Bravo')
        self.assertEqual(generate_pairs([family, other]), [])

    def test_shared_source_endpoint_cannot_hide_conflicting_member_names(self):
        family = person('a', 'Jordan Bravo', ('Jordan Bravo', 'Casey Delta'))
        self.assertIsNone(slam_dunk_verdict(family, person('b', 'Jordan Bravo')))

    def test_unknown_original_source_child_prevents_automatic_identity(self):
        family = person('a', 'Jordan Bravo', ('Jordan Bravo', ''))
        self.assertIsNone(slam_dunk_verdict(family, person('b', 'Jordan Bravo')))

    def test_cache_binds_hidden_source_names_even_with_unchanged_display_and_facts(self):
        first = person('a', 'Jordan Bravo')
        second = person('b', 'Jordan Bravo')
        changed = replace(first, source_names=('Jordan Bravo', 'Casey Delta'))
        self.assertNotEqual(receipts.pair_sig(first, second), receipts.pair_sig(changed, second))

    def test_compatible_original_members_preserve_supported_duplicate(self):
        family = person('a', 'Jordan Bravo', ('Jordan Bravo', 'Jordan Alex Bravo'))
        other = person('b', 'Jordan Bravo')
        self.assertEqual(len(generate_pairs([family, other])), 1)
        self.assertTrue(slam_dunk_verdict(family, other).same_person)

    def test_partial_name_bridge_cannot_join_incompatible_full_names(self):
        a, b, c = (person('a', 'Alex Chow'), person('b', 'Alex C'), person('c', 'Alex Clayton'))
        decisions = [positive(a, b, .9), positive(b, c, .8)]
        accepted = [(row.person_a, row.person_b) for row in receipts.verdict_rows(decisions) if row.accepted]
        self.assertEqual(accepted, [('a', 'b')])
        self.assertEqual(receipts._confirmed([a, b, c], decisions)[1], [['a', 'b']])

    def test_parent_application_checks_every_original_member_name(self):
        members = [('a1', 'a', 'Jordan Bravo'), ('a2', 'a', 'Casey Delta'), ('b', 'b', 'Jordan Bravo')]
        stored = [SimpleNamespace(person_a='a1', person_b='b', same_person=True, accepted=True, confidence=.99)]
        with patch.object(build_parents, 'person_rows', return_value=[
            SimpleNamespace(person_id=child, parent_id=parent, is_owner=False, is_ghost=False) for child, parent, _ in members
        ]), patch.object(build_parents, 'merge_verdicts', return_value=stored), patch(
            'packs.ingestion.primitives.deep_context.merge_candidates.build_parents.imported_people',
            return_value=tuple(PeopleRow(id=child, full_name=name) for child, _, name in members),
        ):
            self.assertEqual(build_parents._accepted_components(None), ())

    def test_parent_application_checks_transitive_original_names(self):
        members = [('a', 'Alex Chow'), ('b', 'Alex C'), ('c', 'Alex Clayton')]
        stored = [SimpleNamespace(person_a=a, person_b=b, same_person=True, accepted=True, confidence=confidence)
                  for a, b, confidence in [('a', 'b', .9), ('b', 'c', .8)]]
        with patch.object(build_parents, 'person_rows', return_value=[
            SimpleNamespace(person_id=key, parent_id=key, is_owner=False, is_ghost=False) for key, _ in members
        ]), patch.object(build_parents, 'merge_verdicts', return_value=stored), patch(
            'packs.ingestion.primitives.deep_context.merge_candidates.build_parents.imported_people',
            return_value=tuple(PeopleRow(id=key, full_name=name) for key, name in members),
        ):
            self.assertEqual(build_parents._accepted_components(None), (('a', 'b'),))


if __name__ == '__main__':
    unittest.main()
