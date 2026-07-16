"""Unit + light-integration tests for the deep-context dossier pipeline.

Covers identity normalization, the privacy gate, adaptive sampling, fact merge,
attributedBody decoding, Jaro-Winkler blocking/merge detection, and an end-to-end
compose -> cluster -> lookup flow over synthetic fixtures (no network, no DB).
"""
from __future__ import annotations

import csv
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context import (
    build_parents as parents,
    cluster_merge_candidates as cluster,
    collect_person_context as collect,
    common,
    compose_dossier as compose,
    lookup_person as lookup,
    apply_retargets as retargets,
    reconcile_deep_research as dresearch,
    reconcile_linkedin as reconcile,
    reconcile_review_web as web,
    sources,
    synthesize_person_context as synth,
)


class TestCommon(unittest.TestCase):
    def test_normalize_phone(self):
        self.assertEqual(common.normalize_phone("(415) 555-1234"), "+14155551234")
        self.assertEqual(common.normalize_phone("+1 415 555 1234"), "+14155551234")
        self.assertEqual(common.normalize_phone("123"), "")

    def test_phone_digits_drops_us_country_code(self):
        self.assertEqual(common.phone_digits("+14155551234"), "4155551234")
        self.assertEqual(common.phone_digits("4155551234"), "4155551234")

    def test_normalize_email_name(self):
        self.assertEqual(common.normalize_email("  Jane@ACME.com "), "jane@acme.com")
        self.assertEqual(common.normalize_name("  Jane   Doe "), "jane doe")

    def test_slugify_stable_and_collision_proof(self):
        self.assertEqual(common.slugify("Jane Doe", "abcd1234-xyz"), "jane-doe-abcd1234")
        self.assertNotEqual(common.slugify("Jane Doe", "id-one"), common.slugify("Jane Doe", "id-two"))

    def test_parse_list_handles_json_and_bare(self):
        self.assertEqual(common.parse_list('["a@x.com", "b@x.com"]'), ["a@x.com", "b@x.com"])
        self.assertEqual(common.parse_list("solo@x.com"), ["solo@x.com"])
        self.assertEqual(common.parse_list(""), [])

    def test_load_people_filters_and_parses(self):
        with tempfile.TemporaryDirectory() as d:
            csv_path = Path(d) / "people.csv"
            csv_path.write_text(
                "id,full_name,primary_email,all_emails,primary_phone,all_phones,source_channels\n"
                'p1,Jane Doe,jane@acme.com,"[""jane@acme.com""]",+14155551234,"[""+14155551234""]",gmail_msgvault,imessage\n'
                "p2,No Channels,,,,,linkedin_csv\n",
                encoding="utf-8",
            )
            people = list(common.load_people(csv_path))
            self.assertEqual(len(people), 1)
            self.assertEqual(people[0].person_id, "p1")
            self.assertIn("jane@acme.com", people[0].emails)
            self.assertIn("+14155551234", people[0].phones)


class TestDeepContextRunnerSafety(unittest.TestCase):
    def test_chained_paid_run_is_disabled(self):
        runner = Path(__file__).resolve().parents[1] / "bin" / "deep-context"
        blocked = subprocess.run(
            [str(runner), "run"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("intentionally disabled", blocked.stderr)

        help_result = subprocess.run(
            [str(runner), "run", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("paid stages require", help_result.stderr)


import sqlite3  # noqa: E402  (local to the msgvault-con helper below)

_MSGVAULT_SCHEMA = """
CREATE TABLE sources (id INTEGER PRIMARY KEY, source_type TEXT, identifier TEXT, display_name TEXT);
CREATE TABLE participants (id INTEGER PRIMARY KEY, email_address TEXT, display_name TEXT, domain TEXT);
CREATE TABLE messages (id INTEGER PRIMARY KEY, source_id INTEGER, conversation_id INTEGER, message_type TEXT,
    sent_at TEXT, received_at TEXT, internal_date TEXT, deleted_at TEXT, deleted_from_source_at TEXT,
    sender_id INTEGER, subject TEXT, snippet TEXT);
CREATE TABLE message_recipients (id INTEGER PRIMARY KEY, message_id INTEGER, participant_id INTEGER,
    recipient_type TEXT, display_name TEXT);
CREATE TABLE message_bodies (id INTEGER PRIMARY KEY, message_id INTEGER, body_text TEXT, body_html TEXT);
"""


class TestAdaptiveGmailCollection(unittest.TestCase):
    """Gmail is its own 1600-vertical now: keep a thread's back-and-forth, honest counts,
    and don't crowd out chat."""

    def _con(self) -> sqlite3.Connection:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(_MSGVAULT_SCHEMA)
        con.executescript("""
            INSERT INTO sources (id, source_type, identifier, display_name) VALUES (1, 'gmail', 'me@gmail.com', 'Me');
            INSERT INTO participants (id, email_address, display_name) VALUES
                (1, 'jordan@acme.dev', 'Jordan Acme'), (2, 'me@gmail.com', 'Me');
            -- One thread (100), a real 4-message back-and-forth (2 Jordan, 2 me).
            INSERT INTO messages (id, source_id, conversation_id, message_type, sent_at, sender_id, subject, snippet) VALUES
                (10, 1, 100, 'email', '2026-01-01T00:00:00Z', 1, 'coffee', 'lets grab coffee next week sometime'),
                (11, 1, 100, 'email', '2026-01-02T00:00:00Z', 2, 'Re: coffee', 'sure how about tuesday afternoon'),
                (12, 1, 100, 'email', '2026-01-03T00:00:00Z', 1, 'Re: coffee', 'tuesday works great see you then'),
                (13, 1, 100, 'email', '2026-01-04T00:00:00Z', 2, 'Re: coffee', 'perfect talk soon and take care');
            INSERT INTO message_recipients (message_id, participant_id, recipient_type) VALUES
                (10, 2, 'to'), (11, 1, 'to'), (12, 2, 'to'), (13, 1, 'to');
        """)
        con.commit()
        return con

    def _person(self, phones=None):
        return common.Person(person_id="p1", full_name="Jordan Acme",
                             emails=["jordan@acme.dev"], phones=phones or [], source_channels=[])

    def test_read_gmail_keeps_thread_back_and_forth(self):
        con = self._con()
        self.addCleanup(con.close)
        accounts = sources.bec.account_emails(con)
        msgs = sources.read_gmail(self._person(), con, accounts)
        self.assertGreater(len(msgs), 1)            # was 1 (thread collapsed); now the back-and-forth
        self.assertEqual(len(msgs), 4)

    def test_collect_one_honest_available_and_capped(self):
        con = self._con()
        self.addCleanup(con.close)
        accounts = sources.bec.account_emails(con)
        nope = Path("/nonexistent-deepctx")
        # deep_cap below the true total => pool trimmed, but `available` reports the true 4.
        pool, available = collect.collect_one(
            self._person(), msgvault_con=con, accounts=accounts,
            chat_db=nope, wacli_db=nope, deep_cap=2)
        self.assertEqual(available, 4)
        self.assertEqual(len(pool), 2)
        self.assertGreater(available, len(pool))    # capped == True downstream
        # deep_cap above the total => honest, not capped (the Bretton case).
        pool2, available2 = collect.collect_one(
            self._person(), msgvault_con=con, accounts=accounts,
            chat_db=nope, wacli_db=nope, deep_cap=50)
        self.assertEqual(available2, 4)
        self.assertEqual(len(pool2), 4)

    def test_gmail_does_not_starve_chat(self):
        con = self._con()
        self.addCleanup(con.close)
        accounts = sources.bec.account_emails(con)
        fake_dms = [{"channel": "imessage", "at": "2026-03-01T00:00:00Z",
                     "direction": "from_them", "text": "hey are we still on for friday"}]
        orig = (sources.read_imessage, sources.count_imessage_dms, sources.read_whatsapp)
        sources.read_imessage = lambda p, db, cap=0: list(fake_dms)
        sources.count_imessage_dms = lambda p, db: len(fake_dms)
        sources.read_whatsapp = lambda p, db, cap=0: []
        try:
            pool, _ = collect.collect_one(
                self._person(phones=["+14155550000"]), msgvault_con=con, accounts=accounts,
                chat_db=Path("/nope"), wacli_db=Path("/nope"), deep_cap=2)
        finally:
            sources.read_imessage, sources.count_imessage_dms, sources.read_whatsapp = orig
        channels = {m["channel"] for m in pool}
        self.assertIn("gmail", channels)            # gmail's capped vertical...
        self.assertIn("imessage", channels)         # ...still leaves room for chat

    def test_manifest_reports_opted_in_group_body_access(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            people = base / "people.csv"
            people.write_text(
                "id,full_name,primary_email,all_emails,primary_phone,all_phones,source_channels\n",
                encoding="utf-8",
            )
            manifest = collect.build(_ns(
                out_dir=base / "raw",
                chat_db=base / "missing-chat.db",
                wacli_db=base / "missing-wacli.db",
                people_csv=people,
                msgvault_db=base / "missing-msgvault.db",
                dry_run=True,
                limit=0,
                person="",
                force=False,
                deep_cap=10,
                include_groups=True,
                max_group_size=12,
            ))
            self.assertTrue(manifest["privacy"]["groups_read"])
            self.assertFalse(manifest["privacy"]["dms_only"])
            self.assertEqual(manifest["privacy"]["group_source"], "imessage")
            self.assertEqual(manifest["privacy"]["max_group_size"], 12)

    def test_default_collection_rebuilds_retained_group_bundles(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            raw = base / "raw"
            raw.mkdir()
            people = base / "people.csv"
            people.write_text(
                "id,full_name,primary_email,all_emails,primary_phone,all_phones,source_channels\n"
                "p1,Person,,,+14155550000,,imessage\n",
                encoding="utf-8",
            )
            bundle = raw / "p1.json"
            bundle.write_text(json.dumps({
                "messages": [
                    {"channel": "imessage", "text": "dm"},
                    {"channel": "imessage_group", "text": "group body"},
                ],
                "messages_available": 2,
                "capped": False,
                "collection_policy": {
                    "deep_cap": 10,
                    "include_groups": True,
                    "max_group_size": 12,
                },
            }), encoding="utf-8")
            (raw / "manifest.json").write_text(json.dumps({
                "privacy_schema_version": 2,
                "privacy": {"group_bodies_present": True},
            }), encoding="utf-8")

            dm_message = {
                "channel": "imessage",
                "at": "2026-07-13T00:00:00Z",
                "direction": "from_them",
                "text": "dm",
            }
            with mock.patch.object(collect, "collect_one", return_value=([dm_message], 1)):
                manifest = collect.build(_ns(
                    out_dir=raw,
                    chat_db=base / "missing-chat.db",
                    wacli_db=base / "missing-wacli.db",
                    people_csv=people,
                    msgvault_db=base / "missing-msgvault.db",
                    dry_run=False,
                    limit=0,
                    person="",
                    force=False,
                    deep_cap=10,
                    include_groups=False,
                    max_group_size=25,
                ))

            saved = json.loads(bundle.read_text(encoding="utf-8"))
            self.assertEqual([message["channel"] for message in saved["messages"]], ["imessage"])
            self.assertFalse(saved["collection_policy"]["include_groups"])
            self.assertEqual(manifest["bundles_purged_for_scope"], 1)
            self.assertFalse(manifest["privacy"]["groups_read"])
            self.assertTrue(manifest["privacy"]["dms_only"])

            opted_in_message = {
                "channel": "imessage_group",
                "at": "2026-07-13T00:00:00Z",
                "direction": "from_them",
                "text": "approved group body",
            }
            with mock.patch.object(
                collect,
                "collect_one",
                return_value=([opted_in_message], 1),
            ) as collect_mock:
                opted_in_manifest = collect.build(_ns(
                    out_dir=raw,
                    chat_db=base / "missing-chat.db",
                    wacli_db=base / "missing-wacli.db",
                    people_csv=people,
                    msgvault_db=base / "missing-msgvault.db",
                    dry_run=False,
                    limit=0,
                    person="",
                    force=False,
                    deep_cap=10,
                    include_groups=True,
                    max_group_size=12,
                ))

            collect_mock.assert_called_once()
            restored = json.loads(bundle.read_text(encoding="utf-8"))
            self.assertEqual(restored["messages"][0]["channel"], "imessage_group")
            self.assertTrue(opted_in_manifest["privacy"]["groups_read"])
            self.assertTrue(opted_in_manifest["privacy"]["group_bodies_present"])

    def test_invalid_input_does_not_purge_retained_bundles(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            raw = base / "raw"
            raw.mkdir()
            bundle = raw / "p1.json"
            bundle.write_text('{"messages":[{"channel":"imessage_group","text":"private"}]}',
                              encoding="utf-8")
            (raw / "manifest.json").write_text(json.dumps({
                "privacy_schema_version": 2,
                "privacy": {"group_bodies_present": True},
            }), encoding="utf-8")

            with self.assertRaises(FileNotFoundError):
                collect.build(_ns(
                    out_dir=raw,
                    people_csv=base / "missing.csv",
                    msgvault_db=base / "missing-msgvault.db",
                    chat_db=base / "missing-chat.db",
                    wacli_db=base / "missing-wacli.db",
                    dry_run=False,
                    limit=0,
                    person="",
                    force=False,
                    deep_cap=10,
                    include_groups=False,
                    max_group_size=25,
                ))
            self.assertTrue(bundle.exists())

    def test_partial_default_collection_refuses_group_scope_transition(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            raw = base / "raw"
            raw.mkdir()
            people = base / "people.csv"
            people.write_text(
                "id,full_name,primary_email,all_emails,primary_phone,all_phones,source_channels\n"
                "p1,Person,,,+14155550000,,imessage\n",
                encoding="utf-8",
            )
            bundle = raw / "p1.json"
            bundle.write_text('{"messages":[{"channel":"imessage_group","text":"private"}]}',
                              encoding="utf-8")
            (raw / "manifest.json").write_text(json.dumps({
                "privacy_schema_version": 2,
                "privacy": {"group_bodies_present": True},
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "full default collection"):
                collect.build(_ns(
                    out_dir=raw,
                    people_csv=people,
                    msgvault_db=base / "missing-msgvault.db",
                    chat_db=base / "missing-chat.db",
                    wacli_db=base / "missing-wacli.db",
                    dry_run=False,
                    limit=1,
                    person="",
                    force=False,
                    deep_cap=10,
                    include_groups=False,
                    max_group_size=25,
                ))
            self.assertTrue(bundle.exists())


class TestSampling(unittest.TestCase):
    def test_signal_rank_prefers_signature(self):
        rich = {"text": "I'm CTO at Acme, call +1 415 555 1234 https://acme.com", "at": "2020"}
        thin = {"text": "thanks!", "at": "2021"}
        self.assertGreater(sources.signal_rank(rich), sources.signal_rank(thin))


class TestAttributedBody(unittest.TestCase):
    def test_decode_single_byte_length(self):
        text = "hey are you free tomorrow?"
        blob = b"\x04\x0bstreamtyped" + b"NSString" + b"\x01\x94\x84\x01+" + bytes([len(text)]) + text.encode()
        self.assertEqual(sources.decode_attributed_body(blob), text)

    def test_decode_empty_returns_blank(self):
        self.assertEqual(sources.decode_attributed_body(None), "")
        self.assertEqual(sources.decode_attributed_body(b"no marker here"), "")


class TestSynthesize(unittest.TestCase):
    def test_chunk_messages_budget(self):
        msgs = [{"text": "a" * 50} for _ in range(5)]
        chunks = synth.chunk_messages(msgs, chunk_chars=120)
        self.assertTrue(all(sum(len(m["text"]) for m in c) <= 120 or len(c) == 1 for c in chunks))
        self.assertEqual(sum(len(c) for c in chunks), 5)

    def test_fact_keys_detect_new_info(self):
        a = {"employers": [{"name": "Acme"}], "topics": ["x"], "title": "", "school": "", "location": "", "field_of_study": "", "identifiers": []}
        b = {"employers": [{"name": "Acme"}], "topics": ["x"], "title": "", "school": "", "location": "", "field_of_study": "", "identifiers": []}
        self.assertEqual(synth.fact_keys(a), synth.fact_keys(b))
        c = dict(b, topics=["x", "y"])
        self.assertTrue(synth.fact_keys(c) - synth.fact_keys(a))


class TestMergeFacts(unittest.TestCase):
    def test_merges_employers_and_picks_confident_scalars(self):
        chunks = [
            {"facts": {"canonical_name": "Jane Doe", "aliases": [], "employers": [{"name": "Acme", "role": "Eng", "status": "past"}],
                       "title": "Engineer", "school": "", "field_of_study": "", "location": "SF",
                       "relationship_to_owner": "colleague", "topics": ["ml"], "notable_events": [], "identifiers": [], "confidence": 0.6}},
            {"facts": {"canonical_name": "Jane Doe", "aliases": ["JD"], "employers": [{"name": "Acme", "role": "", "status": "current"}],
                       "title": "Staff Engineer", "school": "MIT", "field_of_study": "CS", "location": "",
                       "relationship_to_owner": "longtime colleague and friend", "topics": ["ml", "hiring"], "notable_events": [{"date": "2021", "summary": "joined"}], "identifiers": ["@jane"], "confidence": 0.9}},
        ]
        merged = compose.merge_facts(chunks)
        self.assertEqual(merged["canonical_name"], "Jane Doe")
        self.assertEqual(len(merged["employers"]), 1)
        self.assertEqual(merged["employers"][0]["status"], "current")  # current beats past
        self.assertEqual(merged["employers"][0]["role"], "Eng")  # role backfilled
        self.assertEqual(merged["title"], "Staff Engineer")  # higher confidence wins
        self.assertEqual(set(merged["topics"]), {"ml", "hiring"})
        self.assertEqual(merged["school"], "MIT")
        self.assertIn("longtime", merged["relationship_to_owner"])

    def test_headline(self):
        self.assertEqual(
            compose.headline({"title": "CTO", "employers": [{"name": "Acme", "status": "current"}]}),
            "CTO at Acme",
        )


class TestIncrementalSynthesis(unittest.TestCase):
    """Stop-logic for the confidence-gated deepening loop (fakes the OpenAI call)."""

    def _run(self, confidences, *, static_facts, nbatches, target=0.85, saturation=2, max_batches=20):
        import asyncio

        seq = list(confidences)
        calls = {"n": 0}

        async def fake_call_one(client, prompt, **kw):
            i = calls["n"]; calls["n"] += 1
            conf = seq[i] if i < len(seq) else seq[-1]
            topic = "same" if static_facts else f"t{i}"  # static => saturates
            return _facts(confidence=conf, topics=[topic]), {"input_tokens": 1, "output_tokens": 1, "reasoning_tokens": 0}, ""

        orig = synth._call_one
        synth._call_one = fake_call_one
        try:
            batches = [[{"text": "hi", "at": "2020", "channel": "imessage", "direction": "from_them"}] for _ in range(nbatches)]
            return asyncio.run(synth.synthesize_person(
                None, {"person_id": "p", "full_name": "X", "messages_available": 99}, batches,
                model="m", effort="low", semaphore=asyncio.Semaphore(1), max_retries=0,
                system_prompt="s", target_confidence=target, saturation_rounds=saturation, max_batches=max_batches,
            ))
        finally:
            synth._call_one = orig

    def test_stops_when_confident(self):
        res = self._run([0.5, 0.9], static_facts=False, nbatches=5)
        self.assertEqual(res["stop_reason"], "confident")
        self.assertEqual(res["batches_used"], 2)

    def test_stops_when_saturated(self):
        res = self._run([0.5, 0.5, 0.5, 0.5], static_facts=True, nbatches=5)
        self.assertEqual(res["stop_reason"], "saturated")
        self.assertEqual(res["batches_used"], 3)  # batch1 new, then 2 stale

    def test_stops_when_exhausted(self):
        res = self._run([0.5, 0.5, 0.5], static_facts=False, nbatches=3)
        self.assertEqual(res["stop_reason"], "exhausted")
        self.assertEqual(res["batches_used"], 3)

    def test_respects_max_batches(self):
        res = self._run([0.5] * 10, static_facts=False, nbatches=10, max_batches=3)
        self.assertEqual(res["stop_reason"], "max_batches")
        self.assertEqual(res["batches_used"], 3)

    def test_chunked_bounds_resident_set(self):
        chunks = list(synth._chunked(list(range(10)), 3))
        self.assertEqual([len(c) for c in chunks], [3, 3, 3, 1])  # never more than 3 at once
        self.assertEqual([x for c in chunks for x in c], list(range(10)))  # lossless

    def test_render_batch_includes_prior_profile(self):
        person = {"full_name": "Jane", "emails": [], "phones": [], "source_channels": []}
        batch = [{"text": "hello", "at": "2020", "channel": "imessage", "direction": "from_them"}]
        self.assertNotIn("PROFILE SO FAR", synth.render_batch(person, batch, None))
        self.assertIn("PROFILE SO FAR", synth.render_batch(person, batch, {"title": "CTO"}))


class TestBuildOwner(unittest.TestCase):
    def test_owner_from_profile_maps_schools_and_jobs(self):
        from packs.ingestion.primitives.deep_context import build_owner
        normalized = {
            "full_name": "Jane Doe", "headline": "Eng",
            "location_str": "NYC",
            "education": [{"school": "MIT", "degree": "BS", "field": "CS",
                           "starts_at": {"year": 2006}, "ends_at": {"year": 2010}}],
            "experiences": [{"company_name": "Acme", "title": "Engineer",
                             "starts_at": {"year": 2012}, "ends_at": {"year": 2016}}],
        }
        owner = build_owner.owner_from_profile(normalized, email="jane@x.com")
        self.assertEqual(owner["name"], "Jane Doe")
        self.assertEqual(owner["emails"], ["jane@x.com"])
        self.assertEqual(owner["education"][0], {"school": "MIT", "start": 2006, "end": 2010, "note": "BS CS"})
        self.assertEqual(owner["work"][0], {"company": "Acme", "title": "Engineer", "start": 2012, "end": 2016})
        self.assertEqual(owner["locations"], ["NYC"])


class TestOwnerContext(unittest.TestCase):
    def test_owner_background_block(self):
        block = common.owner_background_block({
            "name": "Jane Doe",
            "education": [{"school": "MIT", "end": 2010, "note": "undergrad"}],
            "work": [{"company": "Acme", "title": "Engineer", "start": 2012, "end": 2016}],
            "locations": ["NYC"],
        })
        self.assertIn("Jane Doe", block)
        self.assertIn("MIT [until 2010]", block)
        self.assertIn("Acme as Engineer [2012-2016]", block)

    def test_shared_context_merges_and_dedupes(self):
        chunks = [
            {"facts": _facts(shared_context=[{"overlap": "school", "detail": "Stanford overlap", "evidence": "e1"}])},
            {"facts": _facts(shared_context=[{"overlap": "school", "detail": "Stanford overlap", "evidence": "e1"},
                                             {"overlap": "employer", "detail": "Globex", "evidence": "e2"}])},
        ]
        merged = compose.merge_facts(chunks)
        details = {s["detail"] for s in merged["shared_context"]}
        self.assertEqual(details, {"Stanford overlap", "Globex"})


def _facts(**over):
    base = {"canonical_name": "X", "aliases": [], "employers": [], "title": "", "school": "",
            "field_of_study": "", "location": "", "relationship_to_owner": "", "topics": [],
            "notable_events": [], "identifiers": [], "shared_context": [], "confidence": 0.5}
    base.update(over)
    return base


class TestJaroWinkler(unittest.TestCase):
    def test_identical_and_similar(self):
        self.assertEqual(cluster.jaro_winkler("jane doe", "jane doe"), 1.0)
        self.assertGreater(cluster.jaro_winkler("jon smith", "john smith"), 0.9)
        self.assertLess(cluster.jaro_winkler("jane doe", "bob jones"), 0.7)

    def test_connected_components(self):
        comps = cluster.connected_components(4, [(0, 1), (1, 2)])
        self.assertEqual(sorted(comps[0]), [0, 1, 2])


class TestParents(unittest.TestCase):
    def test_clusters_from_pairs(self):
        pairs = [
            {"slug_a": "a", "slug_b": "b", "score": "1.0", "reason": "x"},
            {"slug_a": "b", "slug_b": "c", "score": "0.9", "reason": "y"},
            {"slug_a": "d", "slug_b": "e", "score": "0.95", "reason": "z"},
        ]
        cl = sorted(parents.clusters_from_pairs(pairs), key=len, reverse=True)
        self.assertEqual(sorted(cl[0]), ["a", "b", "c"])
        self.assertEqual(sorted(cl[1]), ["d", "e"])

    def test_parent_id_is_stable_and_order_independent(self):
        self.assertEqual(parents.parent_id_for(["p1", "p2"]), parents.parent_id_for(["p2", "p1"]))
        self.assertNotEqual(parents.parent_id_for(["p1", "p2"]), parents.parent_id_for(["p1", "p3"]))


class TestEndToEnd(unittest.TestCase):
    """compose -> cluster -> lookup over synthetic fixtures, detecting a duplicate."""

    def _write_person(self, raw_dir: Path, facts_dir: Path, pid: str, name: str, phone: str, email: str):
        common.write_json(raw_dir / f"{pid}.json", {
            "person_id": pid, "full_name": name, "emails": [email] if email else [],
            "phones": [phone] if phone else [], "source_channels": ["imessage"],
            "messages": [{"at": "2023-01-01", "channel": "imessage", "direction": "from_them", "subject": "", "text": "hi"}],
        })
        (facts_dir / f"{pid}.jsonl").write_text(json.dumps({
            "chunk_index": 0,
            "facts": {"canonical_name": name, "aliases": [], "employers": [{"name": "Acme", "role": "Eng", "status": "current"}],
                      "title": "Engineer", "school": "", "field_of_study": "", "location": "SF",
                      "relationship_to_owner": "friend", "topics": ["climbing"], "notable_events": [], "identifiers": [], "confidence": 0.8},
            "usage": {}, "error": "",
        }) + "\n", encoding="utf-8")

    def test_full_flow(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            raw, facts, dossiers = base / "raw", base / "facts", base / "dossiers"
            raw.mkdir(); facts.mkdir()
            index_json, index_md = base / "index.json", base / "index.md"
            merge_csv, merge_md = base / "merge.csv", base / "merge.md"

            # Two rows for the SAME person (shared phone, name variant) + one distinct.
            self._write_person(raw, facts, "p1", "Jonathan Smith", "+14155551234", "jon@acme.com")
            self._write_person(raw, facts, "p2", "Jon Smith", "+14155551234", "jon.smith@gmail.com")
            self._write_person(raw, facts, "p3", "Maria Garcia", "+13105550000", "maria@x.com")

            compose.run(_ns(raw_dir=raw, facts_dir=facts, dossier_dir=dossiers,
                            index_json=index_json, index_md=index_md, person=""))
            self.assertEqual(len(list(dossiers.glob("*.md"))), 3)

            # Lookup by phone returns BOTH duplicates; by name fuzzy works.
            idx = json.loads(index_json.read_text())
            slugs = lookup.find_slugs(idx, name="", phone="+1 415 555 1234", email="")
            self.assertEqual(len(slugs), 2)
            self.assertEqual(lookup.find_slugs(idx, name="Maria Garcia", phone="", email=""),
                             idx["by_name"]["maria garcia"])

            # Cluster detects the duplicate pair. --no-llm = deterministic (offline test);
            # the live pipeline uses the mandatory LLM tone-aware judge.
            manifest = cluster.run(_ns(dossier_dir=dossiers, index_json=index_json, raw_dir=raw, facts_dir=facts,
                                       out_csv=merge_csv, out_md=merge_md, confidence=0.7, no_llm=True,
                                       model="m", reasoning_effort="medium", concurrency=1, timeout=10, max_retries=0))
            self.assertEqual(manifest["judge"], "deterministic")
            self.assertGreaterEqual(manifest["candidate_pairs"], 1)
            self.assertEqual(manifest["clusters"], 1)

            # The injected section names the other person.
            p1_slug = idx["by_phone"]["4155551234"][0]
            text = (dossiers / f"{p1_slug}.md").read_text()
            self.assertIn("Possible same person", text)
            self.assertIn("confidence", text.split("Possible same person")[1])

            # Parent layer: the duplicate pair becomes one canonical parent that
            # links both children, and each child backrefs the parent.
            par_dir = base / "parents"
            # Always a complete canonical layer: 1 merged parent (p1/p2 dup) + 1 pointer
            # parent for the unique p3 (Maria). Every person resolves through parents/.
            pman = parents.run(_ns(merge_csv=merge_csv, index_json=index_json, dossier_dir=dossiers,
                                   facts_dir=facts, raw_dir=raw, parents_dir=par_dir, confirm_threshold=0.85))
            self.assertEqual(pman["merged_parents"], 1)
            self.assertEqual(pman["singleton_parents"], 1)  # Maria, unmerged -> pointer parent
            merged_md = [p.read_text() for p in par_dir.glob("*.md")
                         if "kind: parent\nsingleton" not in p.read_text() and "## Confirmed children" in p.read_text()]
            self.assertTrue(any("[[" + p1_slug + "]]" in t for t in merged_md))
            idx3 = json.loads(index_json.read_text())
            self.assertEqual(len(idx3["parents"]), 2)
            self.assertTrue(any(p.get("singleton") for p in idx3["parents"].values()))
            self.assertIn("Part of [[", (dossiers / f"{p1_slug}.md").read_text())
            # Parent is now resolvable by the shared phone.
            idx2 = json.loads(index_json.read_text())
            self.assertTrue(any(s.endswith(pman_slug := list(idx2["parents"])[0]) or s == pman_slug
                                for s in idx2["by_phone"]["4155551234"]))


def _verdict(verdict, conf, **kw):
    return {"verdict": verdict, "confidence": conf, "supporting_evidence": kw.get("sup", []),
            "contradicting_evidence": kw.get("con", []),
            "linkedin_plausibly_absent": kw.get("absent", False),
            "recommend_deep_research": kw.get("dr", False), "reason": kw.get("reason", "")}


class TestReconcileLinkedIn(unittest.TestCase):
    """Phase 3: verify each parent's attached LinkedIn (pairing, apply, queue, inject)."""

    def _facts(self, facts_dir, pid, name, employer="Acme", title="Engineer", location="SF"):
        (facts_dir / f"{pid}.jsonl").write_text(json.dumps({
            "chunk_index": 0, "facts": {"canonical_name": name, "aliases": [],
                "employers": [{"name": employer, "role": "Eng", "status": "current"}],
                "title": title, "school": "", "field_of_study": "", "location": location,
                "relationship_to_owner": "friend", "topics": ["climbing"], "notable_events": [],
                "identifiers": [], "shared_context": [], "confidence": 0.8}, "usage": {}}) + "\n", encoding="utf-8")

    def _people_csv(self, path, rows):
        cols = ["id", "public_identifier", "linkedin_url", "full_name", "headline",
                "work_experiences", "education", "current_title", "current_company",
                "city", "state", "country", "primary_email", "all_emails", "primary_phone", "all_phones"]
        with path.open("w", newline="", encoding="utf-8") as fh:
            import csv as _csv
            w = _csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in cols})

    def test_linkedin_view_falls_back_to_people_csv(self):
        row = {"public_identifier": "janedoe", "linkedin_url": "https://www.linkedin.com/in/janedoe",
               "full_name": "Jane Doe", "headline": "Eng at X",
               "work_experiences": json.dumps([{"title": "Eng", "company_name": "Stripe",
                                                 "starts_at": {"year": 2018}, "ends_at": {"year": 2022}}]),
               "education": json.dumps([{"school": "MIT", "degree": "BS", "field": "CS"}]),
               "city": "SF", "state": "CA", "country": "USA"}
        with tempfile.TemporaryDirectory() as d:
            view = reconcile.linkedin_view(row, Path(d))  # empty cache dir -> fallback
        self.assertEqual(view["source"], "people_csv")
        self.assertTrue(view["has_profile"])
        self.assertIn("Eng @ Stripe (2018–2022)", view["experiences"][0])
        self.assertIn("MIT", view["education"][0])

    def test_build_tasks_pairs_conflicts_and_no_link(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts, raw, cache = base / "facts", base / "raw", base / "cache"
            facts.mkdir(); raw.mkdir(); cache.mkdir()
            for pid, name in [("pa", "Alice"), ("pb1", "Bob"), ("pb2", "Bob"), ("pc", "Carol")]:
                self._facts(facts, pid, name)
            index = {"slugs": {"alice-c": {"person_id": "pa"}, "bob-c1": {"person_id": "pb1"},
                               "bob-c2": {"person_id": "pb2"}, "carol-c": {"person_id": "pc"}},
                     "parents": {
                         "alice-p": {"name": "Alice", "children": ["alice-c"]},
                         "bob-p": {"name": "Bob", "children": ["bob-c1", "bob-c2"]},   # conflict
                         "carol-p": {"name": "Carol", "children": ["carol-c"]}}}        # no link
            people = {
                "pa": {"id": "pa", "public_identifier": "alice", "linkedin_url": "https://www.linkedin.com/in/alice",
                       "headline": "Eng", "work_experiences": "[]", "education": "[]"},
                "pb1": {"id": "pb1", "public_identifier": "bobx", "linkedin_url": "https://www.linkedin.com/in/bobx",
                        "headline": "PM", "work_experiences": "[]", "education": "[]"},
                "pb2": {"id": "pb2", "public_identifier": "bobceo", "linkedin_url": "https://www.linkedin.com/in/bobceo",
                        "headline": "CEO", "work_experiences": "[]", "education": "[]"},
                "pc": {"id": "pc", "public_identifier": "", "linkedin_url": ""}}
            tasks = reconcile.build_tasks(index, people, facts, raw, cache)
            by_parent = {}
            for t in tasks:
                by_parent.setdefault(t["parent_slug"], []).append(t)
            self.assertEqual(len(by_parent["alice-p"]), 1)
            self.assertEqual(len(by_parent["bob-p"]), 2)             # two distinct linkedins
            self.assertTrue(all(t["conflict"] for t in by_parent["bob-p"]))
            self.assertTrue(by_parent["carol-p"][0]["no_link"])

    def test_candidate_child_uses_existing_link_without_a_second_lookup(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts, raw, cache = base / "facts", base / "raw", base / "cache"
            facts.mkdir(); raw.mkdir(); cache.mkdir()
            self._facts(facts, "person-cass", "Cass")
            self._facts(facts, "candidate:email:cass@x.com", "Cass")
            index = {
                "slugs": {
                    "cass-existing": {"person_id": "person-cass"},
                    "cass-candidate": {"person_id": "candidate:email:cass@x.com"},
                },
                "parents": {
                    "cass-parent": {
                        "name": "Cass",
                        "children": ["cass-existing", "cass-candidate"],
                    },
                },
            }
            people = {
                "person-cass": {
                    "id": "person-cass",
                    "public_identifier": "cass",
                    "linkedin_url": "https://www.linkedin.com/in/cass",
                    "headline": "Engineer",
                    "work_experiences": "[]",
                    "education": "[]",
                },
            }
            (task,) = reconcile.build_tasks(index, people, facts, raw, cache)
            self.assertEqual(task["person_ids"], ["person-cass"])
            self.assertEqual(
                task["parent_person_ids"],
                ["person-cass", "candidate:email:cass@x.com"],
            )

    def test_linkedin_connections_are_ground_truth(self):
        """A contact imported from your LinkedIn Connections (linkedin_csv) is auto-confirmed."""
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts, raw, cache = base / "facts", base / "raw", base / "cache"
            facts.mkdir(); raw.mkdir(); cache.mkdir()
            self._facts(facts, "pa", "Alice")
            self._facts(facts, "pb", "Bob")
            index = {"slugs": {"alice-c": {"person_id": "pa"}, "bob-c": {"person_id": "pb"}},
                     "parents": {"alice-p": {"name": "Alice", "children": ["alice-c"]},
                                 "bob-p": {"name": "Bob", "children": ["bob-c"]}}}
            people = {
                "pa": {"id": "pa", "public_identifier": "alice", "linkedin_url": "https://www.linkedin.com/in/alice",
                       "headline": "Eng", "work_experiences": "[]", "education": "[]",
                       "source_channels": "gmail_msgvault,linkedin_csv"},   # a connection
                "pb": {"id": "pb", "public_identifier": "bobx", "linkedin_url": "https://www.linkedin.com/in/bobx",
                       "headline": "PM", "work_experiences": "[]", "education": "[]",
                       "source_channels": "imessage"}}                       # not a connection
            tasks = {t["parent_slug"]: t for t in reconcile.build_tasks(index, people, facts, raw, cache)}
            self.assertTrue(tasks["alice-p"]["from_connections"])
            self.assertFalse(tasks["bob-p"]["from_connections"])
            v = reconcile.connection_verdict()
            self.assertEqual((v["verdict"], v["confidence"]), ("confirmed", 1.0))

    def _task(self, parent, pub, action_verdict, conf, **kw):
        return {"parent_slug": parent, "name": parent, "candidate_key": pub,
                "person_ids": [f"pid-{pub}"], "conflict": kw.get("conflict", False), "no_link": False,
                "linkedin": {"linkedin_url": f"https://www.linkedin.com/in/{pub}"},
                "match_emails": kw.get("emails", []), "match_phones": kw.get("phones", []),
                "verdict": _verdict(action_verdict, conf, reason=kw.get("reason", ""))}

    def test_write_overrides_emits_detach_and_verify(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ov.csv"
            tasks = [
                self._task("a", "alice", "confirmed", 0.95, emails=["a@x.com"]),
                self._task("b", "bobceo", "wrong_person", 0.92, emails=["bob@x.com"], reason="CEO != plumber"),
                self._task("c", "carol", "wrong_person", 0.50),  # below threshold -> pending in same file
            ]
            reconcile.decide_actions(tasks, 0.85)
            stats = reconcile.write_overrides(path, tasks)
            self.assertEqual(stats["verified"], 1)
            self.assertEqual(stats["detached"], 1)
            import csv as _csv
            with path.open() as fh:
                rows = {r["public_identifier"]: r for r in _csv.DictReader(fh)}
            self.assertEqual(rows["alice"]["action"], "verify")
            self.assertEqual(rows["alice"]["match_emails"], "a@x.com")
            self.assertEqual(rows["bobceo"]["action"], "detach")
            self.assertEqual(rows["alice"]["approved"], "auto")
            self.assertEqual(rows["carol"]["approved"], "")   # low-confidence -> PENDING in the same file

    def test_write_overrides_upsert_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ov.csv"
            tasks = [self._task("b", "bobceo", "wrong_person", 0.95)]
            reconcile.decide_actions(tasks, 0.85)
            reconcile.write_overrides(path, tasks)
            first = path.read_text()
            reconcile.write_overrides(path, tasks)  # same decision again
            import csv as _csv
            with path.open() as fh:
                rows = list(_csv.DictReader(fh))
            self.assertEqual(len(rows), 1)          # one row per public_identifier, no dupes
            # A pre-existing unrelated override row is preserved across re-runs.
            with path.open("a", newline="") as fh:
                w = _csv.DictWriter(fh, fieldnames=reconcile.OVERRIDE_COLUMNS)
                w.writerow({"public_identifier": "zzz", "action": "detach", "approved": "auto"})
            reconcile.write_overrides(path, tasks)
            with path.open() as fh:
                pubs = {r["public_identifier"] for r in _csv.DictReader(fh)}
            self.assertEqual(pubs, {"bobceo", "zzz"})

    def test_write_overrides_preserves_user_approved_rows(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ov.csv"
            # Seed a user decision: bobceo manually approved=no (don't detach).
            with path.open("w", newline="") as fh:
                w = __import__("csv").DictWriter(fh, fieldnames=reconcile.OVERRIDE_COLUMNS)
                w.writeheader()
                w.writerow({"public_identifier": "bobceo", "action": "detach", "approved": "no",
                            "reason": "user says keep"})
            tasks = [self._task("b", "bobceo", "wrong_person", 0.99)]  # judge again says detach
            reconcile.decide_actions(tasks, 0.85)
            stats = reconcile.write_overrides(path, tasks)
            self.assertEqual(stats["preserved_user_rows"], 1)
            import csv as _csv
            with path.open() as fh:
                row = next(_csv.DictReader(fh))
            self.assertEqual(row["approved"], "no")          # sticky: user decision NOT overwritten
            self.assertEqual(row["reason"], "user says keep")

    def test_upsert_retargets_proposes_pending_and_is_sticky(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ov.csv"
            r = reconcile.upsert_retargets(path, [{"old_public_identifier": "bobceo",
                "new_linkedin_url": "https://www.linkedin.com/in/bob-real", "reason": "found"}])
            self.assertEqual(r["proposed"], 1)
            import csv as _csv
            with path.open() as fh:
                row = next(_csv.DictReader(fh))
            self.assertEqual(row["action"], "retarget")
            self.assertEqual(row["approved"], "")            # pending by default
            self.assertEqual(row["new_public_identifier"], "bob-real")
            # User approves; a later proposal must NOT clobber it.
            rows = reconcile.load_override_rows(path); rows["bobceo"]["approved"] = "yes"
            reconcile._write_override_rows(path, rows)
            reconcile.upsert_retargets(path, [{"old_public_identifier": "bobceo",
                "new_linkedin_url": "https://www.linkedin.com/in/someone-else"}])
            with path.open() as fh:
                row = next(_csv.DictReader(fh))
            self.assertEqual(row["approved"], "yes")
            self.assertEqual(row["new_public_identifier"], "bob-real")  # preserved

    def test_conflict_auto_resolves_one_confirmed_rest_wrong(self):
        # One parent, two different attached links: one confirmed, one wrong -> auto-resolve
        # (keep the confirmed, detach the wrong) instead of deferring to review.
        tasks = [
            {"parent_slug": "sam", "name": "Sam", "person_ids": ["good"], "conflict": True,
             "no_link": False, "verdict": _verdict("confirmed", 0.92)},
            {"parent_slug": "sam", "name": "Sam", "person_ids": ["bad"], "conflict": True,
             "no_link": False, "verdict": _verdict("wrong_person", 0.98)}]
        reconcile.decide_actions(tasks, 0.85)
        by_pid = {t["person_ids"][0]: t for t in tasks}
        self.assertEqual(by_pid["good"]["action"], "confirm")
        self.assertEqual(by_pid["good"]["via"], "conflict_resolved")
        self.assertEqual(by_pid["bad"]["action"], "detach")
        self.assertEqual(by_pid["bad"]["via"], "conflict_resolved")

    def test_ambiguous_conflict_stays_in_review(self):
        # Two confirmed under one parent: not the clean shape -> all review, no mutation.
        tasks = [
            {"parent_slug": "x", "name": "X", "person_ids": ["p1"], "conflict": True,
             "no_link": False, "verdict": _verdict("confirmed", 0.9)},
            {"parent_slug": "x", "name": "X", "person_ids": ["p2"], "conflict": True,
             "no_link": False, "verdict": _verdict("confirmed", 0.9)}]
        reconcile.decide_actions(tasks, 0.85)
        self.assertTrue(all(t["action"] == "review" for t in tasks))

    def test_consolidation_folds_children_onto_kept_link(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            people = base / "people.csv"
            cols = ["id", "public_identifier", "linkedin_url", "primary_email", "all_emails",
                    "primary_phone", "all_phones", "interaction_counts", "source_channels"]
            with people.open("w", newline="") as fh:
                w = __import__("csv").DictWriter(fh, fieldnames=cols)
                w.writeheader()
                w.writerow({"id": "pid-keep", "public_identifier": "patlee",
                            "primary_email": "pat@gmail.com", "all_emails": '["pat@gmail.com"]',
                            "interaction_counts": '{"gmail": 5}', "source_channels": "gmail_msgvault"})
                w.writerow({"id": "pid-sib", "public_identifier": "pat-lee",
                            "primary_email": "pat@work.com", "all_emails": '["pat@work.com"]',
                            "interaction_counts": '{"imessage": 9}', "source_channels": "imessage"})
            tasks = [
                self._task("pat", "patlee", "confirmed", 0.95, conflict=True),
                self._task("pat", "pat-lee", "wrong_person", 0.95, conflict=True)]
            tasks[0]["person_ids"] = ["pid-keep"]
            tasks[1]["person_ids"] = ["pid-sib"]
            reconcile.decide_actions(tasks, 0.85)
            out = base / "consolidate.csv"
            stats = reconcile.write_consolidations(out, tasks, people)
            self.assertEqual(stats["consolidated_parents"], 1)
            import csv as _csv
            with out.open() as fh:
                row = next(_csv.DictReader(fh))
            self.assertEqual(row["public_identifier"], "patlee")     # folded onto the KEPT link
            self.assertIn("pat@gmail.com", row["all_emails"])
            self.assertIn("pat@work.com", row["all_emails"])          # sibling email carried
            self.assertEqual(json.loads(row["interaction_counts"]), {"gmail": 5, "imessage": 9})  # per-channel kept
            self.assertEqual(row["rapidapi_response"], "")              # contact-only (no profile pollution)

    def test_conflict_resolution_writes_one_verify_and_rest_detach(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ov.csv"
            tasks = [self._task("sam", "samroe-7a04927", "confirmed", 0.92, conflict=True),
                     self._task("sam", "samroe", "wrong_person", 0.98, conflict=True)]
            reconcile.decide_actions(tasks, 0.85)
            reconcile.write_overrides(path, tasks)
            import csv as _csv
            with path.open() as fh:
                rows = {r["public_identifier"]: r["action"] for r in _csv.DictReader(fh)}
            self.assertEqual(rows["samroe-7a04927"], "verify")
            self.assertEqual(rows["samroe"], "detach")

    def test_override_holds_auto_and_pending_in_one_file(self):
        # Everything judged lands in the ONE decisions table: high-conf -> auto, low-conf -> pending.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ov.csv"
            tasks = [
                self._task("a", "alice", "confirmed", 0.95),        # auto verify
                self._task("b", "bobceo", "wrong_person", 0.95),    # auto detach
                self._task("c", "carol", "wrong_person", 0.50),     # pending (low conf) -> detach
                self._task("e", "erin", "needs_review", 0.40)]      # pending -> verify (keep)
            reconcile.decide_actions(tasks, 0.85)
            stats = reconcile.write_overrides(path, tasks)
            self.assertEqual(stats["verified"], 1)
            self.assertEqual(stats["detached"], 1)
            self.assertEqual(stats["pending"], 2)
            import csv as _csv
            with path.open() as fh:
                rows = {r["public_identifier"]: r for r in _csv.DictReader(fh)}
            self.assertEqual(rows["alice"]["approved"], "auto")
            self.assertEqual(rows["carol"]["approved"], "")          # pending, in the SAME file
            self.assertEqual(rows["carol"]["action"], "detach")      # suggested action from verdict
            self.assertEqual(rows["erin"]["action"], "verify")       # needs_review -> keep, pending
            self.assertEqual(reconcile.count_pending(path), 2)

    def test_inject_section_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            md = Path(d) / "p.md"
            md.write_text("---\nname: X\n---\n\n# X (canonical)\n\nbody\n", encoding="utf-8")
            sec = reconcile.render_section(_verdict("confirmed", 0.9, reason="lines up"),
                                           {"linkedin_url": "u", "headline": "Eng"})
            reconcile.inject_section(md, sec)
            reconcile.inject_section(md, sec)  # second run must REPLACE, not duplicate
            self.assertEqual(md.read_text().count(reconcile.SECTION_ANCHOR), 1)
            self.assertIn("✅ confirmed", md.read_text())

    def test_run_no_llm_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts, raw, cache, pdir, rdir = (base / "facts", base / "raw", base / "cache",
                                             base / "parents", base / "reconcile")
            for p in (facts, raw, cache, pdir, rdir):
                p.mkdir()
            self._facts(facts, "pa", "Alice")
            self._facts(facts, "pc", "Carol")
            (pdir / "alice-p.md").write_text("---\nname: Alice\n---\n\n# Alice (canonical)\n\nbody\n", encoding="utf-8")
            (pdir / "carol-p.md").write_text("---\nname: Carol\n---\n\n# Carol (canonical)\n\nbody\n", encoding="utf-8")
            index_json = base / "index.json"
            index = {"slugs": {"alice-c": {"person_id": "pa"}, "carol-c": {"person_id": "pc"}},
                     "parents": {"alice-p": {"name": "Alice", "children": ["alice-c"]},
                                 "carol-p": {"name": "Carol", "children": ["carol-c"]}}}
            index_json.write_text(json.dumps(index), encoding="utf-8")
            people_csv = base / "people.csv"
            self._people_csv(people_csv, [
                {"id": "pa", "public_identifier": "alice", "linkedin_url": "https://www.linkedin.com/in/alice",
                 "headline": "Eng", "work_experiences": json.dumps([{"title": "Eng", "company_name": "Acme"}])},
                {"id": "pc", "public_identifier": "", "linkedin_url": ""}])  # Carol has no link
            man = reconcile.run(_ns(
                index_json=index_json, people_csv=people_csv, profile_cache_dir=cache,
                facts_dir=facts, raw_dir=raw, parents_dir=pdir,
                verdicts_jsonl=rdir / "verdicts.jsonl", verdicts_csv=rdir / "verdicts.csv",
                overrides_csv=rdir / "review.csv",
                consolidate_people_csv=rdir / "consolidate-people.csv",
                confirm_threshold=0.85, model="m", reasoning_effort="high", concurrency=1,
                timeout=10, max_retries=0, dry_run=False, no_overrides=False, no_llm=True))
            self.assertEqual(man["judge"], "deterministic")
            self.assertEqual(man["no_link"], 1)                      # Carol
            self.assertEqual(man["verdicts"]["confirmed"], 1)        # Alice (offline stub)
            self.assertEqual(man["overrides"]["verified"], 1)        # Alice -> verify in the override
            self.assertTrue((rdir / "verdicts.csv").exists())
            self.assertTrue((rdir / "applied.csv").exists())
            # people.csv is NOT mutated by reconcile anymore (the merge applies the override).
            with people_csv.open() as fh:
                self.assertNotIn("linkedin_verified", next(__import__("csv").reader(fh)))
            self.assertIn("LinkedIn identity", (pdir / "alice-p.md").read_text())

    def test_dry_run_estimates_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts, cache = base / "facts", base / "cache"
            facts.mkdir(); cache.mkdir()
            self._facts(facts, "pa", "Alice")
            index_json = base / "index.json"
            index_json.write_text(json.dumps({"slugs": {"alice-c": {"person_id": "pa"}},
                "parents": {"alice-p": {"name": "Alice", "children": ["alice-c"]}}}), encoding="utf-8")
            people_csv = base / "people.csv"
            self._people_csv(people_csv, [{"id": "pa", "public_identifier": "alice",
                "linkedin_url": "https://www.linkedin.com/in/alice", "headline": "Eng",
                "work_experiences": json.dumps([{"title": "Eng", "company_name": "Acme"}])}])
            man = reconcile.run(_ns(index_json=index_json, people_csv=people_csv, profile_cache_dir=cache,
                facts_dir=facts, raw_dir=base / "raw", parents_dir=base / "parents",
                verdicts_jsonl=base / "r" / "v.jsonl", verdicts_csv=base / "r" / "v.csv",
                overrides_csv=base / "r" / "ov.csv",
                consolidate_people_csv=base / "r" / "consolidate.csv",
                confirm_threshold=0.85, model="m", reasoning_effort="high", concurrency=1,
                timeout=10, max_retries=0, dry_run=True, no_overrides=True, no_llm=True))
            self.assertEqual(man["status"], "dry_run")
            self.assertEqual(man["judgeable"], 1)
            self.assertFalse((base / "r").exists())  # dry-run writes nothing


class TestApplyRetargets(unittest.TestCase):
    """Re-attach a correct LinkedIn: enrich (stubbed) + carry the contact's identity."""

    def test_builds_enriched_row_carrying_contact(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            ov = base / "ov.csv"
            with ov.open("w", newline="") as fh:
                w = __import__("csv").DictWriter(fh, fieldnames=reconcile.OVERRIDE_COLUMNS)
                w.writeheader()
                w.writerow({"public_identifier": "bobceo", "action": "retarget", "approved": "yes",
                            "new_linkedin_url": "https://www.linkedin.com/in/bob-real",
                            "new_public_identifier": "bob-real", "person_id": "pid-bob"})
                w.writerow({"public_identifier": "carol", "action": "retarget", "approved": "",  # pending -> skip
                            "new_linkedin_url": "https://www.linkedin.com/in/carol-real"})
            people = base / "people.csv"
            cols = ["id", "public_identifier", "linkedin_url", "full_name", "primary_email",
                    "all_emails", "primary_phone", "all_phones", "interaction_counts",
                    "last_interaction", "source_channels"]
            with people.open("w", newline="") as fh:
                w = __import__("csv").DictWriter(fh, fieldnames=cols)
                w.writeheader()
                w.writerow({"id": "pid-bob", "public_identifier": "bobceo", "full_name": "Bob",
                            "primary_email": "bob@x.com", "interaction_counts": '{"gmail": 9}',
                            "source_channels": "gmail_msgvault"})
            fake = {"data": {"raw": 1}, "normalized_profile": {"success": True}, "from_cache": True, "error": ""}
            with mock.patch.object(retargets, "rapidapi_profile", return_value=fake), \
                 mock.patch.object(retargets, "normalize_rapidapi", return_value={}), \
                 mock.patch.object(retargets, "merge_provider_profile",
                                   return_value={"public_identifier": "bob-real", "full_name": "Bob Right",
                                                 "rapidapi_response": '{"raw":1}'}):
                man = retargets.run(_ns(overrides_csv=ov, people_csv=people,
                    profile_cache_dir=base / "cache", out_csv=base / "retarget-people.csv"))
            self.assertEqual(man["enriched"], 1)        # only the approved one
            self.assertEqual(man["cache_hits"], 1)
            import csv as _csv
            with (base / "retarget-people.csv").open() as fh:
                rows = list(_csv.DictReader(fh))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["public_identifier"], "bob-real")
            self.assertEqual(rows[0]["primary_email"], "bob@x.com")     # contact identity carried
            self.assertEqual(rows[0]["interaction_counts"], '{"gmail": 9}')


class TestReconcileDeepResearch(unittest.TestCase):
    """Phase 3 escalation: subset selection + explicit cost gate (no spend)."""

    def test_eligible_subset_filters(self):
        verdicts = [
            {"parent_slug": "a", "verdict": _verdict("wrong_person", 0.95, dr=True)},                 # eligible
            {"parent_slug": "b", "verdict": _verdict("wrong_person", 0.95, dr=True, absent=True)},    # excluded: no LinkedIn
            {"parent_slug": "c", "verdict": _verdict("wrong_person", 0.5, dr=True)},                  # excluded: low conf
            {"parent_slug": "d", "verdict": _verdict("wrong_person", 0.95, dr=False)},               # excluded: not recommended
            {"parent_slug": "e", "verdict": _verdict("confirmed", 0.99, dr=True)}]                    # excluded: not wrong
        self.assertEqual(len(dresearch.eligible_subset(verdicts, 0.85)), 1)

    def test_eligible_subset_skips_detaches_whose_parent_kept_a_link(self):
        # Conflict-resolved: parent "x" kept a confirmed LinkedIn AND detached a sibling.
        # The detached sibling is the same person -> no need to research it.
        verdicts = [
            {"parent_slug": "x", "verdict": _verdict("confirmed", 0.92)},                 # kept link
            {"parent_slug": "x", "verdict": _verdict("wrong_person", 0.95, dr=True)},     # sibling -> SKIP
            {"parent_slug": "y", "verdict": _verdict("wrong_person", 0.95, dr=True)}]     # parent has no kept link -> research
        elig = dresearch.eligible_subset(verdicts, 0.85)
        self.assertEqual(len(elig), 1)
        self.assertEqual(elig[0]["parent_slug"], "y")

    def test_eligible_subset_skips_user_excluded(self):
        # An X-ed-out person must never be deep-researched / re-attached, even though the
        # model recommends it (unlike a detach, which IS eligible for recovery).
        verdicts = [{"parent_slug": "z", "candidate_key": "zpub",
                     "verdict": _verdict("wrong_person", 0.95, dr=True)}]
        self.assertEqual(len(dresearch.eligible_subset(verdicts, 0.85)), 1)         # baseline: eligible
        ov = {"zpub": {"action": "exclude", "approved": "yes"}}
        self.assertEqual(dresearch.eligible_subset(verdicts, 0.85, ov), [])         # excluded: skipped

    def test_cost_gate_blocks_over_budget(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            vj = base / "verdicts.jsonl"
            recs = [{"parent_slug": f"p{i}", "name": f"N{i}", "person_ids": [f"x{i}"],
                     "linkedin": {"linkedin_url": "u"},
                     "verdict": _verdict("wrong_person", 0.95, dr=True, reason="wrong")} for i in range(600)]
            vj.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
            old_out, old_queue = dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV
            dresearch.DR_OUT_DIR = base / "research"
            dresearch.QUEUE_CSV = dresearch.DR_OUT_DIR / "research_queue.csv"
            try:
                man = dresearch.run(_ns(
                    verdicts_jsonl=vj, people_csv=base / "nope.csv",
                    overrides_csv=base / "nope_ov.csv",
                    facts_dir=base / "f", raw_dir=base / "r", processor="core2x",
                    confirm_threshold=0.85, budget=25.0, approve=True, dry_run=False,
                ))
            finally:
                dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV = old_out, old_queue
            self.assertEqual(man["status"], "needs_approval")   # 600 * $0.05 = $30 > $25
            self.assertGreater(man["estimated_usd"], 25)

    def test_cost_gate_requires_approval_under_budget(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            vj = base / "verdicts.jsonl"
            vj.write_text(json.dumps({
                "parent_slug": "p1", "candidate_key": "wrong", "name": "N1",
                "person_ids": ["x1"], "linkedin": {"linkedin_url": "u"},
                "verdict": _verdict("wrong_person", 0.95, dr=True, reason="wrong"),
            }) + "\n", encoding="utf-8")
            old_out, old_queue = dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV
            dresearch.DR_OUT_DIR = base / "research"
            dresearch.QUEUE_CSV = dresearch.DR_OUT_DIR / "research_queue.csv"
            try:
                manifest = dresearch.run(_ns(
                    verdicts_jsonl=vj, people_csv=base / "missing.csv",
                    overrides_csv=base / "overrides.csv", facts_dir=base / "facts",
                    raw_dir=base / "raw", processor="core2x", confirm_threshold=0.85,
                    budget=25.0, approve=False, dry_run=False,
                    include_plausibly_absent=False,
                ))
            finally:
                dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV = old_out, old_queue
            self.assertEqual(manifest["status"], "needs_approval")
            self.assertLess(manifest["estimated_usd"], 25)

    def test_dry_run_prices_only_net_new_handles(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            vj = base / "verdicts.jsonl"
            recs = [
                {"parent_slug": "pending", "candidate_key": "wrong-a", "name": "Pending A",
                 "person_ids": ["x1"], "linkedin": {"linkedin_url": "u1"},
                 "verdict": _verdict("wrong_person", 0.95, dr=True, reason="wrong")},
                {"parent_slug": "pending", "candidate_key": "wrong-b", "name": "Pending B",
                 "person_ids": ["x2"], "linkedin": {"linkedin_url": "u2"},
                 "verdict": _verdict("wrong_person", 0.95, dr=True, reason="wrong")},
                {"parent_slug": "complete", "candidate_key": "wrong-c", "name": "Complete",
                 "person_ids": ["x3"], "linkedin": {"linkedin_url": "u3"},
                 "verdict": _verdict("wrong_person", 0.95, dr=True, reason="wrong")},
            ]
            vj.write_text("\n".join(json.dumps(row) for row in recs) + "\n", encoding="utf-8")
            old_out, old_queue = dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV
            dresearch.DR_OUT_DIR = base / "research"
            dresearch.QUEUE_CSV = dresearch.DR_OUT_DIR / "research_queue.csv"
            completed = dresearch.DR_OUT_DIR / "complete" / "01_research_parallel.json"
            completed.parent.mkdir(parents=True)
            completed.write_text("{}\n", encoding="utf-8")
            try:
                manifest = dresearch.run(_ns(
                    verdicts_jsonl=vj, people_csv=base / "missing.csv",
                    overrides_csv=base / "overrides.csv", facts_dir=base / "facts",
                    raw_dir=base / "raw", processor="core2x", confirm_threshold=0.85,
                    budget=0.0, approve=False, dry_run=True,
                    include_plausibly_absent=False, include_candidates=False,
                ))
            finally:
                dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV = old_out, old_queue
            self.assertEqual(manifest["eligible"], 3)
            self.assertEqual(manifest["would_submit"], 1)
            self.assertEqual(manifest["reused_completed"], 1)
            self.assertEqual(manifest["duplicate_handles"], 1)
            self.assertEqual(manifest["estimated_usd"], 0.05)

    def test_cost_gate_runs_only_when_approved_under_budget(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            vj = base / "verdicts.jsonl"
            vj.write_text(json.dumps({
                "parent_slug": "p1", "candidate_key": "wrong", "name": "N1",
                "person_ids": ["x1"], "linkedin": {"linkedin_url": "u"},
                "verdict": _verdict("wrong_person", 0.95, dr=True, reason="wrong"),
            }) + "\n", encoding="utf-8")
            old_out, old_queue = dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV
            dresearch.DR_OUT_DIR = base / "research"
            dresearch.QUEUE_CSV = dresearch.DR_OUT_DIR / "research_queue.csv"
            try:
                with mock.patch.object(
                    dresearch.subprocess,
                    "run",
                    return_value=mock.Mock(returncode=0),
                ) as run_mock:
                    manifest = dresearch.run(_ns(
                        verdicts_jsonl=vj, people_csv=base / "missing.csv",
                        overrides_csv=base / "overrides.csv", facts_dir=base / "facts",
                        raw_dir=base / "raw", processor="core2x", confirm_threshold=0.85,
                        budget=1.0, approve=True, dry_run=False,
                        include_plausibly_absent=False,
                    ))
            finally:
                dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV = old_out, old_queue
            self.assertEqual(manifest["status"], "ran")
            run_mock.assert_called_once()

    def test_invalid_budget_cannot_bypass_gate(self):
        manifest = dresearch.run(_ns(budget=float("nan")))
        self.assertEqual(manifest["status"], "invalid_budget")
        with self.assertRaises(SystemExit):
            dresearch.build_parser().parse_args(["--budget", "nan"])

    def test_retarget_reads_canonical_parallel_linkedin_shape(self):
        profile = {
            "social": {"linkedin_url": "https://www.linkedin.com/in/right-person"},
            "metadata": {"research_notes": "Matched employer and location"},
        }
        self.assertEqual(
            dresearch._find_linkedin(profile),
            "https://www.linkedin.com/in/right-person",
        )
        self.assertIn("Matched employer", dresearch._find_reason(profile))


class TestReviewWeb(unittest.TestCase):
    """The parent-grouped review UI: join verdicts.jsonl + review.csv, and decision writes."""

    def _fixture(self, d: Path) -> tuple[Path, Path]:
        verdicts = d / "verdicts.jsonl"
        review = d / "review.csv"
        recs = [
            {"parent_slug": "jane-doe-p1", "name": "Jane Doe", "candidate_key": "janedoe",
             "person_ids": ["pid-1"], "conflict": False, "no_link": False,
             "linkedin": {"public_identifier": "janedoe", "linkedin_url": "https://www.linkedin.com/in/janedoe",
                          "full_name": "Jane Doe", "headline": "VP at Acme", "experiences": ["VP @ Acme"],
                          "education": ["MIT"], "location": "SF", "has_profile": True},
             "match_emails": ["jane@acme.com"], "match_phones": [],
             "verdict": {"verdict": "needs_review", "confidence": 0.55, "supporting_evidence": ["same company"],
                         "contradicting_evidence": [], "reason": "plausible but unconfirmed",
                         "linkedin_plausibly_absent": False, "recommend_deep_research": False}, "error": ""},
            {"parent_slug": "pat-lee-p2", "name": "Pat Lee", "candidate_key": "patlee",
             "person_ids": ["pid-2"], "conflict": False, "no_link": False,
             "linkedin": {"public_identifier": "patlee", "linkedin_url": "https://www.linkedin.com/in/patlee",
                          "full_name": "Pat Lee", "headline": "Driver", "experiences": [], "education": [],
                          "location": "", "has_profile": True},
             "match_emails": ["pat@globex.com"], "match_phones": [],
             "verdict": {"verdict": "confirmed", "confidence": 0.95, "supporting_evidence": ["exact match"],
                         "contradicting_evidence": [], "reason": "strong", "linkedin_plausibly_absent": False,
                         "recommend_deep_research": False}, "error": ""},
        ]
        verdicts.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
        # review.csv as reconcile would write it: jane pending, pat verified/auto
        reconcile._write_override_rows(review, {
            "janedoe": {"public_identifier": "janedoe", "action": "verify", "approved": "",
                        "linkedin_url": "https://www.linkedin.com/in/janedoe", "confidence": "0.550"},
            "patlee": {"public_identifier": "patlee", "action": "verify", "approved": "auto",
                       "linkedin_url": "https://www.linkedin.com/in/patlee", "confidence": "0.950"},
        })
        return verdicts, review

    def test_build_parents_joins_and_states(self):
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            verdicts, review = self._fixture(d)
            ps, _ = web.build_parents(verdicts, review)
            by = {p["name"]: p for p in ps}
            self.assertEqual(set(by), {"Jane Doe", "Pat Lee"})
            self.assertEqual(web.parent_status(by["Jane Doe"]), "review")
            self.assertEqual(web.parent_status(by["Pat Lee"]), "verified")
            self.assertEqual(web.picked_link(by["Pat Lee"]), "https://www.linkedin.com/in/patlee")
            # reasoning + profile carried through for display
            cand = by["Jane Doe"]["candidates"][0]
            self.assertEqual(cand["headline"], "VP at Acme")
            self.assertEqual(cand["supporting"], ["same company"])

    def test_decisions_keep_detach_fix_reset(self):
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            verdicts, review = self._fixture(d)
            TH = reconcile.DEFAULT_CONFIRM

            r = web.apply_decision(review, verdicts, "janedoe", "keep", "", TH)
            self.assertEqual((r["action"], r["approved"]), ("verify", "yes"))

            r = web.apply_decision(review, verdicts, "janedoe", "detach", "", TH)
            self.assertEqual((r["action"], r["approved"]), ("detach", "yes"))

            r = web.apply_decision(review, verdicts, "janedoe", "fix",
                                   "linkedin.com/in/jane-real", TH)
            self.assertEqual(r["action"], "retarget")
            self.assertEqual(r["new_url"], "https://www.linkedin.com/in/jane-real")
            rows = reconcile.load_override_rows(review)
            self.assertEqual(rows["janedoe"]["new_public_identifier"], "jane-real")

            # reset a high-confidence confirmed -> restores auto/verify (re-applies at merge)
            web.apply_decision(review, verdicts, "patlee", "detach", "", TH)
            r = web.apply_decision(review, verdicts, "patlee", "reset", "", TH)
            self.assertEqual((r["action"], r["approved"]), ("verify", "auto"))

            # no duplicate rows introduced (still exactly the two pubs)
            self.assertEqual(set(reconcile.load_override_rows(review)), {"janedoe", "patlee"})

    def test_fix_requires_url(self):
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            verdicts, review = self._fixture(d)
            with self.assertRaises(ValueError):
                web.apply_decision(review, verdicts, "janedoe", "fix", "", reconcile.DEFAULT_CONFIRM)

    def test_exclude_marks_person_excluded(self):
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            verdicts, review = self._fixture(d)
            r = web.apply_decision(review, verdicts, "janedoe", "exclude", "", reconcile.DEFAULT_CONFIRM)
            self.assertEqual((r["action"], r["approved"]), ("exclude", "yes"))
            parents, _ = web.build_parents(verdicts, review)
            jane = next(p for p in parents if p["name"] == "Jane Doe")
            self.assertEqual(web.candidate_state(jane["candidates"][0]), "excluded")
            self.assertEqual(web.parent_status(jane), "excluded")

    def _merged_fixture(self, d: Path) -> tuple[Path, Path]:
        """One Merged person: a confirmed keeper, a high-confidence wrong namesake, and a
        still-needs-review third link, all on the same parent."""
        def rec(key, verdict, conf):
            return {"parent_slug": "sam-jones-p1", "name": "Sam Jones", "candidate_key": key,
                    "person_ids": ["pid-1", "pid-2"], "conflict": True, "no_link": False,
                    "linkedin": {"public_identifier": key, "linkedin_url": f"https://www.linkedin.com/in/{key}",
                                 "full_name": "Sam Jones", "headline": "", "experiences": [], "education": [],
                                 "location": "", "has_profile": True},
                    "match_emails": [], "match_phones": [],
                    "verdict": {"verdict": verdict, "confidence": conf, "supporting_evidence": [],
                                "contradicting_evidence": [], "reason": "r", "linkedin_plausibly_absent": False,
                                "recommend_deep_research": False}, "error": ""}
        verdicts = d / "verdicts.jsonl"
        review = d / "review.csv"
        recs = [rec("samwrong", "wrong_person", 0.93), rec("samreal", "confirmed", 0.9),
                rec("sammaybe", "needs_review", 0.4)]
        verdicts.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
        review.write_text("", encoding="utf-8")  # all pending
        return verdicts, review

    def test_staged_identity_queue_floats_best_candidate_and_has_binary_actions(self):
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            verdicts, review = self._merged_fixture(d)
            parents, _ = web.build_parents(verdicts, review)
            sam = next(p for p in parents if p["name"] == "Sam Jones")
            self.assertEqual(len(sam["candidates"]), 3)
            pending = web.pending_linkedin_candidates(sam)
            self.assertEqual([cand["pub"] for cand in pending], ["samreal", "sammaybe", "samwrong"])
            html = web.render_linkedin_card(sam, pending[0], d, d)
            self.assertIn("Is this the right LinkedIn?", html)
            self.assertIn("data-decide='keep'", html)
            self.assertIn("data-open-fix", html)
            self.assertNotIn("data-decide='detach'", html)
            self.assertIn("Use a different LinkedIn", html)
            self.assertNotIn("Exclude", html)
            self.assertNotIn("Maybe", html)


class TestSelfReportedRetarget(unittest.TestCase):
    """Recover the correct LinkedIn when the contact shared it themselves in their messages."""

    def _task(self, name, attached_pub, self_url):
        return {"no_link": False, "name": name, "candidate_key": attached_pub, "person_ids": ["pid-1"],
                "match_emails": ["a@fb.com"], "match_phones": [],
                "linkedin": {"linkedin_url": f"https://www.linkedin.com/in/{attached_pub}"},
                "dossier": {"self_linkedin_url": self_url,
                            "self_linkedin_pub": reconcile.extract_public_identifier(self_url).lower()}}

    def test_retarget_when_self_reported_differs_and_name_matches(self):
        # attached link is the WRONG namesake; the dossier has the URL they shared themselves
        props = reconcile.self_reported_retargets([self._task(
            "Ankita Goyal", "ankita-goyal-9aa66453", "https://www.linkedin.com/in/ankita-goyal")])
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["old_public_identifier"], "ankita-goyal-9aa66453")
        self.assertEqual(props[0]["new_public_identifier"], "ankita-goyal")
        self.assertEqual(props[0]["approved"], "auto")   # name-compatible -> auto-recover

    def test_pending_when_shared_url_is_a_third_party(self):
        # the shared URL's name doesn't match the contact -> likely someone they mentioned -> pending
        props = reconcile.self_reported_retargets([self._task(
            "Ben Taft", "ben-taft-46830679", "https://www.linkedin.com/in/brandonmoak")])
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["approved"], "")       # not auto — needs the user's yes

    def test_no_retarget_when_self_reported_matches(self):
        props = reconcile.self_reported_retargets([self._task(
            "Ankita Goyal", "ankita-goyal", "https://www.linkedin.com/in/ankita-goyal")])
        self.assertEqual(props, [])

    def test_no_retarget_without_self_reported(self):
        t = {"no_link": False, "name": "X", "candidate_key": "x", "person_ids": ["p"], "dossier": {}}
        self.assertEqual(reconcile.self_reported_retargets([t]), [])


class TestDeepResearchEligibility(unittest.TestCase):
    """Deep research targets model detaches and never overwrites user decisions."""

    VERDICTS = [
        {"parent_slug": "p1", "candidate_key": "goodlink",
         "verdict": {"verdict": "confirmed", "confidence": 0.9}},
        {"parent_slug": "p2", "candidate_key": "wronglink",
         "verdict": {"verdict": "wrong_person", "confidence": 0.9, "recommend_deep_research": True}},
        {"parent_slug": "p3", "candidate_key": "absentlink",
         "verdict": {"verdict": "wrong_person", "confidence": 0.9, "recommend_deep_research": True,
                     "linkedin_plausibly_absent": True}},
    ]

    def keys(self, overrides):
        return {r["candidate_key"] for r in dresearch.eligible_subset(self.VERDICTS, 0.85, overrides)}

    def test_model_path_unchanged(self):
        # model wrong_person+recommend eligible; the plausibly-absent one excluded
        self.assertEqual(self.keys({}), {"wronglink"})

    def test_user_detach_is_not_researched(self):
        # The one-row override cannot hold a sticky detach and pending retarget together.
        self.assertEqual(self.keys({"goodlink": {"action": "detach", "approved": "yes"}}),
                         {"wronglink"})

    def test_user_decision_blocks_model_research(self):
        self.assertEqual(self.keys({"wronglink": {"action": "detach", "approved": "yes"}}), set())

    def test_pending_user_detach_not_eligible(self):
        # a detach the user hasn't approved (still pending) does NOT trigger research
        self.assertEqual(self.keys({"goodlink": {"action": "detach", "approved": ""}}), {"wronglink"})

    def test_existing_retarget_skipped(self):
        # already has a correct link -> don't research it
        self.assertEqual(self.keys({"wronglink": {"action": "retarget", "approved": "yes"}}), set())


class TestOwnerExclusion(unittest.TestCase):
    """The mailbox owner on another email (is_owner) is excluded from the parent layer."""

    def test_is_owner_reads_the_flag(self):
        with tempfile.TemporaryDirectory() as d:
            facts = Path(d)
            (facts / "owner-pid.jsonl").write_text(
                json.dumps({"facts": {"canonical_name": "Arthur Chen", "is_owner": True}}) + "\n", encoding="utf-8")
            (facts / "contact-pid.jsonl").write_text(
                json.dumps({"facts": {"canonical_name": "Arthur Lam", "is_owner": False}}) + "\n", encoding="utf-8")
            self.assertTrue(parents._is_owner("owner-pid", facts))
            self.assertFalse(parents._is_owner("contact-pid", facts))
            self.assertFalse(parents._is_owner("missing-pid", facts))


class _ns:
    """Lightweight argparse.Namespace stand-in for run() calls."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class TestWhatsAppUSJid(unittest.TestCase):
    """read_whatsapp must match US numbers whose stored JID keeps the +1 country
    code, even though phone_digits() strips it for comparison."""

    def _wacli(self, dirpath: Path) -> Path:
        import sqlite3
        db = dirpath / "wacli.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE messages (chat_jid TEXT, text TEXT, ts INTEGER, from_me INTEGER)")
        con.executemany(
            "INSERT INTO messages (chat_jid, text, ts, from_me) VALUES (?,?,?,?)",
            [
                ("14155551234@s.whatsapp.net", "us dm", 1700000000, 0),   # US, country code kept
                ("447911123456@s.whatsapp.net", "uk dm", 1700000100, 0),  # non-US, no stripping
                ("123456@g.us", "group", 1700000200, 0),                  # group — must be excluded
            ],
        )
        con.commit()
        con.close()
        return db

    def test_us_number_with_country_code_jid_is_found(self):
        with tempfile.TemporaryDirectory() as d:
            db = self._wacli(Path(d))
            person = common.Person(person_id="p1", full_name="US Person", phones=["+14155551234"])
            rows = sources.read_whatsapp(person, db)
            self.assertEqual([r["text"] for r in rows], ["us dm"])

    def test_non_us_number_still_matches_and_groups_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            db = self._wacli(Path(d))
            person = common.Person(person_id="p2", full_name="UK Person", phones=["+447911123456"])
            rows = sources.read_whatsapp(person, db)
            self.assertEqual([r["text"] for r in rows], ["uk dm"])

    def test_us_number_stored_without_country_code_also_matches(self):
        # The other arm of the both-forms fix: a store that kept the bare 10-digit
        # JID must still match a +1 contact.
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "wacli.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE messages (chat_jid TEXT, text TEXT, ts INTEGER, from_me INTEGER)")
            con.execute("INSERT INTO messages VALUES ('4155551234@s.whatsapp.net', 'bare dm', 1700000000, 0)")
            con.commit()
            con.close()
            person = common.Person(person_id="p3", full_name="US Person", phones=["+14155551234"])
            rows = sources.read_whatsapp(person, db)
            self.assertEqual([r["text"] for r in rows], ["bare dm"])

    def test_direction_is_mapped_from_from_me(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "wacli.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE messages (chat_jid TEXT, text TEXT, ts INTEGER, from_me INTEGER)")
            con.executemany(
                "INSERT INTO messages VALUES (?,?,?,?)",
                [("14155551234@s.whatsapp.net", "mine", 1700000200, 1),
                 ("14155551234@s.whatsapp.net", "theirs", 1700000100, 0)],
            )
            con.commit()
            con.close()
            person = common.Person(person_id="p4", full_name="US Person", phones=["+14155551234"])
            rows = sources.read_whatsapp(person, db)
            by_text = {r["text"]: r["direction"] for r in rows}
            self.assertEqual(by_text, {"mine": "from_me", "theirs": "from_them"})


class TestSpamRejectColumns(unittest.TestCase):
    """The machine-owned llm_reject* columns: always refreshed, never a decision."""

    def _task(self, pub: str, spam: bool, conf: float = 0.9) -> dict:
        return {"candidate_key": pub, "action": "confirm", "person_ids": [f"pid-{pub}"],
                "linkedin": {"linkedin_url": f"https://linkedin.com/in/{pub}"},
                "match_emails": [], "match_phones": [],
                "verdict": {"verdict": "confirmed", "confidence": 0.9, "reason": "r",
                            "spam_contact": spam, "spam_confidence": conf, "spam_reason": "cold outreach" if spam else ""}}

    def test_sticky_user_row_gets_llm_columns_without_touching_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "review.csv"
            # user already verified this pub (sticky)
            reconcile._write_override_rows(path, {"spammy": {
                **{k: "" for k in reconcile.OVERRIDE_COLUMNS},
                "public_identifier": "spammy", "action": "verify", "approved": "yes"}})
            reconcile.write_overrides(path, [self._task("spammy", spam=True)])
            row = reconcile.load_override_rows(path)["spammy"]
            self.assertEqual(row["action"], "verify")
            self.assertEqual(row["approved"], "yes")  # decision untouched
            self.assertEqual(row["llm_reject"], "spam")  # machine column refreshed
            self.assertEqual(row["llm_reject_reason"], "cold outreach")
            # a later re-review that clears the flag also propagates
            reconcile.write_overrides(path, [self._task("spammy", spam=False)])
            self.assertEqual(reconcile.load_override_rows(path)["spammy"]["llm_reject"], "")

    def test_backwards_compatible_with_old_csv_without_llm_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "review.csv"
            old_cols = reconcile.OVERRIDE_COLUMNS[:13]  # pre-spam schema
            path.write_text(",".join(old_cols) + "\nold-pub,verify,yes,,,,,,0.9,r,pid-1,src,t\n", encoding="utf-8")
            rows = reconcile.load_override_rows(path)
            self.assertEqual((rows["old-pub"].get("llm_reject") or ""), "")
            reconcile._write_override_rows(path, rows)  # round-trips onto the new schema
            self.assertIn("llm_reject", path.read_text().splitlines()[0])


class TestSubsetReviewMerge(unittest.TestCase):
    def test_subset_run_overlays_instead_of_clobbering(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            verdicts = Path(tmpdir) / "verdicts.jsonl"
            rows = [
                {"parent_slug": "alice", "candidate_key": "alice-1", "no_link": False,
                 "linkedin": {}, "verdict": {"verdict": "confirmed", "confidence": 0.9}, "error": ""},
                {"parent_slug": "bob", "candidate_key": "bob-1", "no_link": False,
                 "linkedin": {}, "verdict": {"verdict": "confirmed", "confidence": 0.8}, "error": ""},
            ]
            verdicts.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            fresh = [{"parent_slug": "bob", "candidate_key": "bob-1", "no_link": False,
                      "linkedin": {}, "verdict": {"verdict": "wrong_person", "confidence": 0.95,
                                                  "spam_contact": True, "spam_confidence": 0.9,
                                                  "spam_reason": "cold outreach"}, "error": ""}]
            merged = reconcile.merge_subset_tasks(verdicts, fresh)
            by_key = {(t["parent_slug"], t["candidate_key"]): t for t in merged}
            self.assertEqual(len(merged), 2)  # alice preserved, bob overlaid
            self.assertEqual(by_key[("alice", "alice-1")]["verdict"]["verdict"], "confirmed")
            self.assertEqual(by_key[("bob", "bob-1")]["verdict"]["verdict"], "wrong_person")
            self.assertTrue(by_key[("bob", "bob-1")]["verdict"]["spam_contact"])


class TestAssembleSyntheticProfile(unittest.TestCase):
    def _profile(self, completeness=0.7, linkedin=None, positions=True):
        return {
            "person": {"full_name": "Ross Nordeen", "first_name": "Ross", "last_name": "Nordeen", "confidence": 0.9},
            "location": {"city": "San Francisco", "country": "United States", "raw": ""},
            "headline": {"text": "builder"},
            "summary": {"text": "career summary"},
            "positions": ([{"title": "CTO", "company_name": "StealthCo", "is_current": True},
                           {"title": "Eng", "company_name": "PriorCo", "is_current": False}] if positions else []),
            "education": [{"school_name": "MTU", "degree": "BS"}],
            "social": {"linkedin_url": linkedin, "twitter_handle": "rpoo"},
            "metadata": {"estimated_completeness": completeness, "gaps": ["education dates"],
                         "research_date": "2026-07-09", "research_method": "parallel-core2x",
                         "source_channel": "twitter"},
        }

    def test_synth_identifier_prefers_email_then_phone_then_handle(self) -> None:
        from packs.ingestion.primitives.deep_context import assemble_synthetic_profile as asp
        a = asp.synth_public_identifier("A@B.com", "+14155551234", "rpoo")
        b = asp.synth_public_identifier("a@b.com", "", "rpoo")
        self.assertEqual(a, b)  # email normalized, wins over phone
        self.assertTrue(asp.synth_public_identifier("", "+14155551234", "rpoo").startswith("synth-phone-"))
        self.assertEqual(asp.synth_public_identifier("", "", "Rpoo"), "synth-x-rpoo")

    def test_build_row_maps_research_to_people_schema(self) -> None:
        from packs.ingestion.primitives.deep_context import assemble_synthetic_profile as asp
        contact = {"handle": "rpoo", "primary_email": "ross@x.com", "source_channel": "twitter"}
        original = {"id": "pid-7", "all_emails": "ross@x.com|r@y.com", "interaction_counts": "{'email': 12}"}
        row = asp.build_synthetic_row(self._profile(), contact, original, "pid-7")
        self.assertTrue(row["public_identifier"].startswith("synth-email-"))
        self.assertEqual(row["enrichment_provider"], "synthetic")
        self.assertEqual(row["entity_urn"], "synthetic:pid-7")
        self.assertEqual(row["current_title"], "CTO")
        self.assertEqual(row["current_company"], "StealthCo")
        self.assertEqual(json.loads(row["work_experiences"])[1]["company_name"], "PriorCo")
        self.assertEqual(row["all_emails"], "ross@x.com|r@y.com")  # carry columns
        self.assertEqual(row["approved"], "auto")  # 0.7 >= 0.6
        self.assertIn("education dates", row["synthetic_metadata"])
        self.assertEqual(row["linkedin_url"], "")

    def test_low_completeness_waits_for_review(self) -> None:
        from packs.ingestion.primitives.deep_context import assemble_synthetic_profile as asp
        row = asp.build_synthetic_row(self._profile(completeness=0.3), {"handle": "rpoo"}, None, "")
        self.assertEqual(row["approved"], "")

    def test_usability_floor(self) -> None:
        from packs.ingestion.primitives.deep_context import assemble_synthetic_profile as asp
        self.assertTrue(asp.profile_is_usable(self._profile()))
        no_name = self._profile(); no_name["person"]["full_name"] = ""
        self.assertFalse(asp.profile_is_usable(no_name))
        bare = self._profile(positions=False); bare["location"] = {}
        self.assertFalse(asp.profile_is_usable(bare))


class TestSyntheticReviewUI(unittest.TestCase):
    CSV_HEADER = ("id,public_identifier,full_name,headline,summary,location_raw,work_experiences,"
                  "education,primary_email,primary_phone,enrichment_provider,approved,synthetic_metadata\n")

    def _csv_row(self, approved: str) -> str:
        work = json.dumps([{"title": "CTO", "company_name": "StealthCo", "is_current": True}]).replace('"', '""')
        meta = json.dumps({"completeness": 0.75, "gaps": ["education dates"]}).replace('"', '""')
        return (f'pid-9,synth-email-abc,Ross Nordeen,stealth founder,long summary,"San Francisco, US",'
                f'"{work}","[]",ross@x.com,,synthetic,{approved},"{meta}"\n')

    def test_load_synthetic_parents_states_and_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "synthetic-people.csv"
            path.write_text(self.CSV_HEADER + self._csv_row(""), encoding="utf-8")
            parents = web.load_synthetic_parents(path)
            self.assertEqual(len(parents), 1)
            cand = parents[0]["candidates"][0]
            self.assertTrue(cand["synthetic"])
            self.assertEqual(web.candidate_state(cand), "review")  # pending -> Needs review pile
            self.assertEqual(cand["experiences"], ["CTO @ StealthCo (present)"])
            self.assertIn("research gaps: education dates", cand["reason"])
            html = web.render_linkedin_card(parents[0], cand, Path(tmpdir), Path(tmpdir))
            self.assertIn("Add without LinkedIn?", html)
            self.assertNotIn("Add Ross Nordeen without LinkedIn?", html)
            # approved=auto surfaces as verified
            path.write_text(self.CSV_HEADER + self._csv_row("auto"), encoding="utf-8")
            cand = web.load_synthetic_parents(path)[0]["candidates"][0]
            self.assertEqual(web.candidate_state(cand), "verified")

    def test_apply_synthetic_decision_flips_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "synthetic-people.csv"
            path.write_text(self.CSV_HEADER + self._csv_row(""), encoding="utf-8")
            self.assertEqual(web.apply_synthetic_decision(path, "synth-email-abc", "keep")["approved"], "yes")
            self.assertIn(",yes,", path.read_text())
            self.assertEqual(web.apply_synthetic_decision(path, "synth-email-abc", "detach")["approved"], "no")
            self.assertEqual(web.apply_synthetic_decision(path, "synth-email-abc", "reset")["approved"], "")
            with self.assertRaises(ValueError):
                web.apply_synthetic_decision(path, "synth-ghost", "keep")
            with self.assertRaises(ValueError):
                web.apply_synthetic_decision(path, "synth-email-abc", "fix")


class TestEligibleSubsetPlausiblyAbsent(unittest.TestCase):
    def _verdict(self, slug: str, absent: bool) -> dict:
        return {"parent_slug": slug, "candidate_key": f"{slug}-key", "person_ids": [slug],
                "verdict": {"verdict": "needs_review", "confidence": 0.5,
                            "linkedin_plausibly_absent": absent, "recommend_deep_research": False}}

    def test_absent_people_excluded_by_default_included_with_flag(self) -> None:
        verdicts = [self._verdict("ghost", absent=True), self._verdict("normal", absent=False)]
        self.assertEqual(dresearch.eligible_subset(verdicts, 0.85, {}), [])
        included = dresearch.eligible_subset(verdicts, 0.85, {}, include_plausibly_absent=True)
        self.assertEqual([r["parent_slug"] for r in included], ["ghost"])


class TestSpamDropAtMerge(unittest.TestCase):
    def _overrides(self, approved: str, action: str, conf: str = "0.950") -> dict:
        return {"spam-guy": {"action": action, "approved": approved, "emails": set(), "phones": set(),
                             "confidence": "0.9", "reason": "r", "person_id": "pid-1",
                             "llm_reject": "spam", "llm_reject_confidence": conf}}

    def test_only_reject_drops_person(self) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "merge_network_sources",
            Path(__file__).resolve().parents[1] / "packs/ingestion/primitives/merge_network_sources/merge_network_sources.py")
        merge = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(merge)

        # only the LLM flag (no user decision) -> dropped
        self.assertEqual(merge.spam_dropped_person_ids(self._overrides("", "verify")), {"pid-1"})
        # auto decisions do NOT protect (machine vs machine)
        self.assertEqual(merge.spam_dropped_person_ids(self._overrides("auto", "verify")), {"pid-1"})
        # a user detach does NOT protect (wrong link + spam -> gone)
        self.assertEqual(merge.spam_dropped_person_ids(self._overrides("yes", "detach")), {"pid-1"})
        # a user keep-ish decision (verify/retarget) protects the person
        self.assertEqual(merge.spam_dropped_person_ids(self._overrides("yes", "verify")), set())
        self.assertEqual(merge.spam_dropped_person_ids(self._overrides("yes", "retarget")), set())
        # low confidence never drops
        self.assertEqual(merge.spam_dropped_person_ids(self._overrides("", "verify", conf="0.500")), set())

        # end-to-end through apply_overrides: the person row is excluded
        ov = self._overrides("", "verify")
        rows = [{"public_identifier": "spam-guy", "linkedin_url": "https://linkedin.com/in/spam-guy"}]
        counts = merge.apply_overrides(rows, ov)
        self.assertEqual(counts["spam_dropped"], 1)
        self.assertTrue(rows[0].get("__excluded__"))

    def test_synthetic_admission_gate_lives_at_load_time(self) -> None:
        # The approved gate is enforced in load_people_file on the RAW row:
        # normalize_people_row strips the non-schema `approved` column, so a
        # keep-gate check on the normalized row can never see it (that was the
        # bug: approved synthetic rows never merged).
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "merge_network_sources",
            Path(__file__).resolve().parents[1] / "packs/ingestion/primitives/merge_network_sources/merge_network_sources.py")
        merge = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(merge)

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "synthetic-people.csv"
            fields = ["id", "public_identifier", "enrichment_provider", "full_name", "approved"]
            with path.open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=fields)
                w.writeheader()
                for approved, pub in (("auto", "synth-a"), ("yes", "synth-b"),
                                      ("", "synth-c"), ("no", "synth-d")):
                    w.writerow({"id": f"candidate:email:{pub}@x.com", "public_identifier": pub,
                                "enrichment_provider": "synthetic", "full_name": "Synth Person",
                                "approved": approved})
            loaded = merge.load_people_file(path)
        # only auto/yes survive load; pending and user-no never enter the merge
        self.assertEqual(sorted(r["public_identifier"] for r in loaded), ["synth-a", "synth-b"])
        # ...and the loaded (normalized, approved-stripped) rows pass the keep gate
        for row in loaded:
            self.assertTrue(merge.keep_people_csv_row(row))
        # a synthetic row without an identity never passes
        self.assertFalse(merge.keep_people_csv_row({"enrichment_provider": "synthetic"}))
        # real rows still require LinkedIn + rapidapi — the relaxation is synthetic-only
        self.assertFalse(merge.keep_people_csv_row({"public_identifier": "someone", "approved": "auto"}))


if __name__ == "__main__":
    unittest.main()
