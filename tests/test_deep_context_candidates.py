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
import unittest
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
