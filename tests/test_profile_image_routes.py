"""Local avatars sign server-side and redirect without exposing credentials."""

from __future__ import annotations

import http.client
import io
import json
import threading
import unittest
import urllib.error
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from packs.shared.web.app import AppRoutes

IMAGE_URL = "https://media.licdn.com/dms/image/synthetic-profile?e=1&v=beta&t=expired"
SIGNED_PATH = "/profile-image?url=synthetic&expires=123&signature=synthetic"


class _Handler(BaseHTTPRequestHandler):
    routes = AppRoutes()

    def do_GET(self) -> None:  # noqa: N802
        if not self.routes.get(self, urllib.parse.urlparse(self.path)):
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


class ProfileImageRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(patch.stopall)
        patch("packs.shared.web.profile_images._read_env_file", return_value={}).start()
        patch.dict("os.environ", {"POWERSET_API_KEY": "synthetic-service-key"}).start()
        self.gateway = patch("packs.shared.web.profile_images.urllib.request.urlopen").start()
        self.gateway.return_value.__enter__.return_value = io.BytesIO(json.dumps({"path": SIGNED_PATH}).encode())

    def _get(self, url: str) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        connection.request("GET", "/api/profile-image?" + urllib.parse.urlencode({"url": url}))
        response = connection.getresponse()
        result = response.status, dict(response.headers), response.read()
        connection.close()
        return result

    def test_linkedin_url_redirects_to_signed_cache_url_with_key_only_on_server(self) -> None:
        status, headers, body = self._get(IMAGE_URL)
        self.assertEqual(status, HTTPStatus.FOUND)
        self.assertEqual(headers["Location"], "https://proxy.powerset.dev" + SIGNED_PATH)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("synthetic-service-key", str(headers) + body.decode())
        request = self.gateway.call_args.args[0]
        self.assertEqual(request.get_header("X-powerset-key"), "synthetic-service-key")
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query), {"url": [IMAGE_URL]})

    def test_invalid_urls_never_request_signing(self) -> None:
        for url in ("", "http://media.licdn.com/image", "https://evil.example/image",
                    "https://media.licdn.com.evil.example/image", "https://media.licdn.com@evil.example/image",
                    "https://[malformed/image"):
            self.assertEqual(self._get(url)[0], HTTPStatus.BAD_REQUEST, url)
        self.gateway.assert_not_called()

    def test_reads_checkout_key_when_process_key_is_missing(self) -> None:
        with patch.dict("os.environ", {"POWERSET_API_KEY": ""}), \
                patch("packs.shared.web.profile_images._read_env_file", return_value={"POWERSET_API_KEY": "synthetic-file-key"}):
            self.assertEqual(self._get(IMAGE_URL)[0], HTTPStatus.FOUND)
        self.assertEqual(self.gateway.call_args.args[0].get_header("X-powerset-key"), "synthetic-file-key")

    def test_missing_key_returns_non_image_without_requesting_signing(self) -> None:
        with patch.dict("os.environ", {"POWERSET_API_KEY": ""}):
            status, headers, body = self._get(IMAGE_URL)
        self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual((headers["Content-Type"], body), ("text/plain", b""))
        self.gateway.assert_not_called()

    def test_signing_failure_returns_non_image_without_upstream_details(self) -> None:
        self.gateway.side_effect = urllib.error.HTTPError("synthetic", 401, "synthetic-service-key", {}, None)
        status, headers, body = self._get(IMAGE_URL)
        self.assertEqual(status, HTTPStatus.BAD_GATEWAY)
        self.assertEqual((headers["Content-Type"], body), ("text/plain", b""))

    def test_invalid_gateway_responses_cannot_redirect_outside_image_route(self) -> None:
        for payload in ({}, [], {"path": "https://evil.example/image"}, {"path": "//evil.example/image"},
                        {"path": "/profile-image?url=x\r\nInjected: value"}):
            self.gateway.return_value.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())
            self.assertEqual(self._get(IMAGE_URL)[0], HTTPStatus.BAD_GATEWAY, payload)


if __name__ == "__main__":
    unittest.main()
