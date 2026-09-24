"""Share labels follow realized identities and the same machine-worth contributor."""
import json
import unittest
import tempfile
from pathlib import Path

from packs.ingestion.primitives.share.evidence import ShareEvidence
from packs.shared.csv_io import CsvIO


def _evidence(tmp_path, *, person_id='person-a', superseded='', parent_facts=False):
    CsvIO.write_dict_rows(tmp_path / 'people.csv', ['id', 'full_name', 'superseded_person_ids'], [
        {'id': person_id, 'full_name': 'Jordan Bravo', 'superseded_person_ids': superseded},
    ])
    (tmp_path / 'index.json').write_text(json.dumps({
        'slugs': {'jordan-a': {'person_id': 'person-a'}, 'jordan-b': {'person_id': 'person-b'}},
        'parents': {'jordan': {'parent_id': 'parent-a', 'children': ['jordan-a', 'jordan-b']}},
    }))
    (tmp_path / 'facts').mkdir()
    identities = [('parent-a', 'yes', 'parent')] if parent_facts else [('person-a', 'no', 'service_provider'), ('person-b', 'yes', 'colleague')]
    for identity, worth, kind in identities:
        (tmp_path / 'facts' / f'{identity}.jsonl').write_text(json.dumps({
            'updated_at': '2026-08-01T00:00:00Z',
            'facts': {'canonical_name': 'Jordan Bravo', 'network_worth': {'decision': worth, 'reason': kind}, 'labels': {'relationship_kind': kind}},
        }) + '\n')
    return ShareEvidence(people_csv=tmp_path / 'people.csv', index_json=tmp_path / 'index.json',
                         facts_dir=tmp_path / 'facts', raw_dir=tmp_path / 'raw', dossier_dir=tmp_path / 'dossiers',
                         parents_dir=tmp_path / 'parents', overrides_csv=tmp_path / 'review.csv', owner_json=tmp_path / 'owner.json')


class ShareEvidenceLabelsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp_path = Path(temporary.name)

    def test_child_facts_follow_highest_machine_worth_and_keep_labels_together(self):
        tmp_path = self.tmp_path
        person = _evidence(tmp_path).load()[0]
        assert not person.linkedin_only
        assert person.network_worth == 'yes'
        assert person.facts['labels']['relationship_kind'] == 'colleague'
        assert person.evidence_date == '2026-08-01'


    def test_realized_row_inherits_context_through_superseded_identity(self):
        tmp_path = self.tmp_path
        person = _evidence(tmp_path, person_id='realized-person', superseded='person-a').load()[0]
        assert not person.linkedin_only
        assert person.facts['labels']['relationship_kind'] == 'colleague'


    def test_parent_keyed_facts_remain_readable(self):
        tmp_path = self.tmp_path
        person = _evidence(tmp_path, parent_facts=True).load()[0]
        assert person.facts['labels']['relationship_kind'] == 'parent'
        assert person.network_worth == 'yes'


    def test_human_worth_does_not_choose_different_machine_labels(self):
        tmp_path = self.tmp_path
        evidence = _evidence(tmp_path)
        CsvIO.write_dict_rows(tmp_path / 'review.csv', ['public_identifier', 'network_worth'], [
            {'public_identifier': 'parent-worth:parent-a', 'network_worth': 'no'},
        ])
        person = evidence.load()[0]
        assert person.network_worth == 'no'
        assert person.facts['labels']['relationship_kind'] == 'colleague'


    def test_tied_machine_worth_uses_identity_order(self):
        tmp_path = self.tmp_path
        evidence = _evidence(tmp_path)
        path = tmp_path / 'facts/person-a.jsonl'
        record = json.loads(path.read_text())
        record['facts']['network_worth']['decision'] = 'yes'
        path.write_text(json.dumps(record) + '\n')
        assert evidence.load()[0].facts['labels']['relationship_kind'] == 'service_provider'
