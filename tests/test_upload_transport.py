"""Exercise the installed TurboPuffer SDK retry policy without network or spend."""
import unittest
from unittest.mock import patch

import httpx
import turbopuffer

from packs.indexing.primitives.upload_powerset.turbopuffer_writer import upsert_docs


class UploadTransportTests(unittest.TestCase):
    def _write(self, handler):
        with httpx.Client(transport=httpx.MockTransport(handler)) as http, \
             turbopuffer.Turbopuffer(api_key="synthetic-test-key", region="gcp-us-central1",
                                    http_client=http) as client, \
             patch("turbopuffer._base_client.time.sleep"):
            return upsert_docs(client.namespace("synthetic-upload-test"), "schools",
                               [{"id": "school-test", "school_name": "Example University"}])

    def test_lost_response_retries_identical_upsert(self):
        bodies = []

        def handle(request):
            bodies.append(request.content)
            if len(bodies) == 1:
                raise httpx.ReadError("synthetic lost response", request=request)
            return httpx.Response(200, json={"rows_affected": 1})

        self.assertEqual(self._write(handle), 1)
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[0], bodies[1])

    def test_rate_limit_and_server_errors_retry_at_most_four_times(self):
        for code in (429, 503):
            with self.subTest(status=code):
                calls = []

                def handle(request):
                    calls.append(request)
                    return httpx.Response(code, json={"error": "synthetic transient failure"})

                with self.assertRaises(turbopuffer.APIStatusError):
                    self._write(handle)
                self.assertEqual(len(calls), 5)

    def test_invalid_request_is_not_retried(self):
        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(400, json={"error": "synthetic bad request"})

        with self.assertRaises(turbopuffer.BadRequestError):
            self._write(handle)
        self.assertEqual(len(calls), 1)
