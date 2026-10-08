"""The review page's re-research and feedback routes, with the paid calls stubbed."""
from __future__ import annotations

import sqlite3
import unittest
from http import HTTPStatus
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.ingestion.primitives.deep_context_v2.enrich import research
from packs.ingestion.primitives.deep_context_v2.review import api
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import OwnedIdentifiers, SynthesizedFacts

FACTS = SynthesizedFacts("Evan Lin", (), (), "", "", "", "", "friend", "personal", (), (), (),
                         OwnedIdentifiers((), (), ()), (), 0.9, False)


class GuidedSubjectTests(unittest.TestCase):
    def test_guidance_makes_its_own_handle_and_rides_the_input(self) -> None:
        plain = research.handle(FACTS)
        guided = research.guided_subject("p:1", FACTS, "the Evan at Stripe, not the one in LA")
        self.assertNotEqual(guided.handle, plain)
        self.assertEqual(guided, research.guided_subject("p:1", FACTS, "the Evan at Stripe, not the one in LA"))
        self.assertEqual(guided.guidance, "the Evan at Stripe, not the one in LA")


class RouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / ".powerpacks"
        (self.root / "deep-context").mkdir(parents=True)
        self.conn: sqlite3.Connection = open_store(self.root / "deep-context" / "deep-context-v2.sqlite")
        self.addCleanup(self.conn.close)
        self.api = api.ReviewApi(self.conn, self.root)

    def test_retarget_needs_a_description(self) -> None:
        with self.assertRaises(api.Refusal) as refused:
            self.api.retarget({"parent_slug": ["p:1"], "guidance": ["  "]})
        self.assertEqual(refused.exception.status, HTTPStatus.BAD_REQUEST)

    def test_retarget_of_a_decided_family_is_a_conflict(self) -> None:
        with self.assertRaises(api.Refusal) as refused:
            self.api.retarget({"parent_slug": ["p:gone"], "guidance": ["the one at Stripe"]})
        self.assertEqual(refused.exception.status, HTTPStatus.CONFLICT)

    def test_feedback_needs_a_comment(self) -> None:
        with self.assertRaises(api.Refusal):
            self.api.feedback({"parent_slug": ["p:1"], "comment": [""], "action": ["general"]})

    def test_feedback_replies_with_powersets_status(self) -> None:
        card = mock.Mock(pending=(), members=(mock.Mock(display_name="Evan Lin"),))
        with mock.patch.object(api, "load_card", return_value=card), \
             mock.patch.object(api.SendFeedback, "run", return_value={"status": "needs_auth", "error": "no token"}):
            reply = self.api.feedback({"parent_slug": ["p:1"], "pub": ["x"], "comment": ["wrong company"],
                                       "action": ["general"]})
        self.assertEqual(reply["status"], "needs_auth")


if __name__ == "__main__":
    unittest.main()
