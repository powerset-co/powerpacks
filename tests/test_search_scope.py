"""The chat's search picker: the user's Powerset sets from Postgres, the local index, and the choice in .env."""

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

SETS = [
    {"set_id": "p-1", "name": "Personal Connections", "is_personal": True, "owned": True, "member_count": 1},
    {"set_id": "t-1", "name": "Powerset", "is_personal": False, "owned": False, "member_count": 6},
]


class SearchScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / ".env").write_text("DATABASE_URL=postgresql://db\n")
        for target, value in (("credentials_subject", "auth0|me"), ("fetch_member_sets", SETS)):
            patcher = patch(f"packs.shared.web.search_scope.postgres_client.{target}", return_value=value)
            self.addCleanup(patcher.stop)
            setattr(self, target, patcher.start())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), persistent_handler(self.root))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/search/scope"

    def request(self, payload: object = None, *, origin: str = "") -> tuple[int, dict]:
        request = urllib.request.Request(
            self.url, data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json", **({"Origin": origin} if origin else {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def env(self) -> str:
        return (self.root / ".env").read_text()

    def test_lists_the_sets_and_searches_the_personal_set_until_one_is_picked(self) -> None:
        self.assertEqual(self.request(), (HTTPStatus.OK, {"sets": SETS, "local": False, "selected": "p-1"}))
        self.fetch_member_sets.assert_called_with("auth0|me", "postgresql://db")

    def test_picking_the_local_index_then_a_set_rewrites_env(self) -> None:
        index = self.root / ".powerpacks/search-index/local-search.duckdb"
        index.parent.mkdir(parents=True)
        index.write_bytes(b"")
        status, scope = self.request({"selected": "local"})
        self.assertEqual((status, scope["local"], scope["selected"]), (HTTPStatus.OK, True, "local"))
        self.assertIn(f"POWERPACKS_LOCAL_SEARCH_DB={index}", self.env())
        status, scope = self.request({"selected": "t-1"})
        self.assertEqual((status, scope["selected"]), (HTTPStatus.OK, "t-1"))
        self.assertIn("POWERPACKS_DEFAULT_SET_ID=t-1", self.env())
        self.assertNotIn("POWERPACKS_LOCAL_SEARCH_DB", self.env())
        self.assertIn("DATABASE_URL=postgresql://db", self.env())

    def test_a_set_in_env_that_is_not_yours_is_no_choice(self) -> None:
        (self.root / ".env").write_text("DATABASE_URL=postgresql://db\nPOWERPACKS_DEFAULT_SET_ID=elsewhere\n")
        self.assertEqual(self.request(), (HTTPStatus.OK, {"sets": SETS, "local": False, "selected": ""}))

    def test_signed_out_has_no_sets(self) -> None:
        self.credentials_subject.return_value = None
        self.assertEqual(self.request(), (HTTPStatus.OK, {"sets": [], "local": False, "selected": ""}))
        self.fetch_member_sets.assert_not_called()

    def test_postgres_failure_and_bad_requests_answer_with_an_error(self) -> None:
        self.fetch_member_sets.side_effect = RuntimeError("could not connect")
        self.assertEqual(self.request(), (HTTPStatus.BAD_GATEWAY, {"error": "could not connect"}))
        for payload in ({}, {"selected": ""}, {"selected": 3}, ["local"]):
            self.assertEqual(self.request(payload)[0], HTTPStatus.BAD_REQUEST, payload)
        self.assertEqual(self.request({"selected": "local"}, origin="http://evil.example")[0], HTTPStatus.FORBIDDEN)
        self.assertEqual(self.env(), "DATABASE_URL=postgresql://db\n")


if __name__ == "__main__":
    unittest.main()
