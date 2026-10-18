"""Local desktop feature flags answer before project dependencies mount."""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from packs.shared.web import account
from packs.shared.web.server import persistent_handler


class FeatureFlagsTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.account = self.enterContext(patch.object(account, "read", return_value={"email": "casey@example.com"}))
        mount = patch("packs.shared.web.server.mounted_handler", side_effect=AssertionError("mounted"))
        mount.start()
        self.addCleanup(mount.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), persistent_handler(self.root))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/features"

    def request(self, payload: object = None, *, origin: str = "") -> tuple[int, dict]:
        request = urllib.request.Request(
            self.url, data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json", **({"Origin": origin} if origin else {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_defaults_and_persists_without_mounting_dependencies(self) -> None:
        self.assertEqual(self.request(), (HTTPStatus.OK, {"sets": False, "auto_reply": False, "share": False, "support": False, "team": False}))
        flags = {"sets": True, "auto_reply": True, "share": True, "support": True, "team": False}
        self.assertEqual(self.request(flags, origin=self.url.removesuffix("/api/features")),
                         (HTTPStatus.OK, flags))
        self.assertEqual(self.request(), (HTTPStatus.OK, flags))
        self.assertEqual(json.loads((self.root / ".powerpacks/desktop/features.json").read_text()), flags)

    def test_saved_flags_without_share_preserve_existing_features(self) -> None:
        path = self.root / ".powerpacks/desktop/features.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"sets": true, "auto_reply": true}')
        self.assertEqual(self.request(), (HTTPStatus.OK, {"sets": True, "auto_reply": True, "share": False, "support": False, "team": False}))

    def test_support_defaults_to_team_email_and_preserves_an_explicit_choice(self) -> None:
        for email, enabled in (("casey@powerset.co", True), ("Casey@POWERSET.CO", True),
                               ("casey@powerset.co.example.com", False), (None, False)):
            self.account.return_value = {"email": email}
            self.assertEqual(self.request()[1]["support"], enabled)
        self.account.return_value = {"email": "casey@powerset.co"}
        flags = {"sets": False, "auto_reply": False, "share": False, "support": False, "team": False}
        self.assertEqual(self.request(flags), (HTTPStatus.OK, flags))
        self.assertFalse(self.request()[1]["support"])

    def test_existing_flags_inherit_support_default_and_old_posts_keep_the_choice(self) -> None:
        self.account.return_value = {"email": "casey@powerset.co"}
        path = self.root / ".powerpacks/desktop/features.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"sets": true, "auto_reply": false, "share": false}')
        self.assertTrue(self.request()[1]["support"])
        self.assertTrue(self.request({"sets": False, "auto_reply": False})[1]["support"])

    def test_malformed_saved_flags_are_off(self) -> None:
        path = self.root / ".powerpacks/desktop/features.json"
        path.parent.mkdir(parents=True)
        for text in ("invalid", '{"sets": true, "auto_reply": "true"}', '{"auto_reply": true}'):
            path.write_text(text)
            self.assertEqual(self.request(), (HTTPStatus.OK, {"sets": False, "auto_reply": False, "share": False, "support": False, "team": False}))

    def test_rejects_incomplete_or_non_boolean_flags_and_cross_origin_writes(self) -> None:
        for payload in ({"sets": True}, {"sets": True, "auto_reply": "true"},
                        {"sets": 1, "auto_reply": False}, [True, False],
                        {"sets": True, "auto_reply": True, "share": "true"},
                        {"sets": True, "auto_reply": True, "support": "true"},
                        {"sets": True, "auto_reply": True, "share": True, "other": False}):
            self.assertEqual(self.request(payload)[0], HTTPStatus.BAD_REQUEST)
        self.assertEqual(self.request({"sets": True, "auto_reply": True}, origin="https://example.com")[0],
                         HTTPStatus.FORBIDDEN)
        self.assertFalse((self.root / ".powerpacks/desktop/features.json").exists())


if __name__ == "__main__":
    unittest.main()
