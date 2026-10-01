"""Vendor request failures remain errors and retry their live provider response."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.enrich import rapidapi_client as client

FAILED = {'success': False, 'data': None, 'message': 'The request failed. You will not be charged for this request'}

class ProfileTransientFailureTests(unittest.TestCase):
    def test_request_failure_is_not_permanent(self):
        self.assertFalse(client.RapidApiClient.is_permanent_failure(200, {'success': False, 'error': FAILED['message']}))
        self.assertTrue(client.RapidApiClient.is_permanent_failure(200, {'success': False, 'error': "This profile can't be accessed. Not valid LinkedIn profile"}))

    def test_existing_request_failure_retries_live_and_remains_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'jordan-bravo.json'
            path.write_text(json.dumps({'raw_response': FAILED, 'normalized_profile': {'success': False, 'error': FAILED['message']}, 'last_checked_at': now_iso(), 'status_code': 200}))
            original = path.read_text()
            instance = client.RapidApiClient(api_key='fixture', retry_attempts=1)
            with patch.object(instance, 'http_json', return_value=(200, FAILED, '')) as http:
                result = instance.get_profile('jordan-bravo', 'https://www.linkedin.com/in/jordan-bravo', cache_dir=Path(tmp))
            self.assertEqual(result['state'], client.PROFILE_ERROR)
            self.assertEqual(http.call_args.kwargs['headers']['X-Freshness'], 'live')
            self.assertEqual(path.read_text(), original)

    def test_keyless_cached_request_failure_remains_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'jordan-bravo.json'
            path.write_text(json.dumps({'raw_response': FAILED, 'normalized_profile': {
                'success': False, 'error': FAILED['message']}, 'last_checked_at': now_iso(), 'status_code': 200}))
            instance = client.RapidApiClient(api_key='')
            with patch.object(instance, 'http_json', side_effect=AssertionError('network called')):
                result = instance.get_profile('jordan-bravo', 'https://www.linkedin.com/in/jordan-bravo', cache_dir=Path(tmp))
            self.assertEqual(result['state'], client.PROFILE_ERROR)
