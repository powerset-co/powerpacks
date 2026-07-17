"""Candidates-support layer for the deep-context pipeline ($deep-context).

Covers: loading import candidates as Person rows (mapping/dedup/channel
translation), the opt-in collect union (default off = unchanged selection),
deep-research eligibility for dossier-bearing candidates, and minting people
rows from candidate research results (synthetic + retarget) with the contact
identity sourced from candidates.csv instead of people.csv.
"""
from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import threading
import unittest
import urllib.parse
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context import (
    apply_retargets as retargets,
    assemble_synthetic_profile as asp,
    candidates,
    check_readiness as readiness,
    collect_person_context as collect,
    common,
    compose_dossier as compose,
    prefetch_profiles as prefetch,
    reconcile_deep_research as dresearch,
    reconcile_linkedin as reconcile,
    reconcile_review_web as web,
    synthesize_person_context as synth,
)
from packs.ingestion.schemas.candidates_schema import (
    CANDIDATES_SCHEMA_COLUMNS,
    normalize_candidate_row,
)


class _ns:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _write_candidates_csv(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CANDIDATES_SCHEMA_COLUMNS)
        w.writeheader()
        for row in rows:
            w.writerow(normalize_candidate_row(row))
    return path


GMAIL_ROW = {
    "candidate_key": "email:cass@x.com", "source": "gmail", "full_name": "Cass Doe",
    "primary_email": "cass@x.com", "all_emails": ["cass@x.com", "cd@y.com"],
    "company_guess": "XCo", "interaction_counts": {"gmail": 12},
    "last_interaction": "2026-06-01T00:00:00Z",
    "evidence": {"sending_domain": "x.com"},
}
PHONE_ROW = {
    "candidate_key": "phone:+14155551234", "source": "imessage", "full_name": "Tex Chat",
    "primary_phone": "+14155551234", "all_phones": ["+14155551234"],
    "interaction_counts": {"imessage": 5, "whatsapp": 2},
    "last_interaction": "2026-05-01T00:00:00Z",
    "evidence": {"channels": ["imessage", "whatsapp"]},
}


def _pools(base: Path, gmail: list[dict], messages: list[dict]) -> list[Path]:
    return [
        _write_candidates_csv(base / "gmail" / "candidates.csv", gmail),
        _write_candidates_csv(base / "messages" / "candidates.csv", messages),
    ]


class TestLoadCandidates(unittest.TestCase):
    def test_mapping_dedup_and_channel_translation(self):
        with tempfile.TemporaryDirectory() as d:
            pools = _pools(Path(d), [GMAIL_ROW], [
                PHONE_ROW,
                {**GMAIL_ROW, "full_name": "Dupe Loses"},                     # dedup: first file wins
                {"candidate_key": "email:no-contact", "source": "gmail"},     # no email/phone -> skipped
                {"candidate_key": "email:evil/../x@x.com", "source": "gmail",  # path-hostile key -> skipped
                 "primary_email": "evil/../x@x.com"},
            ])
            with mock.patch.object(candidates, "CANDIDATE_CSVS", pools):
                people = list(candidates.load_candidates())
        self.assertEqual([p.person_id for p in people],
                         ["candidate:email:cass@x.com", "candidate:phone:+14155551234"])
        cass, tex = people
        self.assertEqual(cass.full_name, "Cass Doe")                          # first file won the dupe
        self.assertEqual(cass.emails, ["cass@x.com", "cd@y.com"])
        self.assertEqual(cass.source_channels, ["gmail_msgvault"])            # gmail -> gmail_msgvault
        self.assertEqual(tex.phones, ["+14155551234"])
        self.assertEqual(tex.source_channels, ["imessage", "whatsapp"])       # evidence.channels honored

    def test_limit_and_key_filter(self):
        with tempfile.TemporaryDirectory() as d:
            pools = _pools(Path(d), [GMAIL_ROW], [PHONE_ROW])
            with mock.patch.object(candidates, "CANDIDATE_CSVS", pools):
                self.assertEqual(len(list(candidates.load_candidates(limit=1))), 1)
                only = list(candidates.load_candidates(candidate_key="phone:+14155551234"))
        self.assertEqual([p.person_id for p in only], ["candidate:phone:+14155551234"])

    def test_candidate_row_lookup_and_carry(self):
        with tempfile.TemporaryDirectory() as d:
            pools = _pools(Path(d), [GMAIL_ROW], [PHONE_ROW])
            with mock.patch.object(candidates, "CANDIDATE_CSVS", pools):
                row = candidates.candidate_row("email:cass@x.com")
                self.assertIsNone(candidates.candidate_row("email:ghost@x.com"))
                self.assertIsNone(candidates.candidate_row(""))
                carry = candidates.candidate_carry(candidates.candidate_row("phone:+14155551234"))
        self.assertEqual(row["company_guess"], "XCo")                         # raw row, schema verbatim
        self.assertEqual(json.loads(row["all_emails"]), ["cass@x.com", "cd@y.com"])
        self.assertEqual(carry["primary_phone"], "+14155551234")
        self.assertEqual(carry["source_channels"], "imessage,whatsapp")
        self.assertEqual(json.loads(carry["interaction_counts"]), {"imessage": 5, "whatsapp": 2})

    def test_candidate_merged_with_existing_person_is_already_resolved(self):
        with tempfile.TemporaryDirectory() as d:
            index = Path(d) / "index.json"
            index.write_text(json.dumps({
                "slugs": {
                    "cass-candidate": {"person_id": "candidate:email:cass@x.com"},
                    "cass-existing": {"person_id": "person-cass"},
                    "tex-candidate": {"person_id": "candidate:phone:+14155551234"},
                },
                "parents": {
                    "cass": {"children": ["cass-candidate", "cass-existing"]},
                    "tex": {"children": ["tex-candidate"]},
                },
            }), encoding="utf-8")
            self.assertEqual(
                candidates.candidates_resolved_by_existing(index),
                {"candidate:email:cass@x.com"},
            )

    def test_person_id_helpers(self):
        pid = candidates.candidate_person_id("email:a@b.com")
        self.assertEqual(pid, "candidate:email:a@b.com")
        self.assertTrue(candidates.is_candidate_id(pid))
        self.assertFalse(candidates.is_candidate_id("p1"))
        self.assertEqual(candidates.candidate_key_of(pid), "email:a@b.com")
        self.assertEqual(candidates.candidate_key_of("p1"), "")

    def test_same_named_candidates_get_distinct_slugs(self):
        # The shared "candidate:" prefix would collapse slugify's first-8-alnum
        # suffix to "candidat" for every candidate; the hashed suffix keeps two
        # same-named candidates from overwriting each other's dossiers.
        from packs.ingestion.primitives.deep_context.common import slugify

        a = slugify("John Smith", candidates.candidate_person_id("phone:+14155550001"))
        b = slugify("John Smith", candidates.candidate_person_id("phone:+14155550002"))
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("john-smith-") and b.startswith("john-smith-"))
        self.assertNotIn("candidat", a.rsplit("-", 1)[1])
        # Non-candidate ids keep the historical suffix scheme (stable dossier names).
        self.assertEqual(slugify("Jane Doe", "abc-123-def"), "jane-doe-abc123de")


class TestCollectIncludesCandidates(unittest.TestCase):
    PEOPLE_CSV = (
        "id,full_name,primary_email,all_emails,primary_phone,all_phones,source_channels\n"
        "p1,Jane Doe,jane@acme.com,,,,gmail_msgvault\n"
    )

    def _fixture(self, d: Path) -> tuple[Path, list[Path]]:
        people_csv = d / "people.csv"
        people_csv.write_text(self.PEOPLE_CSV, encoding="utf-8")
        return people_csv, _pools(d, [GMAIL_ROW], [PHONE_ROW])

    def test_default_off_selection_is_unchanged_and_never_loads_candidates(self):
        with tempfile.TemporaryDirectory() as d:
            people_csv, pools = self._fixture(Path(d))
            baseline = [p.person_id for p in common.load_people(people_csv, limit=0, person_id="")]
            with mock.patch.object(candidates, "CANDIDATE_CSVS", pools), \
                 mock.patch.object(collect, "load_candidates",
                                   side_effect=AssertionError("candidates loaded without the flag")):
                got = [p.person_id for p in collect.selected_people(
                    _ns(include_candidates=False, limit=0, person=""), people_csv)]
        self.assertEqual(got, baseline)
        self.assertEqual(got, ["p1"])

    def test_flag_unions_people_then_candidates(self):
        with tempfile.TemporaryDirectory() as d:
            people_csv, pools = self._fixture(Path(d))
            with mock.patch.object(candidates, "CANDIDATE_CSVS", pools):
                ids = [p.person_id for p in collect.selected_people(
                    _ns(include_candidates=True, limit=0, person=""), people_csv)]
                limited = [p.person_id for p in collect.selected_people(
                    _ns(include_candidates=True, limit=2, person=""), people_csv)]
        self.assertEqual(ids, ["p1", "candidate:email:cass@x.com", "candidate:phone:+14155551234"])
        self.assertEqual(limited, ["p1", "candidate:email:cass@x.com"])       # limit spans the union

    def test_person_selection_covers_both_id_spaces(self):
        with tempfile.TemporaryDirectory() as d:
            people_csv, pools = self._fixture(Path(d))
            with mock.patch.object(candidates, "CANDIDATE_CSVS", pools):
                one_cand = [p.person_id for p in collect.selected_people(
                    _ns(include_candidates=True, limit=0, person="candidate:phone:+14155551234"), people_csv)]
                one_person = [p.person_id for p in collect.selected_people(
                    _ns(include_candidates=True, limit=0, person="p1"), people_csv)]
        self.assertEqual(one_cand, ["candidate:phone:+14155551234"])
        self.assertEqual(one_person, ["p1"])                                  # people id never fans out


class TestCandidateBundlesInheritDownstream(unittest.TestCase):
    """Synthesize/compose key everything off bundle files + stems, not people.csv,
    so candidate ids (with ':'/'@'/'+') must round-trip through the stages."""

    PID = "candidate:email:cass@x.com"

    def test_stems_round_trip_and_compose_writes_a_dossier(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            raw, facts, dossiers = base / "raw", base / "facts", base / "dossiers"
            raw.mkdir(), facts.mkdir()
            common.write_json(raw / f"{self.PID}.json", {
                "person_id": self.PID, "full_name": "Cass Doe", "emails": ["cass@x.com"],
                "phones": [], "source_channels": ["gmail_msgvault"],
                "messages": [{"at": "2026-01-01T00:00:00Z", "channel": "gmail",
                              "direction": "from_them", "text": "hi"}],
            })
            pending = synth.pending_target_paths(raw, facts, force=False, person_id=self.PID)
            self.assertEqual([p.stem for p in pending], [self.PID])           # stem == person_id
            (facts / f"{self.PID}.jsonl").write_text(json.dumps({
                "chunk_index": 0,
                "facts": {"canonical_name": "Cass Doe", "confidence": 0.9, "employers": [],
                          "topics": [], "identifiers": [], "notable_events": [],
                          "aliases": [], "shared_context": []},
            }) + "\n", encoding="utf-8")
            self.assertEqual(synth.pending_target_paths(raw, facts, force=False, person_id=""), [])
            compose.run(_ns(raw_dir=raw, facts_dir=facts, dossier_dir=dossiers,
                            index_json=base / "index.json", index_md=base / "index.md", person=""))
            index = json.loads((base / "index.json").read_text(encoding="utf-8"))
            (slug, info), = index["slugs"].items()
            self.assertEqual(info["person_id"], self.PID)
            self.assertTrue((dossiers / f"{slug}.md").exists())


class TestReconcileDeepResearchCandidates(unittest.TestCase):
    def test_candidate_subset_requires_dossier_and_flag_gates_run(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts = base / "facts"
            facts.mkdir()
            (facts / "candidate:email:cass@x.com.jsonl").write_text(
                json.dumps({"facts": {"canonical_name": "Cass Doe"}}) + "\n", encoding="utf-8")
            pools = _pools(base, [GMAIL_ROW], [PHONE_ROW])
            with mock.patch.object(candidates, "CANDIDATE_CSVS", pools):
                subset = dresearch.candidate_subset(
                    facts, {"candidate:email:cass@x.com": {"network_worth": "yes"}})
                self.assertEqual([r["person_ids"] for r in subset],
                                 [["candidate:email:cass@x.com"]])            # no facts -> not eligible
                self.assertEqual(subset[0]["candidate_key"], "candidate:email:cass@x.com")
                self.assertTrue(subset[0]["parent_slug"])
                # a decided/excluded/retargeted candidate never re-enters the queue
                for row in ({"action": "retarget"}, {"action": "exclude"}, {"approved": "yes"}):
                    ov = {"candidate:email:cass@x.com": {"action": "", "approved": "", **row}}
                    self.assertEqual(dresearch.candidate_subset(facts, ov), [])

    def test_run_dry_run_queues_candidates_only_with_flag(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts = base / "facts"
            facts.mkdir()
            (facts / "candidate:email:cass@x.com.jsonl").write_text(
                json.dumps({"facts": {"canonical_name": "Cass Doe",
                                      "relationship_to_owner": "college friend"}}) + "\n",
                encoding="utf-8")
            pools = _pools(base, [GMAIL_ROW], [])
            reconcile._write_override_rows(base / "ov.csv", {
                "candidate:email:cass@x.com": {
                    "public_identifier": "candidate:email:cass@x.com",
                    "network_worth": "yes",
                },
            })
            args = dict(verdicts_jsonl=base / "verdicts.jsonl", overrides_csv=base / "ov.csv",
                        people_csv=base / "people.csv", facts_dir=facts, raw_dir=base / "raw",
                        processor="core2x", confirm_threshold=0.85, budget=0.0,
                        approve=False, dry_run=True, include_plausibly_absent=False)
            old_out, old_queue = dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV
            dresearch.DR_OUT_DIR = base / "research"
            dresearch.QUEUE_CSV = dresearch.DR_OUT_DIR / "research_queue.csv"
            try:
                with mock.patch.object(candidates, "CANDIDATE_CSVS", pools):
                    off = dresearch.run(_ns(**args, include_candidates=False))
                    on = dresearch.run(_ns(**args, include_candidates=True))
            finally:
                dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV = old_out, old_queue
            self.assertEqual(off["status"], "noop")                           # flag off: nothing eligible
            self.assertEqual(on["status"], "dry_run")
            self.assertEqual(on["eligible"], 1)
            self.assertEqual(on["eligible_candidates"], 1)
            with Path(on["queue_csv"]).open(newline="", encoding="utf-8") as fh:
                (row,) = list(csv.DictReader(fh))
            self.assertEqual(row["primary_email"], "cass@x.com")              # contact from candidates.csv
            self.assertTrue(row["source_parent_slug"])
            self.assertEqual(json.loads(row["source_person_ids"]),
                             ["candidate:email:cass@x.com"])
            self.assertEqual(row["source_candidate_public_identifier"],
                             "candidate:email:cass@x.com")
            self.assertIn("college friend", row["bio"])                       # dossier text as context
            self.assertIn("unresolved import candidate", row["known_info"])


class TestMintingFromCandidateResearch(unittest.TestCase):
    def _research_profile(self, linkedin=None) -> dict:
        return {
            "person": {"full_name": "Cass Doe", "first_name": "Cass", "last_name": "Doe", "confidence": 0.9},
            "location": {"city": "Oakland", "country": "United States", "raw": ""},
            "headline": {"text": "builder"}, "summary": {"text": "s"},
            "positions": [{"title": "CTO", "company_name": "StealthCo", "is_current": True}],
            "education": [], "social": {"linkedin_url": linkedin},
            "metadata": {"estimated_completeness": 0.7, "gaps": []},
        }

    def test_assemble_sources_contact_from_candidate_row(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            pools = _pools(base, [GMAIL_ROW], [])
            research = base / "research"
            common.write_json(research / "cass-doe-parentab" / "01_research_parallel.json",
                              self._research_profile())
            queue = base / "research_queue.csv"
            with queue.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=dresearch.QUEUE_FIELDS)
                writer.writeheader()
                writer.writerow({
                    "handle": "cass-doe-parentab",
                    "source_parent_slug": "cass-doe-parentab",
                    "source_person_ids": json.dumps(["candidate:email:cass@x.com"]),
                    "source_candidate_public_identifier": "candidate:email:cass@x.com",
                    "display_name": "Cass Doe",
                    "primary_email": "cass@x.com",
                    "source_channel": "email",
                })
            people = base / "people.csv"
            people.write_text("id,primary_email,primary_phone\n", encoding="utf-8")
            parents_dir = base / "parents"
            parents_dir.mkdir()
            (parents_dir / "cass-doe-parentab.md").write_text(
                "# Cass Doe (canonical)\n", encoding="utf-8")
            out = base / "synthetic-people.csv"
            with mock.patch.object(candidates, "CANDIDATE_CSVS", pools), \
                 contextlib.redirect_stdout(io.StringIO()):
                asp.main(["--research-dir", str(research), "--queue-csv", str(queue),
                          "--people-csv", str(people),
                          "--verdicts-jsonl", str(base / "verdicts.jsonl"),
                          "--out", str(out)])
            with out.open(newline="", encoding="utf-8") as fh:
                (row,) = list(csv.DictReader(fh))
            self.assertEqual(row["id"], "candidate:email:cass@x.com")
            self.assertEqual(row["entity_urn"], "synthetic:candidate:email:cass@x.com")
            self.assertEqual(row["primary_email"], "cass@x.com")
            self.assertEqual(json.loads(row["all_emails"]), ["cass@x.com", "cd@y.com"])
            self.assertEqual(json.loads(row["interaction_counts"]), {"gmail": 12})
            self.assertEqual(row["source_channels"], "gmail_msgvault")        # the candidate's channels
            self.assertEqual(row["source_parent_slug"], "cass-doe-parentab")
            self.assertEqual(json.loads(row["source_person_ids"]),
                             ["candidate:email:cass@x.com"])
            self.assertEqual(row["approved"], "auto")
            # ...and the review UI surfaces it through the existing synthetic path.
            parent, = web.load_synthetic_parents(out, parents_dir)
            self.assertEqual(parent["person_ids"], ["candidate:email:cass@x.com"])
            self.assertEqual(parent["dossier_slug"], "cass-doe-parentab")
            self.assertTrue(parent["candidates"][0]["synthetic"])
            self.assertEqual(web.apply_synthetic_decision(
                out, row["public_identifier"], "keep")["approved"], "yes")

    def test_assemble_backfills_provenance_without_rewriting_user_gate(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            handle = "cass-doe-parentab"
            research = base / "research"
            common.write_json(research / handle / "01_research_parallel.json",
                              self._research_profile())
            queue = base / "research_queue.csv"
            with queue.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=dresearch.QUEUE_FIELDS)
                writer.writeheader()
                writer.writerow({
                    "handle": handle,
                    "source_parent_slug": handle,
                    "source_person_ids": json.dumps(["pid-cass"]),
                    "source_candidate_public_identifier": "wrong-cass-link",
                    "display_name": "Cass Doe",
                })
            out = base / "synthetic-people.csv"
            pub = f"synth-x-{handle}"
            asp.write_rows(out, {pub: {
                "id": pub,
                "public_identifier": pub,
                "full_name": "Cass Doe",
                "enrichment_provider": "synthetic",
                "approved": "yes",
            }})
            people = base / "people.csv"
            people.write_text("id,primary_email,primary_phone\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                asp.main([
                    "--research-dir", str(research),
                    "--queue-csv", str(queue),
                    "--people-csv", str(people),
                    "--verdicts-jsonl", str(base / "verdicts.jsonl"),
                    "--out", str(out),
                ])
            with out.open(newline="", encoding="utf-8") as fh:
                (row,) = list(csv.DictReader(fh))
        self.assertEqual(row["approved"], "yes")
        self.assertEqual(row["source_parent_slug"], handle)
        self.assertEqual(json.loads(row["source_person_ids"]), ["pid-cass"])
        self.assertEqual(row["source_candidate_public_identifier"], "wrong-cass-link")

    def test_apply_retargets_sources_contact_from_candidate_row(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            pools = _pools(base, [], [PHONE_ROW])
            ov = base / "ov.csv"
            with ov.open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=["public_identifier", "action", "approved",
                                                   "new_linkedin_url", "new_public_identifier", "person_id"])
                w.writeheader()
                w.writerow({"public_identifier": "candidate:phone:+14155551234",
                            "action": "retarget", "approved": "yes",
                            "new_linkedin_url": "https://www.linkedin.com/in/tex-real",
                            "new_public_identifier": "tex-real",
                            "person_id": "candidate:phone:+14155551234"})
            people = base / "people.csv"
            people.write_text("id,public_identifier\n", encoding="utf-8")
            fake = {"data": {"raw": 1}, "normalized_profile": {"success": True},
                    "from_cache": True, "error": ""}
            with mock.patch.object(candidates, "CANDIDATE_CSVS", pools), \
                 mock.patch.object(retargets, "rapidapi_profile", return_value=fake), \
                 mock.patch.object(retargets, "normalize_rapidapi", return_value={}), \
                 mock.patch.object(retargets, "merge_provider_profile",
                                   return_value={"public_identifier": "tex-real",
                                                 "full_name": "Tex Right", "rapidapi_response": '{"raw":1}'}):
                man = retargets.run(_ns(overrides_csv=ov, people_csv=people,
                                        profile_cache_dir=base / "cache",
                                        out_csv=base / "retarget-people.csv"))
            self.assertEqual(man["enriched"], 1)
            with (base / "retarget-people.csv").open(newline="", encoding="utf-8") as fh:
                (row,) = list(csv.DictReader(fh))
            self.assertEqual(row["public_identifier"], "tex-real")            # enriched identity unchanged
            self.assertEqual(row["primary_phone"], "+14155551234")            # contact from candidates.csv
            self.assertEqual(json.loads(row["interaction_counts"]), {"imessage": 5, "whatsapp": 2})
            self.assertEqual(row["source_channels"], "imessage,whatsapp")
            self.assertEqual(row["last_interaction"], "2026-05-01T00:00:00Z")


def _facts_record(decision: str = "", reason: str = "", name: str = "") -> str:
    """One facts JSONL line exactly as synthesize_person_context.on_result writes it
    (the extracted profile nested under 'facts')."""
    facts: dict = {"canonical_name": name}
    if decision:
        facts["network_worth"] = {"decision": decision, "reason": reason}
    return json.dumps({"chunk_index": 0, "facts": facts})


class TestNetworkWorth(unittest.TestCase):
    """The yes|maybe|no worth judgment: schema pins, LLM read, and precedence."""

    def test_fact_schema_and_prompt_pin_network_worth(self):
        self.assertIn("network_worth", synth.FACT_SCHEMA["required"])
        prop = synth.FACT_SCHEMA["properties"]["network_worth"]
        self.assertEqual(prop["properties"]["decision"]["enum"], ["yes", "maybe", "no"])
        self.assertEqual(prop["required"], ["decision", "reason"])
        self.assertIn("network_worth", synth.SYSTEM_PROMPT)
        self.assertIn("family/relatives", synth.SYSTEM_PROMPT)
        self.assertIn("professors/teachers/mentors", synth.SYSTEM_PROMPT)
        self.assertIn("Maybe is exceptional", synth.SYSTEM_PROMPT)
        self.assertIn("network_worth", reconcile.RECONCILE_SCHEMA["required"])
        self.assertIn("family/relatives", reconcile.SYSTEM_PROMPT)
        self.assertIn("professors/teachers/mentors", reconcile.SYSTEM_PROMPT)
        self.assertIn("Maybe is exceptional", reconcile.SYSTEM_PROMPT)
        self.assertEqual(candidates.NETWORK_WORTH_VALUES, ("yes", "maybe", "no"))
        self.assertIn("network_worth", reconcile.OVERRIDE_COLUMNS)

    def test_llm_worth_last_record_wins_and_absent_default(self):
        with tempfile.TemporaryDirectory() as d:
            facts = Path(d)
            pid = "candidate:email:cass@x.com"
            (facts / f"{pid}.jsonl").write_text(
                _facts_record("yes", "founder") + "\n" + _facts_record("no", "actually a vendor") + "\n",
                encoding="utf-8")
            self.assertEqual(candidates.llm_network_worth(pid, facts),
                             {"decision": "no", "reason": "actually a vendor"})
            self.assertEqual(candidates.llm_network_worth("ghost", facts),
                             {"decision": "", "reason": ""})

    def test_effective_worth_precedence_user_llm_default(self):
        with tempfile.TemporaryDirectory() as d:
            facts = Path(d)
            pid = "candidate:email:cass@x.com"
            (facts / f"{pid}.jsonl").write_text(_facts_record("no", "vendor") + "\n", encoding="utf-8")
            got = candidates.effective_network_worth(pid, {pid: {"network_worth": "yes"}}, facts)
            self.assertEqual((got["decision"], got["source"]), ("yes", "user"))          # user wins
            got = candidates.effective_network_worth(pid, {pid: {"network_worth": "dunno"}}, facts)
            self.assertEqual((got["decision"], got["source"]), ("no", "llm"))            # invalid mark -> LLM
            self.assertEqual(got["reason"], "vendor")
            got = candidates.effective_network_worth("ghost", {}, facts)
            self.assertEqual((got["decision"], got["source"]), ("maybe", "default"))     # nothing -> maybe

    def test_effective_worth_row_llm_worth_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            facts = Path(d)
            # facts absent: the review row's mirrored llm_worth supplies the LLM signal
            got = candidates.effective_network_worth(
                "janedoe", {"janedoe": {"llm_worth": "no", "llm_worth_reason": "cold outreach"}}, facts)
            self.assertEqual((got["decision"], got["source"], got["reason"]),
                             ("no", "llm", "cold outreach"))
            # fresh reconcile judgment wins over the older synthesis fact
            (facts / "janedoe.jsonl").write_text(_facts_record("yes", "founder") + "\n", encoding="utf-8")
            got = candidates.effective_network_worth(
                "janedoe", {"janedoe": {"llm_worth": "no", "llm_worth_reason": "stale"}}, facts)
            self.assertEqual((got["decision"], got["source"], got["reason"]), ("no", "llm", "stale"))
            # the user's mark still beats every machine signal
            got = candidates.effective_network_worth(
                "janedoe", {"janedoe": {"network_worth": "no", "llm_worth": "yes"}}, facts)
            self.assertEqual((got["decision"], got["source"]), ("no", "user"))

    def test_effective_worth_treats_approved_exclude_as_user_no(self):
        with tempfile.TemporaryDirectory() as d:
            facts = Path(d)
            # an approved exclude IS a user no — even against a machine yes
            got = candidates.effective_network_worth(
                "janedoe", {"janedoe": {"action": "exclude", "approved": "yes", "llm_worth": "yes"}}, facts)
            self.assertEqual((got["decision"], got["source"]), ("no", "user"))
            # a pending exclude is not a decision
            got = candidates.effective_network_worth(
                "janedoe", {"janedoe": {"action": "exclude", "approved": ""}}, facts)
            self.assertEqual((got["decision"], got["source"]), ("maybe", "default"))
            # approved=auto is machine state, not a human decision; fresh
            # re-review output can still replace it.
            got = candidates.effective_network_worth(
                "janedoe", {"janedoe": {
                    "action": "exclude", "approved": "auto",
                    "llm_worth": "yes", "llm_worth_reason": "real relationship",
                }}, facts)
            self.assertEqual((got["decision"], got["source"]), ("yes", "llm"))
            # an explicit user mark wins over a stale exclude
            got = candidates.effective_network_worth(
                "janedoe", {"janedoe": {"action": "exclude", "approved": "yes", "network_worth": "yes"}}, facts)
            self.assertEqual((got["decision"], got["source"]), ("yes", "user"))


class TestCandidateSubsetWorthGate(unittest.TestCase):
    def test_added_pile_is_eligible_and_user_override_wins(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts = base / "facts"
            facts.mkdir()
            (facts / "candidate:email:cass@x.com.jsonl").write_text(
                _facts_record("yes", "real relationship") + "\n", encoding="utf-8")
            (facts / "candidate:phone:+14155551234.jsonl").write_text(
                _facts_record("no", "food delivery updates") + "\n", encoding="utf-8")
            pools = _pools(base, [GMAIL_ROW], [PHONE_ROW])
            with mock.patch.object(candidates, "CANDIDATE_CSVS", pools):
                skipped: list[str] = []
                subset = dresearch.candidate_subset(facts, {}, worth_skipped=skipped)
                self.assertEqual([row["candidate_key"] for row in subset],
                                 ["candidate:email:cass@x.com"])
                self.assertEqual(skipped, ["candidate:phone:+14155551234"])
                # User No removes a model Yes; user Yes rescues a model No.
                skipped = []
                subset = dresearch.candidate_subset(
                    facts, {
                        "candidate:email:cass@x.com": {"network_worth": "no"},
                        "candidate:phone:+14155551234": {"network_worth": "yes"},
                    },
                    worth_skipped=skipped)
                self.assertEqual([row["candidate_key"] for row in subset],
                                 ["candidate:phone:+14155551234"])
                self.assertEqual(skipped, ["candidate:email:cass@x.com"])

    def test_candidate_already_merged_with_existing_person_skips_lookup(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts = base / "facts"
            facts.mkdir()
            pid = "candidate:email:cass@x.com"
            (facts / f"{pid}.jsonl").write_text(
                _facts_record("yes", "real relationship") + "\n", encoding="utf-8")
            pools = _pools(base, [GMAIL_ROW], [])
            with mock.patch.object(candidates, "CANDIDATE_CSVS", pools):
                self.assertEqual(
                    dresearch.candidate_subset(
                        facts, {}, resolved_candidates={pid}, worth_skipped=[]),
                    [],
                )


class TestCandidateParentConsolidation(unittest.TestCase):
    def test_merged_candidate_contacts_fold_onto_existing_link(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            people = base / "people.csv"
            people.write_text(
                "id,public_identifier,linkedin_url,primary_email,all_emails,primary_phone,all_phones,interaction_counts,source_channels\n"
                'person-cass,cass,https://www.linkedin.com/in/cass,cass@work.com,"[""cass@work.com""]",,,,linkedin_csv\n',
                encoding="utf-8",
            )
            pools = _pools(base, [GMAIL_ROW], [])
            tasks = [{
                "parent_slug": "cass-parent",
                "candidate_key": "cass",
                "person_ids": ["person-cass"],
                "parent_person_ids": ["person-cass", "candidate:email:cass@x.com"],
                "action": "confirm",
                "linkedin": {"linkedin_url": "https://www.linkedin.com/in/cass"},
            }]
            out = base / "consolidate.csv"
            with mock.patch.object(candidates, "CANDIDATE_CSVS", pools):
                stats = reconcile.write_consolidations(out, tasks, people)
            self.assertEqual(stats["consolidated_parents"], 1)
            with out.open(newline="", encoding="utf-8") as fh:
                row = next(csv.DictReader(fh))
            self.assertIn("cass@work.com", row["all_emails"])
            self.assertIn("cass@x.com", row["all_emails"])
            self.assertIn("gmail_msgvault", row["source_channels"])


class TestAssembleWorthGate(unittest.TestCase):
    def _profile(self) -> dict:
        return {
            "person": {"full_name": "Cass Doe", "first_name": "Cass", "last_name": "Doe", "confidence": 0.9},
            "location": {"city": "Oakland", "country": "United States", "raw": ""},
            "headline": {"text": "builder"}, "summary": {"text": "s"},
            "positions": [{"title": "CTO", "company_name": "StealthCo", "is_current": True}],
            "education": [], "social": {"linkedin_url": None},
            "metadata": {"estimated_completeness": 0.7, "gaps": []},
        }

    def _run(self, base: Path, pools: list[Path], review: Path) -> dict:
        research = base / "research"
        common.write_json(research / "cass-doe-parentab" / "01_research_parallel.json", self._profile())
        queue = base / "research_queue.csv"
        queue.write_text("handle,display_name,primary_email,phone_e164,source_channel\n"
                         "cass-doe-parentab,Cass Doe,cass@x.com,,email\n", encoding="utf-8")
        people = base / "people.csv"
        people.write_text("id,primary_email,primary_phone\n", encoding="utf-8")
        buf = io.StringIO()
        with mock.patch.object(candidates, "CANDIDATE_CSVS", pools), \
             mock.patch.object(asp, "LINKEDIN_OVERRIDES_CSV", review), \
             contextlib.redirect_stdout(buf):
            asp.main(["--research-dir", str(research), "--queue-csv", str(queue),
                      "--people-csv", str(people), "--out", str(base / "synthetic-people.csv")])
        return json.loads(buf.getvalue())

    def test_no_candidate_is_never_minted_yes_still_is(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            pools = _pools(base, [GMAIL_ROW], [])
            review = base / "review.csv"
            web.apply_worth_decision(review, "candidate:email:cass@x.com", "no")
            man = self._run(base, pools, review)
            self.assertEqual((man["built"], man["skipped_worth_no"]), (0, 1))
            with (base / "synthetic-people.csv").open(newline="", encoding="utf-8") as fh:
                self.assertEqual(list(csv.DictReader(fh)), [])                           # nothing minted
            # flip the mark to yes -> the same research result mints a row
            web.apply_worth_decision(review, "candidate:email:cass@x.com", "yes")
            man = self._run(base, pools, review)
            self.assertEqual((man["built"], man["skipped_worth_no"]), (1, 0))
            with (base / "synthetic-people.csv").open(newline="", encoding="utf-8") as fh:
                (row,) = list(csv.DictReader(fh))
            self.assertEqual(row["id"], "candidate:email:cass@x.com")

    def test_current_empty_queue_prunes_machine_rows_but_preserves_user_gates(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            research = base / "research"
            research.mkdir()
            queue = base / "research_queue.csv"
            queue.write_text("handle,display_name\n", encoding="utf-8")
            out = base / "synthetic-people.csv"
            asp.write_rows(out, {
                "synth-x-old-auto": {
                    "id": "candidate:email:auto@x.com",
                    "public_identifier": "synth-x-old-auto",
                    "full_name": "Old Auto", "enrichment_provider": "synthetic",
                    "source_parent_slug": "old-auto", "approved": "auto",
                },
                "synth-x-user-kept": {
                    "id": "candidate:email:kept@x.com",
                    "public_identifier": "synth-x-user-kept",
                    "full_name": "User Kept", "enrichment_provider": "synthetic",
                    "source_parent_slug": "user-kept", "approved": "yes",
                },
            })
            people = base / "people.csv"
            people.write_text("id,primary_email,primary_phone\n", encoding="utf-8")
            review = base / "review.csv"
            with mock.patch.object(asp, "LINKEDIN_OVERRIDES_CSV", review), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                asp.main([
                    "--research-dir", str(research), "--queue-csv", str(queue),
                    "--people-csv", str(people),
                    "--verdicts-jsonl", str(base / "verdicts.jsonl"),
                    "--out", str(out),
                ])
            result = json.loads(output.getvalue())
            rows = asp.load_rows(out)
            self.assertEqual(set(rows), {"synth-x-user-kept"})
            self.assertEqual(result["pruned_stale_machine_rows"], 1)


class TestWorthCarryForward(unittest.TestCase):
    """The USER-owned network_worth column survives every machine rewrite of a
    non-user-approved row (write_overrides rebuilds + retarget upserts)."""

    def test_machine_rebuild_preserves_user_worth_mark(self):
        with tempfile.TemporaryDirectory() as d:
            review = Path(d) / "review.csv"
            web.apply_worth_decision(review, "janedoe", "yes")   # mark a PENDING row
            task = {"no_link": False, "candidate_key": "janedoe", "action": "review",
                    "person_ids": ["pid-1"], "match_emails": [], "match_phones": [],
                    "linkedin": {"linkedin_url": "https://www.linkedin.com/in/janedoe"},
                    "verdict": {"verdict": "needs_review", "confidence": 0.5, "reason": "r"}}
            reconcile.write_overrides(review, [task])
            row = reconcile.load_override_rows(review)["janedoe"]
            self.assertEqual(row["source"], "deep-context-reconcile")  # the machine rebuilt the row…
            self.assertEqual(row["approved"], "")                       # …it is NOT user-approved…
            self.assertEqual(row["network_worth"], "yes")               # …but the mark carried forward
            rows = reconcile.load_override_rows(review)
            rows["janedoe"].update({
                "llm_worth": "yes",
                "llm_worth_reason": "genuine relationship",
                "llm_reject": "spam",
                "llm_reject_confidence": "0.875",
                "llm_reject_reason": "machine signal",
            })
            reconcile._write_override_rows(review, rows)
            reconcile.upsert_retargets(review, [{
                "old_public_identifier": "janedoe",
                "new_linkedin_url": "https://www.linkedin.com/in/jane-real"}])
            row = reconcile.load_override_rows(review)["janedoe"]
            self.assertEqual(row["action"], "retarget")
            self.assertEqual(row["network_worth"], "yes")               # retarget upsert too
            self.assertEqual(row["llm_worth"], "yes")
            self.assertEqual(row["llm_worth_reason"], "genuine relationship")
            self.assertEqual(row["llm_reject"], "spam")
            self.assertEqual(row["llm_reject_confidence"], "0.875")
            self.assertEqual(row["llm_reject_reason"], "machine signal")


class TestReviewWebWorth(unittest.TestCase):
    def test_worth_mark_writes_and_reset_clears(self):
        with tempfile.TemporaryDirectory() as d:
            review = Path(d) / "review.csv"
            pid = "candidate:email:cass@x.com"
            self.assertEqual(web.apply_worth_decision(review, pid, "no"), {"network_worth": "no"})
            row = reconcile.load_override_rows(review)[pid]
            self.assertEqual(row["network_worth"], "no")
            self.assertEqual((row["action"], row["approved"]), ("", ""))  # worth is not a link decision
            # marking worth never clobbers an existing decision…
            web.apply_decision(review, Path(d) / "verdicts.jsonl", pid, "detach", "", 0.85)
            web.apply_worth_decision(review, pid, "maybe")
            row = reconcile.load_override_rows(review)[pid]
            self.assertEqual((row["action"], row["approved"], row["network_worth"]),
                             ("detach", "yes", "maybe"))
            # …and ↺ (empty mark) clears it back to the LLM/default
            web.apply_worth_decision(review, pid, "")
            self.assertEqual(reconcile.load_override_rows(review)[pid]["network_worth"], "")
            with self.assertRaises(ValueError):
                web.apply_worth_decision(review, pid, "sometimes")
            with self.assertRaises(ValueError):
                web.apply_worth_decision(review, "", "yes")


class TestReviewWebCandidateRows(unittest.TestCase):
    def _fixture(self, base: Path) -> tuple[Path, list[Path]]:
        facts = base / "facts"
        facts.mkdir()
        (facts / "candidate:email:cass@x.com.jsonl").write_text(
            _facts_record("yes", "strong founder", name="Cassandra Doe") + "\n", encoding="utf-8")
        (facts / "candidate:phone:+14155551234.jsonl").write_text(
            _facts_record("no", "delivery updates") + "\n", encoding="utf-8")
        return facts, _pools(base, [GMAIL_ROW], [PHONE_ROW])

    def test_candidate_rows_shape_worth_grouping_and_filters(self):
        with tempfile.TemporaryDirectory() as d:
            facts, pools = self._fixture(Path(d))
            (facts / "candidate:email:cass@x.com.jsonl").write_text(json.dumps({
                "facts": {
                    "canonical_name": "Cassandra Doe",
                    "network_worth": {"decision": "yes", "reason": "strong founder"},
                    "identifiers": ["cass@x.com", "alias@example.com", "not an email"],
                }
            }) + "\n", encoding="utf-8")
            with mock.patch.object(candidates, "CANDIDATE_CSVS", pools):
                parents = web.load_candidate_parents(facts, {}, set())
            web.annotate_worth(parents, {}, facts)
            by = {p["person_ids"][0]: p for p in parents}
            cass, tex = by["candidate:email:cass@x.com"], by["candidate:phone:+14155551234"]
            self.assertEqual(cass["name"], "Cassandra Doe")                # canonical name from facts
            self.assertEqual(cass["sources"], ["gmail"])
            self.assertEqual(tex["sources"], ["imessage", "whatsapp"])
            cand = cass["candidates"][0]
            self.assertTrue(cand["import_candidate"])
            self.assertEqual(cand["pub"], "candidate:email:cass@x.com")    # review.csv key = person_id
            self.assertEqual(cand["match_emails"],
                             ["cass@x.com", "cd@y.com", "alias@example.com"])
            self.assertEqual(web.candidate_state(cand), "review")          # pending -> Needs review pile
            self.assertEqual((cass["worth"]["decision"], cass["worth"]["source"]), ("yes", "llm"))
            # effective-no groups with Rejected (like spam), not the review pile
            self.assertTrue(web.is_worth_no(tex))
            self.assertTrue(web.parent_in_tab(tex, "rejected"))
            self.assertFalse(web.parent_in_tab(tex, "review"))
            self.assertTrue(web.parent_in_tab(cass, "review"))
            # source + worth filters
            self.assertTrue(web.parent_matches_source(cass, "gmail"))
            self.assertFalse(web.parent_matches_source(cass, "imessage"))
            self.assertTrue(web.parent_matches_source(cass, "all"))
            self.assertTrue(web.parent_matches_worth(cass, "yes"))
            self.assertFalse(web.parent_matches_worth(cass, "no"))
            # The user decision is binary; model yes/maybe/no is display-only advice.
            html = web.render_candidate(0, 1, cand)
            self.assertIn("data-model-worth='yes'", html)
            self.assertIn("data-worth='yes'", html)
            self.assertIn("data-worth='no'", html)
            self.assertNotIn("data-worth='maybe'", html)
            self.assertNotIn("Exclude", html)

    def test_candidate_rows_dedupe_against_shown_person_ids(self):
        with tempfile.TemporaryDirectory() as d:
            facts, pools = self._fixture(Path(d))
            with mock.patch.object(candidates, "CANDIDATE_CSVS", pools):
                parents = web.load_candidate_parents(facts, {}, {"candidate:email:cass@x.com"})
        self.assertEqual([p["person_ids"][0] for p in parents], ["candidate:phone:+14155551234"])


def _old_needs_worth_review(parent: dict) -> bool:
    """The pre-fix predicate: required is_import_candidate_parent, so an auto-verified
    machine-Maybe candidate (import_candidate now False) fell out of every worth tab."""
    machine = str((parent.get("machine_worth") or {}).get("decision") or "maybe").lower()
    return (web.is_worth_subject(parent)
            and web.is_import_candidate_parent(parent)
            and not web.is_effective_no(parent)
            and machine == "maybe"
            and web.explicit_worth(parent) not in web.USER_WORTH_VALUES)


class TestLimboMaybeVisibility(unittest.TestCase):
    """Part 1: a machine-Maybe candidate whose LinkedIn was auto-verified at the link level
    (approved=auto) keeps its candidate-id worth key, so it is still a standalone contact whose
    add/no decision is unmade. Before the fix it flipped is_import_candidate_parent off and
    vanished from every worth tab (the digest still counted it); now it stays reviewable, while
    plain pending candidates, user marks, machine yes/no, and effective-no rows are unchanged."""

    # 10 fictional people: (candidate_key, action, approved, machine_worth, reason, extra).
    PEOPLE = [
        # 3 LIMBO maybes: an auto link-level decision, machine worth still Maybe.
        ("email:ada@example.com", "verify", "auto", "maybe", "real person but no professional context", {}),
        ("email:bram@example.net", "verify", "auto", "maybe", "real person but no professional context", {}),
        ("email:cleo@example.com", "retarget", "auto", "maybe", "real person but no professional context", {}),
        # plain pending candidate (no identity decision) — visible before AND after.
        ("email:dara@example.net", "", "", "maybe", "no professional context", {}),
        # user marks — terminal, never in the queue.
        ("email:evan@example.com", "verify", "auto", "maybe", "x", {"network_worth": "yes"}),
        ("email:faye@example.net", "verify", "auto", "maybe", "x", {"network_worth": "no"}),
        # machine yes — not Maybe, not in the queue.
        ("email:gil@example.com", "verify", "auto", "yes", "clearly worth adding", {}),
        # machine no — effective-no, not in the queue.
        ("email:hana@example.net", "verify", "auto", "no", "automated vendor", {}),
        # verified but spam-flagged — effective-no despite a Maybe worth.
        ("email:ivan@example.com", "verify", "auto", "maybe", "cold recruiter",
         {"llm_reject": "spam", "llm_reject_confidence": "0.950", "llm_reject_reason": "cold recruiter"}),
        # a second plain pending candidate — visible before AND after.
        ("email:juno@example.net", "", "", "maybe", "no professional context", {}),
    ]
    LIMBO = ["candidate:email:ada@example.com", "candidate:email:bram@example.net",
             "candidate:email:cleo@example.com"]
    ALWAYS_VISIBLE = ["candidate:email:dara@example.net", "candidate:email:juno@example.net"]
    NEVER_VISIBLE = ["candidate:email:evan@example.com", "candidate:email:faye@example.net",
                     "candidate:email:gil@example.com", "candidate:email:hana@example.net",
                     "candidate:email:ivan@example.com"]

    def _build(self, base: Path):
        facts = base / "facts"
        facts.mkdir()
        cand_rows: list[dict] = []
        overrides: dict[str, dict] = {}
        for key, action, approved, worth, reason, extra in self.PEOPLE:
            pid = f"candidate:{key}"
            handle = key.split(":", 1)[1].split("@", 1)[0]
            (facts / f"{pid}.jsonl").write_text(
                _facts_record(worth, reason, name=handle.title()) + "\n", encoding="utf-8")
            cand_rows.append({
                "candidate_key": key, "source": "gmail", "full_name": handle.title(),
                "primary_email": key.split(":", 1)[1], "all_emails": [key.split(":", 1)[1]],
            })
            row = {**{col: "" for col in reconcile.OVERRIDE_COLUMNS},
                   "public_identifier": pid, "person_id": pid,
                   "action": action, "approved": approved,
                   "llm_worth": worth, "llm_worth_reason": reason, **extra}
            if action == "verify":
                row["linkedin_url"] = f"https://www.linkedin.com/in/{handle}-li"
            elif action == "retarget":
                row["new_linkedin_url"] = f"https://www.linkedin.com/in/{handle}-li"
                row["new_public_identifier"] = f"{handle}-li"
            overrides[pid] = row
        pools = [_write_candidates_csv(base / "gmail" / "candidates.csv", cand_rows),
                 _write_candidates_csv(base / "messages" / "candidates.csv", [])]
        review = base / "review.csv"
        reconcile._write_override_rows(review, overrides)
        with mock.patch.object(candidates, "CANDIDATE_CSVS", pools):
            parents = web.load_candidate_parents(
                facts, reconcile.load_override_rows(review), set(), resolved_candidates=set())
        web.annotate_worth(parents, reconcile.load_override_rows(review), facts)
        return facts, parents

    def test_limbo_maybes_hidden_before_visible_after(self):
        with tempfile.TemporaryDirectory() as d:
            _, parents = self._build(Path(d))
            by = {p["person_ids"][0]: p for p in parents}
            self.assertEqual(len(parents), 10)
            for pid in self.LIMBO:
                parent = by[pid]
                # A verified limbo person is a worth subject but no longer import_candidate.
                self.assertTrue(web.is_worth_subject(parent), pid)
                self.assertFalse(web.is_import_candidate_parent(parent), pid)
                self.assertEqual(parent["machine_worth"]["decision"], "maybe", pid)
                # Hidden before the fix, visible after it.
                self.assertFalse(_old_needs_worth_review(parent), f"{pid} should be hidden before")
                self.assertTrue(web.needs_worth_review(parent), f"{pid} should be visible after")
            for pid in self.ALWAYS_VISIBLE:  # plain pending candidates unchanged
                self.assertTrue(_old_needs_worth_review(by[pid]), pid)
                self.assertTrue(web.needs_worth_review(by[pid]), pid)
            for pid in self.NEVER_VISIBLE:   # user marks / machine yes-no / effective-no unchanged
                self.assertFalse(web.needs_worth_review(by[pid]), pid)

    def test_pending_count_jumps_and_digest_symmetry_holds(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            _, parents = self._build(base)
            # worth_pending now surfaces the 3 limbo + 2 plain pending maybes.
            progress = web.review_progress(parents)
            self.assertEqual(progress["worth_pending"], 5)
            # The pre-fix count would have shown only the 2 plain pending candidates.
            old_pending = sum(1 for parent in parents if _old_needs_worth_review(parent))
            self.assertEqual(old_pending, 2)
            # The worth-selection digest counts is_worth_subject rows by decision and is UNCHANGED
            # by Part 1 (it never depended on needs_worth_review) — so the review-side and
            # enrichment-side digests stay identical and cannot drift by the surfaced people.
            manifest = base / "review" / "manifest.json"
            review_side = web.worth_selection_from_parents(parents, manifest_path=manifest)
            enrichment_side = web.worth_selection_from_parents(parents, manifest_path=manifest)
            self.assertEqual(review_side, enrichment_side)
            self.assertEqual(review_side["sha256"], enrichment_side["sha256"])
            # Every candidate here is a worth subject, so the digest counted them all along —
            # the limbo people were in `total`/`maybe` even while no tab showed them.
            self.assertEqual(review_side["total"], 10)
            self.assertEqual(review_side["maybe"], 6)   # ada,bram,cleo,dara,juno,ivan


class TestComposeNetworkWorth(unittest.TestCase):
    def test_merge_carries_last_worth_and_dossier_renders_it(self):
        chunks = [json.loads(_facts_record("maybe", "early read", name="Cass Doe")),
                  json.loads(_facts_record("yes", "founder with real traction", name="Cass Doe"))]
        merged = compose.merge_facts(chunks)
        self.assertEqual(merged["network_worth"],
                         {"decision": "yes", "reason": "founder with real traction"})
        md = compose.render_dossier({"person_id": "p1", "full_name": "Cass Doe"}, merged)
        self.assertIn("**Network worth:** yes — founder with real traction", md)
        # absent worth -> no line
        merged = compose.merge_facts([json.loads(_facts_record(name="Cass Doe"))])
        self.assertEqual(merged["network_worth"], {})
        md = compose.render_dossier({"person_id": "p1", "full_name": "Cass Doe"}, merged)
        self.assertNotIn("Network worth", md)


class TestLlmWorthColumns(unittest.TestCase):
    """The machine-owned llm_worth/llm_worth_reason columns: written from facts or the
    spam screen, ALWAYS refreshed (sticky rows included), never touching the
    user-owned network_worth mark."""

    def _task(self, pub: str, pid: str, spam: bool = False, spam_conf: float = 0.9) -> dict:
        return {"no_link": False, "candidate_key": pub, "action": "review",
                "person_ids": [pid], "match_emails": [], "match_phones": [],
                "linkedin": {"linkedin_url": f"https://www.linkedin.com/in/{pub}"},
                "verdict": {"verdict": "needs_review", "confidence": 0.5, "reason": "r",
                            "spam_contact": spam, "spam_confidence": spam_conf,
                            "spam_reason": "cold outreach" if spam else ""}}

    def test_fresh_row_mirrors_facts_worth(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts = base / "facts"
            facts.mkdir()
            (facts / "pid-1.jsonl").write_text(_facts_record("no", "vendor") + "\n", encoding="utf-8")
            review = base / "review.csv"
            reconcile.write_overrides(review, [self._task("janedoe", "pid-1"),
                                               self._task("ghost", "pid-ghost")], facts)
            rows = reconcile.load_override_rows(review)
            self.assertEqual((rows["janedoe"]["llm_worth"], rows["janedoe"]["llm_worth_reason"]),
                             ("no", "vendor"))
            self.assertEqual(rows["janedoe"]["network_worth"], "")     # user column stays user-owned
            # no facts + no spam -> the machine columns stay blank
            self.assertEqual((rows["ghost"]["llm_worth"], rows["ghost"]["llm_worth_reason"]), ("", ""))

    def test_confident_spam_is_an_llm_no_and_keeps_reject_detail(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts = base / "facts"
            facts.mkdir()
            review = base / "review.csv"
            reconcile.write_overrides(review, [self._task("spammy", "pid-2", spam=True),
                                               self._task("softspam", "pid-3", spam=True, spam_conf=0.5)],
                                      facts)
            rows = reconcile.load_override_rows(review)
            self.assertEqual((rows["spammy"]["llm_worth"], rows["spammy"]["llm_worth_reason"]),
                             ("no", "cold outreach"))                  # spam is one way the LLM says no
            self.assertEqual(rows["spammy"]["llm_reject"], "spam")     # detail columns kept as-is
            # below the bar the flag stays informational: worth falls back to facts (blank here)
            self.assertEqual(rows["softspam"]["llm_worth"], "")
            self.assertEqual(rows["softspam"]["llm_reject"], "spam")

    def test_sticky_user_row_refreshes_llm_worth_without_touching_user_columns(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts = base / "facts"
            facts.mkdir()
            (facts / "pid-1.jsonl").write_text(_facts_record("no", "vendor") + "\n", encoding="utf-8")
            review = base / "review.csv"
            reconcile._write_override_rows(review, {"janedoe": {
                **{k: "" for k in reconcile.OVERRIDE_COLUMNS},
                "public_identifier": "janedoe", "action": "verify", "approved": "yes",
                "network_worth": "yes"}})
            reconcile.write_overrides(review, [self._task("janedoe", "pid-1")], facts)
            row = reconcile.load_override_rows(review)["janedoe"]
            self.assertEqual((row["action"], row["approved"]), ("verify", "yes"))  # decision untouched
            self.assertEqual(row["network_worth"], "yes")                          # user mark untouched
            self.assertEqual((row["llm_worth"], row["llm_worth_reason"]), ("no", "vendor"))

    def test_no_link_candidates_get_fresh_machine_worth_and_keep_human_decisions(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts = base / "facts"
            facts.mkdir()
            review = base / "review.csv"
            human_pid = "candidate:email:human@example.com"
            untouched_pid = "candidate:email:untouched@example.com"
            stable_pid = "candidate:email:stable@example.com"
            reconcile._write_override_rows(review, {
                human_pid: {
                    **{k: "" for k in reconcile.OVERRIDE_COLUMNS},
                    "public_identifier": human_pid,
                    "person_id": human_pid,
                    "network_worth": "yes",
                    "llm_worth": "maybe",
                },
                stable_pid: {
                    **{k: "" for k in reconcile.OVERRIDE_COLUMNS},
                    "public_identifier": stable_pid,
                    "person_id": stable_pid,
                    "llm_worth": "yes",
                    "llm_worth_reason": "already decisive",
                },
            })
            task = {
                "no_link": True,
                "person_ids": [human_pid, untouched_pid, stable_pid],
                "worth_person_ids": [untouched_pid],
                "verdict": {
                    "spam_contact": False,
                    "spam_confidence": 0.0,
                    "spam_reason": "",
                    "network_worth": {
                        "decision": "no",
                        "reason": "automated marketing sender",
                    },
                },
            }
            stats = reconcile.write_overrides(review, [task], facts)
            rows = reconcile.load_override_rows(review)
            self.assertEqual(stats["worth_refreshed"], 1)
            self.assertEqual(rows[human_pid]["network_worth"], "yes")
            self.assertEqual(rows[human_pid]["llm_worth"], "maybe")
            self.assertEqual(rows[untouched_pid]["network_worth"], "")
            self.assertEqual(
                (rows[untouched_pid]["llm_worth"], rows[untouched_pid]["llm_worth_reason"]),
                ("no", "automated marketing sender"),
            )
            self.assertEqual(
                (rows[stable_pid]["llm_worth"], rows[stable_pid]["llm_worth_reason"]),
                ("yes", "already decisive"),
            )

            task["verdict"]["network_worth"] = {
                "decision": "yes",
                "reason": "real professor relationship",
            }
            reconcile.write_overrides(review, [task], facts)
            rows = reconcile.load_override_rows(review)
            self.assertEqual(rows[human_pid]["network_worth"], "yes")
            self.assertEqual(rows[human_pid]["llm_worth"], "maybe")
            self.assertEqual(rows[untouched_pid]["llm_worth"], "yes")
            self.assertEqual(rows[stable_pid]["llm_worth"], "yes")


def _verdict_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


class TestUnifiedRejected(unittest.TestCase):
    """Worth buttons on plain verdict rows + the unified effective-no Rejected
    grouping (== what the fan-in merge drops), with rescue in both directions."""

    VERDICT = {"parent_slug": "bob-jones", "name": "Bob Jones", "candidate_key": "bob-1",
               "person_ids": ["pid-bob"], "conflict": False, "no_link": False,
               "linkedin": {"linkedin_url": "https://www.linkedin.com/in/bob-1", "has_profile": True},
               "verdict": {"verdict": "needs_review", "confidence": 0.0, "reason": "thin"}, "error": ""}

    def test_verdict_row_worth_round_trip_and_rejected_grouping(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts = base / "facts"
            facts.mkdir()
            (facts / "pid-bob.jsonl").write_text(_facts_record("no", "pure vendor thread") + "\n",
                                                 encoding="utf-8")
            review = base / "review.csv"
            reconcile.write_overrides(review, [dict(self.VERDICT, action="review")], facts)
            verdicts = _verdict_jsonl(base / "verdicts.jsonl", [self.VERDICT])
            empty_facts = base / "nofacts"
            empty_facts.mkdir()
            parents, overrides = web.build_parents(verdicts, review)
            web.annotate_worth(parents, overrides, empty_facts)  # UI must work from llm_worth alone
            (p,) = parents
            cand = p["candidates"][0]
            self.assertEqual(cand["worth_key"], "bob-1")         # verdict rows key worth by their pub
            self.assertEqual((p["worth"]["decision"], p["worth"]["source"]), ("no", "llm"))
            self.assertTrue(web.is_effective_no(p))
            self.assertTrue(web.parent_in_tab(p, "rejected"))
            self.assertFalse(web.parent_in_tab(p, "review"))
            html = web.render_candidate(0, 1, cand)
            self.assertIn("data-model-worth='no'", html)
            self.assertIn("data-worth='yes'", html)              # binary rescue remains available
            self.assertNotIn("data-worth='maybe'", html)
            # a user Yes rescues: round-trips through the /worth writer + live rejected state
            web.apply_worth_decision(review, "bob-1", "yes")
            rows = reconcile.load_override_rows(review)
            self.assertEqual(rows["bob-1"]["network_worth"], "yes")
            self.assertEqual(rows["bob-1"]["llm_worth"], "no")   # the machine column is untouched
            self.assertFalse(web.effective_no_for_key("bob-1", rows, empty_facts)["rejected"])
            parents, overrides = web.build_parents(verdicts, review)
            web.annotate_worth(parents, overrides, empty_facts)
            self.assertFalse(web.is_effective_no(parents[0]))    # evicted from Rejected
            # clearing the mark brings the machine no back — nothing destructive
            web.apply_worth_decision(review, "bob-1", "")
            self.assertTrue(web.effective_no_for_key(
                "bob-1", reconcile.load_override_rows(review), empty_facts)["rejected"])

    def test_keep_click_rescues_a_machine_no(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts = base / "facts"
            facts.mkdir()
            review = base / "review.csv"
            verdicts = _verdict_jsonl(base / "verdicts.jsonl", [self.VERDICT])
            row = {k: "" for k in reconcile.OVERRIDE_COLUMNS}
            row.update({"public_identifier": "bob-1", "person_id": "pid-bob",
                        "llm_worth": "no", "llm_worth_reason": "vendor"})
            reconcile._write_override_rows(review, {"bob-1": row})
            parents, overrides = web.build_parents(verdicts, review)
            web.annotate_worth(parents, overrides, facts)
            self.assertTrue(web.is_effective_no(parents[0]))
            web.apply_decision(review, verdicts, "bob-1", "keep", "", 0.7)   # keep-ish rescue
            self.assertFalse(web.effective_no_for_key(
                "bob-1", reconcile.load_override_rows(review), facts)["rejected"])
            parents, overrides = web.build_parents(verdicts, review)
            web.annotate_worth(parents, overrides, facts)
            self.assertFalse(web.is_effective_no(parents[0]))

    def test_no_link_row_keys_worth_by_person_id(self):
        # The acceptance case: a plain "no link chosen" verdict row still gets worth
        # buttons, keyed by its person_id, with the LLM judgment read from facts.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts = base / "facts"
            facts.mkdir()
            (facts / "pid-leon.jsonl").write_text(_facts_record("no", "newsletter blasts") + "\n",
                                                  encoding="utf-8")
            verdicts = _verdict_jsonl(base / "verdicts.jsonl", [{
                "parent_slug": "leon-james", "name": "Leon James", "candidate_key": "",
                "person_ids": ["pid-leon"], "conflict": False, "no_link": True,
                "linkedin": {}, "verdict": {"verdict": "needs_review", "confidence": 0.0,
                                            "reason": "no usable LinkedIn profile"}, "error": ""}])
            parents, overrides = web.build_parents(verdicts, base / "review.csv")
            web.annotate_worth(parents, overrides, facts)
            (p,) = parents
            self.assertEqual(p["candidates"][0]["worth_key"], "pid-leon")    # falls back to person_id
            self.assertEqual((p["worth"]["decision"], p["worth"]["source"]), ("no", "llm"))
            self.assertTrue(web.parent_in_tab(p, "rejected"))
            self.assertFalse(web.parent_in_tab(p, "review"))
            rendered = web.render_candidate(0, 1, p["candidates"][0])
            self.assertIn("data-worth='yes'", rendered)
            self.assertIn("data-worth='no'", rendered)

    def test_no_link_import_candidate_remains_in_maybe_review_queue(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts = base / "facts"
            facts.mkdir()
            pid = "candidate:email:sharon@example.com"
            (facts / f"{pid}.jsonl").write_text(
                _facts_record("maybe", "unclear one-off introduction") + "\n",
                encoding="utf-8",
            )
            review = base / "review.csv"
            reconcile._write_override_rows(review, {pid: {
                **{key: "" for key in reconcile.OVERRIDE_COLUMNS},
                "public_identifier": pid,
                "person_id": pid,
                "llm_worth": "maybe",
                "llm_worth_reason": "unclear one-off introduction",
            }})
            verdicts = _verdict_jsonl(base / "verdicts.jsonl", [{
                "parent_slug": "sharon-parent", "name": "Sharon",
                "candidate_key": "", "person_ids": [pid],
                "conflict": False, "no_link": True, "linkedin": {},
                "verdict": {
                    "verdict": "needs_review", "confidence": 0.0,
                    "reason": "no LinkedIn attached",
                },
                "error": "",
            }])
            parents, overrides = web.build_parents(verdicts, review)
            web.annotate_worth(parents, overrides, facts)
            (parent,) = parents
            candidate = parent["candidates"][0]
            self.assertEqual(candidate["pub"], pid)
            self.assertTrue(candidate["import_candidate"])
            self.assertTrue(web.is_worth_subject(parent))
            self.assertTrue(web.needs_worth_review(parent))


class TestExcludeIsUnifiedNo(unittest.TestCase):
    def test_excluded_row_lands_in_rejected_and_worth_yes_rescues(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts = base / "facts"
            facts.mkdir()
            review = base / "review.csv"
            verdicts = _verdict_jsonl(base / "verdicts.jsonl", [TestUnifiedRejected.VERDICT])
            web.apply_decision(review, verdicts, "bob-1", "exclude", "", 0.7)
            parents, overrides = web.build_parents(verdicts, review)
            web.annotate_worth(parents, overrides, facts)
            (p,) = parents
            # an approved exclude reads as a user no everywhere
            self.assertEqual((p["worth"]["decision"], p["worth"]["source"]), ("no", "user"))
            self.assertTrue(web.is_effective_no(p))
            self.assertTrue(web.parent_in_tab(p, "rejected"))
            self.assertIn("data-model-worth='no'", web.render_candidate(0, 1, p["candidates"][0]))
            # worth-Yes rescues AND clears the exclude so both stores agree
            web.apply_worth_decision(review, "bob-1", "yes")
            row = reconcile.load_override_rows(review)["bob-1"]
            self.assertEqual((row["action"], row["approved"], row["network_worth"]), ("", "", "yes"))
            parents, overrides = web.build_parents(verdicts, review)
            web.annotate_worth(parents, overrides, facts)
            self.assertFalse(web.is_effective_no(parents[0]))


class TestSyntheticWorthGateSync(unittest.TestCase):
    """A worth mark on a synthetic row mirrors onto its approved mint gate:
    No == Detach, Yes == Keep, ↺ restores pending; maybe leaves the gate alone."""

    CSV_HEADER = "id,public_identifier,full_name,enrichment_provider,approved\n"

    def _path(self, base: Path, approved: str = "auto") -> Path:
        path = base / "synthetic-people.csv"
        path.write_text(self.CSV_HEADER
                        + f"pid-9,synth-email-abc,Ross Nordeen,synthetic,{approved}\n",
                        encoding="utf-8")
        return path

    def _approved(self, path: Path) -> str:
        with path.open(newline="", encoding="utf-8") as fh:
            (row,) = list(csv.DictReader(fh))
        return row["approved"]

    def test_worth_marks_mirror_the_approved_gate(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._path(Path(d))
            self.assertEqual(web.synthetic_worth_key(path, "synth-email-abc"), "pid-9")
            self.assertEqual(web.synthetic_worth_key(path, "synth-ghost"), "")
            self.assertEqual(web.sync_synthetic_gate(path, "pid-9", "no"),
                             {"action": "verify", "approved": "no"})
            self.assertEqual(self._approved(path), "no")           # No == Detach: mint gate agrees
            self.assertEqual(web.sync_synthetic_gate(path, "pid-9", "yes"),
                             {"action": "verify", "approved": "yes"})
            self.assertEqual(self._approved(path), "yes")          # Yes == Keep
            # maybe is not a gate decision: state is returned for the repaint,
            # but the approved gate is left untouched
            self.assertEqual(
                web.sync_synthetic_gate(path, "pid-9", "maybe"),
                {"action": "verify", "approved": "yes"},
            )
            self.assertEqual(self._approved(path), "yes")
            self.assertEqual(web.sync_synthetic_gate(path, "pid-9", ""),
                             {"action": "verify", "approved": ""})
            self.assertEqual(self._approved(path), "")             # ↺ restores pending
            self.assertIsNone(web.sync_synthetic_gate(path, "pid-ghost", "no"))

    def test_detached_synthetic_row_is_effective_no(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts = base / "facts"
            facts.mkdir()
            parents = web.load_synthetic_parents(self._path(base, approved="no"))
            web.annotate_worth(parents, {}, facts)
            self.assertTrue(web.is_effective_no(parents[0]))       # gate no == unified Rejected
            self.assertTrue(web.parent_in_tab(parents[0], "rejected"))

    def test_legacy_handle_only_synthetic_recovers_existing_parent_dossier(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            path = base / "synthetic-people.csv"
            path.write_text(
                self.CSV_HEADER
                + "pid-9,synth-x-ross-nordeen-parent12,Ross Nordeen,synthetic,\n",
                encoding="utf-8",
            )
            parents_dir = base / "parents"
            parents_dir.mkdir()
            (parents_dir / "ross-nordeen-parent12.md").write_text(
                "# Ross Nordeen (canonical)\n", encoding="utf-8")
            (parent,) = web.load_synthetic_parents(path, parents_dir)
        self.assertEqual(parent["dossier_slug"], "ross-nordeen-parent12")

    def test_candidate_synthetic_recovers_composed_child_dossier(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            path = base / "synthetic-people.csv"
            person_id = "candidate:email:annmay.yang@example.com"
            columns = [
                "id", "public_identifier", "full_name", "enrichment_provider",
                "approved", "source_parent_slug", "source_person_ids",
                "source_candidate_public_identifier",
            ]
            with path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=columns)
                writer.writeheader()
                writer.writerow({
                    "id": person_id,
                    "public_identifier": "synth-email-annmay",
                    "full_name": "Annmay Yang",
                    "enrichment_provider": "synthetic",
                    "approved": "",
                    "source_parent_slug": "annmay-yang-parenta6",
                    "source_person_ids": json.dumps([person_id]),
                    "source_candidate_public_identifier": person_id,
                })
            parents_dir = base / "parents"
            dossier_dir = base / "dossiers"
            facts_dir = base / "facts"
            parents_dir.mkdir()
            dossier_dir.mkdir()
            facts_dir.mkdir()
            child_slug = web.slugify("Annmay Yang", person_id)
            (dossier_dir / f"{child_slug}.md").write_text(
                "# Annmay Yang\n", encoding="utf-8")
            (parent,) = web.load_synthetic_parents(
                path, parents_dir, dossier_dir, facts_dir)
        self.assertEqual(parent["dossier_slug"], child_slug)


class TestStagedReviewUI(unittest.TestCase):
    def _candidate_parent(self, decision: str = "maybe", source: str = "llm") -> dict:
        worth = {"decision": decision, "source": source, "reason": "useful relationship"}
        candidate = {
            "pub": "candidate:email:ada@example.com", "full_name": "Ada Lovelace",
            "import_candidate": True, "synthetic": False, "approved": "", "action": "",
            "confidence": 0.0, "verdict": "no_linkedin_candidate", "worth": worth,
            "machine_worth": worth, "worth_key": "candidate:email:ada@example.com",
            "match_emails": ["ada@example.com"], "match_phones": [],
            "supporting": [], "contradicting": [], "reason": "",
        }
        return {
            "slug": "ada-lovelace", "name": "Ada Lovelace",
            "person_ids": ["candidate:email:ada@example.com"], "sources": ["gmail"],
            "candidates": [candidate], "worth": worth, "machine_worth": worth,
            "connection": False,
        }

    def test_proposed_linkedin_uses_current_research_profile_without_provider_call(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            research = base / "research"
            handle = "ada-lovelace-parent01"
            research.mkdir()
            with (research / "research_queue.csv").open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=dresearch.QUEUE_FIELDS)
                writer.writeheader()
                writer.writerow({
                    "handle": handle,
                    "source_parent_slug": handle,
                    "source_person_ids": json.dumps(["candidate:email:ada@example.com"]),
                    "source_candidate_public_identifier": "candidate:email:ada@example.com",
                })
            notes = "Matched employer, school, and location with complete supporting evidence."
            common.write_json(research / handle / "01_research_parallel.json", {
                "person": {"full_name": "Ada Byron Lovelace"},
                "headline": {"text": "Analytical Engine Pioneer"},
                "location": {"raw": "London, United Kingdom"},
                "positions": [{
                    "title": "Founder", "company_name": "Analytical Engines",
                    "start_date": "1842", "is_current": True,
                }],
                "education": [{"degree": "Mathematics", "school_name": "Home study"}],
                "social": {"linkedin_url": "https://www.linkedin.com/in/ada-lovelace"},
                "metadata": {"research_notes": notes},
            })
            parent = self._candidate_parent("yes", "user")
            parent["candidates"][0].update({
                "profile_pub": "ada-lovelace",
                "url": "https://www.linkedin.com/in/ada-lovelace",
                "new_url": "https://www.linkedin.com/in/ada-lovelace",
                "action": "retarget",
                "approved": "",
            })
            web.hydrate_proposed_profiles(
                [parent], profile_cache_dir=base / "empty-cache", research_dir=research)
            candidate = parent["candidates"][0]
        self.assertEqual(candidate["full_name"], "Ada Byron Lovelace")
        self.assertEqual(candidate["headline"], "Analytical Engine Pioneer")
        self.assertEqual(candidate["location"], "London, United Kingdom")
        self.assertEqual(candidate["experiences"], ["Founder @ Analytical Engines (1842 / present)"])
        self.assertEqual(candidate["education"], ["Mathematics — Home study"])
        self.assertEqual(candidate["reason"], f"deep research: {notes}")

    def test_worth_page_is_one_binary_card_without_legacy_controls(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            html = web.page_html(
                [self._candidate_parent()], {}, base / "review.csv",
                parents_dir=base / "parents", dossier_dir=base / "dossiers",
                manifest_path=base / "review" / "manifest.json",
            ).decode("utf-8")
        self.assertIn("<h1 class='topbar-title'>Add People</h1>", html)
        self.assertIn(
            "class='step active' href='/?stage=worth&amp;preview=1' aria-current='step'",
            html,
        )
        self.assertIn("href='/?stage=enrich&amp;preview=1'", html)
        self.assertIn("class='step' href='/?stage=linkedin&amp;preview=1'", html)
        self.assertNotIn("href='/?stage=done'", html)
        self.assertNotIn("class='title-row'", html)
        self.assertNotIn("Add Ada Lovelace to your network?", html)
        self.assertEqual(html.count("data-worth="), 2)
        self.assertIn("class='decision-card identity-card worth-card'", html)
        self.assertIn("class='identity-scroll'", html)
        self.assertIn("class='identity-decision'", html)
        self.assertIn("data-scroll-cue", html)
        self.assertIn("aria-label='Scroll down'", html)
        self.assertIn("<section class='details' data-slug='ada-lovelace'>", html)
        # no "Details"/"Context" section labels — the facts + preview stand alone
        self.assertNotIn("details-heading", html)
        self.assertNotIn(">Context</h4>", html)
        self.assertIn("class='dossier-text'", html)
        self.assertNotIn("<summary>Details", html)
        self.assertNotIn("AI is unsure", html)
        self.assertNotIn("data-worth='maybe'", html)
        self.assertNotIn("Exclude", html)
        self.assertNotIn("Keep this LinkedIn", html)
        self.assertNotIn("self-heal", html)
        self.assertNotIn(str(base / "review.csv"), html)

    # A full fictional child dossier mirroring compose_dossier's heading layout;
    # every persona/domain here is fictional (example.com / example.net) only.
    _FULL_DOSSIER = (
        "---\nname: Ada Lovelace\nslug: ada-lovelace\n---\n\n# Ada Lovelace\n\n"
        "<!-- parent-link --> _Part of [[ada-lovelace-parentaa]] **Ada Lovelace** (proposed merge)_\n\n"
        "## Summary\n\nAda Lovelace (example.net)\n\n"
        "**Network worth:** no — automated relay, not an individual relationship.\n\n"
        "## Relationship & cadence\n\n"
        "Longtime colleague at **Example Co**; we coordinated on hiring.\n\n"
        "_grokked 9 of 9 messages over 1 batch(es) across gmail_msgvault; last on 2019-01-28 (stopped: exhausted)._\n\n"
        "## Who they are\n\n- **Employer (unknown):** Example Co\n\n"
        "## Topics\n\n- Hiring\n- ML infra\n\n"
        "## Timeline\n\n"
        "- **2018-06-07** — Discussed the **offer** <script>alert('x')</script> with Ada.\n"
        "- **2019-01-28** — Sent a note about the transition.\n\n"
        "## Identifiers\n\n- ada@example.net\n- ADA@EXAMPLE.NET\n- +1-415-555-0142\n- https://example.net/ada\n\n"
        "## Possible same person\n\n_None detected._\n"
    )

    def test_preview_shows_only_relationship_summary_and_timeline(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            dossiers = base / "dossiers"
            dossiers.mkdir()
            (dossiers / "ada-lovelace.md").write_text(self._FULL_DOSSIER, encoding="utf-8")
            rendered = web.render_dossier_markdown(base / "parents", dossiers, "ada-lovelace")
        # exactly two extracted sections, in order, as dt/dd rows (same style
        # as the card's Contact / Match signal sections — no inset box)
        self.assertIn("<dt>Summary</dt>", rendered)
        self.assertIn("<dt>Timeline</dt>", rendered)
        self.assertLess(rendered.index("<dt>Summary</dt>"), rendered.index("<dt>Timeline</dt>"))
        # Summary body is the Relationship & cadence prose, cleaned of markdown
        self.assertIn("Longtime colleague at Example Co; we coordinated on hiring.", rendered)
        self.assertNotIn("[[", rendered)
        # Timeline keeps its per-date bullets
        self.assertIn("<li>2018-06-07 — Discussed the offer", rendered)
        self.assertIn("<li>2019-01-28 — Sent a note about the transition.</li>", rendered)
        # dropped: name block, dossier Summary, Network worth, grokked line,
        # Topics, Identifiers-as-a-section, Possible same person, parent-link
        self.assertNotIn("Ada Lovelace (example.net)", rendered)
        self.assertNotIn("Network worth", rendered)
        self.assertNotIn("grokked", rendered)
        self.assertNotIn("Topics", rendered)
        self.assertNotIn("Identifiers", rendered)
        self.assertNotIn("ada@example.net", rendered)
        self.assertNotIn("Possible same person", rendered)
        self.assertNotIn("parent-link", rendered)
        # message-derived text is HTML-escaped, never executable
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_preview_degrades_gracefully_when_sections_missing(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            dossiers = base / "dossiers"
            dossiers.mkdir()
            # No "Relationship & cadence": no Summary, but Timeline still renders.
            (dossiers / "no-rel.md").write_text(
                "# Grace Hopper\n\n## Summary\n\nx\n\n"
                "## Timeline\n\n- **2020-01-01** — First note.\n",
                encoding="utf-8")
            no_rel = web.render_dossier_markdown(base / "parents", dossiers, "no-rel")
            # No dossier file at all: empty preview, never an exception.
            missing = web.render_dossier_markdown(base / "parents", dossiers, "nope")
            # Only Relationship & cadence: Summary renders, Timeline omitted.
            (dossiers / "rel-only.md").write_text(
                "# Alan Turing\n\n## Relationship & cadence\n\nOld friend from the lab.\n",
                encoding="utf-8")
            rel_only = web.render_dossier_markdown(base / "parents", dossiers, "rel-only")
        self.assertNotIn("<dt>Summary</dt>", no_rel)
        self.assertIn("<dt>Timeline</dt>", no_rel)
        self.assertEqual(missing, "")
        self.assertIn("<dt>Summary</dt>", rel_only)
        self.assertIn("Old friend from the lab.", rel_only)
        self.assertNotIn("<dt>Timeline</dt>", rel_only)

    def test_worth_card_bubbles_dossier_identifiers_into_contact(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            dossiers = base / "dossiers"
            dossiers.mkdir()
            (dossiers / "ada-lovelace.md").write_text(self._FULL_DOSSIER, encoding="utf-8")
            parent = self._candidate_parent()
            # A contact already shown on the card (case differs from the dossier's).
            parent["candidates"][0]["match_emails"] = ["Ada@Example.net"]
            html = web.render_worth_card(parent, base / "parents", dossiers)
        contact = html.split("<dt>Contact</dt><dd>")[1].split("</dd>")[0]
        # already-shown email is kept once (case-insensitive dedup, no dupe)
        self.assertIn("Ada@Example.net", contact)
        self.assertNotIn("ada@example.net", contact)
        self.assertNotIn("ADA@EXAMPLE.NET", contact)
        # new dossier identifiers bubble up
        self.assertIn("+1-415-555-0142", contact)
        self.assertIn("https://example.net/ada", contact)

    def test_decision_table_bubbles_dossier_identifiers_into_contact(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            dossiers = base / "dossiers"
            dossiers.mkdir()
            (dossiers / "ada-lovelace.md").write_text(self._FULL_DOSSIER, encoding="utf-8")
            parent = self._candidate_parent("yes", "user")
            html = web.render_decision_table(
                [parent], "yes", parents_dir=base / "parents", dossier_dir=dossiers)
        self.assertIn("+1-415-555-0142", html)
        self.assertIn("https://example.net/ada", html)
        # without the dossier dirs, the table still renders (graceful, no bubble-up)
        plain = web.render_decision_table([parent], "yes")
        self.assertNotIn("https://example.net/ada", plain)

    def _linkedin_parent(self) -> dict:
        candidate = {
            "pub": "ada-lovelace", "profile_pub": "ada-lovelace",
            "url": "https://www.linkedin.com/in/ada-lovelace",
            "full_name": "Ada Lovelace", "headline": "Engineer",
            "profile_pic_url": "", "experiences": [], "education": [],
            "location": "", "has_profile": True,
            "verdict": "needs_review", "confidence": 0.5,
            "supporting": [], "contradicting": [], "reason": "name matches messages",
            "match_emails": ["Ada@Example.net"], "match_phones": [],
            "conflict": False, "synthetic": False,
            "action": "", "approved": "", "new_url": "",
        }
        return {"slug": "ada-lovelace", "name": "Ada Lovelace",
                "person_ids": ["pid-1"], "sources": ["gmail"],
                "candidates": [candidate]}

    def test_linkedin_card_bubbles_dossier_identifiers_into_contact(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            dossiers = base / "dossiers"
            dossiers.mkdir()
            (dossiers / "ada-lovelace.md").write_text(self._FULL_DOSSIER, encoding="utf-8")
            parent = self._linkedin_parent()
            html = web.render_linkedin_card(
                parent, parent["candidates"][0], base / "parents", dossiers)
        contact = html.split("<dt>Contact</dt><dd>")[1].split("</dd>")[0]
        # already-shown email kept once (case-insensitive dedup); new identifiers bubble up
        self.assertIn("Ada@Example.net", contact)
        self.assertNotIn("ada@example.net", contact)
        self.assertNotIn("ADA@EXAMPLE.NET", contact)
        self.assertIn("+1-415-555-0142", contact)
        self.assertIn("https://example.net/ada", contact)
        # LinkedIn-specific parts of the card are unchanged (and no section labels)
        self.assertIn(">View LinkedIn", html)
        self.assertIn("<dt>Match signal</dt><dd>name matches messages</dd>", html)
        self.assertIn("data-decide='keep'", html)
        self.assertNotIn("details-heading", html)
        self.assertNotIn(">Context</h4>", html)
        self.assertIn("class='dossier-text'", html)
        self.assertIn("data-slug='ada-lovelace'", html)

    def test_linkedin_card_without_dossier_keeps_contact_and_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            parent = self._linkedin_parent()
            html = web.render_linkedin_card(
                parent, parent["candidates"][0], base / "parents", base / "dossiers")
        contact = html.split("<dt>Contact</dt><dd>")[1].split("</dd>")[0]
        self.assertEqual(contact, "Ada@Example.net")
        self.assertNotIn("https://example.net/ada", html)

    def test_worth_review_body_serves_next_card_then_complete_state(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            pending = self._candidate_parent()  # model maybe -> queued
            card = web.worth_review_body([pending], web.review_progress([pending]),
                                         base / "parents", base / "dossiers")
            done = web.worth_review_body([], web.review_progress([]),
                                         base / "parents", base / "dossiers")
        self.assertIn("worth-card", card)
        self.assertIn("Ada Lovelace", card)
        self.assertIn("Decisions ready", done)
        self.assertIn("data-complete='worth'", done)

    def test_linkedin_review_body_serves_card_and_terminal_states(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            parent = self._linkedin_parent()
            progress = web.review_progress([parent])
            card = web.linkedin_review_body(
                [parent], progress, enrichment_complete=True, linkedin_complete=False,
                parents_dir=base / "parents", dossier_dir=base / "dossiers")
            finished = web.linkedin_review_body(
                [], web.review_progress([]), enrichment_complete=True, linkedin_complete=False,
                parents_dir=base / "parents", dossier_dir=base / "dossiers")
            completed = web.linkedin_review_body(
                [], web.review_progress([]), enrichment_complete=True, linkedin_complete=True,
                parents_dir=base / "parents", dossier_dir=base / "dossiers")
        self.assertIn("Is this the right profile?", card)
        self.assertIn("data-decide='keep'", card)
        self.assertNotIn("enrichment-note", card)  # complete -> no passive note
        self.assertIn("LinkedIn profiles checked", finished)
        self.assertIn("data-complete='linkedin'", finished)
        self.assertNotIn("data-complete='linkedin'", completed)

    def test_linkedin_review_is_not_blocked_by_incomplete_enrichment(self):
        # The old hard gate ("Enrichment not finished" wall) is gone: cards render
        # for every enrichment status, with only a passive status note added.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            parent = self._linkedin_parent()
            progress = web.review_progress([parent])
            bodies = {
                status: web.linkedin_review_body(
                    [parent], progress, enrichment_complete=False, linkedin_complete=False,
                    parents_dir=base / "parents", dossier_dir=base / "dossiers",
                    enrichment={"status": status})
                for status in ("not_started", "running", "failed")
            }
        for status, body in bodies.items():
            self.assertNotIn("Enrichment not finished", body)
            self.assertIn("Is this the right profile?", body)   # the card renders
            self.assertIn("data-decide='keep'", body)            # ...and is clickable
            self.assertIn("class='enrichment-note'", body)
        self.assertIn("Enrichment is still running", bodies["running"])
        self.assertIn("Enrichment failed", bodies["failed"])
        # esc() HTML-escapes the apostrophe in "hasn't"
        self.assertIn("finished — more people may appear here", bodies["not_started"])

    def test_linkedin_completion_requires_worth_but_not_enrichment(self):
        with tempfile.TemporaryDirectory() as d:
            manifest = Path(d) / "review" / "manifest.json"
            progress = web.review_progress([])  # nothing pending anywhere
            # without worth completed, linkedin completion still refuses
            with self.assertRaises(ValueError):
                web.write_review_manifest("linkedin", "completed", progress, path=manifest)
            # worth completed (enrich NOT) is now enough to finish linkedin
            web.write_review_manifest("worth", "completed", progress, path=manifest)
            done = web.write_review_manifest("linkedin", "completed", progress, path=manifest)
        self.assertIn("linkedin", done["completed_stages"])
        self.assertNotIn("enrich", done["completed_stages"])

    def test_only_model_maybe_is_queued_and_decision_tables_are_editable(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            model_yes = self._candidate_parent("yes", "llm")
            model_no = self._candidate_parent("no", "llm")
            model_no["slug"] = "grace-hopper"
            model_no["name"] = "Grace Hopper"
            model_no["person_ids"] = ["candidate:email:grace@example.com"]
            model_no["candidates"][0].update({
                "pub": "candidate:email:grace@example.com",
                "full_name": "Grace Hopper",
                "worth_key": "candidate:email:grace@example.com",
            })
            html = web.page_html(
                [model_yes, model_no], {}, base / "review.csv",
                parents_dir=base / "parents", dossier_dir=base / "dossiers",
                manifest_path=base / "review" / "manifest.json",
            ).decode("utf-8")
            self.assertNotIn("Add Ada Lovelace to your network?", html)
            self.assertIn("Review<span>0</span>", html)
            self.assertIn("Yes<span>1</span>", html)
            self.assertIn("No<span>1</span>", html)
            self.assertIn("data-complete='worth'", html)

            added = web.page_html(
                [model_yes, model_no], {"stage": ["worth"], "view": ["yes"]}, base / "review.csv",
                parents_dir=base / "parents", dossier_dir=base / "dossiers",
                manifest_path=base / "review" / "manifest.json",
            ).decode("utf-8")
            self.assertIn("Ada Lovelace", added)
            self.assertIn("data-worth='no'", added)
            self.assertIn(">No</button>", added)
            self.assertNotIn("Restore", added)
            self.assertNotIn("Suggested", added)
            self.assertNotIn("AI is unsure", added)

            rejected = web.page_html(
                [model_yes, model_no], {"stage": ["worth"], "view": ["no"]}, base / "review.csv",
                parents_dir=base / "parents", dossier_dir=base / "dossiers",
                manifest_path=base / "review" / "manifest.json",
            ).decode("utf-8")
            self.assertIn("Grace Hopper", rejected)
            self.assertIn("data-worth='yes'", rejected)
            self.assertIn(">Yes</button>", rejected)
            self.assertNotIn("Restore", rejected)

    def _many_candidate_parents(self, count: int = 120) -> list[dict]:
        parents = []
        for index in range(count):
            parent = self._candidate_parent("yes", "llm")
            key = f"candidate:email:person{index:03d}@example.com"
            parent["slug"] = f"person-{index:03d}"
            parent["name"] = f"Person {index:03d}"
            parent["person_ids"] = [key]
            parent["candidates"][0].update({
                "pub": key, "full_name": parent["name"], "worth_key": key,
            })
            parents.append(parent)
        return parents

    def test_yes_table_renders_first_chunk_for_infinite_scroll(self):
        parents = self._many_candidate_parents(120)
        html = web.render_decision_table(parents, "yes")
        chunk = web.DECISION_CHUNK_SIZE
        self.assertEqual(html.count("data-worth='no'"), chunk)
        self.assertIn("data-decision-list", html)
        self.assertIn("data-view='yes'", html)
        self.assertIn("data-total='120'", html)
        self.assertIn(f"data-chunk='{chunk}'", html)
        self.assertIn("Person 000", html)
        self.assertIn(f"Person {chunk - 1:03d}", html)
        self.assertNotIn(f"Person {chunk:03d}", html)
        # pagination is gone: the rest streams through /api/decision-rows
        self.assertNotIn("pagination", html)
        self.assertNotIn("page=", html)
        self.assertNotIn(">Previous<", html)
        self.assertNotIn(">Next<", html)

    def test_decision_row_chevron_leads_the_summary(self):
        # The expand affordance renders at the LEFT edge of the row: the chevron
        # is the summary's first element, ahead of the avatar and name, while the
        # decision button stays in the actions cell on the far right.
        row = web.decision_rows_payload(self._many_candidate_parents(1), "yes")["rows"][0]
        summary = row.split("</summary>")[0]
        self.assertLess(summary.index("decision-row-caret"), summary.index("avatar"))
        self.assertLess(summary.index("decision-row-caret"),
                        summary.index("decision-row-main"))
        self.assertLess(summary.index("decision-row-main"),
                        summary.index("decision-row-actions"))
        self.assertNotIn("decision-row-caret",
                         summary.split("decision-row-actions")[1])

    def test_decision_rows_payload_slices_with_stable_total(self):
        parents = self._many_candidate_parents(120)
        payload = web.decision_rows_payload(parents, "yes", offset=40, limit=40)
        self.assertEqual((payload["view"], payload["total"], payload["offset"]),
                         ("yes", 120, 40))
        self.assertEqual(len(payload["rows"]), 40)
        self.assertIn("Person 040", payload["rows"][0])
        self.assertIn("Person 079", payload["rows"][-1])
        tail = web.decision_rows_payload(parents, "yes", offset=110, limit=40)
        self.assertEqual(len(tail["rows"]), 10)          # clamped at the end of scope
        beyond = web.decision_rows_payload(parents, "yes", offset=500, limit=40)
        self.assertEqual((beyond["total"], beyond["rows"]), (120, []))

    def test_rejected_spam_uses_the_classifier_reason_not_default_worth_copy(self):
        parent = self._candidate_parent("maybe", "default")
        parent["machine_worth"] = {
            "decision": "maybe", "source": "default", "reason": "not yet judged",
        }
        parent["worth"] = dict(parent["machine_worth"])
        parent["candidates"][0].update({
            "llm_reject": "spam",
            "llm_reject_reason": "Unsolicited recruiter outreach with no engagement.",
            "worth": parent["worth"],
            "machine_worth": parent["machine_worth"],
        })
        self.assertTrue(web.is_effective_no(parent))
        self.assertEqual(
            web._rejection_reason(parent),
            "Unsolicited recruiter outreach with no engagement.",
        )

    def test_manifest_completion_requires_zero_pending_and_rejects_stale_counts(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            path = base / "review" / "manifest.json"
            pending = web.review_progress([self._candidate_parent()])
            with self.assertRaisesRegex(ValueError, "1 decisions"):
                web.write_review_manifest("worth", "completed", pending, path=path,
                                          review_path=base / "review.csv",
                                          synthetic_path=base / "synthetic.csv")
            accepted_parent = self._candidate_parent("yes", "user")
            accepted_parent["candidates"][0]["worth"] = accepted_parent["worth"]
            accepted = web.review_progress([accepted_parent])
            manifest = web.write_review_manifest(
                "worth", "completed", accepted, path=path,
                review_path=base / "review.csv", synthetic_path=base / "synthetic.csv")
            self.assertEqual((manifest["stage"], manifest["status"]), ("worth", "completed"))
            self.assertEqual(manifest["counts"]["pending"], 0)
            self.assertTrue(web.phase_is_completed("worth", accepted, path))
            self.assertFalse(web.phase_is_completed("worth", pending, path))

    def test_manifest_launch_is_fresh_and_custom_filename_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            path = base / "review" / "manifest.json"
            progress = web.review_progress([self._candidate_parent()])
            first = web.write_review_manifest(
                "worth", "awaiting_user", progress, path=path,
                review_path=base / "review.csv", synthetic_path=base / "synthetic.csv",
                launched=True)
            second = web.write_review_manifest(
                "worth", "awaiting_user", progress, path=path,
                review_path=base / "review.csv", synthetic_path=base / "synthetic.csv",
                launched=True)
            self.assertGreater(second["launched_at_unix_ns"], first["launched_at_unix_ns"])
            self.assertNotEqual(second["people_revision"], first["people_revision"])
            with self.assertRaisesRegex(ValueError, "must end in manifest.json"):
                web.write_review_manifest(
                    "worth", "awaiting_user", progress,
                    path=base / "review" / "custom-state.json",
                    review_path=base / "review.csv", synthetic_path=base / "synthetic.csv")

    def test_new_people_review_makes_old_enrichment_stale_even_when_decisions_match(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            review_manifest = base / "review" / "manifest.json"
            enrichment_manifest = base / "research" / "manifest.json"
            parent = self._candidate_parent("yes", "user")
            parent["candidates"][0]["worth"] = parent["worth"]
            progress = web.review_progress([parent])
            web.write_review_manifest(
                "worth", "awaiting_user", progress, path=review_manifest,
                review_path=base / "review.csv", synthetic_path=base / "synthetic.csv",
                launched=True)
            web.write_review_manifest(
                "worth", "completed", progress, path=review_manifest,
                review_path=base / "review.csv", synthetic_path=base / "synthetic.csv")
            first_selection = web.worth_selection_from_parents(
                [parent], manifest_path=review_manifest)
            enrichment_manifest.parent.mkdir(parents=True)
            enrichment_manifest.write_text(json.dumps({
                "stage": "enrich", "status": "completed", "selection": first_selection,
                "counts": {"total": 1, "completed": 1, "pending": 0, "failed": 0},
            }), encoding="utf-8")
            self.assertTrue(web.read_enrichment_manifest(
                enrichment_manifest, selection=first_selection)["current"])

            web.write_review_manifest(
                "worth", "awaiting_user", progress, path=review_manifest,
                review_path=base / "review.csv", synthetic_path=base / "synthetic.csv",
                launched=True)
            web.write_review_manifest(
                "worth", "completed", progress, path=review_manifest,
                review_path=base / "review.csv", synthetic_path=base / "synthetic.csv")
            repeated_selection = web.worth_selection_from_parents(
                [parent], manifest_path=review_manifest)
            stale = web.read_enrichment_manifest(
                enrichment_manifest, selection=repeated_selection)
            self.assertEqual(stale["status"], "stale")
            self.assertNotEqual(first_selection["review_revision"],
                                repeated_selection["review_revision"])
            stale_html = web.render_enrichment(stale, progress)
            self.assertIn("Asking agent to continue", stale_html)
            self.assertIn("is-indeterminate", stale_html)

    def test_enrichment_approval_is_bound_to_current_revision_and_budget(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            review_manifest = base / "review" / "manifest.json"
            enrichment_manifest = base / "research" / "manifest.json"
            parent = self._candidate_parent("yes", "user")
            parent["candidates"][0]["worth"] = parent["worth"]
            progress = web.review_progress([parent])
            web.write_review_manifest(
                "worth", "awaiting_user", progress, path=review_manifest,
                review_path=base / "review.csv", synthetic_path=base / "synthetic.csv",
                launched=True)
            web.write_review_manifest(
                "worth", "completed", progress, path=review_manifest,
                review_path=base / "review.csv", synthetic_path=base / "synthetic.csv")
            selection = web.worth_selection_from_parents(
                [parent], manifest_path=review_manifest)
            enrichment_manifest.parent.mkdir(parents=True)
            enrichment_manifest.write_text(json.dumps({
                "stage": "enrich", "status": "needs_approval", "selection": selection,
                "would_submit": 3, "reused_completed": 1, "estimated_usd": 0.15,
                "counts": {"total": 4, "completed": 1, "pending": 3, "failed": 0},
            }), encoding="utf-8")

            pending = web.read_enrichment_manifest(
                enrichment_manifest, selection=selection)
            self.assertFalse(pending["approval_current"])
            self.assertIn("data-approve-enrichment", web.render_enrichment(pending, progress))
            self.assertIn("Approve $0.15", web.render_enrichment(pending, progress))
            with mock.patch.object(web, "_all_review_parents", return_value=[parent]):
                waiting = web.workflow_status(
                    review_path=base / "review.csv", verdicts_path=base / "verdicts.jsonl",
                    synthetic_path=base / "synthetic.csv", facts_dir=base / "facts",
                    people_csv=base / "people.csv", manifest_path=review_manifest,
                    enrichment_manifest_path=enrichment_manifest)
            self.assertEqual(waiting["next_action"], "await_enrichment_approval")

            zero_work = {
                **pending,
                "would_submit": 0,
                "reused_completed": 4,
                "estimated_usd": 0.0,
            }
            zero_html = web.render_enrichment(zero_work, progress)
            self.assertNotIn("data-approve-enrichment", zero_html)
            self.assertIn("Asking agent to continue", zero_html)
            self.assertIn("no approval needed", zero_html)
            self.assertIn("is-indeterminate", zero_html)

            approved = web.approve_enrichment_manifest(
                enrichment_manifest, selection=selection)
            self.assertTrue(approved["approval_current"])
            self.assertEqual(approved["approval"]["approved_budget_usd"], 0.15)
            self.assertNotIn("data-approve-enrichment",
                             web.render_enrichment(approved, progress))
            self.assertIn(
                "<h2>Asking agent to continue</h2>",
                web.render_enrichment(approved, progress),
            )
            self.assertIn("is-indeterminate", web.render_enrichment(approved, progress))

            with mock.patch.object(web, "_all_review_parents", return_value=[parent]):
                status = web.workflow_status(
                    review_path=base / "review.csv", verdicts_path=base / "verdicts.jsonl",
                    synthetic_path=base / "synthetic.csv", facts_dir=base / "facts",
                    people_csv=base / "people.csv", manifest_path=review_manifest,
                    enrichment_manifest_path=enrichment_manifest)
            self.assertEqual(status["next_action"], "run_approved_enrichment")
            self.assertEqual(
                status["command"],
                "bin/deep-context reconcile-deep-research --include-candidates "
                "--include-plausibly-absent --approve --budget 0.15")

            web.write_review_manifest(
                "worth", "awaiting_user", progress, path=review_manifest,
                review_path=base / "review.csv", synthetic_path=base / "synthetic.csv",
                launched=True)
            repeated_selection = web.worth_selection_from_parents(
                [parent], manifest_path=review_manifest)
            stale = web.read_enrichment_manifest(
                enrichment_manifest, selection=repeated_selection)
            self.assertEqual(stale["status"], "stale")
            self.assertFalse(stale["approval_current"])

    def test_linkedin_cannot_complete_before_people_stage(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            progress = web.review_progress([self._candidate_parent()])
            with self.assertRaisesRegex(ValueError, "People decisions"):
                web.write_review_manifest(
                    "linkedin", "completed", progress,
                    path=base / "review" / "manifest.json",
                    review_path=base / "review.csv", synthetic_path=base / "synthetic.csv")

    def test_explicit_linkedin_tab_can_show_existing_pending_matches(self):
        with tempfile.TemporaryDirectory() as d:
            progress = {
                "total": 3,
                "worth_total": 2,
                "worth_pending": 1,
                "worth_yes": 0,
                "worth_no": 1,
                "lookup_ready": 0,
                "linkedin_total": 1,
                "linkedin_pending": 1,
                "linkedin_done": 0,
                "rejected": 1,
            }
            view = web._phase_view(
                {"stage": ["linkedin"]}, progress,
                Path(d) / "review" / "manifest.json")
        self.assertEqual(view, "linkedin")

    def test_manifest_preserves_people_completion_during_linkedin_stage(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            path = base / "review" / "manifest.json"
            accepted = self._candidate_parent("yes", "user")
            accepted["candidates"][0]["worth"] = accepted["worth"]
            web.write_review_manifest("worth", "completed", web.review_progress([accepted]),
                                      path=path, review_path=base / "review.csv",
                                      synthetic_path=base / "synthetic.csv")
            web.write_enrichment_handoff(
                {"status": "completed", "current": True,
                 "counts": {"total": 1, "completed": 1, "pending": 0, "failed": 0}},
                path=path, review_path=base / "review.csv",
                synthetic_path=base / "synthetic.csv")

            rejected = self._candidate_parent("no", "user")
            rejected["candidates"][0]["worth"] = rejected["worth"]
            synth_worth = {"decision": "yes", "source": "user", "reason": ""}
            synthetic = {
                "slug": "synth", "name": "Ada Lovelace",
                "person_ids": ["candidate:email:ada@example.com"], "sources": ["gmail"],
                "worth": synth_worth, "machine_worth": synth_worth, "connection": False,
                "candidates": [{
                    "pub": "synth-ada", "full_name": "Ada Lovelace", "synthetic": True,
                    "import_candidate": False, "approved": "auto", "action": "verify",
                    "confidence": 0.8, "verdict": "synthetic", "match_emails": [],
                    "match_phones": [], "supporting": [], "contradicting": [], "reason": "",
                }],
            }
            transitioned = web.review_progress([rejected, synthetic])
            manifest = web.write_review_manifest(
                "linkedin", "awaiting_user", transitioned, path=path,
                review_path=base / "review.csv", synthetic_path=base / "synthetic.csv",
                launched=True)
            self.assertEqual(manifest["completed_stages"], ["worth", "enrich"])
            self.assertTrue(web.phase_is_completed("worth", transitioned, path))
            self.assertEqual(web._phase_view({}, transitioned, path), "worth")

    def test_candidate_retarget_moves_to_linkedin_and_yes_preserves_replacement(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts = base / "facts"
            facts.mkdir()
            pid = "candidate:email:cass@x.com"
            (facts / f"{pid}.jsonl").write_text(
                json.dumps({"facts": {"canonical_name": "Cass Doe"}}) + "\n",
                encoding="utf-8")
            pools = _pools(base, [GMAIL_ROW], [])
            review = base / "review.csv"
            reconcile._write_override_rows(review, {pid: {
                "public_identifier": pid,
                "network_worth": "yes",
                "action": "retarget",
                "approved": "",
                "new_linkedin_url": "https://www.linkedin.com/in/cass-real",
                "new_public_identifier": "cass-real",
                "reason": "deep research found the profile",
            }})
            with mock.patch.object(candidates, "CANDIDATE_CSVS", pools):
                parents = web.load_candidate_parents(
                    facts, reconcile.load_override_rows(review), set())
            web.annotate_worth(parents, reconcile.load_override_rows(review), facts)
            self.assertFalse(web.is_import_candidate_parent(parents[0]))
            self.assertTrue(web.is_lookup_ready(parents[0]))
            pending = web.pending_linkedin_candidates(parents[0])
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["url"], "https://www.linkedin.com/in/cass-real")
            web.apply_decision(review, base / "verdicts.jsonl", pid, "keep", "", 0.7, 0.85)
            row = reconcile.load_override_rows(review)[pid]
            self.assertEqual((row["action"], row["approved"]), ("retarget", "yes"))
            self.assertEqual(row["new_public_identifier"], "cass-real")

    def test_avatar_endpoint_helper_caches_live_bytes_and_reuses_them(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            profiles, avatars = base / "profiles", base / "avatars"
            profiles.mkdir()
            (profiles / "ada.json").write_text(json.dumps({
                "normalized_profile": {"profile_pic_url": "https://media.licdn.com/ada.jpg"},
                "raw_response": {},
            }), encoding="utf-8")
            body = b"\xff\xd8\xff" + b"avatar"
            with mock.patch.object(web.urllib.request, "urlopen", return_value=io.BytesIO(body)) as fetch:
                self.assertEqual(web.load_avatar("ada", profile_cache_dir=profiles,
                                                 avatar_dir=avatars), (body, "image/jpeg"))
                fetch.assert_called_once()
            with mock.patch.object(web.urllib.request, "urlopen",
                                   side_effect=AssertionError("must use local bytes")):
                self.assertEqual(web.load_avatar("ada", profile_cache_dir=profiles,
                                                 avatar_dir=avatars), (body, "image/jpeg"))

    def test_avatar_profile_key_cannot_escape_cache_directory(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            profiles, avatars = base / "profiles", base / "avatars"
            profiles.mkdir()
            (base / "outside.json").write_text(json.dumps({
                "normalized_profile": {
                    "profile_pic_url": "https://media.licdn.com/outside.jpg",
                },
            }), encoding="utf-8")
            with mock.patch.object(web.urllib.request, "urlopen") as fetch:
                self.assertIsNone(web.load_avatar("../outside", profile_cache_dir=profiles,
                                                  avatar_dir=avatars))
                fetch.assert_not_called()


class TestWorthSelectionSingleSource(unittest.TestCase):
    """The People-review status and the enrichment manifest must stamp the SAME worth-
    selection digest. A candidate promoted to a verified LinkedIn parent (retargeted, so its
    worth_key is a LinkedIn id, not a candidate id) leaves the worth pool; if the two sides
    computed the population differently it would be counted by one and not the other, the
    sha256 would never match, and the flow would loop on 'preview enrichment' forever."""

    def test_enrichment_reuses_the_review_selection_function(self):
        # Single source of truth: enrichment does NOT re-derive its own worth selection.
        self.assertIs(dresearch.current_worth_selection, web.current_worth_selection)

    def _plain_candidate_parent(self) -> dict:
        pid = "candidate:email:plain@example.com"
        return {"person_ids": [pid], "worth": {"decision": "yes"},
                "candidates": [{"import_candidate": True, "worth_key": pid}]}

    def _promoted_candidate_parent(self) -> dict:
        # Retargeted onto LinkedIn 'promo-li' and verified: still keyed on the candidate id,
        # but its worth_key is now the LinkedIn id and it is no longer an import_candidate.
        return {"person_ids": ["candidate:email:promo@example.com"], "worth": {"decision": "maybe"},
                "candidates": [{"import_candidate": False, "worth_key": "promo-li"}]}

    def test_promoted_candidate_leaves_the_worth_pool(self):
        plain, promoted = self._plain_candidate_parent(), self._promoted_candidate_parent()
        self.assertTrue(web.is_worth_subject(plain))
        self.assertFalse(web.is_worth_subject(promoted))   # promotion removes it from worth review

    def test_worth_selection_excludes_the_promoted_candidate(self):
        with tempfile.TemporaryDirectory() as d:
            manifest = Path(d) / "review" / "manifest.json"   # absent -> empty review_revision
            plain, promoted = self._plain_candidate_parent(), self._promoted_candidate_parent()
            sel = web.worth_selection_from_parents([plain, promoted], manifest_path=manifest)
            self.assertEqual(sel["total"], 1)                 # only the plain candidate counts
            self.assertEqual(sel["yes"], 1)
            # dropping the promoted parent entirely yields the identical digest
            only_plain = web.worth_selection_from_parents([plain], manifest_path=manifest)
            self.assertEqual(sel["sha256"], only_plain["sha256"])


class TestLiveEndpoints(unittest.TestCase):
    """Acceptance: every decision endpoint persists to CSV AND returns everything the
    client needs to live-update the page in the same click — the row's decision state
    (action/approved), its effective worth (+source), the unified `rejected` flag, and
    fresh GLOBAL tab counts for the header stats / nav pills. Both directions."""

    COUNT_KEYS = {"total", "review", "verified", "detached", "conflict",
                  "fixed", "excluded", "decided", "rejected"}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.facts = base / "facts"
        self.facts.mkdir()
        self.review = base / "review.csv"
        self.verdicts = _verdict_jsonl(base / "verdicts.jsonl", [TestUnifiedRejected.VERDICT])
        row = {k: "" for k in reconcile.OVERRIDE_COLUMNS}
        row.update({"public_identifier": "bob-1", "person_id": "pid-bob",
                    "llm_worth": "no", "llm_worth_reason": "vendor"})
        reconcile._write_override_rows(self.review, {"bob-1": row})
        self.synthetic = base / "synthetic-people.csv"
        self.manifest = base / "review" / "manifest.json"
        self.enrichment = base / "deep-research" / "manifest.json"
        self.people = base / "people.csv"
        self.synthetic.write_text(
            "id,public_identifier,full_name,enrichment_provider,approved\n"
            "pid-9,synth-email-abc,Ross Nordeen,synthetic,auto\n", encoding="utf-8")
        # keep live_counts off any real candidate pools in the repo checkout
        self._pools = mock.patch.object(candidates, "CANDIDATE_CSVS", [])
        self._pools.start()
        self.agent_notifications = 0

        def notify_agent():
            self.agent_notifications += 1

        handler = web.make_handler(self.review, self.verdicts, base / "parents",
                                   base / "dossiers", 0.7, 0.85,
                                   synthetic_path=self.synthetic, facts_dir=self.facts,
                                   people_csv=self.people,
                                   manifest_path=self.manifest,
                                   enrichment_manifest_path=self.enrichment,
                                   agent_notifier=notify_agent)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._pools.stop()
        self._tmp.cleanup()

    def _post(self, path: str, **form) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=urllib.parse.urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def test_worth_no_and_rescue_round_trip_with_live_counts(self):
        # A user No joins the same rejected pile; the UI endpoint is intentionally binary.
        j = self._post("/worth", pub="bob-1", worth="no")
        self.assertTrue(j["rejected"])
        self.assertEqual((j["effective"], j["source"]), ("no", "user"))
        self.assertLessEqual(self.COUNT_KEYS, set(j["counts"]))
        self.assertEqual((j["counts"]["rejected"], j["counts"]["review"]), (1, 0))
        # worth-Yes rescues in the same click: rejected flips, counts move tabs
        j = self._post("/worth", pub="bob-1", worth="yes")
        self.assertFalse(j["rejected"])
        self.assertEqual((j["effective"], j["source"]), ("yes", "user"))
        self.assertEqual((j["action"], j["approved"]), ("", ""))   # chip repaint fields
        self.assertEqual((j["counts"]["rejected"], j["counts"]["review"]), (0, 1))
        self.assertEqual(reconcile.load_override_rows(self.review)["bob-1"]["network_worth"],
                         "yes")                                     # ...and the CSV persisted
        j = self._post("/worth", pub="bob-1", worth="restore")
        self.assertEqual((j["effective"], j["source"]), ("no", "llm"))
        self.assertEqual(reconcile.load_override_rows(self.review)["bob-1"]["network_worth"], "")
        self.assertEqual(self.agent_notifications, 3)

    def test_rejected_ui_mutation_does_not_notify_agent(self):
        before = self.agent_notifications
        with self.assertRaises(urllib.error.HTTPError):
            self._post("/worth", pub="bob-1", worth="maybe")
        self.assertEqual(self.agent_notifications, before)

    def test_exclude_then_keep_report_live_state_both_ways(self):
        j = self._post("/decide", pub="bob-1", decision="exclude")
        self.assertTrue(j["rejected"])                              # exclude IS the unified no
        self.assertEqual((j["action"], j["approved"]), ("exclude", "yes"))
        self.assertEqual((j["effective"], j["source"]), ("no", "user"))
        self.assertEqual((j["counts"]["rejected"], j["counts"]["excluded"]), (1, 1))
        j = self._post("/decide", pub="bob-1", decision="keep")     # keep-ish rescue
        self.assertFalse(j["rejected"])
        self.assertEqual(j["counts"]["rejected"], 0)
        self.assertEqual(reconcile.load_override_rows(self.review)["bob-1"]["action"], "verify")

    def test_synthetic_worth_and_decide_report_gate_state(self):
        j = self._post("/worth", pub="pid-9", worth="no")           # the mint gate follows
        self.assertTrue(j["rejected"])
        self.assertEqual((j["action"], j["approved"]), ("verify", "no"))
        with self.synthetic.open(newline="", encoding="utf-8") as fh:
            (row,) = list(csv.DictReader(fh))
        self.assertEqual(row["approved"], "no")
        j = self._post("/worth", pub="pid-9", worth="yes")
        self.assertFalse(j["rejected"])
        self.assertEqual(j["approved"], "yes")
        j = self._post("/decide", pub="synth-email-abc", decision="detach")
        self.assertEqual((j["action"], j["approved"]), ("verify", "no"))
        self.assertIn("counts", j)

    def test_synthetic_fix_retargets_and_rejects_synthetic_row(self):
        j = self._post(
            "/decide", pub="synth-email-abc", decision="fix",
            new_url="https://www.linkedin.com/in/ross-real")
        self.assertEqual((j["action"], j["approved"]), ("retarget", "yes"))
        row = reconcile.load_override_rows(self.review)["pid-9"]
        self.assertEqual(row["new_public_identifier"], "ross-real")
        self.assertEqual(row["person_id"], "pid-9")
        with self.synthetic.open(newline="", encoding="utf-8") as fh:
            (synthetic_row,) = list(csv.DictReader(fh))
        self.assertEqual(synthetic_row["approved"], "no")

    def test_get_is_read_only_for_decision_csv(self):
        before = self.review.read_bytes()
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as response:
            self.assertEqual(response.status, 200)
            html = response.read().decode("utf-8")
        self.assertEqual(self.review.read_bytes(), before)
        self.assertIn("POWERPACKS", html)

    def test_decision_rows_endpoint_serves_json_chunks(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/decision-rows?view=no&offset=0&limit=5") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers.get("Content-Type", ""))
            payload = json.loads(resp.read())
        # authoritative shape: full-scope total + the requested window
        self.assertEqual(payload["view"], "no")
        self.assertEqual(payload["offset"], 0)
        self.assertLessEqual(len(payload["rows"]), 5)
        self.assertGreaterEqual(payload["total"], len(payload["rows"]))
        for row in payload["rows"]:
            self.assertIn("decision-row", row)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/decision-rows?view=bogus")
        self.assertEqual(ctx.exception.code, 400)

    def test_card_endpoints_serve_stage_content_for_reloadless_swaps(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/worth-card") as resp:
            self.assertEqual(resp.status, 200)
            worth_html = resp.read().decode("utf-8")
        # fixture has no model-maybe import candidates: the queue reads complete
        self.assertIn("data-complete='worth'", worth_html)
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/linkedin-card") as resp:
            self.assertEqual(resp.status, 200)
            linkedin_html = resp.read().decode("utf-8")
        # enrichment hasn't run, yet the reviewable synthetic card still renders,
        # with only a passive status note (the stage is never hard-blocked)
        self.assertIn("class='enrichment-note'", linkedin_html)
        self.assertIn("identity-card", linkedin_html)
        self.assertNotIn("Enrichment not finished", linkedin_html)

    def test_identity_post_rejects_mismatched_parent_slug(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/decide", pub="bob-1", decision="keep", parent_slug="someone-else")
        self.assertEqual(ctx.exception.code, 400)

    def test_ui_endpoint_rejects_maybe_and_enforces_file_handoffs(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/worth", pub="bob-1", worth="maybe")
        self.assertEqual(ctx.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/complete", stage="linkedin")
        self.assertEqual(ctx.exception.code, 409)
        self._post("/worth", pub="bob-1", worth="yes")
        review_manifest = web.read_review_manifest(self.manifest)
        selection = web.worth_selection_from_parents([], manifest_path=self.manifest)
        self.enrichment.parent.mkdir(parents=True, exist_ok=True)
        self.enrichment.write_text(json.dumps({
            "stage": "enrich", "status": "completed", "selection": selection,
            "counts": {"total": 0, "completed": 0, "pending": 0, "failed": 0},
        }), encoding="utf-8")
        self.assertTrue(review_manifest["people_revision"])
        self._post("/complete", stage="enrich")
        self._post("/decide", pub="bob-1", decision="keep")
        self._post("/decide", pub="synth-email-abc", decision="keep")
        complete = self._post("/complete", stage="linkedin")
        self.assertEqual(complete["manifest"]["status"], "completed")
        self.assertEqual(complete["manifest"]["counts"]["pending"], 0)

    def test_enrichment_approval_endpoint_persists_inert_current_approval(self):
        self._post("/worth", pub="bob-1", worth="yes")
        parents = web._all_review_parents(
            self.verdicts, self.review, self.synthetic, self.facts, self.people)
        selection = web.worth_selection_from_parents(parents, manifest_path=self.manifest)
        self.enrichment.parent.mkdir(parents=True, exist_ok=True)
        self.enrichment.write_text(json.dumps({
            "stage": "enrich", "status": "needs_approval", "selection": selection,
            "would_submit": 2, "reused_completed": 1, "estimated_usd": 0.10,
            "counts": {"total": 3, "completed": 1, "pending": 2, "failed": 0},
        }), encoding="utf-8")

        result = self._post("/approve-enrichment")
        self.assertTrue(result["enrichment"]["approval_current"])
        saved = json.loads(self.enrichment.read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "needs_approval")
        self.assertEqual(saved["approval"]["approved_budget_usd"], 0.10)
        self.assertNotIn("current", saved)

        # The endpoint is idempotent for the same current preview.
        repeated = self._post("/approve-enrichment")
        self.assertTrue(repeated["enrichment"]["approval_current"])

        # A browser can retain the button for up to one observer interval after
        # the bridge has already advanced the manifest. That stale click is an
        # idempotent success, not a conflict.
        saved["status"] = "completed"
        saved["counts"] = {"total": 3, "completed": 3, "pending": 0, "failed": 0}
        self.enrichment.write_text(json.dumps(saved), encoding="utf-8")
        already_done = self._post("/approve-enrichment")
        self.assertEqual(already_done["enrichment"]["status"], "completed")

        # A newly started People revision makes the old approval unusable.
        current_parents = web._all_review_parents(
            self.verdicts, self.review, self.synthetic, self.facts, self.people)
        progress = web.review_progress(current_parents)
        web.write_review_manifest(
            "worth", "awaiting_user", progress, path=self.manifest,
            review_path=self.review, synthetic_path=self.synthetic, launched=True)
        web.write_review_manifest(
            "worth", "completed", progress, path=self.manifest,
            review_path=self.review, synthetic_path=self.synthetic)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/approve-enrichment")
        self.assertEqual(ctx.exception.code, 409)


class TestReadinessCandidateCounts(unittest.TestCase):
    def test_counts_per_source_and_with_dossiers(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            pools = _pools(base, [GMAIL_ROW], [PHONE_ROW])
            facts = base / "facts"
            facts.mkdir()
            (facts / "candidate:email:cass@x.com.jsonl").write_text("{}\n", encoding="utf-8")
            with mock.patch.object(candidates, "CANDIDATE_CSVS", pools):
                counts = readiness.count_candidates(facts)
        self.assertEqual(counts, {"total": 2, "per_source": {"gmail": 1, "imessage": 1},
                                  "with_dossiers": 1})


class TestLinkedInConnectionGuard(unittest.TestCase):
    """LinkedIn connections are GROUND TRUTH: a machine no never rejects them in
    the UI; only the user's own No/Exclude can."""

    def test_connection_never_machine_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            facts = Path(d)
            rows = {"connfriend": {"public_identifier": "connfriend", "person_id": "pid-li",
                                   "llm_worth": "no", "llm_worth_reason": "looks transactional",
                                   "network_worth": "", "action": "", "approved": ""}}
            state = web.effective_no_for_key("connfriend", rows, facts,
                                             connections={"pid-li"})
            self.assertFalse(state["rejected"])
            self.assertTrue(state["connected"])
            # without connection membership, the same machine no rejects
            state = web.effective_no_for_key("connfriend", rows, facts, connections=set())
            self.assertTrue(state["rejected"])
            # the user's own no still rejects a connection (user wins)
            rows["connfriend"]["network_worth"] = "no"
            state = web.effective_no_for_key("connfriend", rows, facts,
                                             connections={"pid-li"})
            self.assertTrue(state["rejected"])

    def test_load_connection_keys_reads_linkedin_csv_channel(self):
        with tempfile.TemporaryDirectory() as d:
            people = Path(d) / "people.csv"
            people.write_text(
                "id,public_identifier,source_channels\n"
                "pid-li,connfriend,\"linkedin_csv,gmail_msgvault\"\n"
                "pid-gm,gmailonly,gmail_msgvault\n",
                encoding="utf-8",
            )
            keys = web.load_connection_keys(people)
        self.assertEqual(keys, {"pid-li", "connfriend"})




class TestCardProfileAndReasonDisplay(unittest.TestCase):
    """Cache-only profile hydration, Work/Education fact rows, and the
    display-only match-signal cleanup (fictional personas only)."""

    def _card_parent(self, headline: str = "") -> dict:
        candidate = {
            "pub": "ada-lovelace", "profile_pub": "ada-lovelace",
            "url": "https://www.linkedin.com/in/ada-lovelace",
            "full_name": "Ada Lovelace", "headline": headline,
            "profile_pic_url": "", "experiences": [], "education": [],
            "location": "", "has_profile": True,
            "verdict": "needs_review", "confidence": 0.5,
            "supporting": [], "contradicting": [], "reason": "name matches messages",
            "match_emails": ["ada@example.net"], "match_phones": [],
            "conflict": False, "synthetic": False,
            "action": "", "approved": "", "new_url": "",
        }
        return {"slug": "ada-lovelace", "name": "Ada Lovelace",
                "person_ids": ["pid-1"], "sources": ["gmail"],
                "candidates": [candidate]}

    def _write_cached_profile(self, cache_dir: Path) -> None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "ada-lovelace.json").write_text(json.dumps({
            "public_identifier": "ada-lovelace",
            "linkedin_url": "https://www.linkedin.com/in/ada-lovelace",
            "raw_response": {"firstName": "Ada"},
            "normalized_profile": {
                "success": True, "full_name": "Ada Lovelace",
                "headline": "Analytical Engines Lead", "profile_pic_url": "",
                "location_str": "London",
                "experiences": [{"title": f"Role {i}", "company_name": "Example Engines"}
                                for i in range(5)],
                "education": [{"school": "Home Study", "degree": "Mathematics"}],
            },
        }), encoding="utf-8")

    def test_card_hydrates_profile_from_cache_without_any_fetch(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            cache = base / "cache"
            self._write_cached_profile(cache)
            parent = self._card_parent()
            html = web.render_linkedin_card(
                parent, parent["candidates"][0], base / "parents", base / "dossiers",
                cache)
        self.assertIn("Analytical Engines Lead", html)
        self.assertIn("<dt>Work</dt>", html)
        self.assertIn("Role 0 @ Example Engines", html)
        self.assertIn("<dt>Education</dt>", html)
        self.assertNotIn("No cached profile data", html)

    def test_card_cache_miss_shows_passive_prefetch_note(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            parent = self._card_parent()
            html = web.render_linkedin_card(
                parent, parent["candidates"][0], base / "parents", base / "dossiers",
                base / "empty-cache")
            progress = web.review_progress([parent])
            body = web.linkedin_review_body(
                [self._card_parent()], progress, enrichment_complete=True,
                linkedin_complete=False, parents_dir=base / "parents",
                dossier_dir=base / "dossiers", profile_cache_dir=base / "empty-cache")
        self.assertIn("No cached profile data", html)
        self.assertIn("run profile prefetch", html)
        # ...and the stage surfaces the aggregate miss count passively
        self.assertIn("1 person here has no cached profile", body)

    def test_profile_fact_rows_pin_three_with_show_more_toggle(self):
        rows = web.profile_fact_rows({
            "experiences": [f"Role {i} @ ExampleCo" for i in range(5)],
            "education": ["Mathematics — Home Study"],
        })
        work, education = rows
        self.assertEqual(work.count("<li>"), 3)                    # pinned
        self.assertEqual(work.count("<li hidden data-more-item>"), 2)
        self.assertIn("+ show 2 more", work)
        self.assertIn("data-show-more", work)
        self.assertNotIn("data-show-more", education)              # short list, no toggle

    def test_display_reason_trims_deep_research_tail_only_when_summary_exists(self):
        self.assertEqual(
            web._display_reason("Longtime teammate at ExampleCo; deep research: matched "
                                "employer, school and location with process notes"),
            "Longtime teammate at ExampleCo")
        blob = "deep research: matched employer and school"
        self.assertEqual(web._display_reason(blob), blob)          # only signal -> keep
        self.assertEqual(web._display_reason("plain reason"), "plain reason")

    def test_debug_carousel_renders_only_with_flag_and_indexes_the_queue(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            first = self._card_parent(headline="Engineer")
            second = self._card_parent(headline="Engineer")
            second["slug"] = second["name"] = "Grace Hopper"
            second["candidates"][0].update({"pub": "grace-hopper",
                                            "profile_pub": "grace-hopper",
                                            "full_name": "Grace Hopper"})
            progress = web.review_progress([first, second])
            common_kwargs = dict(enrichment_complete=True, linkedin_complete=False,
                                 parents_dir=base / "parents", dossier_dir=base / "dossiers",
                                 profile_cache_dir=base / "cache")
            plain = web.linkedin_review_body([first, second], progress, **common_kwargs)
            debug0 = web.linkedin_review_body([first, second], progress,
                                              debug=True, **common_kwargs)
            debug1 = web.linkedin_review_body([first, second], progress,
                                              debug=True, index=1, **common_kwargs)
        self.assertNotIn("data-carousel", plain)                   # debug-only
        self.assertIn("data-carousel='prev'", debug0)
        self.assertIn("data-carousel='next'", debug0)
        self.assertIn("data-queue-total='2'", debug0)
        self.assertIn("data-queue-index='0'", debug0)
        self.assertIn("Ada Lovelace", debug0)
        self.assertIn("data-queue-index='1'", debug1)
        self.assertIn("Grace Hopper", debug1)


class TestPrefetchProfiles(unittest.TestCase):
    """Offline profile prefetch stage: miss detection, spend-free dry run,
    mocked fetch writing the shared cache, and idempotent reruns."""

    VERDICT = {"parent_slug": "ada-lovelace", "name": "Ada Lovelace",
               "candidate_key": "ada-lovelace", "person_ids": ["pid-ada"],
               "conflict": False, "no_link": False,
               "linkedin": {"linkedin_url": "https://www.linkedin.com/in/ada-lovelace",
                            "has_profile": True},
               "verdict": {"verdict": "needs_review", "confidence": 0.4,
                           "reason": "thin"}, "error": ""}

    def _args(self, base: Path, **overrides) -> _ns:
        values = dict(
            verdicts=str(base / "verdicts.jsonl"), review=str(base / "review.csv"),
            synthetic_people=str(base / "synthetic-people.csv"),
            facts_dir=str(base / "facts"), people_csv=str(base / "people.csv"),
            parents_dir=str(base / "parents"), dossier_dir=str(base / "dossiers"),
            profile_cache_dir=str(base / "cache"), fetch=False, limit=0)
        values.update(overrides)
        return _ns(**values)

    def _fixture(self, base: Path) -> None:
        (base / "facts").mkdir()
        _verdict_jsonl(base / "verdicts.jsonl", [self.VERDICT])
        reconcile._write_override_rows(base / "review.csv", {})

    @staticmethod
    def _fake_fetch(pub, url, key, cache_dir=None, **_):
        path = prefetch.profile_cache_path(cache_dir, pub)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "public_identifier": pub, "linkedin_url": url,
            "raw_response": {"firstName": "Ada"},
            "normalized_profile": {"success": True, "public_identifier": pub},
        }), encoding="utf-8")
        return {"status_code": 200, "data": {"firstName": "Ada"}, "error": "",
                "from_cache": False, "normalized_profile": {"success": True}}

    def test_dry_run_reports_misses_and_never_fetches(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            self._fixture(base)
            fetcher = mock.MagicMock()
            with mock.patch.object(candidates, "CANDIDATE_CSVS", []), \
                    mock.patch.object(prefetch, "ROOT", base / "out"), \
                    mock.patch.object(prefetch, "rapidapi_profile", fetcher):
                manifest = prefetch.run(self._args(base))
        self.assertEqual(manifest["status"], "dry_run")
        self.assertEqual(manifest["queue_links"], 1)
        self.assertEqual(manifest["cache_misses"], 1)
        self.assertEqual(manifest["estimated_rapidapi_calls"], 1)
        self.assertEqual(manifest["missing_public_identifiers"], ["ada-lovelace"])
        fetcher.assert_not_called()
        self.assertFalse(manifest["privacy"]["paid_provider_called"])

    def test_fetch_writes_cache_and_second_run_has_zero_misses(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            self._fixture(base)
            with mock.patch.object(candidates, "CANDIDATE_CSVS", []), \
                    mock.patch.object(prefetch, "ROOT", base / "out"), \
                    mock.patch.object(prefetch, "rapidapi_key", lambda: "test-key"), \
                    mock.patch.object(prefetch, "rapidapi_profile", self._fake_fetch):
                first = prefetch.run(self._args(base, fetch=True))
                second = prefetch.run(self._args(base))  # idempotent recheck
            cache_written = (base / "cache" / "ada-lovelace.json").exists()
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["counts"], {"fetched": 1, "from_cache": 0,
                                           "failed": 0, "attempted": 1})
        self.assertEqual(first["remaining_misses"], 0)
        self.assertTrue(cache_written)
        self.assertEqual(second["status"], "dry_run")
        self.assertEqual(second["cache_misses"], 0)

    def test_fetch_without_key_is_blocked_and_spends_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            self._fixture(base)
            fetcher = mock.MagicMock()
            with mock.patch.object(candidates, "CANDIDATE_CSVS", []), \
                    mock.patch.object(prefetch, "ROOT", base / "out"), \
                    mock.patch.object(prefetch, "rapidapi_key", lambda: ""), \
                    mock.patch.object(prefetch, "rapidapi_profile", fetcher):
                manifest = prefetch.run(self._args(base, fetch=True))
        self.assertEqual(manifest["status"], "blocked_no_key")
        fetcher.assert_not_called()
        self.assertFalse(manifest["privacy"]["paid_provider_called"])


if __name__ == "__main__":
    unittest.main()
