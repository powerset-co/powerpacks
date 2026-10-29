"""LinkedIn source families, exercised only against synthetic local stores."""
from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from dataclasses import replace
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from packs.ingestion.primitives.deep_context_v2.collect.bundle import CollectionBundle, MessageChannel, MessageEntry, Person
from packs.ingestion.primitives.deep_context_v2.collect.collect import Collect
from packs.ingestion.primitives.deep_context_v2.db import queries, queries_dedupe, queries_enrich, queries_review, queries_share
from packs.ingestion.primitives.deep_context_v2.db.schema import DDL, SCHEMA_VERSION
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso, open_store, store_path
from packs.ingestion.primitives.deep_context_v2.decisions import request_digest
from packs.ingestion.primitives.deep_context_v2.dedupe.dedupe import Dedupe
from packs.ingestion.primitives.deep_context_v2.enrich import judge, proposals
from packs.ingestion.primitives.deep_context_v2.enrich.family import load_families
from packs.ingestion.primitives.deep_context_v2.import_load import load
from packs.ingestion.primitives.deep_context_v2.realize.realize import Realize
from packs.ingestion.primitives.deep_context_v2.review import decisions
from packs.ingestion.primitives.deep_context_v2.review.api import ReviewApi
from packs.ingestion.primitives.deep_context_v2.review.queue import load_card, review_list
from packs.ingestion.primitives.deep_context_v2.run import realize
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import OwnedIdentifiers, SynthesizedFacts
from packs.ingestion.primitives.deep_context_v2.synthesize.synthesize import Synthesize
from packs.ingestion.primitives.deep_context_v2.worth import luna
from packs.ingestion.primitives.deep_context_v2.worth.worth import Worth
from packs.ingestion.primitives.enrich.profile_transforms import normalize_rapidapi
from packs.ingestion.primitives.share.person_labels import PersonLabels
from packs.ingestion.primitives.share.web.model import SharePeople
from packs.ingestion.primitives.share.web.server import ShareRoutes, make_handler
from packs.ingestion.schemas.people_schema import CONTACT_CARRY_COLUMNS, PEOPLE_SCHEMA_COLUMNS
from packs.indexing.lib.people import build_unified_profiles, flatten_people
from packs.shared.csv_io import CsvIO

NOW = "2026-10-28T00:00:00Z"


def linkedin_row(member_id: str, slug: str = "jordan-bravo") -> dict[str, str]:
    url = "https://www.linkedin.com/in/" + slug
    raw = {"member_id": member_id, "first_name": "Jordan", "last_name": "Bravo", "headline": "Engineer",
           "experiences": [], "education": []}
    row = dict.fromkeys(PEOPLE_SCHEMA_COLUMNS, "")
    row.update(normalize_rapidapi(raw, slug, url))
    row.update(linkedin_url=url, public_identifier=slug, primary_email="jordan@example.com",
               rapidapi_response=json.dumps(raw), source_channels="linkedin_csv")
    return row


class LinkedInFamiliesTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.conn = open_store(store_path(self.root), shared=True)
        self.addCleanup(self.conn.close)
        owner = self.root / load.OWNER_JSON
        owner.parent.mkdir(parents=True, exist_ok=True)
        owner.write_text(json.dumps({"name": "Alex Owner", "emails": ["owner@example.com"]}))
        self.import_rows([])

    def import_rows(self, rows: list[dict[str, str]]) -> dict[str, int]:
        CsvIO.write_dict_rows(self.root / load.CONNECTIONS_CSV, PEOPLE_SCHEMA_COLUMNS, rows)
        return load.ImportLoad(self.conn, self.root, msgvault_db=self.root / "absent-mail.sqlite").run().counts

    def count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def message(self, suffix: str = "jordan", parent: str = "p:message") -> str:
        candidate = f"candidate:email:{suffix}@example.com"
        queries.upsert_candidates(self.conn, [(candidate, "Jordan Bravo", 0, "{}", NOW)])
        queries.insert_candidate_sources(self.conn, [(candidate, "gmail_msgvault")])
        queries.insert_candidate_identifiers(self.conn, [(candidate, "email", f"{suffix}@example.com", f"{suffix}@example.com")])
        queries_dedupe.append_parent_rows(self.conn, [(candidate, parent, "singleton", None, NOW)])
        person = Person(candidate, "Jordan Bravo", (f"{suffix}@example.com",), (), ("gmail_msgvault",))
        bundle = CollectionBundle.of(person, messages=[MessageEntry.of(MessageChannel.GMAIL, NOW,
            from_me=False, text="We worked together on the project.")], groups=[], thread_participants=(), available=1)
        queries.upsert_bundle(self.conn, candidate, json.dumps(bundle.to_payload()), "bundle", NOW)
        work, = Synthesize(self.conn, self.root, limit=100).work()
        facts = SynthesizedFacts("Jordan Bravo", (), (), "Engineer", "", "", "Springfield", "colleague", "professional",
                                 (), (), (), OwnedIdentifiers((), (), ()), (), 0.9, False)
        queries.upsert_facts(self.conn, candidate, json.dumps(facts.to_payload()), work.fingerprint, "synthetic", "medium", NOW)
        self.conn.commit()
        return candidate

    def test_cold_import_is_atomic_optional_context_and_zero_processing_work(self) -> None:
        result = self.import_rows([linkedin_row("42"), linkedin_row("43", "casey-delta"), linkedin_row("", "no-id")])
        self.assertEqual((result["source_linkedin"], result["linkedin_missing_member_id"]), (2, 1))
        self.assertEqual(queries_dedupe.current_parents(self.conn), {
            "candidate:linkedin:42": "li:42", "candidate:linkedin:43": "li:43"})
        self.assertEqual(self.count("candidate_identifiers"), 0)
        self.assertEqual(queries.channels_present(self.conn), set())
        collected = Collect(self.conn, self.root, 1, self.root / "absent-chat.sqlite").run()
        self.assertEqual((collected.counts["candidates"], collected.counts["bundles"]), (0, 0))
        self.assertEqual(Synthesize(self.conn, self.root, limit=100).work(), [])
        self.assertEqual(Worth(self.conn, self.root, limit=100).families(), [])
        with patch.object(Dedupe, "_judge_all", return_value={}) as call:
            self.assertEqual(Dedupe(self.conn, self.root, limit=100).run().counts["singletons"], 0)
            self.assertEqual(call.call_args.args, ([], []))
        self.assertEqual(judge.plan(proposals.derive(self.conn), {}, set()).tasks, [])
        self.assertEqual(review_list(self.conn), [])
        indexed = realize(self.root)
        with indexed.open() as handle:
            self.assertEqual({r["id"] for r in csv.DictReader(handle)}, {"li:42", "li:43"})
        people = SharePeople(self.conn, self.root)
        self.assertEqual(len(people.load()), 2)
        for row in people.load():
            self.assertEqual((row.worth, row.worth_source, row.labels, row.channels), ("", "", (), ("linkedin",)))
            self.assertFalse(row.in_progress)
            detail = people.detail(row.parent_id)
            self.assertEqual((detail.dossier_html, detail.probabilities, detail.emails, detail.phones), ("", {}, (), ()))
        for table in ("bundles", "facts", "worth", "person_labels"):
            self.assertEqual(self.count(table), 0, table)
        # A failed membership write cannot leave a candidate for dedupe to put onto p:.
        CsvIO.write_dict_rows(self.root / load.CONNECTIONS_CSV, PEOPLE_SCHEMA_COLUMNS, [linkedin_row("99")])
        with patch.object(load, "append_parent_rows", side_effect=RuntimeError("synthetic interrupted import")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                load.write_connections(self.conn, self.root / load.CONNECTIONS_CSV, NOW)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM candidates WHERE candidate_id='candidate:linkedin:99'").fetchone())
        self.assertEqual(self.count("candidate_linkedins"), 2)

    def test_imported_provider_fields_survive_realize_and_local_index_transforms(self) -> None:
        source = linkedin_row("42")
        payload = json.loads(source["rapidapi_response"])
        payload.update(skills=["Python"], follower_count=321, connection_count=123,
                       instagram_handle="jordan_synthetic", instagram_followers=45)
        source.update(id="import-row-42", rapidapi_response=json.dumps(payload),
                      current_company_urn="urn:li:company:123", entity_urn="urn:li:member:42",
                      enrichment_provider="rapidapi", enriched_at=NOW,
                      enrichment_status="enriched", enrichment_error="synthetic provider note",
                      harmonic_response='{"id":"synthetic-42"}', harmonic_location='{"city":"Springfield"}',
                      twitter_handle="jordan_synthetic", twitter_response='{"followers_count":67}',
                      source_artifacts='["synthetic-import.csv"]', superseded_person_ids='["old-import-42"]',
                      all_emails='["import@example.com"]', primary_phone="+15550100099",
                      all_phones='["+15550100099"]', interaction_counts='{"gmail":999}', last_interaction=NOW)
        self.import_rows([source])
        source, = CsvIO.read_dict_rows(self.root / load.CONNECTIONS_CSV)
        for mixed in (False, True):
            with self.subTest(mixed=mixed):
                if mixed:
                    first = self.message("first", "li:42")
                    second = self.message("second", "li:42")
                    queries.upsert_candidates(self.conn, [(first, "Jordan Bravo", 0,
                        json.dumps({"interaction_counts": '{"gmail":3}', "last_interaction": "2026-10-27T00:00:00Z"}), NOW),
                        (second, "Jordan Bravo", 0,
                         json.dumps({"interaction_counts": '{"gmail":2}', "last_interaction": NOW}), NOW)])
                    queries.insert_candidate_sources(self.conn, [(first, "imessage")])
                    queries.insert_candidate_identifiers(self.conn, [(first, "phone", "+15550100001", "+15550100001")])
                    self.conn.commit()
                indexed = realize(self.root)
                self.assertEqual(indexed.read_bytes(), (self.root / "network-import/merged/people.csv").read_bytes())
                with indexed.open() as handle:
                    row, = csv.DictReader(handle)
                self.assertEqual(row["id"], "li:42")
                for column in PEOPLE_SCHEMA_COLUMNS:
                    if column != "id" and column not in CONTACT_CARRY_COLUMNS:
                        self.assertEqual(row[column], source[column], column)
                self.assertEqual(row["primary_email"], "first@example.com" if mixed else "")
                self.assertEqual(json.loads(row["all_emails"]), ["first@example.com", "second@example.com"] if mixed else [])
                self.assertEqual(row["primary_phone"], "+15550100001" if mixed else "")
                self.assertEqual(json.loads(row["all_phones"]), ["+15550100001"] if mixed else [])
                self.assertEqual(row["source_channels"], "gmail_msgvault,imessage,linkedin" if mixed else "linkedin")
                self.assertEqual(json.loads(row["interaction_counts"]), {"gmail": 5} if mixed else {})
                self.assertEqual(row["last_interaction"], NOW if mixed else "")
                person, = flatten_people(indexed)
                self.assertEqual(person["provider_entity_urn"], source["entity_urn"])
                self.assertEqual(person["rapidapi_response"], payload)
                profile, = build_unified_profiles([person])
                self.assertEqual(profile["tech_skills"], ["Python"])
                for field, expected in {"linkedin_followers": 321, "linkedin_connections": 123,
                                        "x_twitter_followers": 67, "instagram_followers": 45,
                                        "x_twitter_handle": "jordan_synthetic", "instagram_handle": "jordan_synthetic"}.items():
                    self.assertEqual(profile[field], expected, field)

    def test_same_identity_keeps_human_decisions_and_retarget_moves_selected_messages(self) -> None:
        first = self.message(parent="li:42")
        second = self.message("other", "li:42")
        queries_review.insert_linkedin(self.conn, first, linkedin_row("42")["linkedin_url"], "42", "human_override", "confirmed", "old", NOW)
        queries_review.insert_worth(self.conn, first, "no", "human decision", NOW)
        queries_share.write_tags(self.conn, [(first, "is_investor", "human note", NOW)])
        before = [tuple(r) for r in self.conn.execute("SELECT * FROM worth")]
        self.import_rows([linkedin_row("42"), linkedin_row("43", "casey-delta")])
        Realize(self.conn, self.root).run()
        row = SharePeople(self.conn, self.root).load(("li:42",))[0]
        self.assertEqual((row.worth, row.tags), ("no", ("is_investor",)))
        card = load_card(self.conn, self.root, "li:42")
        selected = replace(card, members=tuple(m for m in card.members if m.candidate_id in (first, "candidate:linkedin:42")))
        with patch.object(decisions, "load_profiles", side_effect=AssertionError("paid fetch must not run")):
            decisions.retarget(self.conn, self.root, selected, "https://www.linkedin.com/in/casey-delta/")
            history = (self.count("candidate_parent"), self.count("candidate_linkedins"))
            decisions.retarget(self.conn, self.root, selected, "https://www.linkedin.com/in/casey-delta")
        self.assertEqual((self.count("candidate_parent"), self.count("candidate_linkedins")), history)
        parents = queries_dedupe.current_parents(self.conn)
        self.assertEqual(parents[first], "li:43")
        self.assertEqual(parents[second], "li:42")
        self.assertEqual(parents["candidate:linkedin:42"], "li:42")
        self.assertEqual(parents["candidate:linkedin:43"], "li:43")
        self.assertEqual([tuple(r) for r in self.conn.execute("SELECT * FROM worth")], before)
        node = Dedupe(self.conn, self.root, limit=100)
        node.signatures[(first, second)] = "saved"
        rows, counts = node.merge({(first, second): 1})
        self.assertEqual((rows, counts["components_two_linkedins"]), ([], 1))
        Realize(self.conn, self.root).run()
        self.assertEqual(set(queries_share.families(self.conn)), {"li:42", "li:43"})

    def test_proposal_does_not_attach_and_evidence_bearing_message_can_still_match(self) -> None:
        candidate = self.message()
        self.import_rows([linkedin_row("42")])
        url = linkedin_row("42")["linkedin_url"]
        queries_enrich.insert_linkedin(self.conn, (candidate, url, "42", "linkedin_network", "needs_review", "machine", "proposal", None, "", NOW))
        self.conn.commit()
        self.assertEqual(queries_dedupe.current_parents(self.conn)[candidate], "p:message")
        Realize(self.conn, self.root).run()
        self.assertEqual(len(SharePeople(self.conn, self.root).load()), 2)
        queries_review.insert_worth(self.conn, candidate, "yes", "human", NOW)
        found = proposals.derive(self.conn)
        _, profile = decisions.fetch_profile(self.conn, self.root, url)
        plan = judge.plan(found, {url: profile}, set())
        self.assertEqual([task.family.parent_id for task in plan.tasks], ["p:message"])
        self.assertEqual(len(load_families(self.conn)), 1)
        decisions.retarget(self.conn, self.root, load_card(self.conn, self.root, "p:message"), url)
        Realize(self.conn, self.root).run()
        self.assertEqual(set(queries_share.families(self.conn)), {"li:42"})
        self.assertEqual(self.conn.execute("SELECT worth, decided_by FROM current_worth").fetchone()["decided_by"], "human")

    def test_resync_alias_missing_id_and_deletion_preserve_history_and_export(self) -> None:
        self.import_rows([linkedin_row("42", "a-old")])
        queries_share.write_tags(self.conn, [("candidate:linkedin:42", "is_founder", "keep", NOW)])
        self.import_rows([linkedin_row("42", "z-new"), linkedin_row("42", "z-new")])
        history = (self.count("candidate_parent"), self.count("candidate_linkedins"))
        self.import_rows([linkedin_row("42", "z-new")])
        self.assertEqual((self.count("candidate_parent"), self.count("candidate_linkedins")), history)
        self.assertEqual(self.conn.execute("SELECT profile_key FROM current_profile").fetchone()[0], "https://www.linkedin.com/in/z-new")
        with patch.object(decisions, "load_profiles", side_effect=AssertionError("paid fetch")):
            self.assertEqual(decisions.fetch_profile(self.conn, self.root, "https://www.linkedin.com/in/a-old")[1].member_id, "42")
        self.import_rows([linkedin_row("", "z-new")])
        self.assertEqual(self.count("candidates"), 1)
        self.assertEqual(queries_share.current_tags(self.conn)["li:42"].note, "keep")
        queries_share.delete_parents(self.conn, ("li:42", "li:43"), NOW)
        self.import_rows([linkedin_row("42", "z-new"), linkedin_row("43", "new-deleted")])
        indexed = realize(self.root)
        with indexed.open() as handle:
            self.assertEqual(list(csv.DictReader(handle)), [])
        self.assertEqual(SharePeople(self.conn, self.root).load(), ())
        self.assertEqual(queries_dedupe.current_parents(self.conn)["candidate:linkedin:43"], "li:43")

    def test_dedupe_joins_message_families_into_the_imported_li_destination(self) -> None:
        first = self.message("first", "p:first")
        second = self.message("second", "p:second")
        third = self.message("third", "li:42")
        self.import_rows([linkedin_row("42")])
        pairs = [(first, second), (second, third), (first, third)]
        for a, b in pairs:
            a, b = sorted((a, b))
            queries_dedupe.insert_pair_verdict(self.conn, (a, b, "saved", 1, 1.0, "synthetic saved match", NOW))
        self.conn.commit()
        with patch.object(Dedupe, "_judge_all", return_value={}) as called:
            Dedupe(self.conn, self.root, limit=100).run()
            count = self.count("candidate_parent")
            Dedupe(self.conn, self.root, limit=100).run()
        self.assertTrue(all(call.args[0] == [] for call in called.call_args_list))
        self.assertEqual(self.count("candidate_parent"), count)
        self.assertEqual(set(queries_dedupe.current_parents(self.conn).values()), {"li:42"})

    def test_initial_linkedin_install_realizes_without_an_owner_or_message_processing(self) -> None:
        from packs.powerset.primitives.install import linkedin_setup as setup

        project = self.root / "install"
        data = project / ".powerpacks"
        CsvIO.write_dict_rows(data / load.CONNECTIONS_CSV, PEOPLE_SCHEMA_COLUMNS, [linkedin_row("42")])
        with patch.object(setup, "load_env"), \
             patch.object(setup.LinkedInConnections, "run", return_value={
                 "status": "completed", "outcome": "read", "connections": 1, "added": 1}), \
             patch.object(setup.api_runs, "upload", return_value="uploaded"), \
             patch.object(setup.api_runs, "start", return_value="synthetic"), \
             patch.object(setup.api_runs, "status", return_value={"status": "completed"}), \
             patch.object(setup.api_runs, "download", return_value=100), \
             patch.object(setup, "validate_search_index", return_value={"status": "ok", "total_people": 1}):
            self.assertEqual(setup.main(["--root", str(project)]), 0)
        indexed = data / "search-index/people.csv"
        self.assertEqual(indexed.read_bytes(), (data / "network-import/merged/people.csv").read_bytes())
        with indexed.open() as handle:
            self.assertEqual([r["id"] for r in csv.DictReader(handle)], ["li:42"])
        conn = open_store(store_path(data))
        try:
            for table in ("owner", "bundles", "facts", "worth", "person_labels"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        finally:
            conn.close()

    def test_8000_imports_leave_message_selection_and_actual_requests_unchanged(self) -> None:
        candidate = self.message(parent="li:42")
        phone = "candidate:phone:+15550100001"
        queries.upsert_candidates(self.conn, [(phone, "Casey Delta", 0, "{}", NOW)])
        queries.insert_candidate_sources(self.conn, [(phone, "imessage")])
        queries.insert_candidate_identifiers(self.conn, [(phone, "phone", "+15550100001", "+15550100001")])
        self.conn.commit()
        before_people = queries.candidates_to_collect(self.conn, 2)
        synth = Synthesize(self.conn, self.root, limit=1)
        self.assertEqual(synth.work(), [])
        worth = Worth(self.conn, self.root, limit=1)
        family, = worth.families()
        before = worth._request(family)
        digest = request_digest(luna.REQUEST_VERSION, before)
        self.import_rows([linkedin_row(str(i), f"source-{i}") for i in range(8000)])
        self.assertEqual(queries.candidates_to_collect(self.conn, 2), before_people)
        self.assertEqual(queries.channels_present(self.conn), {"gmail_msgvault", "imessage"})
        self.assertEqual(Synthesize(self.conn, self.root, limit=1).work(), [])
        worth = Worth(self.conn, self.root, limit=1)
        family, = worth.families()
        after = worth._request(family)
        self.assertEqual(after, before)
        self.assertEqual(request_digest(luna.REQUEST_VERSION, after), digest)
        bundle = json.loads(self.conn.execute("SELECT payload_json FROM bundles WHERE candidate_id=?", (candidate,)).fetchone()[0])
        bundle["messages"][0]["text"] += " A new message changes the evidence."
        queries.upsert_bundle(self.conn, candidate, json.dumps(bundle), "changed", now_iso())
        self.conn.commit()
        self.assertEqual([w.candidate_id for w in Synthesize(self.conn, self.root, limit=1).work()], [candidate])
        worth = Worth(self.conn, self.root, limit=1)
        self.assertNotEqual(request_digest(luna.REQUEST_VERSION, worth._request(worth.families()[0])), digest)

    def test_real_people_http_edits_without_labels_and_review_remains_empty(self) -> None:
        self.import_rows([linkedin_row("42")])
        Realize(self.conn, self.root).run()
        people = SharePeople(self.conn, self.root)
        routes = ShareRoutes(self.conn, people, people.load, Mock(), logbook_root=self.root / "logbook")
        server = HTTPServer(("127.0.0.1", 0), make_handler(routes))
        self.addCleanup(server.server_close)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}/api/people/"
        def request(path: str, payload: dict | None = None) -> dict:
            data = json.dumps(payload).encode() if payload is not None else None
            with urllib.request.urlopen(urllib.request.Request(base + path, data=data), timeout=5) as response:
                self.assertEqual(response.status, 200)
                return json.load(response)
        initial = request("rows")
        self.assertEqual(initial["counts"]["people"], 1)
        self.assertEqual(request("person?id=li%3A42")["dossier_html"], "")
        self.assertEqual(request("worth", {"ids": ["li:42"], "worth": "yes"})["rows"][0]["worth_source"], "human")
        self.assertEqual(request("name", {"parent_id": "li:42", "name": "My colleague"})["rows"][0]["name"], "My colleague")
        self.assertEqual(request("relationship", {"parent_id": "li:42", "relationship_kind": "colleague"})["rows"][0]["relationship_kind"], "colleague")
        self.assertEqual(request("tags", {"people": [{"parent_id": "li:42", "tags": ["is_investor"]}]})["rows"][0]["tags"], ["is_investor"])
        self.assertEqual(request("person?id=li%3A42")["relationship_kind_source"], "human")
        self.assertEqual(ReviewApi(self.conn, self.root).linkedin_card({}).pending, 0)
        PersonLabels(self.conn, self.root).run()
        self.assertEqual(self.count("person_labels"), 0)
        self.assertEqual(request("rows")["columns"], initial["columns"])


class MigrationTests(unittest.TestCase):
    def test_schema_9_upgrade_preserves_every_existing_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "old.sqlite"
            old = sqlite3.connect(path)
            old.executescript(DDL.replace("'linkedin', ", ""))
            old.execute("INSERT INTO meta VALUES ('schema_version', '9')")
            old.execute("INSERT INTO candidates VALUES ('candidate:email:jordan@example.com', 'Jordan Bravo', 0, '{}', ?)", (NOW,))
            old.execute("INSERT INTO candidate_sources VALUES ('candidate:email:jordan@example.com', 'gmail_msgvault')")
            old.execute("INSERT INTO candidate_parent (candidate_id, parent_id, reason, created_at) VALUES ('candidate:email:jordan@example.com', 'li:42', 'human', ?)", (NOW,))
            old.execute("INSERT INTO worth (candidate_id, worth, decided_by, reason, created_at) VALUES ('candidate:email:jordan@example.com', 'no', 'human', 'keep', ?)", (NOW,))
            old.commit()
            tables = [r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('meta','sqlite_sequence')")]
            saved = {table: old.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
            old.close()
            conn = open_store(path)
            self.addCleanup(conn.close)
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], str(SCHEMA_VERSION))
            for table in tables:
                self.assertEqual([tuple(r) for r in conn.execute(f"SELECT * FROM {table}")], saved[table], table)
            conn.execute("INSERT INTO candidate_sources VALUES ('candidate:email:jordan@example.com', 'linkedin')")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
