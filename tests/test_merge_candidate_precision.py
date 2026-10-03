"""Merge proposals need compatible names and source contact identifiers."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow, FactRow, ParentRow, PersonRow, PersonIdentifierRow, PersonIdentifiersProjection,
)
from packs.ingestion.primitives.deep_context.db.merge_queries import merge_people
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.merge_candidates.candidate_pairs import generate_pairs, slam_dunk_verdict
from packs.ingestion.primitives.deep_context.merge_candidates.models import MergePerson
from packs.ingestion.primitives.deep_context.merge_candidates.receipts import survey_pairs
from packs.ingestion.primitives.pipeline.contract import PeopleRow


def person(person_id, name, **identifiers):
    return MergePerson(person_id, person_id, name, name.lower(), parent_id=f'parent-{person_id}',
                       member_person_ids=(person_id,), **identifiers)


class TestMergePrecision(unittest.TestCase):
    def test_missing_source_name_cannot_borrow_an_extracted_or_parent_name(self):
        for parent_name in ("", "Jordan Bravo"):
            with self.subTest(parent_name=parent_name), tempfile.TemporaryDirectory() as directory:
                db = Db(Path(directory) / 'deep-context.sqlite')
                for person_id, display_name in (
                    ('unknown', parent_name), ('named', 'Jordan Bravo'),
                ):
                    parent_id = 'parent-' + person_id
                    db.project_rows((
                        ParentRow(parent_id, 'parent-worth:' + parent_id, display_name),
                        PersonRow(person_id, parent_id, display_name=display_name),
                        PersonIdentifiersProjection(person_id, (
                            PersonIdentifierRow(person_id, 'phone', '+15550100123'),
                        )),
                        ArtifactRow('facts:' + parent_id, 'facts', parent_id, '/synthetic/facts.jsonl',
                                    '0' * 64, 'projected'),
                        FactRow(parent_id, parent_id, 'facts:' + parent_id,
                                facts_json=json.dumps({'canonical_name': 'Jordan Bravo'})),
                    ))
                db.replace_imported_people((PeopleRow(id='unknown', full_name=''),
                                            PeopleRow(id='named', full_name='Jordan Bravo')))
                hydrated = {person.person_id: person for person in merge_people(db)}
                self.assertEqual(hydrated['unknown'].name, '')
                self.assertEqual(survey_pairs(db).pairs, [])

    def test_representative_is_a_named_original_source_member(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Db(Path(directory) / 'deep-context.sqlite')
            db.project_rows((
                ParentRow('parent-family', 'parent-worth:parent-family', 'Wrong Extracted Name'),
                PersonRow('a-aggregate', 'parent-family', display_name='Wrong Extracted Name'),
                PersonRow('b-unknown', 'parent-family', display_name='Wrong Extracted Name'),
                PersonRow('c-source', 'parent-family', display_name='Wrong Extracted Name'),
                ArtifactRow('facts:parent-family', 'facts', 'parent-family', '/synthetic/facts.jsonl',
                            '0' * 64, 'projected'),
                FactRow('parent-family', 'parent-family', 'facts:parent-family',
                        facts_json=json.dumps({'canonical_name': 'Wrong Extracted Name'})),
            ))
            db.replace_imported_people((PeopleRow(id='b-unknown'),
                                        PeopleRow(id='c-source', full_name='Casey Delta')))
            self.assertEqual([(person.person_id, person.name) for person in merge_people(db)],
                             [('c-source', 'Casey Delta')])
            db.replace_imported_people(())
            self.assertEqual(merge_people(db), [])

    def test_extracted_office_phone_does_not_propose_two_staff_as_one_person(self):
        first = person('a', 'Jordan Bravo', extra_phones=('15550100',))
        second = person('b', 'Casey Delta', extra_phones=('15550100',))
        self.assertEqual(generate_pairs([first, second]), [])

    def test_extracted_identifier_alone_does_not_pair_a_short_name(self):
        first = person('a', 'Jordan', extra_emails=('jordan@example.com',))
        second = person('b', 'Jordan Bravo', emails=('jordan@example.com',))
        self.assertEqual(generate_pairs([first, second]), [])

    def test_source_office_phone_does_not_override_incompatible_names(self):
        first = person('a', 'Jordan Bravo', phone_digits=('15550100',))
        second = person('b', 'Casey Delta', phone_digits=('15550100',))
        self.assertEqual(generate_pairs([first, second]), [])

    def test_same_name_without_a_source_tie_requires_a_positive_judgment(self):
        first = person('a', 'Jordan Bravo', extra_phones=('15550100',))
        second = person('b', 'Jordan Bravo', extra_phones=('15550100',))
        self.assertIsNone(slam_dunk_verdict(first, second))
        with tempfile.TemporaryDirectory() as directory:
            db = Db(Path(directory) / 'deep-context.sqlite')
            db.project_rows(tuple(row for subject in (first, second) for row in (
                ParentRow(subject.parent_id, f'parent-worth:{subject.parent_id}', subject.name, subject.slug),
                PersonRow(subject.person_id, subject.parent_id, subject.slug, subject.slug, subject.name),
            )))
            with patch('packs.ingestion.primitives.deep_context.merge_candidates.receipts.merge_people',
                       return_value=[first, second]):
                survey = survey_pairs(db)
            self.assertEqual(survey.slam, [])
            self.assertEqual(len(survey.to_judge), 1)

    def test_source_email_and_exact_same_name_preserve_a_free_duplicate_join(self):
        first = person('a', 'Jordan Bravo', emails=('jordan@example.com',))
        second = person('b', 'Jordan Bravo', emails=('jordan@example.com',))
        self.assertEqual(len(generate_pairs([first, second])), 1)
        verdict = slam_dunk_verdict(first, second)
        self.assertTrue(verdict.same_person)
        self.assertEqual((verdict.judge, verdict.confidence), ('slam_dunk', .99))


if __name__ == '__main__':
    unittest.main()
