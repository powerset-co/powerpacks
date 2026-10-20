"""Synthetic offline evidence; fake vectors check plumbing, never semantic recall."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from collections.abc import Iterable
from dataclasses import asdict
from unittest.mock import patch

import httpx

from packs.ingestion.primitives.common.jsonio import sha256_file
from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.ingestion.primitives.deep_context_v2.lookup import matches
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import OwnedIdentifiers, SynthesizedFacts
from packs.memory.__main__ import main
from packs.memory.generate import Generate
from packs.memory.models import Claim, Filters, Kind
from packs.memory.provider import OpenAIProvider, parse_response
from packs.memory import discovery_provider
from packs.memory.query import Query
from packs.memory.sources import V2
from packs.memory.store import Store

ROOT = Path(__file__).resolve().parents[1]
STORIES = json.loads((ROOT / "tests/fixtures/memory/stories.json").read_text())


def response(claims: list[dict], status: str = "completed") -> str:
    return json.dumps({"status": status, "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps({"claims": claims})}]}]})


class FixtureProvider:
    generation_key = "synthetic-generation-v1"
    embedding_key = "synthetic-embedding-v1"

    def __init__(self) -> None:
        self.generations = 0
        self.embeddings = 0
        self.fail_generation = 0
        self.fail_embedding = 0

    def generate(self, content: str) -> str:
        self.generations += 1
        if self.generations == self.fail_generation:
            raise ValueError("synthetic interruption")
        return response(json.loads(content).get("memory_claims", []))

    def embed(self, content: str) -> tuple[float, ...]:
        self.embeddings += 1
        if self.embeddings == self.fail_embedding:
            raise ValueError("synthetic embedding interruption")
        # Distinct from lexical matching: this fixture maps a paraphrase to the IAM axis.
        if "permission architecture" in content or "IAM" in content:
            return (1.0, 0.0, 0.0)
        if "friend" in content:
            return (0.0, 1.0, 0.0)
        return (0.0, 0.0, 1.0)


def source_db(path: Path, stories: Iterable[dict]) -> None:
    conn = open_store(path)
    for story in stories:
        member = story["member"]
        facts = SynthesizedFacts(story["name"], (), (), "", "", "", "", "", "", (), (), (),
                                 OwnedIdentifiers((), (), ()), (), 0.9, False).to_payload()
        facts["memory_claims"] = story["claims"]
        conn.execute("INSERT INTO candidates VALUES(?,?,0,'{}','now')", (member, story["name"]))
        conn.execute("INSERT INTO candidate_parent(candidate_id,parent_id,reason,created_at) VALUES(?,?,'singleton','now')",
                     (member, story["parent"]))
        conn.execute("INSERT INTO candidate_identifiers VALUES(?,'email',?,?)",
                     (member, member + "@example.com", member + "@example.com"))
        conn.execute("INSERT INTO facts VALUES(?,?,'fixture','fixture','low','now')", (member, json.dumps(facts)))
    conn.commit()
    conn.close()


class MemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "v2"
        self.adapter = V2(self.root)
        source_db(self.adapter.database, STORIES)
        self.store = Store(self.base / "derived.sqlite", create=True)
        self.addCleanup(self.store.close)
        self.provider = FixtureProvider()
        self.report = self.store.import_source(self.adapter)

    def generate(self) -> None:
        Generate(self.store, self.provider).run(limit=100)

    def search(self, text: str = "", **filters) -> tuple:
        return Query(self.store, self.provider).run(text, filters=Filters(**filters)).hits

    def test_import_identity_parity_isolation_and_family_dedup(self) -> None:
        before = sha256_file(self.adapter.database)
        self.generate()
        again = self.store.import_source(self.adapter)
        self.assertEqual((again.members, again.parents, again.changed), (5, 4, 0))
        conn = sqlite3.connect(self.adapter.database)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        for name in (story["name"] for story in STORIES):
            expected = {match.parent_id for match in matches(conn, name=name)}
            actual = {row.parent.split("#")[-1] for row in self.store.lookup(name)}
            self.assertEqual(actual, expected)
        self.assertEqual(len(self.store.lookup("C1@EXAMPLE.COM")), 1)
        friends = self.search(kind=Kind.RELATIONSHIP, topic="friends")
        self.assertEqual(len(friends), 1)
        self.assertEqual(len(friends[0].citations), 2)
        self.assertEqual(before, sha256_file(self.adapter.database))
        self.assertIn("unverified", friends[0].citations[0].level)
        self.assertFalse(self.store.open(friends[0].claim_id).original_message_citation_verified)

    def test_identical_run_zero_calls_one_source_updates_only_dependents(self) -> None:
        self.generate()
        calls = (self.provider.generations, self.provider.embeddings)
        self.store.import_source(self.adapter)
        report = Generate(self.store, self.provider).run(limit=100)
        self.assertEqual((report.generation_calls, report.embedding_calls), (0, 0))
        self.assertEqual(calls, (self.provider.generations, self.provider.embeddings))
        conn = sqlite3.connect(self.adapter.database)
        payload = json.loads(conn.execute("SELECT facts_json FROM facts WHERE candidate_id='c4'").fetchone()[0])
        payload["memory_claims"][0]["text"] = "Taylor Foxtrot listed IAM as a topic of interest, once."
        conn.execute("UPDATE facts SET facts_json=? WHERE candidate_id='c4'", (json.dumps(payload),))
        conn.commit()
        conn.close()
        imported = self.store.import_source(self.adapter)
        self.assertEqual(imported.changed, 1)
        self.assertEqual(self.search(kind=Kind.MENTION), ())
        report = Generate(self.store, self.provider).run(limit=100)
        self.assertEqual((report.generation_calls, report.embedding_calls), (1, 1))

    def test_merge_remaps_without_generation_or_source_changes(self) -> None:
        self.generate()
        conn = sqlite3.connect(self.adapter.database)
        conn.execute("INSERT INTO candidate_parent(candidate_id,parent_id,reason,created_at) VALUES('c2','p:1','human','later')")
        conn.commit()
        conn.close()
        report = self.store.import_source(self.adapter)
        self.assertEqual(report.changed, 0)
        self.assertEqual(self.store.lookup("Casey Delta")[0].parent, self.store.lookup("Jordan Bravo")[0].parent)
        parent = self.store.lookup("Jordan Bravo")[0].parent
        self.assertEqual(len(self.search(parents=(parent,), kind=Kind.LP)), 1)
        report = Generate(self.store, self.provider).run(limit=100)
        self.assertEqual((report.generation_calls, report.embedding_calls), (0, 0))

    def test_generation_and_embedding_interrupt_restart(self) -> None:
        self.provider.fail_generation = 2
        events = list(Generate(self.store, self.provider).progress(limit=100))
        self.assertEqual([event.error for event in events if event.error], ["synthetic interruption"])
        first_chunk = self.store.conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        self.assertEqual(first_chunk, 4)
        self.provider.fail_generation = 0
        report = Generate(self.store, self.provider).run(limit=100)
        self.assertEqual(report.generation_calls, 1)
        self.assertEqual(report.reused_chunks, 4)
        self.assertEqual(Generate(self.store, self.provider).run(limit=100).embedding_calls, 0)

    def test_generation_model_change_keeps_claims_on_resume(self) -> None:
        self.generate()
        before = self.search(kind=Kind.WORK)
        self.provider.generation_key = "synthetic-generation-v2"
        self.assertEqual(Generate(self.store, self.provider).run(limit=100).generation_calls, 5)
        self.assertEqual(Generate(self.store, self.provider).run(limit=100).generation_calls, 0)
        self.assertEqual(len(self.search(kind=Kind.WORK)), len(before))

    def test_embedding_failure_keeps_generation_checkpoint(self) -> None:
        self.provider.fail_embedding = 2
        events = list(Generate(self.store, self.provider).progress(limit=100))
        self.assertTrue(any(event.error == "synthetic embedding interruption" for event in events))
        self.provider.fail_embedding = 0
        report = Generate(self.store, self.provider).run(limit=100)
        self.assertEqual(report.generation_calls, 0)
        self.assertEqual(self.provider.generations, 5)
        self.assertEqual(report.embedding_calls, 0)

    def test_hybrid_filters_aliases_context_and_stories(self) -> None:
        self.generate()
        query = Query(self.store, self.provider)
        result = query.run("permission architecture", filters=Filters(kind=Kind.WORK))
        self.assertEqual(result.lexical_candidates, 0)
        self.assertEqual(result.vector_candidates, 1)
        self.assertEqual(result.hits[0].name, "Jordan Bravo")
        fusion = query.run("IAM")
        self.assertTrue(any(hit.lexical_rank and hit.vector_rank for hit in fusion.hits))
        self.assertEqual({hit.name for hit in self.search(kind=Kind.DISCUSSION)}, {"Casey Delta"})
        self.assertEqual({hit.name for hit in self.search(kind=Kind.MENTION)}, {"Taylor Foxtrot"})
        self.assertEqual({hit.name for hit in self.search(kind=Kind.LP, object="Cedar Fund I")}, {"Casey Delta"})
        self.assertEqual(self.search(kind=Kind.LP, object="Cedar Fund II"), ())
        self.assertEqual(self.search(kind=Kind.PREFERENCE, scope="all communication"), ())
        self.assertEqual(len(self.search(kind=Kind.PREFERENCE, scope="Cedar Fund updates")), 1)
        self.assertEqual(self.search(topic="IAM"), ())  # suggestion alone never merges
        self.store.confirm_alias("IAM", "identity access management")
        self.assertEqual(len(self.search(topic="IAM")), 3)
        with self.store.conn:
            self.store.conn.execute("INSERT INTO topics VALUES('industrial machinery')")
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.store.confirm_alias("IAM", "industrial machinery")
        self.assertEqual(len(self.search(topic="IAM")), 3)
        parent = self.store.lookup("Jordan Bravo")[0].parent
        pairs = query.overlap(parent)
        self.assertEqual({right.name for _, right in pairs}, {"Casey Delta"})
        self.assertEqual(query.overlap(parent, organization="Another Corp"), ())
        bounded = query.run("IAM", context_chars=20)
        self.assertEqual(bounded.hits, ())
        self.assertTrue(bounded.truncated)
        self.assertEqual(self.search(parents=("unknown",)), ())

    def test_correction_removes_stale_fts_vectors_and_survives_reimport(self) -> None:
        self.generate()
        old = self.search(kind=Kind.RELATIONSHIP)[0]
        replacement = Claim("Jordan Bravo is a former colleague.", Kind.RELATIONSHIP, "colleagues", "owner", "", "", "", "human statement")
        replacement_id = self.store.correct(old.claim_id, replacement, "Human corrected relationship")
        self.assertEqual(self.search(topic="friends"), ())
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM lexical WHERE lexical MATCH 'friend'").fetchone()[0], 0)
        self.store.import_source(self.adapter)
        self.generate()
        result = self.search("friend", kind=Kind.RELATIONSHIP)
        self.assertEqual([hit.claim_id for hit in result], [replacement_id])
        self.assertEqual(self.search(topic="friends"), ())
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM corrections").fetchone()[0], 1)
        self.assertIn("close friend", json.dumps(asdict(self.store.open(old.claim_id))))
        self.assertEqual(self.search(topic="colleagues")[0].citations[0].level, "human correction")
        # Changed artifact reintroduces the same machine claim; human decision still wins.
        conn = sqlite3.connect(self.adapter.database)
        conn.execute("UPDATE facts SET facts_json=json_set(facts_json,'$.title','new title') WHERE candidate_id='c1'")
        conn.commit()
        conn.close()
        self.store.import_source(self.adapter)
        self.generate()
        self.assertEqual(self.search(topic="friends"), ())

    def test_unsupported_quote_is_saved_excluded_and_not_bought_again(self) -> None:
        raw = response([json.loads(Claim("Made up", Kind.WORK, "work", "", "", "", "", "not in source").serialized())])
        with patch.object(FixtureProvider, "generate", return_value=raw) as generate:
            report = Generate(self.store, self.provider).run(limit=100)
            self.assertEqual(report.exclusions, 5)
            self.assertEqual(self.store.conn.execute("SELECT count(*) FROM chunks WHERE response=? AND error<>''", (raw,)).fetchone()[0], 5)
            self.assertEqual(self.store.conn.execute("SELECT count(*) FROM active_claims").fetchone()[0], 0)
            self.assertEqual(Generate(self.store, self.provider).run(limit=100).generation_calls, 0)
            self.assertEqual(generate.call_count, 5)

    def test_nonmemory_database_refused_and_readonly_enforced(self) -> None:
        before = sha256_file(self.adapter.database)
        with self.assertRaisesRegex(ValueError, "non-memory"):
            Store(self.adapter.database, create=True)
        self.assertEqual(before, sha256_file(self.adapter.database))
        from packs.memory.sources import readonly
        conn = readonly(self.adapter.database)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("DELETE FROM facts")

    def test_source_requires_current_schema_without_migration(self) -> None:
        with sqlite3.connect(self.adapter.database) as conn:
            conn.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
        before = sha256_file(self.adapter.database)
        with self.assertRaisesRegex(ValueError, "current Deep Context schema 6"):
            self.store.import_source(self.adapter)
        self.assertEqual(sha256_file(self.adapter.database), before)
        with sqlite3.connect(self.adapter.database) as conn:
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "4")

    def test_human_tags_and_confirmed_profile_keep_structured_dates(self) -> None:
        url = "https://www.linkedin.com/in/synthetic-jordan"
        conn = sqlite3.connect(self.adapter.database)
        conn.execute("INSERT INTO person_tags(candidate_id,tags,note,updated_at) VALUES('c1','friend|mentor','human note','now')")
        conn.execute("INSERT INTO candidate_parent(candidate_id,parent_id,reason,created_at) VALUES('c1','li:123','human','later')")
        conn.execute("INSERT INTO candidate_linkedins(candidate_id,linkedin_url,member_id,origin,verdict,decided_by,"
                     "judgment_fingerprint,reason,created_at) VALUES('c1',?,'123','human_override','confirmed','human','x','','now')", (url,))
        conn.commit()
        conn.close()
        profile = self.root / "network-import/profile_cache_v2/synthetic-jordan.json"
        profile.parent.mkdir(parents=True)
        normalized = {"success": True, "full_name": "Jordan Bravo", "member_id": "123", "location_str": "Springfield",
                      "experiences": [{"title": "Engineer", "company_name": "Synthetic Corp",
                                       "starts_at": {"year": 2020, "month": 5, "day": 10}}]}
        profile.write_text(json.dumps({"normalized_profile": normalized, "raw_response": {}, "fetched_at": "now"}))
        before = sha256_file(profile)
        report = self.store.import_source(self.adapter)
        self.assertEqual(report.changed, 2)
        self.assertEqual(report.failed_reads, 0)
        rows = list(self.store.conn.execute("SELECT key,r.content FROM sources s JOIN revisions r ON r.source_id=s.id AND r.hash=s.hash"))
        self.assertEqual(json.loads(next(row[1] for row in rows if row[0] == "profile:li:123")), normalized)
        self.assertEqual(json.loads(next(row[1] for row in rows if row[0] == "tags:c1"))["human_tags"], ["friend", "mentor"])
        normalized["member_id"] = "wrong"
        profile.write_text(json.dumps({"normalized_profile": normalized, "raw_response": {}, "fetched_at": "now"}))
        changed_file = sha256_file(profile)
        self.assertNotEqual(before, changed_file)
        failed = self.store.import_source(self.adapter)
        self.assertEqual(failed.failed_reads, 1)
        self.assertEqual(sha256_file(profile), changed_file)
        self.assertEqual(self.store.conn.execute("SELECT error FROM sources WHERE key='profile:li:123'").fetchone()[0], "profile member id mismatch")


class ProviderAndCliTests(unittest.TestCase):
    def test_real_request_shape_no_store_no_input_trace_and_embedding_paths(self) -> None:
        requests: list[dict] = []
        claim = STORIES[0]["claims"][0]

        def respond(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append(body)
            if request.url.path.endswith("responses"):
                return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": json.dumps({"claims": [claim]})}]}]})
            return httpx.Response(200, json={"data": [{"embedding": [1, 2, 3]}]})

        client = httpx.Client(base_url="https://api.openai.com/v1/", transport=httpx.MockTransport(respond))
        self.addCleanup(client.close)
        provider = OpenAIProvider("approved-model", "approved-embedding", client=client, approve_spend=True)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic"}):
            self.assertEqual(parse_response(provider.generate("close friend"), "close friend")[0][0].kind, Kind.RELATIONSHIP)
            provider.embed("document")
            provider.embed("query")
            provider.discover("Synthetic saved dossier excerpt")
        self.assertFalse(requests[0]["store"])
        self.assertEqual(requests[1]["input"], "document")
        self.assertEqual(requests[2]["input"], "query")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(requests[0]["max_output_tokens"], 6000)
        instructions = requests[3]['instructions']
        self.assertIn('Upstream saved dossiers, profile facts and human tags are valid evidence', instructions)
        self.assertIn('Do not require raw messages or event records', instructions)
        self.assertIn('Original-message verification remains unverified', instructions)
        self.assertFalse(requests[3]['store'])
        self.assertEqual(requests[3]['text']['format']['name'], 'memory_discovery')
        with patch.object(discovery_provider, 'SYSTEM', instructions + '\nChanged wording.'):
            changed = OpenAIProvider('approved-model', 'approved-embedding', client=client)
        self.assertNotEqual(provider.discovery_key, changed.discovery_key)
        # This is an HTTP mock. Live dashboard log absence is explicitly unverified.

    def test_cli_import_generate_resume_query_open_and_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            adapter = V2(base / "source")
            source_db(adapter.database, STORIES)
            db = base / "derived.sqlite"
            prefix = ["--db", str(db)]
            fixture = FixtureProvider()

            def cli(*args: str) -> object:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(prefix + list(args)), 0)
                return json.loads(output.getvalue())

            self.assertEqual(cli("import", "--v2-root", str(adapter.root))[0]["members"], 5)
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(cli("query", "--embedding-model", "fixture")["hits"], [])
            with patch.object(OpenAIProvider, "generate", side_effect=fixture.generate), patch.object(OpenAIProvider, "embed", side_effect=fixture.embed):
                generated = cli("generate", "--model", "fixture", "--embedding-model", "fixture", "--limit", "1")
                self.assertEqual(generated["generation_calls"], 1)
                generated = cli("generate", "--model", "fixture", "--embedding-model", "fixture", "--limit", "100")
                self.assertEqual(generated["generation_calls"], 4)
                self.assertEqual(cli("generate", "--model", "fixture", "--embedding-model", "fixture", "--limit", "100")["generation_calls"], 0)
                results = cli("query", "IAM", "--embedding-model", "fixture", "--kind", "work", "--person", "Jordan Bravo")
                self.assertEqual(len(results["hits"]), 1)
                opened = cli("open", str(results["hits"][0]["claim_id"]))
                self.assertFalse(opened["original_message_citation_verified"])
            env = dict(os.environ)
            env.pop("OPENAI_API_KEY", None)
            result = subprocess.run([sys.executable, "-m", "packs.memory", *prefix, "generate", "--model", "fixture",
                                     "--embedding-model", "fixture", "--limit", "1", "--dry-run"], cwd=ROOT, env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["pending_chunks"], 0)


if __name__ == "__main__":
    unittest.main()
