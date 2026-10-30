"""The chat's search picker: the user's Powerset sets from Postgres and the local index."""

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

# Largest first, as fetch_member_sets orders them.
SETS = [
    {"set_id": "t-1", "name": "Powerset", "is_personal": False, "owned": False, "person_count": 40795},
    {"set_id": "p-1", "name": "Personal Connections", "is_personal": True, "owned": True, "person_count": 15},
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

    def request(self) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(self.url, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_lists_the_sets_and_the_local_index_when_this_mac_has_one(self) -> None:
        self.assertEqual(self.request(), (HTTPStatus.OK, {"sets": SETS, "local_db": None}))
        self.fetch_member_sets.assert_called_with("auth0|me", "postgresql://db")
        index = self.root / ".powerpacks/search-index/local-search.duckdb"
        index.parent.mkdir(parents=True)
        index.write_bytes(b"")
        self.assertEqual(self.request()[1]["local_db"], str(index))
        # Nothing is written: the chat pins its pick through Codex.
        self.assertEqual((self.root / ".env").read_text(), "DATABASE_URL=postgresql://db\n")

    def test_signed_out_has_no_sets(self) -> None:
        self.credentials_subject.return_value = None
        self.assertEqual(self.request(), (HTTPStatus.OK, {"sets": [], "local_db": None}))
        self.fetch_member_sets.assert_not_called()

    def test_postgres_failure_answers_with_an_error(self) -> None:
        self.fetch_member_sets.side_effect = RuntimeError("pooler down")
        self.assertEqual(self.request(), (HTTPStatus.BAD_GATEWAY, {"error": "pooler down"}))
