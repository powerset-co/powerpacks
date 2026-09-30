"""People-page Logbook: selected parents from SQLite, the build job, and its routes.

Synthetic stores only; nothing reads a real message store or the network.
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import urllib.parse
from pathlib import Path
from unittest import mock

from deep_context_sqlite_test_helpers import seed_identity
from packs.ingestion.primitives.deep_context.db.models import (
    PersonIdentifierRow,
    PersonIdentifiersProjection,
    PersonRow,
)
from packs.ingestion.primitives.logbook import logbook_common, logbook_export
from packs.ingestion.primitives.logbook.logbook_people import people_for_parents
from packs.ingestion.primitives.share.web import logbook as web_logbook
from packs.ingestion.primitives.share.web.server import share_routes
from test_logbook_build import RECENT_TS, add_whatsapp, make_wacli
from test_share_upload_web import Handler
from test_share_web import ShareWebFixture


LOGBOOK_TYPES = Path(__file__).resolve().parents[1] / "web" / "src" / "lib" / "api" / "logbook.ts"


def ts_interface(name: str) -> tuple[str, ...]:
    """The field names of one `export interface` in lib/api/logbook.ts, in order."""
    body = re.search(rf"^export interface {name} \{{\n(.*?)^\}}", LOGBOOK_TYPES.read_text(encoding="utf-8"),
                     re.S | re.M)
    assert body is not None, name
    return tuple(re.findall(r"^  (\w+)\??:", body.group(1), re.M))


def _identifiers(person_id: str, *rows: tuple[str, str]) -> PersonIdentifiersProjection:
    return PersonIdentifiersProjection(
        person_id, tuple(PersonIdentifierRow(person_id, kind, value, value) for kind, value in rows))


class LogbookPeopleFixture(ShareWebFixture):
    """Casey's parent merges a work email (person-e) and a phone (person-b), plus a ghost
    and an owner child whose identifiers must stay out. Parent-ffff is Worth: No and
    not on the People list at all."""

    def setUp(self) -> None:
        super().setUp()
        seed_identity(self.db, parent_id="parent-ffff", person_id="person-f", row_key="quinn-gray-ffff",
                      name="Quinn Gray", machine_worth="no", public_identifier="quinn-gray")
        self.db.project_rows((
            PersonRow("person-ghost", "parent-bbbb", "ghost", "casey-delta", "Casey Ghost", is_ghost=True),
            PersonRow("person-owner", "parent-bbbb", "owner", "casey-delta", "Me", is_owner=True),
            _identifiers("person-e", ("email", "casey.work@example.com")),
            _identifiers("person-b", ("phone", "4155550101"), ("email", "casey@example.com")),
            _identifiers("person-ghost", ("email", "ghost@example.com")),
            _identifiers("person-owner", ("email", "me@example.com"), ("phone", "+15550199")),
            _identifiers("person-f", ("phone", "+15550100")),
        ))
        self.logbook_root = self.root / "logbook"
        self.wacli = make_wacli(self.root / "wacli")
        self.stores = {"gmail": self.root / "no-msgvault.db", "imessage": self.root / "no-chat.db",
                       "whatsapp": self.wacli}


class ResolveParentsTests(LogbookPeopleFixture):
    def test_a_merged_parent_carries_every_child_identifier_but_owner_and_ghost(self):
        (casey,) = people_for_parents(self.db, ["parent-bbbb"])
        self.assertEqual(casey.person_id, "parent-bbbb")
        self.assertEqual(sorted(casey.emails), ["casey.work@example.com", "casey@example.com"])
        self.assertEqual(casey.phones, ["+14155550101"])

    def test_explicit_selection_ignores_worth_and_the_people_list(self):
        people = people_for_parents(self.db, ["parent-ffff", "parent-cccc"])
        self.assertEqual([person.person_id for person in people], ["parent-ffff", "parent-cccc"])
        self.assertEqual(people[0].phones, ["+15550100"])

    def test_an_unknown_parent_fails_by_name(self):
        with self.assertRaisesRegex(LookupError, "parent-zzzz"):
            people_for_parents(self.db, ["parent-bbbb", "parent-zzzz"])


class LogbookRoutesTests(LogbookPeopleFixture):
    def setUp(self) -> None:
        super().setUp()
        self.routes = share_routes(self.db, self.people_csv, upload_db=self.root / "local-search.duckdb",
                                   upload_dir=self.root / "upload-powerset", logbook_root=self.logbook_root,
                                   logbook_stores=self.stores)

    def _post(self, people, origin=None, host="127.0.0.1:8797"):
        handler = Handler(origin, host, json.dumps({"people": people}).encode())
        self.assertTrue(self.routes.post(handler, urllib.parse.urlparse("/api/people/logbook")))
        sent = handler.wfile.getvalue()
        return handler.response_status, json.loads(sent) if sent.startswith(b"{") else None

    def _get(self, path="/api/people/logbook"):
        handler = Handler()
        self.assertTrue(self.routes.get(handler, urllib.parse.urlparse(path)))
        return handler.response_status, handler.wfile.getvalue()

    def _built(self, people):
        code, status = self._post(people)
        self.assertEqual(code, 200)
        self.routes.logbook._thread.join(timeout=10)
        return json.loads(self._get()[1])

    def test_idle_status_has_every_contract_field_and_opening_never_builds(self):
        with mock.patch.object(web_logbook, "build_logbook") as build:
            self.assertEqual(self._get(), (200, b'{"status":"idle","people":[],"result":null,"error":null}'))
        build.assert_not_called()

    def test_building_selected_people_reads_the_stores_directly_without_a_csv(self):
        add_whatsapp(self.wacli, "14155550101@s.whatsapp.net", "c1", RECENT_TS, "hi casey")
        with mock.patch.object(logbook_common, "load_people_from_csv", side_effect=AssertionError("no CSV")), \
                mock.patch.object(logbook_export, "load_people_from_csv", side_effect=AssertionError("no CSV")), \
                mock.patch.object(socket.socket, "connect", side_effect=AssertionError("no network")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")):
            status = self._built(["parent-bbbb"])
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["people"], ["parent-bbbb"])
        result = status["result"]
        self.assertEqual((result["messages"], result["files"]), (1, 1))
        self.assertEqual({row["channel"]: row["status"] for row in result["channels"]},
                         {"gmail": "missing", "imessage": "missing", "whatsapp": "ok"})
        self.assertEqual(list(self.logbook_root.rglob("*.csv")), [])

    def test_a_selection_without_messages_completes_with_zero(self):
        status = self._built(["parent-cccc"])
        self.assertEqual((status["result"]["messages"], status["result"]["files"]), (0, 0))

    def test_post_rejects_unknown_parents_bad_bodies_and_other_origins(self):
        self.assertEqual(self._post(["parent-zzzz"])[0], 400)
        self.assertEqual(self._post([])[0], 400)
        self.assertEqual(self._post([7])[0], 400)
        self.assertEqual(self._post(["parent-bbbb"], origin="http://attacker.example:8797",
                                    host="attacker.example:8797")[0], 403)
        self.assertIsNone(self.routes.logbook._thread)

    def test_a_second_build_while_one_runs_is_refused(self):
        self.routes.logbook._thread = mock.Mock(is_alive=mock.Mock(return_value=True))
        code, body = self._post(["parent-bbbb"])
        self.assertEqual((code, body), (409, {"error": web_logbook.BUILD_ACTIVE}))

    def test_a_failed_build_says_why(self):
        with mock.patch.object(web_logbook, "build_logbook", side_effect=OSError("disk full")), \
                mock.patch("traceback.print_exc"):
            status = self._built(["parent-bbbb"])
        self.assertEqual((status["status"], status["error"], status["result"]), ("failed", "disk full", None))


    def test_a_store_that_exits_on_open_fails_the_build_instead_of_going_idle(self):
        # MsgvaultStore.connect raises SystemExit on a bad store; a thread would swallow it.
        with mock.patch.object(web_logbook, "build_logbook", side_effect=SystemExit("msgvault db unreadable")), \
                mock.patch("traceback.print_exc"):
            status = self._built(["parent-bbbb"])
        self.assertEqual((status["status"], status["error"]), ("failed", "msgvault db unreadable"))

    def test_status_keys_match_the_page_types(self):
        add_whatsapp(self.wacli, "14155550101@s.whatsapp.net", "c1", RECENT_TS, "hi casey")
        status = self._built(["parent-bbbb"])
        self.assertEqual(ts_interface("LogbookStatus"), tuple(status))
        self.assertEqual(ts_interface("LogbookResult"), tuple(status["result"]))
        self.assertEqual(ts_interface("ChannelCoverage"), tuple(status["result"]["channels"][0]))
