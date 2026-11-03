import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from packs.indexing.lib.artifact_io import iter_artifact_rows, write_parquet_rows
from packs.indexing.lib.io import write_jsonl
from packs.indexing.primitives.embed_records_checkpointed import embed_records_checkpointed as embeddings
from packs.indexing.primitives.enrich_companies_checkpointed import enrich_companies_checkpointed as companies
from packs.indexing.primitives.enrich_roles_checkpointed import enrich_roles_checkpointed as roles


class IndexCacheFilterTests(unittest.TestCase):
    def test_parquet_filter_before_vector_validation_and_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'embeddings.parquet'
            write_parquet_rows(path, [
                {'id': ' unrelated ', 'embedding': [1.0]},
                {'id': ' hit ', 'embedding': [1.0, 2.0]},
                {'id': 'hit', 'embedding': [3.0, 4.0]},
            ])
            rows = list(iter_artifact_rows(path, ['embedding'], key_field='id', needed_keys={'hit', 'miss'}))
            self.assertEqual(rows, [{'embedding': [1.0, 2.0]}, {'embedding': [3.0, 4.0]}])
            loaded = embeddings.load_input_embeddings(
                str(path), 'id', 'embedding', expected_dimension=2, needed_keys={'hit', 'miss'},
            )
            self.assertEqual(set(loaded), {'hit'})
            self.assertEqual(list(loaded['hit']), [3.0, 4.0])
            self.assertEqual(embeddings.load_input_embeddings(str(path), 'id', 'embedding', needed_keys=set()), {})

    def test_roles_filter_preserves_last_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'roles.jsonl'
            write_jsonl(path, [
                {'title_hash': 'unrelated', 'raw_title': 'Unrelated'},
                {'title_hash': ' hit ', 'raw_title': 'First'},
                {'title_hash': 'hit', 'raw_title': 'Last'},
            ])
            loaded = roles.load_input_classifications(str(path), needed_keys={'hit', 'miss'})
            self.assertEqual(set(loaded), {'hit'})
            self.assertEqual(loaded['hit']['raw_title'], 'Last')
            self.assertEqual(roles.load_input_classifications(str(path), needed_keys=set()), {})

    def test_company_slug_before_name_and_first_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'companies.jsonl'
            name_hit = {'company_name': '  Jordan   Labs ', 'linkedin_url': 'https://linkedin.com/company/other'}
            slug_hit = {'company_name': 'Old Name', 'linkedin_url': 'https://linkedin.com/company/jordan-labs'}
            write_jsonl(path, [
                {'company_name': 'Unrelated', 'company_public_identifier': 'unrelated'},
                name_hit, slug_hit,
                {'company_name': 'Later Name', 'company_public_identifier': 'jordan-labs'},
            ])
            requested = {'company_name': 'JORDAN LABS', 'company_key': 'linkedin_company:JORDAN-LABS'}
            keys = set(companies.company_index_keys(requested)) | {'name:missing'}
            loaded = companies.load_company_artifact(str(path), needed_keys=keys)
            self.assertEqual(set(loaded), {'slug:jordan-labs', 'name:jordan labs'})
            self.assertEqual(companies.lookup_artifact_row(loaded, requested), slug_hit)
            self.assertEqual(companies.lookup_artifact_row(loaded, {'company_name': 'Jordan Labs'}), name_hit)
            self.assertIsNone(companies.lookup_artifact_row(loaded, {'company_name': 'Missing'}))
            self.assertEqual(companies.load_company_artifact(str(path), needed_keys=set()), {})

    def test_empty_role_input_replays_an_empty_filtered_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_jsonl(root / 'input.jsonl', [])
            cache = write_jsonl(root / 'cache.jsonl', [{'title_hash': 'unrelated'}])
            result = roles.run(Namespace(
                flattened=str(source), output_dir=str(root / 'roles'), provider='openai',
                input_classifications=str(cache), allow_paid=False, api_key=None,
                checkpoint_every=1, force=False, dry_run=False, stop_after_chunks=None,
            ))
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['provider'], 'input-classifications')
            self.assertEqual(result['counts']['paid_calls'], 0)

    def test_company_all_misses_still_skips_unresolved_without_paid_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_jsonl(root / 'input.jsonl', [{'company_name': 'Jordan Labs'}])
            cache = write_jsonl(root / 'cache.jsonl', [{'company_name': 'Unrelated'}])
            result = companies.run(Namespace(
                input=str(source), output=str(root / 'output.jsonl'), output_dir=str(root / 'checkpoints'),
                provider='openai', artifact_path=str(cache), checkpoint_every=1, allow_paid=False,
                skip_unresolved=True, artifact_missing_policy='error', dry_run=False, estimate=False,
                force=False, stop_after_chunks=None,
            ))
            self.assertEqual(result['status'], 'completed')
            checkpoint = json.loads((root / 'checkpoints/checkpoint.json').read_text())
            self.assertEqual(checkpoint['skipped_unresolved'], 1)
            self.assertEqual(result['counts']['artifact_misses'], 1)


if __name__ == '__main__':
    unittest.main()
