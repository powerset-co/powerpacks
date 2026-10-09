"""Ask the set as agent messages: who is asked (set members who shared the candidate), what goes out, and
how the answers read back from this machine's inbox. The relay and TurboPuffer are stubbed.

Changelog:
- 2026-10-08: created with ask_set.py.
"""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from packs.indexing.lib.identity import stable_person_id
from packs.ingestion.primitives.share.web.sets import Member, SetView
from packs.search.primitives.ask_set import ask_set

JORDAN_ID, CASEY_ID, RILEY_ID, STRANGER_ID = (str(uuid.uuid4()) for _ in range(4))
ME = Member("Jordan Bravo", "jordan@example.com", "owner", JORDAN_ID)
CASEY = Member("Casey Delta", "casey@example.com", "member", CASEY_ID)
RILEY = Member("Riley Echo", "riley@example.com", "member", RILEY_ID)
SNAPSHOT = {
    "tags": {"assignments": {"p1": ["Pinned"], "p2": ["pinned"], "p3": [], "p4": ["pinned"]}},
    "search": {"title": "Founding engineer", "company": "Acme", "jd_text": "Build the platform. " * 500, "candidates": [
        {"person_id": "p1", "name": "Alex Foxtrot", "linkedin_url": "https://www.linkedin.com/in/alex-foxtrot"},
        {"person_id": "p2", "name": "Sam Golf", "linkedin_url": "https://www.linkedin.com/in/sam-golf"},
        {"person_id": "p3", "name": "Not Pinned", "linkedin_url": "https://www.linkedin.com/in/not-pinned"},
        {"person_id": "p4", "name": "No Link", "linkedin_url": ""},
    ]},
}


class AskSetTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.sent: list[tuple[str, str, dict]] = []
        self.sets = mock.Mock()
        self.sets.data_root = self.root / ".powerpacks"
        self.sets.me.return_value = ME
        self.sets.kept.return_value = [SetView("set-1", "Founders", "owner", (ME, CASEY, RILEY))]
        # Casey shared Alex and Sam; Riley shared only Sam; nobody outside the set counts.
        self.sets.shared_by.return_value = {
            stable_person_id(public_identifier="alex-foxtrot"): (CASEY_ID, STRANGER_ID),
            stable_person_id(public_identifier="sam-golf"): (CASEY_ID, RILEY_ID, JORDAN_ID),
        }
        self.sets.message.side_effect = lambda to, kind, payload: self.sent.append((to, kind, payload))
        self.sets.presence.return_value = {CASEY_ID: datetime.now(timezone.utc).isoformat()}
        self.enterContext(mock.patch.object(ask_set.snapshot, "export_snapshot", return_value=SNAPSHOT))

    def test_preview_names_the_members_who_know_each_pinned_candidate(self) -> None:
        preview = ask_set.preview(self.run_dir, self.sets)
        self.assertEqual(preview["skipped"], 1)
        self.assertEqual([(c["name"], [o["name"] for o in c["owners"]]) for c in preview["candidates"]],
                         [("Alex Foxtrot", ["Casey Delta"]), ("Sam Golf", ["Casey Delta", "Riley Echo"])])
        self.assertEqual({o["name"]: o["candidates"] for o in preview["operators"]}, {"Casey Delta": 2, "Riley Echo": 1})
        self.assertEqual(self.sent, [])

    def test_send_is_one_message_per_member_with_only_their_candidates(self) -> None:
        result = ask_set.send(self.run_dir, "Would you intro?", self.sets)
        self.assertEqual(result["status"], "sent")
        by_member = {to: (kind, [c["public_identifier"] for c in payload["candidates"]], payload["question"])
                     for to, kind, payload in self.sent}
        self.assertEqual(by_member, {CASEY_ID: ("ask", ["alex-foxtrot", "sam-golf"], "Would you intro?"),
                                     RILEY_ID: ("ask", ["sam-golf"], "Would you intro?")})
        role = self.sent[0][2]["role"]
        self.assertEqual((role["title"], role["company"]), ("Founding engineer", "Acme"))
        self.assertEqual(len(role["job_description"]), ask_set.MAX_JD)
        saved = json.loads((self.run_dir / "ask.json").read_text())
        self.assertEqual(saved["ask_id"], self.sent[0][2]["ask_id"])

    def test_status_reads_answers_from_the_inbox(self) -> None:
        ask_id = ask_set.send(self.run_dir, "Would you intro?", self.sets)["ask"]["ask_id"]
        inbox = self.sets.data_root / "inbox"
        inbox.mkdir(parents=True)
        answer = {"verdict": "recommend", "reason": "Worked together.", "can_intro": True,
                  "relationship": "colleague", "last_contact": None, "confidence": 0.9}
        message_id = str(uuid.uuid4())
        (inbox / f"{message_id}.json").write_text(json.dumps({
            "id": message_id, "kind": "ask_answer", "from": {"operator_id": CASEY_ID, "name": "Casey Delta"},
            "created_at": "2026-10-08T00:00:00Z",
            "payload": {"ask_id": ask_id, "answers": [
                {"public_identifier": "alex-foxtrot", "answer": answer},
                {"public_identifier": "sam-golf", "answer": {"declined": True, "reason": "not_in_store"}}]}}))
        status = ask_set.status(self.run_dir, self.sets)
        owners = {(c["name"], o["name"]): (o["status"], o["awake"], o["answer"])
                  for c in status["answers"]["candidates"] for o in c["owners"]}
        self.assertEqual(owners[("Alex Foxtrot", "Casey Delta")], ("answered", True, answer))
        self.assertEqual(owners[("Sam Golf", "Casey Delta")], ("declined", True, None))
        self.assertEqual(owners[("Sam Golf", "Riley Echo")], ("pending", False, None))
        self.assertEqual(status["answers"]["pending"], 1)

    def test_status_without_an_ask(self) -> None:
        self.assertEqual(ask_set.status(self.run_dir, self.sets), {"ask": None})


if __name__ == "__main__":
    unittest.main()
