"""The chat's search picker and Sets page: the user's Powerset sets from the Powerset API and the local index."""

import io
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

from packs.shared.web.server import persistent_handler

# GET /v2/sets as an app owner sees it: their sets, and every other set with role "admin".
API_SETS = [
    {"id": "p-1", "name": "Personal Connections", "role": "owner", "is_personal": True, "person_count": 15},
    {"id": "x-1", "name": "Someone else's", "role": "admin", "is_personal": False, "person_count": 90000},
    {"id": "t-1", "name": "Powerset", "role": "member", "is_personal": False, "person_count": 40795},
]
# Largest first, the admin-only set left out.
SETS = [
    {"set_id": "t-1", "name": "Powerset", "is_personal": False, "owned": False, "person_count": 40795},
    {"set_id": "p-1", "name": "Personal Connections", "is_personal": True, "owned": True, "person_count": 15},
]


class SearchScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name, target, value in (
                ("credentials_subject", "packs.shared.web.search_scope.postgres_client.credentials_subject", "auth0|me"),
                ("api", "packs.shared.web.search_scope._api", API_SETS)):
            patcher = patch(target, return_value=value)
            self.addCleanup(patcher.stop)
            setattr(self, name, patcher.start())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), persistent_handler(self.root))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/search/scope"

    def request(self) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(self.url, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_lists_the_sets_and_the_local_index_when_this_mac_has_one(self) -> None:
        self.assertEqual(self.request(), (HTTPStatus.OK, {"sets": SETS, "local_db": None}))
        self.api.assert_called_with(self.root, "/v2/sets")
        index = self.root / ".powerpacks/search-index/local-search.duckdb"
        index.parent.mkdir(parents=True)
        index.write_bytes(b"")
        self.assertEqual(self.request()[1]["local_db"], str(index))
        # Nothing is written: the chat pins its pick through Codex.
        self.assertFalse((self.root / ".env").exists())

    def test_signed_out_has_no_sets(self) -> None:
        self.credentials_subject.return_value = None
        self.assertEqual(self.request(), (HTTPStatus.OK, {"sets": [], "local_db": None}))
        self.api.assert_not_called()

    def test_api_failure_answers_with_an_error(self) -> None:
        self.api.side_effect = RuntimeError("api down")
        self.assertEqual(self.request(), (HTTPStatus.BAD_GATEWAY, {"error": "api down"}))

    def test_one_set_comes_from_the_powerset_api(self) -> None:
        detail = {"id": "t-1", "name": "Powerset", "role": "member", "members": []}
        with patch("packs.shared.web.search_scope.set_detail", return_value=detail) as read:
            with urllib.request.urlopen(self.url.replace("/scope", "/set?set_id=t-1"), timeout=5) as response:
                self.assertEqual(json.loads(response.read()), detail)
        read.assert_called_with(self.root, "t-1")

    def test_an_invite_goes_to_the_powerset_api(self) -> None:
        sent = {"invite_url": "https://app.powerset.co/sets/invite/tok"}
        with patch("packs.shared.web.search_scope.invite", return_value=sent) as invite:
            request = urllib.request.Request(self.url.replace("/scope", "/set/invite"), method="POST",
                                             data=json.dumps({"set_id": "t-1"}).encode())
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(json.loads(response.read()), sent)
        invite.assert_called_with(self.root, "t-1")

    def test_a_new_set_and_a_delete_go_to_the_powerset_api(self) -> None:
        for path, target, body, args in (("/sets", "create_set", {"name": "Team"}, ("Team",)),
                                         ("/set/delete", "delete_set", {"set_id": "t-1"}, ("t-1",))):
            with patch(f"packs.shared.web.search_scope.{target}", return_value={}) as call:
                request = urllib.request.Request(self.url.replace("/scope", path), method="POST",
                                                 data=json.dumps(body).encode())
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, HTTPStatus.OK)
            call.assert_called_with(self.root, *args)

    def test_a_failed_set_action_answers_with_the_api_reason(self) -> None:
        refusals = (
            urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b'{"detail": "Only owners"}')),
            urllib.error.HTTPError("u", 502, "Bad Gateway", {}, io.BytesIO(b"<html>upstream down</html>")),
            urllib.error.URLError(ConnectionRefusedError("refused")),
        )
        for refusal, reason in zip(refusals, ("Only owners", "<html>upstream down</html>", "refused")):
            with patch("packs.shared.web.search_scope.create_set", side_effect=refusal):
                request = urllib.request.Request(self.url.replace("/scope", "/sets"), method="POST",
                                                 data=json.dumps({"name": "Team"}).encode())
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(caught.exception.code, HTTPStatus.BAD_GATEWAY)
                self.assertEqual(json.loads(caught.exception.read()), {"error": reason})
