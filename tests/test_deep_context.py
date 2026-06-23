"""Unit + light-integration tests for the deep-context dossier pipeline.

Covers identity normalization, the privacy gate, adaptive sampling, fact merge,
attributedBody decoding, Jaro-Winkler blocking/merge detection, and an end-to-end
compose -> cluster -> lookup flow over synthetic fixtures (no network, no DB).
"""
from __future__ import annotations

import json
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
    reconcile_deep_research as dresearch,
    reconcile_linkedin as reconcile,
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


class TestOwnerContext(unittest.TestCase):
    def test_owner_background_block(self):
        block = common.owner_background_block({
            "name": "Arthur Chen",
            "education": [{"school": "UCLA", "end": 2010, "note": "undergrad"}],
            "work": [{"company": "Intel", "title": "Engineer", "start": 2012, "end": 2016}],
            "locations": ["LA"],
        })
        self.assertIn("Arthur Chen", block)
        self.assertIn("UCLA [until 2010]", block)
        self.assertIn("Intel as Engineer [2012-2016]", block)

    def test_shared_context_merges_and_dedupes(self):
        chunks = [
            {"facts": _facts(shared_context=[{"overlap": "school", "detail": "USC overlap", "evidence": "e1"}])},
            {"facts": _facts(shared_context=[{"overlap": "school", "detail": "USC overlap", "evidence": "e1"},
                                             {"overlap": "employer", "detail": "Intel", "evidence": "e2"}])},
        ]
        merged = compose.merge_facts(chunks)
        details = {s["detail"] for s in merged["shared_context"]}
        self.assertEqual(details, {"USC overlap", "Intel"})


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

    def test_apply_verdicts_confirms_detaches_and_backs_up(self):
        with tempfile.TemporaryDirectory() as d:
            people_csv = Path(d) / "people.csv"
            self._people_csv(people_csv, [
                {"id": "pa", "public_identifier": "alice", "linkedin_url": "https://www.linkedin.com/in/alice"},
                {"id": "pb", "public_identifier": "bobceo", "linkedin_url": "https://www.linkedin.com/in/bobceo"},
                {"id": "pc", "public_identifier": "carol", "linkedin_url": "https://www.linkedin.com/in/carol"}])
            results = [
                {"person_ids": ["pa"], "conflict": False, "verdict": _verdict("confirmed", 0.95)},
                {"person_ids": ["pb"], "conflict": False, "verdict": _verdict("wrong_person", 0.92, reason="CEO != plumber")},
                {"person_ids": ["pc"], "conflict": False, "verdict": _verdict("wrong_person", 0.50)},  # below threshold
            ]
            stats = reconcile.apply_verdicts(people_csv, results, 0.85)
            self.assertEqual(stats["confirmed"], 1)
            self.assertEqual(stats["detached"], 1)
            self.assertTrue(Path(stats["backup"]).exists())  # .bkup made before mutating
            import csv as _csv
            with people_csv.open() as _fh:
                rows = {r["id"]: r for r in _csv.DictReader(_fh)}
            self.assertEqual(rows["pa"]["linkedin_verified"], "confirmed")
            self.assertEqual(rows["pa"]["linkedin_url"], "https://www.linkedin.com/in/alice")  # untouched
            self.assertEqual(rows["pb"]["linkedin_verified"], "wrong_person")
            self.assertEqual(rows["pb"]["linkedin_url"], "")                                   # detached
            self.assertEqual(rows["pb"]["public_identifier"], "")
            self.assertEqual(rows["pb"]["linkedin_url_rejected"], "https://www.linkedin.com/in/bobceo")
            self.assertEqual(rows["pc"]["linkedin_verified"], "")  # low-confidence: not applied

    def test_review_queue_routes_low_confidence_and_conflicts(self):
        with tempfile.TemporaryDirectory() as d:
            qpath = Path(d) / "review.csv"
            tasks = [
                {"parent_slug": "a", "name": "A", "linkedin": {"linkedin_url": "u"}, "conflict": False,
                 "no_link": False, "verdict": _verdict("confirmed", 0.95)},     # NOT queued
                {"parent_slug": "b", "name": "B", "linkedin": {"linkedin_url": "u"}, "conflict": False,
                 "no_link": False, "verdict": _verdict("needs_review", 0.4)},   # queued
                {"parent_slug": "c", "name": "C", "linkedin": {"linkedin_url": "u"}, "conflict": True,
                 "no_link": False, "verdict": _verdict("confirmed", 0.95)},     # queued (conflict)
                {"parent_slug": "d", "name": "D", "linkedin": {}, "conflict": False,
                 "no_link": True, "verdict": _verdict("needs_review", 0.0, absent=True)}]  # queued (no link)
            n = reconcile.write_review_queue(qpath, tasks, 0.85)
            self.assertEqual(n, 3)
            import csv as _csv
            with qpath.open() as _fh:
                slugs = {r["parent_slug"] for r in _csv.DictReader(_fh)}
            self.assertEqual(slugs, {"b", "c", "d"})

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
                review_queue=rdir / "review-queue.csv", confirm_threshold=0.85,
                model="m", reasoning_effort="high", concurrency=1, timeout=10, max_retries=0,
                dry_run=False, no_apply=False, no_llm=True))
            self.assertEqual(man["judge"], "deterministic")
            self.assertEqual(man["no_link"], 1)                      # Carol
            self.assertEqual(man["verdicts"]["confirmed"], 1)        # Alice (offline stub)
            self.assertEqual(man["applied"]["confirmed"], 1)
            self.assertTrue((rdir / "verdicts.csv").exists())
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
                review_queue=base / "r" / "q.csv", confirm_threshold=0.85, model="m",
                reasoning_effort="high", concurrency=1, timeout=10, max_retries=0,
                dry_run=True, no_apply=True, no_llm=True))
            self.assertEqual(man["status"], "dry_run")
            self.assertEqual(man["judgeable"], 1)
            self.assertFalse((base / "r").exists())  # dry-run writes nothing


class TestReconcileDeepResearch(unittest.TestCase):
    """Phase 3 escalation: subset selection + $25 cost gate (no Parallel.ai spend)."""

    def test_eligible_subset_filters(self):
        verdicts = [
            {"verdict": _verdict("wrong_person", 0.95, dr=True)},                 # eligible
            {"verdict": _verdict("wrong_person", 0.95, dr=True, absent=True)},    # excluded: no LinkedIn
            {"verdict": _verdict("wrong_person", 0.5, dr=True)},                  # excluded: low conf
            {"verdict": _verdict("wrong_person", 0.95, dr=False)},               # excluded: not recommended
            {"verdict": _verdict("confirmed", 0.99, dr=True)}]                    # excluded: not wrong
        self.assertEqual(len(dresearch.eligible_subset(verdicts, 0.85)), 1)

    def test_cost_gate_blocks_over_budget(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            vj = base / "verdicts.jsonl"
            recs = [{"parent_slug": f"p{i}", "name": f"N{i}", "person_ids": [f"x{i}"],
                     "linkedin": {"linkedin_url": "u"},
                     "verdict": _verdict("wrong_person", 0.95, dr=True, reason="wrong")} for i in range(600)]
            vj.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
            man = dresearch.run(_ns(verdicts_jsonl=vj, people_csv=base / "nope.csv",
                facts_dir=base / "f", raw_dir=base / "r", processor="core2x",
                confirm_threshold=0.85, budget=25.0, approve=False, dry_run=False))
            self.assertEqual(man["status"], "needs_approval")   # 600 * $0.05 = $30 > $25
            self.assertGreater(man["estimated_usd"], 25)


class _ns:
    """Lightweight argparse.Namespace stand-in for run() calls."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


if __name__ == "__main__":
    unittest.main()
