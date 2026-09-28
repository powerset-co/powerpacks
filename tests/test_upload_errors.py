"""Upload failures remain diagnosable across retries without leaking credentials."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

import httpx
import turbopuffer

from packs.indexing.primitives.upload_powerset.upload_powerset import UploadPowerset
from packs.indexing.primitives.upload_powerset.manifest import Stage


class UploadErrorTests(unittest.TestCase):
    def test_provider_failure_records_traceback_body_request_and_survives_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploader = UploadPowerset(db=root/'db', share_db=root/'share', people_csv=root/'people',
                                      out_dir=root/'out')
            request = httpx.Request('POST', 'https://api.example.test/v2/namespaces/education/query',
                                    headers={'Authorization': 'Bearer synthetic-private-key'})
            response = httpx.Response(400, request=request, headers={'x-request-id': 'request-example'})
            error = turbopuffer.BadRequestError('Invalid schema synthetic-private-key', response=response,
                       body={'error': 'attribute person_id is not filterable', 'token': 'synthetic-private-key'})

            def fail(payload, previous):
                replace(payload, stage=Stage.EDUCATION).write(uploader.manifest_path)
                raise error

            with patch.object(uploader, '_run', side_effect=fail), \
                    patch.dict(os.environ, {'TURBOPUFFER_API_KEY': 'synthetic-private-key'}):
                for _ in range(2):
                    with self.assertRaises(turbopuffer.BadRequestError):
                        uploader.run()
            log = (root/'out'/'errors.log').read_text()
            self.assertEqual(log.count('stage=education'), 2)
            self.assertIn('Traceback', log)
            self.assertIn('attribute person_id is not filterable', log)
            self.assertIn('request-example', log)
            self.assertIn('/education/query', log)
            self.assertNotIn('synthetic-private-key', log)
            self.assertEqual((root/'out'/'errors.log').stat().st_mode & 0o777, 0o600)
            with patch.object(uploader, '_run', return_value={'status': 'completed'}):
                uploader.run()
            self.assertEqual((root/'out'/'errors.log').read_text(), log)
            self.assertNotIn('person_id is not filterable', json.dumps(json.loads((root/'out'/'manifest.json').read_text())))

    def test_connection_failure_redacts_passwords_and_bearer_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploader = UploadPowerset(db=root/'db', share_db=root/'share', people_csv=root/'people', out_dir=root)
            with patch.dict(os.environ, {'DATABASE_URL':'postgresql://user:synthetic-password@host/db'}), \
                 patch.object(uploader, '_run', side_effect=ConnectionError(
                     'postgresql://user:synthetic-password@host/db Authorization: Bearer unknown-token')):
                with self.assertRaises(ConnectionError):
                    uploader.run()
            log = (root/'errors.log').read_text()
            self.assertIn('ConnectionError', log)
            self.assertNotIn('synthetic-password', log)
            self.assertNotIn('unknown-token', log)
