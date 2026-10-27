"""Normal Deep Context reruns refresh Gmail attribution and reuse unchanged evidence.

Provider answers are deterministic test data; this checks orchestration and cache
invalidation, not model classification quality. All source stores are synthetic.
"""
from __future__ import annotations

import io
import json
import os
import socket
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from openai import AsyncOpenAI

from packs.ingestion.primitives.deep_context_v2 import run
from packs.ingestion.primitives.deep_context_v2.collect.bundle import MessageEntry
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.openai import OpenAIResponsesCaller
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import OwnedIdentifiers, SynthesizedFacts
from packs.shared.csv_io import CsvIO

STAMP = "2026-10-01T12:00:00Z"
CONTACTS = {
    "jordan@example.com": "Jordan Bravo",
    "casey@example.com": "Casey Delta",
    "morgan@example.com": "Morgan Echo",
    "taylor@example.com": "Taylor Foxtrot",
}
JORDAN = "candidate:email:jordan@example.com"
CASEY = "candidate:email:casey@example.com"
MORGAN = "candidate:email:morgan@example.com"
TAYLOR = "candidate:email:taylor@example.com"
PHONE = "candidate:phone:+15550100001"
TAYLOR_URL = "https://www.linkedin.com/in/taylor-foxtrot"


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    CsvIO.write_dict_rows(path, tuple(rows[0]), rows)


def _source_stores(root: Path) -> Path:
    """Importer outputs, the real msgvault schema, and an unrelated phone conversation."""
    (root / "deep-context").mkdir(parents=True)
    (root / "deep-context/owner.json").write_text(json.dumps({
        "name": "Alex Owner", "emails": ["owner@example.com", "owner-alias@example.com"], "phones": [],
    }))
    imports = root / "network-import/import"
    _write_csv(imports / "gmail/people.csv", [{
        "full_name": name, "primary_email": email, "source_channels": "gmail_msgvault",
        "interaction_counts": '{"gmail_msgvault":2}', "last_interaction": STAMP,
    } for email, name in CONTACTS.items()])
    _write_csv(imports / "messages/people.csv", [{
        "full_name": "Robin Golf", "primary_phone": "+15550100001", "source_channels": "whatsapp",
        "interaction_counts": '{"whatsapp":2}', "last_interaction": STAMP,
    }])
    _write_csv(imports / "linkedin/people.csv", [{
        "full_name": CONTACTS["taylor@example.com"], "primary_email": "taylor@example.com",
        "linkedin_url": TAYLOR_URL, "current_title": "Engineer", "current_company": "Example Labs",
    }])
    cache = root / "network-import/profile_cache_v2/taylor-foxtrot.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({
        "fetched_at": STAMP, "public_identifier": "taylor-foxtrot", "linkedin_url": TAYLOR_URL,
        "raw_response": {"firstName": "Taylor", "lastName": "Foxtrot",
                         "publicIdentifier": "taylor-foxtrot", "experiences": []},
        "normalized_profile": {
            "success": True, "member_id": "synthetic-taylor", "full_name": "Taylor Foxtrot",
            "location_str": "Springfield", "experiences": [], "education": [],
        },
    }))
    archive = root / "msgvault.db"
    with sqlite3.connect(archive) as conn:
        conn.executescript("""
            CREATE TABLE sources (id INTEGER, identifier TEXT);
            CREATE TABLE participants (id INTEGER PRIMARY KEY, email_address TEXT, display_name TEXT);
            CREATE TABLE conversations (id INTEGER PRIMARY KEY, source_conversation_id TEXT);
            CREATE TABLE messages (id INTEGER PRIMARY KEY, sender_id INTEGER, sent_at TEXT,
                received_at TEXT, internal_date TEXT, conversation_id INTEGER, subject TEXT,
                snippet TEXT, message_type TEXT, deleted_at TEXT, deleted_from_source_at TEXT, source_message_id TEXT);
            CREATE TABLE message_bodies (message_id INTEGER, body_text TEXT);
            CREATE TABLE message_recipients (message_id INTEGER, participant_id INTEGER,
                recipient_type TEXT, display_name TEXT);
            INSERT INTO sources VALUES (1, 'owner@example.com'), (2, 'owner-alias@example.com');
            INSERT INTO participants VALUES
                (1, 'owner@example.com', 'Alex Owner'),
                (2, 'owner-alias@example.com', 'Alex Owner'),
                (3, 'dana@example.com', 'Dana Hotel'),
                (4, 'archive@example.com', 'Archive Copy');
        """)
        for index, (email, name) in enumerate(CONTACTS.items(), 10):
            conn.execute("INSERT INTO participants VALUES (?, ?, ?)", (index, email, name))
            conn.execute("INSERT INTO conversations VALUES (?, ?)", (index, f"mail-thread-{index}"))
            for message_id, sender, body in (
                (index * 2, index, "Thanks for reviewing the engineering project. Here is the update."),
                (index * 2 + 1, 2, f"Dana, please send the invoice. {name} is copied for visibility."),
            ):
                conn.execute("INSERT INTO messages VALUES (?, ?, ?, NULL, NULL, ?, ?, '', 'email', NULL, NULL, ?)",
                             (message_id, sender, STAMP, index, f"Project {name}", f"mail-message-{message_id}"))
                conn.execute("INSERT INTO message_bodies VALUES (?, ?)", (message_id, body))
                if sender != index:
                    recipients = [(sender, "from"), (3, "to"), (index, "cc"), (4, "bcc")]
                elif email == "jordan@example.com":
                    recipients = [(sender, "from"), (3, "to"), (1, "cc")]
                else:
                    recipients = [(sender, "from"), (1, "to")]
                for participant, role in recipients:
                    display = conn.execute("SELECT display_name FROM participants WHERE id=?", (participant,)).fetchone()[0]
                    conn.execute("INSERT INTO message_recipients VALUES (?, ?, ?, ?)",
                                 (message_id, participant, role, display))
    whatsapp = root / "messages/wacli/wacli.db"
    whatsapp.parent.mkdir(parents=True)
    with sqlite3.connect(whatsapp) as conn:
        conn.executescript("""
            CREATE TABLE messages (chat_jid TEXT, text TEXT, ts INTEGER, from_me INTEGER);
            INSERT INTO messages VALUES
                ('15550100001@s.whatsapp.net', 'Robin, see you at the picnic.', 1790856000, 1),
                ('15550100001@s.whatsapp.net', 'Yes Alex, bringing lunch.', 1790856100, 0);
        """)
    return archive


class _Providers:
    """Substitute only paid interfaces, leaving request construction and disk caches real."""

    def __init__(self) -> None:
        self.synthesis: list[str] = []
        self.decisions: list[dict] = []

    async def synthesize(self, _caller: OpenAIResponsesCaller, **request: object) -> dict:
        if request["context"] != "synthesize":
            raise AssertionError(f"Unexpected provider call: {request['context']}")
        prompt = str(request["user_prompt"])
        self.synthesis.append(prompt)
        name = prompt.splitlines()[0].removeprefix("CONTACT: ")
        headers = "From:" in prompt
        facts = SynthesizedFacts(
            name, (), (), "", "", "", "", "synthetic correspondence", "professional",
            ("headers present" if headers else "legacy evidence",), (), (), OwnedIdentifiers((), (), ()), (), 0.9, False,
        )
        return facts.to_payload()

    async def decide(self, _client: AsyncOpenAI, path: str, *, body: dict, **_kwargs: object) -> dict:
        if path != "/decisions":
            raise AssertionError(f"Unexpected provider endpoint: {path}")
        self.decisions.append(body)
        state = json.loads(body["input"])
        headers = "From:" in state["messages"]
        directly_addressed = "To: Alex Owner <owner@example.com>" in state["messages"]
        relationship = "unknown" if headers and not directly_addressed else "business_contact"
        answers = []
        for question in body["questions"]:
            name, kind = question["name"], question["type"]
            answer = {"name": name, "type": kind}
            if kind == "choice":
                choices = {"relationship_kind": relationship,
                           "intro_source": "work", "function": "engineering", "worth": "no"}
                choice = choices[name]
                answer.update(choice=choice, probabilities=[
                    {"value": item["value"], "probability": float(item["value"] == choice)}
                    for item in question["choices"]])
            elif kind == "predicate":
                answer["probability"] = 0.1
            else:
                answer["score"] = 1.0
            answers.append(answer)
        return {"answers": answers, "usage": {"input_tokens": 1}}


class GmailAttributionPipelineTests(unittest.TestCase):
    def test_normal_rerun_refreshes_gmail_and_preserves_decisions_then_costs_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as patches:
            root = Path(directory)
            archive = _source_stores(root)
            providers = _Providers()
            patches.enter_context(patch.dict(os.environ, {
                "OPENAI_API_KEY": "synthetic-offline-test-key", "POWERPACKS_USAGE_LOG": str(root / "usage.jsonl"),
            }))
            patches.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")))
            patches.enter_context(patch.object(socket.socket, "connect_ex", side_effect=AssertionError("Network forbidden")))
            patches.enter_context(patch.object(OpenAIResponsesCaller, "call", autospec=True, side_effect=providers.synthesize))
            patches.enter_context(patch.object(AsyncOpenAI, "post", autospec=True, side_effect=providers.decide))
            index = patches.enter_context(patch.object(run, "index_in_background", return_value=root / "index.log"))
            review = patches.enter_context(patch.object(run, "start_review", return_value="http://offline.invalid"))
            patches.enter_context(patch.object(run, "pending_reviews", return_value=0))
            command = ["run", "--data-root", str(root), "--msgvault-db", str(archive),
                       "--chat-db", str(root / "unused-chat.db"), "--operator-id", "synthetic-operator"]

            # Only the historical source representation is substituted. Every stage runs normally.
            to_payload = MessageEntry.to_payload

            def legacy_payload(message: MessageEntry) -> dict:
                payload = to_payload(message)
                payload.pop("email_headers", None)
                return payload

            with patch.object(MessageEntry, "to_payload", legacy_payload):
                self._run(command, root)
            self.assertEqual(len(providers.synthesis), 5)
            self.assertEqual(len(providers.decisions), 5)
            enrich_manifest = json.loads((root / "deep-context/v2-manifests/enrich.json").read_text())
            self.assertEqual(enrich_manifest["counts"]["email_confirmed"], 1)
            with open_store(store_path(root)) as conn:
                before = self._snapshot(conn)
                self.assertEqual(json.loads(before["labels"][JORDAN][4])["relationship_kind"], "business_contact")
                self.assertEqual(before["parents"][TAYLOR], "li:synthetic-taylor")
                self._human_decisions(conn)
                preserved = self._preserved(conn)

            synthesis_count, decisions_count = len(providers.synthesis), len(providers.decisions)
            self._run(command, root)
            with open_store(store_path(root)) as conn:
                refreshed = self._snapshot(conn)
                self.assertEqual(self._preserved(conn), preserved)
                for candidate in (JORDAN, CASEY, TAYLOR):
                    self.assertNotEqual(before["bundles"][candidate], refreshed["bundles"][candidate])
                    self.assertNotEqual(before["facts"][candidate], refreshed["facts"][candidate])
                    self.assertEqual(json.loads(refreshed["labels"][candidate][4])["relationship_kind"],
                                     "unknown" if candidate == JORDAN else "business_contact")
                self.assertEqual(before["facts"][MORGAN], refreshed["facts"][MORGAN])
                self.assertNotIn(MORGAN, refreshed["labels"])
                for table in ("bundles", "facts", "labels"):
                    self.assertEqual(before[table][PHONE], refreshed[table][PHONE], table)
                self.assertEqual(refreshed["worth"][CASEY][0:2], ("maybe", "human"))
                for candidate in (JORDAN, TAYLOR, PHONE):
                    self.assertEqual(before["worth"][candidate][0], refreshed["worth"][candidate][0])
                bundle = json.loads(refreshed["bundles"][JORDAN][0])
                contact_mail = next(message for message in bundle["messages"] if message["direction"] == "from_them")
                self.assertEqual(contact_mail["email_headers"]["to"], ["Dana Hotel <dana@example.com>"])
                self.assertEqual(contact_mail["email_headers"]["cc"], ["Alex Owner <owner@example.com>"])
                direct_bundle = json.loads(refreshed["bundles"][CASEY][0])
                direct_mail = next(message for message in direct_bundle["messages"]
                                   if message["direction"] == "from_them")
                self.assertEqual(contact_mail["text"], direct_mail["text"])
                self.assertEqual(direct_mail["email_headers"]["to"], ["Alex Owner <owner@example.com>"])
                owner_mail = next(message for message in bundle["messages"] if message["direction"] == "from_me")
                self.assertEqual(owner_mail["email_headers"], {
                    "sender": "Alex Owner <owner-alias@example.com>", "to": ["Dana Hotel <dana@example.com>"],
                    "cc": ["Jordan Bravo <jordan@example.com>"], "bcc": ["Archive Copy <archive@example.com>"],
                    "thread_id": "mail-thread-10", "account_emails": ["owner-alias@example.com", "owner@example.com"],
                    "recipients_omitted": False,
                })
            new_synthesis = providers.synthesis[synthesis_count:]
            self.assertEqual(len(new_synthesis), 3)
            self.assertTrue(all("From:" in prompt for prompt in new_synthesis))
            self.assertTrue(all("Robin Golf" not in prompt and "Morgan Echo" not in prompt for prompt in new_synthesis))
            self.assertEqual(len(providers.decisions) - decisions_count, 3)
            jordan_request = next(request for request in providers.decisions[decisions_count:]
                                  if json.loads(request["input"])["profile"]["name"] == "Jordan Bravo")
            messages = json.loads(jordan_request["input"])["messages"]
            for header in ("Alex Owner <owner-alias@example.com>", "Dana Hotel <dana@example.com>",
                           "Jordan Bravo <jordan@example.com>", "Archive Copy <archive@example.com>", "mail-thread-10"):
                self.assertIn(header, messages)
            export = root / "network-import/merged/people.csv"
            export_bytes = export.read_bytes()
            self.assertNotIn(b"morgan@example.com", export_bytes)
            caches = {path.name: path.read_bytes() for path in (root / "deep-context/decisions").glob("*.json")}
            provider_counts = len(providers.synthesis), len(providers.decisions)

            self._run(command, root)
            self.assertEqual((len(providers.synthesis), len(providers.decisions)), provider_counts)
            with open_store(store_path(root)) as conn:
                self.assertEqual(self._snapshot(conn), refreshed)
                self.assertEqual(self._preserved(conn), preserved)
            self.assertEqual(export.read_bytes(), export_bytes)
            self.assertEqual({path.name: path.read_bytes() for path in (root / "deep-context/decisions").glob("*.json")}, caches)
            self.assertEqual(index.call_count, 3)
            self.assertEqual(review.call_count, 3)

    def _run(self, command: list[str], root: Path) -> None:
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run.main(command), 0)
        for name in ("load", "collect", "synthesize", "dedupe", "worth", "enrich", "realize", "labels"):
            self.assertIn(f"{name}: completed", output.getvalue())
        manifests = root / "deep-context/v2-manifests"
        self.assertEqual(len(list(manifests.glob("*.json"))), 8)

    @staticmethod
    def _snapshot(conn: sqlite3.Connection) -> dict:
        queries = {
            "bundles": "SELECT candidate_id, payload_json, content_fingerprint FROM bundles",
            "facts": "SELECT candidate_id, facts_json, input_fingerprint FROM facts",
            "labels": "SELECT candidate_id, public_identifier, full_name, worth, flag, labels_json FROM person_labels",
            "parents": "SELECT candidate_id, parent_id FROM current_parent",
            "worth": "SELECT candidate_id, worth, decided_by, reason, labels_json FROM current_worth",
            "profiles": "SELECT parent_id, profile_key FROM current_profile",
        }
        return {name: {row[0]: tuple(row)[1:] if name not in {"parents", "profiles"} else row[1]
                       for row in conn.execute(sql)} for name, sql in queries.items()}

    @staticmethod
    def _human_decisions(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO worth (candidate_id, worth, decided_by, reason, created_at) "
                     "VALUES (?, 'maybe', 'human', 'Keep my decision', ?)", (CASEY, STAMP))
        conn.execute("INSERT INTO person_tags (candidate_id, tags, relationship_kind, note, updated_at) "
                     "VALUES (?, 'trusted', 'close_friend', 'Human note', ?)", (CASEY, STAMP))
        conn.execute("INSERT INTO deleted_parents SELECT parent_id, ? FROM current_parent WHERE candidate_id=?",
                     (STAMP, MORGAN))
        conn.execute("INSERT INTO candidate_linkedins (candidate_id, linkedin_url, member_id, origin, verdict, "
                     "decided_by, judgment_fingerprint, reason, created_at) VALUES (?, ?, 'synthetic-taylor', "
                     "'human_override', 'confirmed', 'human', 'human-choice', 'Accepted identity', ?)",
                     (TAYLOR, TAYLOR_URL, STAMP))
        conn.commit()

    @staticmethod
    def _preserved(conn: sqlite3.Connection) -> dict:
        queries = {
            "tags": "SELECT * FROM person_tags",
            "deleted": "SELECT * FROM deleted_parents",
            "human_worth": "SELECT * FROM worth WHERE decided_by='human'",
            "identity": "SELECT * FROM candidate_linkedins WHERE decided_by='human'",
            "parent_rows": "SELECT * FROM candidate_parent",
        }
        return {name: [tuple(row) for row in conn.execute(sql)] for name, sql in queries.items()}


if __name__ == "__main__":
    unittest.main()
