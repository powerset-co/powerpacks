"""The People page holds tag edits until share catches up with a corrected profile."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlparse

from packs.ingestion.primitives.deep_context_v2.db import queries, queries_review, queries_share
from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.ingestion.primitives.share.web.model import SharePeople, people_payload
from packs.ingestion.primitives.share.web.server import ShareRoutes, decide_tags

NOW = "2026-10-07T20:00:00Z"


class ShareInProgressTests(unittest.TestCase):
    def test_changed_profile_blocks_tags_until_share_catches_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = open_store(root / "store.sqlite")
            self.addCleanup(conn.close)
            imported = {"full_name": "Jordan Bravo", "primary_email": "j@example.test", "primary_phone": "",
                        "source_channels": "gmail_msgvault", "interaction_counts": "", "last_interaction": ""}
            queries.upsert_candidates(conn, [("c1", "Jordan Bravo", 0, json.dumps(imported), NOW)])
            conn.execute("INSERT INTO candidate_identifiers VALUES ('c1', 'email', 'j@example.test', 'j@example.test')")
            conn.execute("INSERT INTO candidate_sources VALUES ('c1', 'gmail_msgvault')")
            facts = {
                "canonical_name": "Jordan Bravo", "aliases": [], "employers": [], "title": "",
                "school": "", "field_of_study": "", "location": "", "relationship_to_owner": "",
                "relationship_category": "", "topics": [], "notable_events": [], "identifiers": [],
                "owned_identifiers": {"emails": [], "phones": [], "urls": []},
                "shared_context": [], "confidence": 1.0, "is_owner": False,
            }
            conn.execute("INSERT INTO facts VALUES ('c1', ?, 'f1', 'test', '', ?)", (json.dumps(facts), NOW))
            conn.execute(
                "INSERT INTO worth (candidate_id, worth, decided_by, reason, created_at) "
                "VALUES ('c1', 'yes', 'human', '', ?)", (NOW,))
            queries_review.insert_parent(conn, "c1", "p:1", "singleton", None, NOW)
            labels = [("c1", "old-profile", "Jordan Bravo", "yes", "", "{}", NOW)]
            shares = [("c1", "old-profile", "confirm", "worth_maybe", "", "machine", NOW)]
            queries_share.replace_share(conn, labels, shares)
            queries_review.insert_linkedin(conn, "c1", "https://www.linkedin.com/in/new-profile",
                                           "4242", "human_override", "confirmed", "f1", NOW)
            queries_review.insert_parent(conn, "c1", "li:4242", "human", None, NOW)
            conn.commit()

            people = SharePeople(conn, root)
            row, = people.load()
            self.assertEqual(row.public_identifier, "new-profile")
            self.assertTrue(row.in_progress)
            payload = people_payload((row,))
            self.assertTrue(payload["rows"][0][payload["columns"].index("in_progress")])
            message = "Jordan Bravo is being updated; finish the run first"
            with self.assertRaisesRegex(ValueError, message):
                decide_tags(conn, people, {row.parent_id: frozenset({"share"})})

            body = json.dumps({"people": [{"parent_id": row.parent_id, "tags": ["share"]}]}).encode()
            handler = Mock(headers={"Content-Length": str(len(body))}, rfile=io.BytesIO(body), wfile=io.BytesIO())
            routes = ShareRoutes(conn, people, people.load, Mock())
            self.assertTrue(routes.post(handler, urlparse("/api/people/tags")))
            handler.send_response.assert_called_once_with(409)
            self.assertEqual(json.loads(handler.wfile.getvalue()), {"error": message})
            self.assertEqual(queries_share.current_tags(conn), {})
            self.assertEqual(queries_share.current_share(conn)[0].public_identifier, "old-profile")

            queries_share.replace_share(conn,
                                        [(c, "new-profile", *rest) for c, _, *rest in labels],
                                        [(c, "new-profile", *rest) for c, _, *rest in shares])
            self.assertFalse(people.load()[0].in_progress)
            decided = decide_tags(conn, people, {row.parent_id: frozenset({"share"})})
            self.assertEqual(decided[row.parent_id].public_identifier, "new-profile")
            self.assertEqual(queries_share.current_tags(conn)[row.parent_id].tags, "share")
