"""A failed batch leaves the person pending and the prior facts intact."""
from __future__ import annotations

import json
import tempfile
import unittest
from collections import Counter
from http import HTTPStatus
from pathlib import Path
from unittest import mock

import httpx
from openai import AsyncOpenAI

from deep_context_sqlite_test_helpers import message_payload
from packs.ingestion.primitives.deep_context.db.models import ArtifactRow, OwnerContextRow, ParentRow, PersonRow
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.shared import openai_responses
from packs.ingestion.primitives.deep_context.synthesis import runner
from packs.ingestion.primitives.deep_context.synthesis.synthesize_person_context import (
    DEFAULT_MAX_RETRIES, SynthesizePersonContext, build_parser, main,
)


class SynthesisFailureTests(unittest.TestCase):
    def test_default_is_three_sdk_retries(self):
        self.assertEqual(DEFAULT_MAX_RETRIES, 3)
        self.assertEqual(build_parser().parse_args([]).max_retries, 3)

    def test_failed_cli_returns_nonzero(self):
        with mock.patch('packs.ingestion.primitives.deep_context.synthesis.synthesize_person_context.open_existing_db'), \
                mock.patch.object(SynthesizePersonContext, 'run') as run, \
                mock.patch('packs.ingestion.primitives.deep_context.synthesis.synthesize_person_context.emit'):
            run.return_value.status = 'failed'
            self.assertEqual(main([]), 1)

    def _run(self, root: Path, *, failures: int, force: bool = False, text: str = "alpha-marker", failure_marker: str = "beta"):
        raw = root / 'raw'
        raw.mkdir(exist_ok=True)
        db = Db(root / 'deep-context.sqlite')
        bundle = {'person_id': 'parent-casey', 'messages': [
            message_payload(text, at='2026-01-01'),
            message_payload('beta-marker', at='2026-01-02'),
        ]}
        db.project_rows((
            OwnerContextRow('owner', json.dumps({'name': 'Synthetic Owner'}), str(root / 'owner.json'), '0' * 64),
            ParentRow('parent-casey', 'parent-worth:parent-casey'),
            PersonRow('person-casey', 'parent-casey'),
            ArtifactRow('source-bundle:parent-casey', 'source_bundle', 'parent-casey', str(raw / 'parent-casey.json'),
                        '1' * 64, 'projected', payload_json=json.dumps(bundle)),
        ))
        counts = Counter()

        def respond(request):
            body = json.loads(request.content)
            marker = 'beta' if 'beta-marker' in body['input'][1]['content'] else 'alpha'
            counts[marker] += 1
            if marker == failure_marker and counts[marker] <= failures:
                return httpx.Response(HTTPStatus.TOO_MANY_REQUESTS,
                    headers={'retry-after-ms': '1'}, json={'error': {'message': 'private content', 'type': 'rate_limit'}})
            return httpx.Response(HTTPStatus.OK, json={
                'id': 'resp_test', 'object': 'response', 'created_at': 0, 'status': 'completed', 'model': 'fixture-model',
                'output': [{'id': 'msg_test', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                            'content': [{'type': 'output_text', 'text': json.dumps({'canonical_name': 'Casey Bravo'}), 'annotations': []}]}],
                'usage': {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12},
            })

        def client(**kwargs):
            return AsyncOpenAI(**{**kwargs, 'api_key': 'synthetic-key', 'base_url': 'https://synthetic.test'},
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))

        node = SynthesizePersonContext(db=db, raw_dir=raw, out_dir=root / 'facts', people_csv=root / 'people.csv',
                                       model='fixture-model', chunk_chars=1, force=force)
        with mock.patch.object(openai_responses, 'AsyncOpenAI', side_effect=client), \
                mock.patch.object(runner, 'tag_saved_facts', return_value=runner.JevUsage()) as tag:
            result = node.run()
        return db, node, result, counts, tag

    def test_transient_429_succeeds_on_fourth_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, node, result, counts, _ = self._run(root, failures=3)
            self.assertEqual(counts, {'alpha': 1, 'beta': 4})
            self.assertEqual(result.status, 'completed')
            self.assertTrue((root / 'facts/parent-casey.jsonl').is_file())
            self.assertEqual(node._plan().bundles, ())

    def test_exhausted_batch_keeps_prior_facts_and_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db, _, _, _, _ = self._run(root, failures=0)
            path = root / 'facts/parent-casey.jsonl'
            prior_bytes = path.read_bytes()
            prior_row = tuple(db.query('SELECT * FROM facts')[0])
            _, node, result, counts, tag = self._run(root, failures=10, force=True)
            self.assertEqual(counts, {'alpha': 1, 'beta': 4})
            self.assertEqual(result.status, 'failed')
            self.assertEqual(path.read_bytes(), prior_bytes)
            self.assertEqual(tuple(db.query('SELECT * FROM facts')[0]), prior_row)
            tag.assert_not_called()
            manifest = json.loads((root / 'facts/manifest.json').read_text())
            self.assertEqual(manifest['failures'], [{'person_id': 'parent-casey', 'batch': 1, 'error': 'RateLimitError HTTP 429'}])
            self.assertNotIn('private content', json.dumps(manifest))
            resumed = SynthesizePersonContext(db=db, raw_dir=root / 'raw', out_dir=root / 'facts',
                people_csv=root / 'people.csv', model='fixture-model', chunk_chars=1, force=True)
            self.assertEqual([bundle.person_id for bundle in resumed._plan().bundles], ['parent-casey'])

    def test_changed_evidence_failure_retries_normally(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root, failures=0)
            path = root / 'facts/parent-casey.jsonl'
            prior = path.read_bytes()
            _, node, result, _, _ = self._run(root, failures=10, text='alpha-marker changed', failure_marker='alpha')
            self.assertEqual(result.status, 'failed')
            self.assertEqual(path.read_bytes(), prior)
            self.assertEqual([bundle.person_id for bundle in node._plan().bundles], ['parent-casey'])

    def test_exhausted_new_person_is_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db, node, result, counts, _ = self._run(root, failures=10)
            self.assertEqual(counts['beta'], 4)
            self.assertEqual(result.status, 'failed')
            self.assertFalse((root / 'facts/parent-casey.jsonl').exists())
            self.assertEqual(db.query('SELECT COUNT(*) FROM facts')[0][0], 0)
            self.assertEqual(len(node._plan().bundles), 1)
