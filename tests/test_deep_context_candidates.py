"""Candidates-support layer for the deep-context pipeline ($deep-setup).

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
                subset = dresearch.candidate_subset(facts)
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
            queue.write_text("handle,display_name,primary_email,phone_e164,source_channel\n"
                             "cass-doe-parentab,Cass Doe,cass@x.com,,email\n", encoding="utf-8")
            people = base / "people.csv"
            people.write_text("id,primary_email,primary_phone\n", encoding="utf-8")
            out = base / "synthetic-people.csv"
            with mock.patch.object(candidates, "CANDIDATE_CSVS", pools), \
                 contextlib.redirect_stdout(io.StringIO()):
                asp.main(["--research-dir", str(research), "--queue-csv", str(queue),
                          "--people-csv", str(people), "--out", str(out)])
            with out.open(newline="", encoding="utf-8") as fh:
                (row,) = list(csv.DictReader(fh))
            self.assertEqual(row["id"], "candidate:email:cass@x.com")
            self.assertEqual(row["entity_urn"], "synthetic:candidate:email:cass@x.com")
            self.assertEqual(row["primary_email"], "cass@x.com")
            self.assertEqual(json.loads(row["all_emails"]), ["cass@x.com", "cd@y.com"])
            self.assertEqual(json.loads(row["interaction_counts"]), {"gmail": 12})
            self.assertEqual(row["source_channels"], "gmail_msgvault")        # the candidate's channels
            self.assertEqual(row["approved"], "auto")
            # ...and the review UI surfaces it through the existing synthetic path.
            parent, = web.load_synthetic_parents(out)
            self.assertEqual(parent["person_ids"], ["candidate:email:cass@x.com"])
            self.assertTrue(parent["candidates"][0]["synthetic"])
            self.assertEqual(web.apply_synthetic_decision(
                out, row["public_identifier"], "keep")["approved"], "yes")

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
            # facts on disk win over the mirrored row value
            (facts / "janedoe.jsonl").write_text(_facts_record("yes", "founder") + "\n", encoding="utf-8")
            got = candidates.effective_network_worth(
                "janedoe", {"janedoe": {"llm_worth": "no", "llm_worth_reason": "stale"}}, facts)
            self.assertEqual((got["decision"], got["source"], got["reason"]), ("yes", "llm", "founder"))
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
            # an explicit user mark wins over a stale exclude
            got = candidates.effective_network_worth(
                "janedoe", {"janedoe": {"action": "exclude", "approved": "yes", "network_worth": "yes"}}, facts)
            self.assertEqual((got["decision"], got["source"]), ("yes", "user"))


class TestCandidateSubsetWorthGate(unittest.TestCase):
    def test_effective_no_is_excluded_and_reported(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts = base / "facts"
            facts.mkdir()
            (facts / "candidate:email:cass@x.com.jsonl").write_text(
                _facts_record("maybe", "thin signal") + "\n", encoding="utf-8")
            (facts / "candidate:phone:+14155551234.jsonl").write_text(
                _facts_record("no", "food delivery updates") + "\n", encoding="utf-8")
            pools = _pools(base, [GMAIL_ROW], [PHONE_ROW])
            with mock.patch.object(candidates, "CANDIDATE_CSVS", pools):
                skipped: list[str] = []
                subset = dresearch.candidate_subset(facts, {}, worth_skipped=skipped)
                self.assertEqual([r["candidate_key"] for r in subset],
                                 ["candidate:email:cass@x.com"])                         # maybe stays in
                self.assertEqual(skipped, ["candidate:phone:+14155551234"])              # no is gated out
                # a user "yes" mark overrides the LLM's no -> back in the paid queue
                skipped = []
                subset = dresearch.candidate_subset(
                    facts, {"candidate:phone:+14155551234": {"network_worth": "yes"}},
                    worth_skipped=skipped)
                self.assertEqual(len(subset), 2)
                self.assertEqual(skipped, [])


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
            reconcile.upsert_retargets(review, [{
                "old_public_identifier": "janedoe",
                "new_linkedin_url": "https://www.linkedin.com/in/jane-real"}])
            row = reconcile.load_override_rows(review)["janedoe"]
            self.assertEqual(row["action"], "retarget")
            self.assertEqual(row["network_worth"], "yes")               # retarget upsert too


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
            # the card renders the badge, the worth buttons, and the LLM's reasoning
            html = web.render_candidate(0, 1, cand)
            self.assertIn("candidate — no LinkedIn", html)
            self.assertIn("Worth adding?", html)
            self.assertIn("worth-yes on", html)                            # effective value highlighted
            self.assertIn("LLM: yes — strong founder", html)
            self.assertNotIn("Keep this LinkedIn", html)                   # nothing attached to keep

    def test_candidate_rows_dedupe_against_shown_person_ids(self):
        with tempfile.TemporaryDirectory() as d:
            facts, pools = self._fixture(Path(d))
            with mock.patch.object(candidates, "CANDIDATE_CSVS", pools):
                parents = web.load_candidate_parents(facts, {}, {"candidate:email:cass@x.com"})
        self.assertEqual([p["person_ids"][0] for p in parents], ["candidate:phone:+14155551234"])


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
            self.assertIn("Worth adding?", html)                 # worth buttons on a plain verdict row
            self.assertIn("worth-no on", html)
            self.assertIn("LLM: no — pure vendor thread", html)  # machine decision + reason alongside
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
            self.assertIn("Worth adding?", web.render_candidate(0, 1, p["candidates"][0]))


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
            self.assertIn("worth-no on", web.render_candidate(0, 1, p["candidates"][0]))
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


if __name__ == "__main__":
    unittest.main()
