"""Batch avatar signing uses one server-side call without exposing credentials."""

from __future__ import annotations

import http.client
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from packs.shared.web.app import AppRoutes
from packs.shared.web.profile_images import MAX_BODY_BYTES
from packs.shared.web.server import persistent_handler

IMAGE_URL = "https://media.licdn.com/dms/image/synthetic-profile?e=1&v=beta&t=expired"
SIGNED_PATH = "/profile-image?url=synthetic&expires=123&signature=synthetic"
BATCH_PATH = "/api/profile-image/sign/batch"


class _Handler(BaseHTTPRequestHandler):
    routes = AppRoutes()

    def do_GET(self) -> None:  # noqa: N802
        if not self.routes.get(self, urllib.parse.urlparse(self.path)):
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if not self.routes.post(self, urllib.parse.urlparse(self.path)):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(HTTPStatus.NOT_FOUND)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


class ProfileImageRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._serve(_Handler)
        self.addCleanup(patch.stopall)
        patch("packs.shared.web.profile_images._read_env_file", return_value={}).start()
        patch.dict("os.environ", {"POWERSET_API_KEY": "synthetic-service-key"}).start()
        self.gateway = patch("packs.shared.web.profile_images.urllib.request.urlopen").start()
        self._gateway_response({"paths": [SIGNED_PATH, None]})

    def _serve(self, handler: type[BaseHTTPRequestHandler]) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _gateway_response(self, payload: object) -> None:
        self.gateway.return_value.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())

    def _post(self, payload: object, *, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        return self._request("POST", BATCH_PATH, json.dumps(payload).encode(), headers)

    def _request(self, method: str, path: str, body: bytes = b"", headers: dict[str, str] | None = None
                 ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        connection.request(method, path, body=body, headers={"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        result = response.status, dict(response.headers), response.read()
        connection.close()
        return result

    def test_batch_uses_one_gateway_post_and_preserves_paths_without_exposing_key(self) -> None:
        urls = [IMAGE_URL, "https://example.com/not-linkedin"]
        status, headers, body = self._post({"urls": urls})
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(json.loads(body), {"paths": [SIGNED_PATH, None]})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("synthetic-service-key", str(headers) + body.decode())
        self.gateway.assert_called_once()
        request = self.gateway.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.full_url, "https://proxy.powerset.dev/profile-image/sign/batch")
        self.assertEqual(request.get_header("X-powerset-key"), "synthetic-service-key")
        self.assertEqual(json.loads(request.data), {"urls": urls})

    def test_invalid_batch_and_json_never_request_signing(self) -> None:
        for payload in ({}, [], {"urls": "wrong"}, {"urls": []}, {"urls": [IMAGE_URL] * 101}):
            self.assertEqual(self._post(payload)[0], HTTPStatus.BAD_REQUEST, payload)
        self.assertEqual(self._request("POST", BATCH_PATH, b"not JSON")[0], HTTPStatus.BAD_REQUEST)
        self.gateway.assert_not_called()

    def test_invalid_body_lengths_never_request_signing(self) -> None:
        for length in ("0", "-1", "invalid", str(MAX_BODY_BYTES + 1)):
            self.assertEqual(self._request("POST", BATCH_PATH, headers={"Content-Length": length})[0],
                             HTTPStatus.BAD_REQUEST, length)
        self.gateway.assert_not_called()

    def test_cross_origin_requests_are_rejected_and_same_origin_is_allowed(self) -> None:
        urls = {"urls": [IMAGE_URL, "invalid"]}
        self.assertEqual(self._post(urls, headers={"Origin": "https://other.example"})[0], HTTPStatus.FORBIDDEN)
        self.gateway.assert_not_called()
        origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.assertEqual(self._post(urls, headers={"Origin": origin})[0], HTTPStatus.OK)

    def test_reads_checkout_key_when_process_key_is_missing(self) -> None:
        with patch.dict("os.environ", {"POWERSET_API_KEY": ""}), \
                patch("packs.shared.web.profile_images._read_env_file", return_value={"POWERSET_API_KEY": "synthetic-file-key"}):
            self.assertEqual(self._post({"urls": [IMAGE_URL, "invalid"]})[0], HTTPStatus.OK)
        self.assertEqual(self.gateway.call_args.args[0].get_header("X-powerset-key"), "synthetic-file-key")

    def test_missing_key_does_not_request_signing(self) -> None:
        with patch.dict("os.environ", {"POWERSET_API_KEY": ""}):
            status, headers, body = self._post({"urls": [IMAGE_URL]})
        self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual((headers["Content-Type"], body), ("text/plain", b""))
        self.gateway.assert_not_called()

    def test_signing_failure_does_not_expose_upstream_details(self) -> None:
        self.gateway.side_effect = urllib.error.HTTPError("synthetic", 401, "synthetic-service-key", {}, None)
        status, headers, body = self._post({"urls": [IMAGE_URL]})
        self.assertEqual(status, HTTPStatus.BAD_GATEWAY)
        self.assertEqual((headers["Content-Type"], body), ("text/plain", b""))

    def test_invalid_gateway_responses_cannot_return_external_paths(self) -> None:
        for payload in ({}, [], {"paths": SIGNED_PATH}, {"paths": []}, {"paths": [123]},
                        {"paths": ["https://evil.example/image"]}, {"paths": ["//evil.example/image"]},
                        {"paths": ["/profile-image?url=x\r\nInjected: value"]}):
            self._gateway_response(payload)
            self.assertEqual(self._post({"urls": [IMAGE_URL]})[0], HTTPStatus.BAD_GATEWAY, payload)

    def test_obsolete_single_get_and_other_post_routes_fall_through(self) -> None:
        self.assertEqual(self._request("GET", "/api/profile-image?url=synthetic")[0], HTTPStatus.NOT_FOUND)
        self.assertEqual(self._request("POST", "/api/unrelated", b"{}")[0], HTTPStatus.NOT_FOUND)
        self.gateway.assert_not_called()

    def test_persistent_server_signs_batches_before_network_mount(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            self._serve(persistent_handler(Path(root)))
            status, _, body = self._post({"urls": [IMAGE_URL, "invalid"]})
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(json.loads(body), {"paths": [SIGNED_PATH, None]})
        self.gateway.assert_called_once()


if __name__ == "__main__":
    unittest.main()
