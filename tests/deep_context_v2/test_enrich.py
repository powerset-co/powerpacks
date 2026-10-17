"""Synthetic enrichment fixtures: real SQLite/cache IO, external providers only faked."""
from __future__ import annotations

import asyncio
import json
import io
import tempfile
import shutil
from contextlib import redirect_stdout
from collections.abc import Callable
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import jsonschema

from packs.ingestion.primitives.deep_context_v2.db import queries, queries_dedupe, queries_enrich, queries_worth
from packs.ingestion.primitives.deep_context_v2.db.queries_dedupe import Identifier
from packs.ingestion.primitives.deep_context_v2.db.queries_enrich import FamilyWorth, LinkedinVerdict, Research
from packs.ingestion.primitives.deep_context_v2.db.queries_worth import Connection, MemberFacts
from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.ingestion.primitives.deep_context_v2.enrich import jev_identity, judge, pre_match, profiles, proposals, research, settle, sol_identity
from packs.ingestion.primitives.deep_context_v2.enrich import enrich
from packs.ingestion.primitives.deep_context_v2.enrich.enrich import EMAIL_CONFIRMED_REASON, Enrich, confirm_by_email
from packs.ingestion.primitives.deep_context_v2.enrich.family import Family, family_evidence, judgment_fingerprint, load_families
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import Profile, Profiles
from packs.ingestion.primitives.deep_context_v2.enrich.proposals import Proposal, Proposals
from packs.ingestion.primitives.deep_context_v2.enrich.sol_identity import SolVerdict
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import EmployerFact, OwnedIdentifiers, SharedContextFact, SynthesizedFacts

URL = "https://www.linkedin.com/in/jordan-bravo"
OTHER = "https://www.linkedin.com/in/casey-example"
NOW = "2026-10-07T20:00:00Z"


def facts() -> SynthesizedFacts:
    return SynthesizedFacts("Jordan Bravo", (), (), "Founder", "", "", "Springfield", "", "unknown", (), (), (),
                            OwnedIdentifiers((), (), ()), (), 0.8, False)


def family() -> Family:
    member = MemberFacts("c1", "p:1", json.dumps(facts().to_payload()), NOW)
    return Family("p:1", ("c1", "c2"), (member,), ("f1",), FamilyWorth("yes", "machine"),
                  ("Jordan Bravo",), (Identifier("email", "jordan@example.com"),), facts(), 3, ())


def profile(url: str = URL, member_id: str = "4242") -> Profile:
    return Profile(url, url.rsplit("/", 1)[-1], member_id, "Jordan Bravo", "Founder", "Springfield",
                   ("Founder @ Example Labs, 2020-present",), (), NOW)


def held(candidate: str = "c1", verdict: str = "needs_review", origin: str = "linkedin_network",
         decided_by: str = "machine", seq: int = 1) -> LinkedinVerdict:
    return LinkedinVerdict(seq, candidate, URL, "4242", origin, verdict, decided_by)


def found(fam: Family | None = None, urls: tuple[str, ...] = (URL,)) -> Proposals:
    fam = fam or family()
    return Proposals([fam], {}, {fam.parent_id: [Proposal(url, "linkedin_network") for url in urls]}, {})


def task(fam: Family | None = None, urls: tuple[str, ...] = (URL,)) -> judge.JudgeTask:
    return judge.JudgeTask(fam or family(), tuple(judge.ProposedProfile("linkedin_network", profile(url, str(4242 + i)))
                                               for i, url in enumerate(urls)), "judgment", ())


def output(**changes: object) -> dict:
    content = {"real_name": "Jordan Bravo", "work_experience": [], "education": [], "summary": "Synthetic",
               "location_city": "Springfield", "linkedin_url": None}
    content.update(changes)
    return {"content": content, "basis": []}


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = open_store(self.root / "store.sqlite")
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO owner VALUES ('owner', ?, 'owner-fixture', ?)",
                          (json.dumps({"name": "Casey Example"}), NOW))
        queries.upsert_candidates(self.conn, [(cid, "Jordan Bravo", 0, "{}", NOW) for cid in ("c1", "c2")])
        queries_dedupe.append_parent_rows(self.conn, [(cid, "p:1", "singleton", None, NOW) for cid in ("c1", "c2")])
        self.conn.execute("INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?)",
                          ("c1", json.dumps(facts().to_payload()), "f1", "synthetic", "none", NOW))
        queries_worth.append_worth(self.conn, [("c1", "yes", "machine", "fixture", "{}", "worth", NOW)])
        self.conn.execute("INSERT INTO candidate_identifiers VALUES (?, 'email', ?, ?)",
                          ("c2", "jordan@example.com", "jordan@example.com"))
        self.conn.commit()

    def cache(self, url: str = URL, **changes: object) -> None:
        normalized = {"success": True, "member_id": "4242", "full_name": "Jordan Bravo", "headline": "Founder",
                      "city": "Springfield", "experiences": [], "education": []}
        normalized.update(changes)
        record = {"normalized_profile": normalized, "raw_response": {}, "fetched_at": NOW}
        directory = self.root / profiles.CACHE_RELATIVE_DIR
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (url.rsplit("/", 1)[-1] + ".json")).write_text(json.dumps(record))

    def connection(self, email: str | None = "jordan@example.com") -> None:
        self.conn.execute("INSERT INTO connections VALUES (?, ?, ?, ?, ?, ?)",
                          (URL, "Jordan Bravo", email, "Founder", "Example Labs", NOW))
        self.conn.commit()


class FamilyAndProposalTests(StoreCase):
    def test_family_load_includes_members_without_facts_and_their_identifiers(self) -> None:
        actual = load_families(self.conn)[0]
        self.assertEqual(actual.candidates, ("c1", "c2"))
        self.assertEqual(len(actual.members), 1)
        self.assertEqual(actual.emails(), {"jordan@example.com"})
        self.assertEqual(actual.facts_fingerprints, ("f1",))
        self.assertEqual(family_evidence(actual)["facts"], actual.facts.to_payload())

    def test_family_without_worth_does_not_enter(self) -> None:
        self.conn.execute("DELETE FROM worth")
        self.assertEqual(load_families(self.conn), [])

    def test_fingerprint_order_invariant_but_origin_time_and_version_sensitive(self) -> None:
        inputs = [(URL, "research", NOW), (OTHER, "linkedin_network", NOW)]
        key = judgment_fingerprint(family(), inputs, "v1")
        self.assertEqual(key, judgment_fingerprint(family(), inputs[::-1], "v1"))
        self.assertNotEqual(key, judgment_fingerprint(family(), inputs, "v2"))
        self.assertNotEqual(key, judgment_fingerprint(family(), [(URL, "linkedin_network", NOW)], "v1"))
        self.assertNotEqual(key, judgment_fingerprint(family(), [(URL, "research", "later"), inputs[1]], "v1"))

    def test_email_confirmed_proposal_bypasses_judge_and_moves_all_candidates(self) -> None:
        self.connection()
        actual = proposals.derive(self.conn)
        self.assertEqual(actual.by_email, {"p:1": URL})
        self.assertEqual(actual.proposed, {})
        counts: dict[str, int] = {}
        parents = confirm_by_email(self.conn, actual, Profiles({URL: profile()}, 0, 0), NOW, counts)
        self.assertEqual(len(parents), 2)
        self.assertEqual(counts, {"email_confirmed": 1, "email_confirm_no_profile": 0})
        rows = self.conn.execute("SELECT verdict, confidence, reason FROM candidate_linkedins").fetchall()
        self.assertEqual([tuple(row) for row in rows], [("confirmed", None, EMAIL_CONFIRMED_REASON)] * 2)
        self.assertEqual(queries_dedupe.current_parents(self.conn), {"c1": "li:4242", "c2": "li:4242"})
        self.assertEqual(proposals.derive(self.conn).by_email, {})

    def test_email_confirmation_without_profile_leaves_parent_and_verdicts_untouched(self) -> None:
        self.connection()
        counts: dict[str, int] = {}
        self.assertEqual(confirm_by_email(self.conn, proposals.derive(self.conn), Profiles({}, 1, 0), NOW, counts), [])
        self.assertEqual(counts["email_confirm_no_profile"], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM candidate_linkedins").fetchone()[0], 0)

    def test_connection_and_research_same_url_is_one_network_proposal(self) -> None:
        self.connection(None)
        row = research.row_from_output(research.ResearchSubject("p:1", research.handle(facts()), "{}"),
                                       output(linkedin_url=URL), NOW)
        queries_enrich.upsert_research(self.conn, row)
        actual = proposals.derive(self.conn)
        self.assertEqual(actual.proposed, {"p:1": [Proposal(URL, "linkedin_network")]})
        self.assertEqual(proposals.all_urls(actual), [URL])

    def test_no_match_research_does_not_propose(self) -> None:
        queries_enrich.upsert_research(self.conn, (research.handle(facts()), "p:1", "no_match", json.dumps(output()), NOW))
        self.assertEqual(proposals.derive(self.conn).proposed, {})


class PreMatchTests(unittest.TestCase):
    def test_one_name_and_email_match_confirms(self) -> None:
        actual = pre_match.pre_match_families([family()], [Connection(URL, "Jordan Bravo", "Founder")],
                                            {URL: "jordan@example.com"})
        self.assertEqual(actual.by_email, {"p:1": URL})

    def test_two_matching_connections_are_only_proposals(self) -> None:
        actual = pre_match.pre_match_families([family()], [Connection(URL, "Jordan Bravo", ""),
                                                           Connection(OTHER, "Jordan Bravo", "")],
                                            {URL: "jordan@example.com"})
        self.assertEqual(actual.by_email, {})
        self.assertEqual(actual.matched, {"p:1": [URL, OTHER]})

    def test_every_written_name_must_match(self) -> None:
        fam = replace(family(), names=("Jordan Bravo", "Casey Example"))
        self.assertEqual(pre_match.pre_match_families([fam], [Connection(URL, "Jordan Bravo", "")], {}).matched, {})

    def test_no_blank_email_confirmation(self) -> None:
        fam = replace(family(), identifiers=())
        self.assertEqual(pre_match.pre_match_families([fam], [Connection(URL, "Jordan Bravo", "")], {}).by_email, {})

    def test_entry_excludes_existing_linkedin_human_answer_and_non_yes_worth(self) -> None:
        excluded = [replace(family(), parent_id="li:4242"),
                    replace(family(), verdicts=(held(decided_by="human"),)),
                    replace(family(), worth=FamilyWorth("maybe", "machine")),
                    replace(family(), worth=FamilyWorth("no", "machine"))]
        for fam in excluded:
            with self.subTest(fam=fam):
                self.assertFalse(pre_match.enters(fam))


class ProfileTests(StoreCase):
    def test_cached_urls_normalize_and_deduplicate_without_provider(self) -> None:
        self.cache()
        with patch.object(profiles.RapidApiClient, "get_profile", side_effect=AssertionError("paid call")):
            actual = profiles.load_profiles(self.root, [URL, URL + "/?tracking=fixture"], fetch=True)
        self.assertEqual((len(actual.found), actual.missing, actual.fetched), (1, 0, 0))
        self.assertEqual(actual.found[URL].location, "Springfield")
        self.assertTrue(actual.found[URL].real)

    def test_missing_or_memberless_cache_is_missing(self) -> None:
        self.cache(member_id="")
        actual = profiles.load_profiles(self.root, [URL, OTHER], fetch=False)
        self.assertEqual(actual, Profiles({}, 2, 0))

    def test_fetch_writes_cache_once_for_duplicate_url(self) -> None:
        with patch.object(profiles.RapidApiClient, "get_profile", side_effect=lambda *a, **kw: self.cache()) as fetch:
            actual = profiles.load_profiles(self.root, [URL, URL], fetch=True)
        self.assertEqual(actual.fetched, 1)
        self.assertEqual(fetch.call_count, 1)
        self.assertIn(URL, actual.found)

    def test_unsuccessful_fetch_is_counted_missing(self) -> None:
        with patch.object(profiles.RapidApiClient, "get_profile", return_value={}) as fetch:
            actual = profiles.load_profiles(self.root, [URL], fetch=True)
        self.assertEqual(actual, Profiles({}, 1, 1))
        fetch.assert_called_once()

    def test_provider_exception_propagates_without_fabricated_profile(self) -> None:
        with patch.object(profiles.RapidApiClient, "get_profile", side_effect=RuntimeError("synthetic provider failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic provider failure"):
                profiles.load_profiles(self.root, [URL], fetch=True)

    def test_profile_rendering_dates_school_and_location(self) -> None:
        record = {"fetched_at": NOW, "normalized_profile": {
            "member_id": 4242, "full_name": " Jordan Bravo ", "location_str": "", "city": "Springfield",
            "state": "Example State", "country": "US", "experiences": [
                {"title": "Founder", "company_name": "Example Labs", "starts_at": {"year": 2020}}, {}],
            "education": [{"degree": "BS", "fieldOfStudy": "Computing", "schoolName": "Example College"}, {}]}}
        actual = profiles.profile_from_record(URL, record)
        self.assertEqual(actual.member_id, "4242")
        self.assertEqual(actual.experiences, ("Founder @ Example Labs, 2020-present", "? @ ?"))
        self.assertEqual(actual.education, ("BS, Computing — Example College",))
        self.assertEqual(actual.location, "Springfield, Example State, US")
        self.assertEqual(actual.judge_view()["experiences"], list(actual.experiences))

    def test_shell_is_not_real(self) -> None:
        self.assertFalse(replace(profile(), location="", experiences=()).real)
        self.assertFalse(replace(profile(), full_name="").real)


class ResearchTests(unittest.TestCase):
    def test_identical_facts_researched_once_independent_of_parent(self) -> None:
        fam = family()
        subjects = research.subjects([fam, replace(fam, parent_id="p:2")], pre_match.PreMatch({}, {}), {})
        self.assertEqual(len(subjects), 1)
        self.assertEqual(json.loads(subjects[0].dossier), fam.facts.to_payload())
        self.assertNotEqual(research.handle(fam.facts), research.handle(replace(fam.facts, title="Engineer")))

    def test_existing_status_or_pre_match_prevents_research(self) -> None:
        for status in ("complete", "no_match", "failed"):
            with self.subTest(status=status):
                self.assertEqual(research.subjects([family()], pre_match.PreMatch({}, {}),
                                                  {research.handle(facts()): Research(status, None)}), [])
        self.assertEqual(research.subjects([family()], pre_match.PreMatch({"p:1": [URL]}, {}), {}), [])

    def test_output_status_and_synthetic_card_retention(self) -> None:
        subject = research.ResearchSubject("p:1", "h", "{}")
        complete = research.row_from_output(subject, output(linkedin_url=URL), NOW)
        self.assertEqual(complete[2], "complete")
        self.assertEqual(research.research_url(Research(complete[2], complete[3])), URL)
        retained = research.row_from_output(subject, output(), NOW)
        self.assertEqual(retained[2], "no_match")
        self.assertIsNotNone(retained[3])
        thin = research.row_from_output(subject, output(real_name=None, location_city=None), NOW)
        self.assertEqual(thin[2:4], ("no_match", None))


class JudgePlanTests(unittest.TestCase):
    def test_alias_urls_with_same_member_are_one_candidate(self) -> None:
        plan = judge.plan(found(urls=(URL, OTHER)), {URL: profile(), OTHER: profile(OTHER)}, set())
        self.assertEqual(plan.entering, 1)
        self.assertEqual(len(plan.tasks[0].candidates), 1)

    def test_missing_profile_count_and_already_judged_all_members(self) -> None:
        self.assertEqual(judge.plan(found(), {}, set()), judge.JudgePlan([], 1, 1, 0))
        first = judge.plan(found(), {URL: profile()}, set()).tasks[0]
        done = {(cid, first.fingerprint) for cid in family().candidates}
        self.assertEqual(judge.plan(found(), {URL: profile()}, done), judge.JudgePlan([], 1, 0, 1))
        self.assertEqual(len(judge.plan(found(), {URL: profile()}, {("c1", first.fingerprint)}).tasks), 1)

    def test_wrong_profile_excluded_only_when_every_member_holds_wrong(self) -> None:
        wrong = replace(family(), verdicts=(held(verdict="wrong_person"), held("c2", "wrong_person")))
        self.assertEqual(judge.plan(found(wrong), {URL: profile()}, set()), judge.JudgePlan([], 0, 0, 0))
        partial = replace(wrong, verdicts=wrong.verdicts[:1])
        self.assertEqual(len(judge.plan(found(partial), {URL: profile()}, set()).tasks), 1)

    def test_human_answer_no_worth_and_linkedin_parent_excluded(self) -> None:
        for fam in (replace(family(), verdicts=(held(decided_by="human"),)),
                    replace(family(), parent_id="li:4242"), replace(family(), worth=FamilyWorth("no", "machine"))):
            with self.subTest(fam=fam):
                self.assertEqual(judge.plan(found(fam), {URL: profile()}, set()).entering, 0)
        self.assertEqual(len(judge.plan(found(replace(family(), worth=FamilyWorth("maybe", "machine"))),
                                        {URL: profile()}, set()).tasks), 1)

    def test_citations_deduplicated_and_non_evidence_fields_removed(self) -> None:
        row = Research("complete", json.dumps({"basis": [{"citations": [
            {"url": "https://example.com", "title": "Synthetic", "irrelevant": 1},
            {"url": "https://example.com", "title": "Synthetic"}]}]}))
        self.assertEqual(judge._citations(row), ({"url": "https://example.com", "title": "Synthetic"},))


class JudgeWriteTests(StoreCase):
    def test_confirm_splits_previously_wrong_member_and_retains_verdict_reference(self) -> None:
        seq = queries_enrich.insert_linkedin(self.conn, ("c1", URL, "4242", "research", "wrong_person", "machine",
                                                       "old", 0.9, "Different person", NOW))
        actual = task(replace(family(), verdicts=(held(verdict="wrong_person", seq=seq),)))
        counts = judge.write(self.conn, actual, {URL: SolVerdict("confirmed", 0.8, "Matches")}, NOW)
        self.assertEqual(counts["split"], 1)
        self.assertEqual(counts["parent_rows"], 2)
        parents = queries_dedupe.current_parents(self.conn)
        self.assertTrue(parents["c1"].startswith("p:"))
        self.assertNotEqual(parents["c1"], "p:1")
        self.assertEqual(parents["c2"], "li:4242")
        self.assertEqual(self.conn.execute("SELECT verdict_ref FROM candidate_parent WHERE reason='judge_wrong_person'").fetchone()[0],
                         f"candidate_linkedins:{seq}")

    def test_wrong_and_review_write_each_member_without_parent_moves(self) -> None:
        counts = judge.write(self.conn, task(urls=(URL, OTHER)),
                             {URL: SolVerdict("wrong_person", 0.9, "Different"),
                              OTHER: SolVerdict("needs_review", 0.5, "Ambiguous")}, NOW)
        self.assertEqual(counts, {"confirmed": 0, "wrong_person": 1, "needs_review": 1, "split": 0, "parent_rows": 0})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM candidate_linkedins").fetchone()[0], 4)
        self.assertEqual(set(queries_dedupe.current_parents(self.conn).values()), {"p:1"})

    def test_multiple_sol_confirmations_become_review_with_reasons_preserved(self) -> None:
        caller = SimpleNamespace(call=AsyncMock(return_value={
            "verdict": "confirmed", "confidence": 0.8, "reason": "Matches", "supporting_evidence": [],
            "contradicting_evidence": [], "linkedin_plausibly_absent": False, "recommend_deep_research": False}))
        actual = asyncio.run(judge.sol(caller, task(urls=(URL, OTHER)), "Synthetic owner"))
        self.assertEqual(set(actual), {URL, OTHER})
        self.assertEqual(set(actual.values()), {SolVerdict("needs_review", 0.8, "Matches")})
        self.assertEqual(caller.call.await_count, 2)

    def test_sol_rejects_missing_invalid_and_out_of_range_answers(self) -> None:
        for answer in ({}, {"verdict": "invented", "confidence": 0.5, "reason": "x"},
                       {"verdict": "confirmed", "confidence": 2, "reason": "x"}):
            caller = SimpleNamespace(call=AsyncMock(return_value=answer))
            with self.subTest(answer=answer), self.assertRaises(jsonschema.ValidationError):
                asyncio.run(sol_identity.verdict(caller, "synthetic prompt"))


class SettleTests(unittest.TestCase):
    def test_under_message_bar_settles_no_for_all_candidates(self) -> None:
        rows = settle.settle_rows([replace(family(), messages=24)], Profiles({}, 0, 0), NOW)
        self.assertEqual([row[:2] for row in rows], [("c1", "no"), ("c2", "no")])
        self.assertEqual(rows[0][3], settle.REASON)
        self.assertTrue(rows[0][5])
        self.assertEqual(settle.settle_rows([replace(family(), messages=25)], Profiles({}, 0, 0), NOW), [])

    def test_human_worth_and_already_no_are_untouched(self) -> None:
        for worth in (FamilyWorth("yes", "human"), FamilyWorth("no", "human"), FamilyWorth("no", "machine")):
            with self.subTest(worth=worth):
                self.assertEqual(settle.settle_rows([replace(family(), worth=worth)], Profiles({}, 0, 0), NOW), [])

    def test_network_review_is_worth_yes_wrong_is_not(self) -> None:
        fam = replace(family(), worth=FamilyWorth("maybe", "machine"), verdicts=(held(),))
        rows = settle.settle_rows([fam], Profiles({}, 0, 0), NOW)
        self.assertEqual([row[1] for row in rows], ["yes", "yes"])
        self.assertEqual(rows[0][3], settle.OWN_CONNECTION_REASON)
        self.assertFalse(settle.own_connection(replace(fam, verdicts=(held(verdict="wrong_person"),))))

    def test_confirmed_real_profile_requires_matching_parent(self) -> None:
        fam = replace(family(), parent_id="li:4242", verdicts=(held(verdict="confirmed", origin="research"),))
        cached = Profiles({URL: profile()}, 0, 0)
        self.assertTrue(settle.has_real_profile(fam, cached))
        self.assertEqual(settle.confirmed_urls([fam]), [URL])
        self.assertEqual(settle.settle_rows([fam], cached, NOW), [])
        self.assertFalse(settle.has_real_profile(replace(fam, parent_id="p:1"), cached))
        self.assertFalse(settle.has_real_profile(fam, Profiles({}, 1, 0)))

    def test_human_accepted_synthetic_card_counts_as_real_profile(self) -> None:
        fam = replace(family(), verdicts=(held(verdict="confirmed", origin="synthetic", decided_by="human"),))
        self.assertTrue(settle.has_real_profile(fam, Profiles({}, 0, 0)))


class NodeReachabilityTests(StoreCase):
    def test_research_dry_run_has_no_writes_or_provider_calls(self) -> None:
        before = self.conn.total_changes
        actual = research.ResearchStep(self.conn, self.root, limit=1).estimate()
        self.assertEqual(actual["families_to_research"], 1)
        self.assertEqual(actual["estimated_cost_usd"], 0.05)
        self.assertEqual(self.conn.total_changes, before)

    def test_enrich_run_reaches_email_confirmation_and_persists_manifest(self) -> None:
        self.connection()
        self.cache()
        # Concrete provider boundaries only: keep derive, judge planning, writes, settlement and Node.run real.
        fake_caller = AsyncMock()
        with patch.object(jev_identity, "answer_all", new=AsyncMock(return_value=[])), \
             patch.object(judge.OpenAIResponsesConfig, "resolve", return_value=SimpleNamespace()), \
             patch.object(judge, "OpenAIResponsesCaller", return_value=fake_caller):
            manifest = Enrich(self.conn, self.root, limit=None).run()
        self.assertEqual(manifest.status, "completed")
        self.assertEqual(manifest.counts["email_confirmed"], 1)
        self.assertEqual(manifest.counts["judge_families"], 0)
        self.assertEqual(manifest.counts["settle_worth_rows"], 0)
        self.assertEqual(set(queries_dedupe.current_parents(self.conn).values()), {"li:4242"})
        saved = json.loads((self.root / "deep-context/v2-manifests/enrich.json").read_text())
        self.assertEqual(saved["counts"], manifest.counts)


class ProviderFlowTests(StoreCase):
    def caller(self, answer: dict | None = None, error: Exception | None = None) -> MagicMock:
        fake = MagicMock()
        fake.__aenter__ = AsyncMock(return_value=SimpleNamespace(call=AsyncMock(return_value=answer, side_effect=error)))
        fake.__aexit__ = AsyncMock(return_value=False)
        return fake

    def test_jev_alone_confirms_only_one_profile_without_sol_call(self) -> None:
        fake = self.caller(error=AssertionError("Sol must not run"))
        with patch.object(jev_identity, "answer_all", new=AsyncMock(return_value=["confirmed", "needs_review"])), \
             patch.object(judge, "OpenAIResponsesCaller", return_value=fake):
            counts = asyncio.run(judge.decide(self.conn, [task(urls=(URL, OTHER))], self.root, "Synthetic owner", NOW))
        self.assertEqual(counts["confirmed"], 1)
        self.assertEqual(counts["judge_failed"], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM candidate_linkedins").fetchone()[0], 2)
        fake.__aenter__.return_value.call.assert_not_awaited()

    def test_failed_sol_is_counted_and_has_no_verdict_or_parent_writes(self) -> None:
        fake = self.caller(error=RuntimeError("synthetic provider failure"))
        with patch.object(jev_identity, "answer_all", new=AsyncMock(return_value=["needs_review"])), \
             patch.object(judge, "OpenAIResponsesCaller", return_value=fake), patch("builtins.print") as printed:
            counts = asyncio.run(judge.decide(self.conn, [task()], self.root, "Synthetic owner", NOW))
        self.assertEqual(counts, {"judge_failed": 1})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM candidate_linkedins").fetchone()[0], 0)
        printed.assert_called_once_with("failed one family: RuntimeError")

    def test_sol_after_pending_jev_writes_review_and_preserves_confidence(self) -> None:
        answer = {"verdict": "needs_review", "confidence": 0.6, "reason": "Ambiguous", "supporting_evidence": [],
                  "contradicting_evidence": [], "linkedin_plausibly_absent": False, "recommend_deep_research": False}
        with patch.object(jev_identity, "answer_all", new=AsyncMock(return_value=["needs_review"])), \
             patch.object(judge, "OpenAIResponsesCaller", return_value=self.caller(answer)):
            counts = asyncio.run(judge.decide(self.conn, [task()], self.root, "Synthetic owner", NOW))
        self.assertEqual(counts["needs_review"], 1)
        self.assertEqual([tuple(row) for row in self.conn.execute("SELECT confidence, reason FROM candidate_linkedins")],
                         [(0.6, "Ambiguous")] * 2)


    def test_parallel_events_write_complete_no_match_and_failed_results(self) -> None:
        from parallel.types import TaskGroupStatusEvent, TaskRunEvent

        subjects = [research.ResearchSubject("p:1", key, "{}") for key in ("complete", "no_match", "failed")]
        def event(key: str, status: str, result: dict | None = None) -> TaskRunEvent:
            payload = None if result is None else SimpleNamespace(model_dump=lambda **kw: result)
            return TaskRunEvent.model_construct(
                type="task_run.state", run=SimpleNamespace(metadata={"handle": key}, status=status, is_active=False,
                                                            run_id="synthetic-run"), output=payload)

        client = MagicMock()
        client.task_group.create.return_value.task_group_id = "synthetic-group"
        client.task_run.result.return_value.output = SimpleNamespace(model_dump=lambda **kw: output(linkedin_url=URL))
        done = TaskGroupStatusEvent.model_construct(type="task_group_status", status=SimpleNamespace(is_active=False))
        client.task_group.events.return_value.__enter__.return_value = iter([
            object(), event("complete", "completed"), event("no_match", "completed", output()),
            event("failed", "failed"), done])
        with patch.object(research, "Parallel", return_value=client), patch.dict("os.environ", {"PARALLEL_API_KEY": "synthetic"}):
            counts = research.submit(self.conn, subjects)
        self.assertEqual(counts, {"research_submitted": 3, "complete": 1, "no_match": 1, "failed": 1})
        stored = queries_enrich.research_by_handle(self.conn)
        self.assertEqual({key: value.status for key, value in stored.items()},
                         {"complete": "complete", "no_match": "no_match", "failed": "failed"})
        self.assertIsNone(stored["failed"].result_json)
        self.assertIsNotNone(stored["no_match"].result_json)
        client.task_run.result.assert_called_once()
        client.task_group.add_runs.assert_called_once()

    def test_enrich_run_researches_then_judges_and_settles_thin_wrong_person(self) -> None:
        from parallel.types import TaskRunEvent

        self.cache()
        event = TaskRunEvent.model_construct(type="task_run.state",
            output=SimpleNamespace(model_dump=lambda **kw: output(linkedin_url=URL)),
            run=SimpleNamespace(is_active=False, status="completed", metadata={"handle": research.handle(facts())}))
        client = MagicMock()
        client.task_group.create.return_value.task_group_id = "synthetic-group"
        client.task_group.events.return_value.__enter__.return_value = iter([event])
        answer = {"verdict": "wrong_person", "confidence": 0.9, "reason": "Different", "supporting_evidence": [],
                  "contradicting_evidence": [], "linkedin_plausibly_absent": False, "recommend_deep_research": False}
        with patch.object(research, "Parallel", return_value=client), \
             patch.dict("os.environ", {"PARALLEL_API_KEY": "synthetic"}), \
             patch.object(jev_identity, "answer_all", new=AsyncMock(return_value=["needs_review"])), \
             patch.object(judge, "OpenAIResponsesCaller", return_value=self.caller(answer)):
            manifest = Enrich(self.conn, self.root, limit=1).run()
        self.assertEqual(manifest.status, "completed")
        self.assertEqual(manifest.counts["research_submitted"], 1)
        self.assertEqual(manifest.counts["judge_families"], 1)
        self.assertEqual(manifest.counts["wrong_person"], 1)
        self.assertEqual(manifest.counts["settle_worth_rows"], 2)
        self.assertEqual(queries_enrich.family_worth(self.conn), {"p:1": FamilyWorth("no", "machine")})

    def test_research_node_run_uses_real_subjects_and_authorized_store_writes(self) -> None:
        from parallel.types import TaskRunEvent

        result = SimpleNamespace(model_dump=lambda **kw: output(linkedin_url=URL))
        event = TaskRunEvent.model_construct(type="task_run.state", output=result,
                                             run=SimpleNamespace(is_active=False, status="completed",
                                                                 metadata={"handle": research.handle(facts())}))
        client = MagicMock()
        client.task_group.create.return_value.task_group_id = "synthetic-group"
        client.task_group.events.return_value.__enter__.return_value = iter([event])
        with patch.object(research, "Parallel", return_value=client), patch.dict("os.environ", {"PARALLEL_API_KEY": "synthetic"}):
            manifest = research.ResearchStep(self.conn, self.root, limit=1).run()
        self.assertEqual(manifest.status, "completed")
        self.assertEqual(manifest.counts["complete"], 1)
        self.assertEqual(proposals.derive(self.conn).proposed, {"p:1": [Proposal(URL, "research")]})


class CliTests(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.data_root = self.root / "data"
        self.sync_store()
        import tiktoken
        # Real tokenization with tiny local ranks; the model-rank loader otherwise downloads assets.
        encoder = tiktoken.Encoding(name="synthetic-byte", pat_str=r"(?s).",
                                    mergeable_ranks={bytes([i]): i for i in range(256)}, special_tokens={})
        tokenizer = patch("tiktoken.get_encoding", return_value=encoder)
        tokenizer.start()
        self.addCleanup(tokenizer.stop)

    def sync_store(self) -> None:
        from packs.ingestion.primitives.deep_context_v2.db.store import store_path
        target = store_path(self.data_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        disk = open_store(target)
        self.conn.commit()
        self.conn.backup(disk)
        disk.close()
        cached = self.root / profiles.CACHE_RELATIVE_DIR
        if cached.exists():
            shutil.copytree(cached, self.data_root / profiles.CACHE_RELATIVE_DIR, dirs_exist_ok=True)

    def payload(self, function: Callable[[list[str]], int], flags: list[str]) -> dict:
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = function(["--data-root", str(self.data_root), *flags])
        self.assertEqual(code, 0)
        return json.loads(stream.getvalue())

    def test_enrich_and_judge_dry_runs_price_uncached_and_cached_requests(self) -> None:
        self.connection(None)
        self.cache()
        self.sync_store()
        actual = self.payload(enrich.main, ["--dry-run", "--limit", "1"])
        self.assertEqual(actual["judge_families"], 1)
        self.assertEqual(actual["jev_requests"], 2)
        self.assertEqual(actual["jev_cached"], 0)
        self.assertEqual(actual["sol_calls_at_most"], 1)
        first = proposals.derive(self.conn)
        planned = judge.plan(first, profiles.load_profiles(self.root, proposals.all_urls(first), fetch=False).found, set())
        from packs.search.primitives.llm_rerank_candidates.jev.client import cache_path, request_digest
        for pair in judge.jev_pairs(planned.tasks[0]):
            for view, request in pair.items():
                path = cache_path(self.data_root / judge.JEV_CACHE_RELATIVE_DIR / view, request_digest(request))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}")
        actual = self.payload(judge.main, ["--limit", "1"])
        self.assertEqual(actual["profiles_to_judge"], 1)
        self.assertEqual(actual["jev_cached"], 2)
        self.assertEqual(actual["jev_input_tokens"], 0)
        self.assertEqual(actual["jev_cost_usd"], 0)

    def test_enrich_cli_runs_email_confirmation_without_sol_calls(self) -> None:
        self.connection()
        self.cache()
        self.sync_store()
        fake = MagicMock()
        fake.__aenter__ = AsyncMock(return_value=SimpleNamespace(call=AsyncMock(side_effect=AssertionError("Sol call"))))
        fake.__aexit__ = AsyncMock(return_value=False)
        stream = io.StringIO()
        with patch.object(jev_identity, "answer_all", new=AsyncMock(return_value=[])), \
             patch.object(judge, "OpenAIResponsesCaller", return_value=fake), redirect_stdout(stream):
            self.assertEqual(enrich.main(["--data-root", str(self.data_root), "--limit", "0"]), 0)
        self.assertIn("completed", stream.getvalue())
        self.assertIn("'email_confirmed': 1", stream.getvalue())
        fake.__aenter__.return_value.call.assert_not_awaited()

    def test_research_cli_runs_with_external_sdk_events(self) -> None:
        from parallel.types import TaskRunEvent
        event = TaskRunEvent.model_construct(type="task_run.state",
            output=SimpleNamespace(model_dump=lambda **kw: output()),
            run=SimpleNamespace(is_active=False, status="completed", metadata={"handle": research.handle(facts())}))
        client = MagicMock()
        client.task_group.create.return_value.task_group_id = "synthetic-group"
        client.task_group.events.return_value.__enter__.return_value = iter([event])
        stream = io.StringIO()
        with patch.object(research, "Parallel", return_value=client), \
             patch.dict("os.environ", {"PARALLEL_API_KEY": "synthetic"}), redirect_stdout(stream):
            self.assertEqual(research.main(["--data-root", str(self.data_root), "--limit", "1"]), 0)
        self.assertIn("'no_match': 1", stream.getvalue())

    def test_pre_match_cli_reports_one_and_several_matching_connections(self) -> None:
        self.connection(None)
        self.sync_store()
        actual = self.payload(pre_match.main, [])
        self.assertEqual(actual["exactly_one_match"], 1)
        self.assertEqual(actual["proposed_urls"], 1)
        self.conn.execute("INSERT INTO connections VALUES (?, ?, NULL, '', '', ?)", (OTHER, "Jordan Bravo", NOW))
        self.sync_store()
        actual = self.payload(pre_match.main, [])
        self.assertEqual(actual["several_matches"], 1)
        self.assertEqual(actual["proposed_urls"], 2)

    def test_profile_cli_limits_to_one_missing_profile(self) -> None:
        self.connection(None)
        self.sync_store()
        def fetched(*args: object, **kwargs: object) -> None:
            directory = Path(kwargs["cache_dir"])
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "jordan-bravo.json").write_text(json.dumps({"fetched_at": NOW, "raw_response": {},
                "normalized_profile": {"success": True, "member_id": "4242", "full_name": "Jordan Bravo"}}))
        with patch.object(profiles.RapidApiClient, "get_profile", side_effect=fetched) as fetch:
            self.assertEqual(self.payload(profiles.main, ["--limit", "1"]), {"fetched": 1, "found": 1, "missing": 0})
        fetch.assert_called_once()

    def test_pre_match_cli_reports_zero_matches(self) -> None:
        actual = self.payload(pre_match.main, [])
        self.assertEqual(actual["families_worth_yes_on_p"], 1)
        self.assertEqual(actual["matched_families"], 0)

    def test_profile_cli_dry_run_reports_missing_without_fetch(self) -> None:
        actual = self.payload(profiles.main, ["--dry-run"])
        self.assertEqual(actual, {"urls": 0, "cached": 0, "missing": 0})

    def test_profile_cli_zero_limit_has_no_provider_calls(self) -> None:
        actual = self.payload(profiles.main, ["--limit", "0"])
        self.assertEqual(actual, {"fetched": 0, "found": 0, "missing": 0})

    def test_settle_cli_reports_family_and_candidate_counts(self) -> None:
        self.assertEqual(self.payload(settle.main, []), {"families_to_yes": 0, "families_to_no": 1, "worth_rows": 2})

    def test_research_cli_dry_run_respects_zero_limit(self) -> None:
        actual = self.payload(research.main, ["--dry-run", "--limit", "0"])
        self.assertEqual(actual["families_to_research"], 0)
        self.assertEqual(actual["estimated_cost_usd"], 0)


class JevTests(unittest.TestCase):
    def answers(self, strong: bool) -> tuple[dict, dict]:
        network = {"identity": {"probabilities": {"confirmed": 1.0, "needs_review": 0.0, "wrong_person": 0.0}},
                   "competing_identity": {"noul": 0.0}, "concrete_conflict": {"noul": 0.0},
                   "positive_connection": {"noul": 1.0}}
        association = {"association": {"probabilities": {"yes": 1.0, "review": 0.0, "no": 0.0}},
                       "source_conflict": {"noul": 0.0}, "specific_connection": {"noul": 1.0}}
        if not strong:
            network["competing_identity"]["noul"] = 1.0
            network["concrete_conflict"]["noul"] = 1.0
            network["positive_connection"]["noul"] = 0.0
            association["source_conflict"]["noul"] = 1.0
            association["specific_connection"]["noul"] = 0.0
        return network, association

    def test_calibrated_views_must_both_agree_and_clear_threshold(self) -> None:
        network, association = self.answers(True)
        self.assertGreaterEqual(jev_identity.probability(network, association), jev_identity.MODEL.threshold)
        self.assertEqual(jev_identity.classify(network, association), "confirmed")
        network["identity"]["probabilities"] = {"confirmed": 0.1, "wrong_person": 0.9}
        self.assertEqual(jev_identity.classify(network, association), "needs_review")
        network, association = self.answers(True)
        association["association"]["probabilities"] = {"yes": 0.1, "no": 0.9}
        self.assertEqual(jev_identity.classify(network, association), "needs_review")
        network, association = self.answers(False)
        self.assertEqual(jev_identity.classify(network, association), "needs_review")

    def test_request_provenance_and_view_policy(self) -> None:
        pair = jev_identity.requests(family_evidence(family()), profile().judge_view(), False, [OTHER, URL])
        self.assertEqual(set(pair), {"network", "association"})
        dossier = json.loads(pair["network"]["state"]["dossier"])
        self.assertFalse(dossier["network_context"]["proposed_url_in_imported_linkedin_record"])
        self.assertEqual(dossier["network_context"]["same_parent_imported_urls"], sorted([OTHER, URL]))
        self.assertIn("web research", dossier["network_context"]["provenance"])
        self.assertNotEqual(pair["network"]["state"]["evidence_policy"], pair["association"]["state"]["evidence_policy"])

    def test_answer_all_deduplicates_requests_and_preserves_pair_order(self) -> None:
        pair = jev_identity.requests({}, profile().judge_view(), True, [URL])
        network, association = self.answers(True)
        async def answers(batch: dict, **kwargs: object) -> dict:
            response = network if Path(kwargs["output_dir"]).name == "network" else association
            return {digest: SimpleNamespace(response={"answers": response}) for digest in batch}
        with tempfile.TemporaryDirectory() as directory, patch.object(jev_identity, "answer_requests", side_effect=answers) as provider:
            actual = asyncio.run(jev_identity.answer_all([pair, pair], Path(directory)))
        self.assertEqual(actual, ["confirmed", "confirmed"])
        self.assertEqual(provider.await_count, 2)
        self.assertTrue(all(len(call.args[0]) == 1 for call in provider.call_args_list))

    def test_sol_prompt_renders_optional_evidence_and_omits_empty_fields(self) -> None:
        enriched = replace(facts(), relationship_to_owner="Synthetic colleague", school="Example College",
                           employers=(EmployerFact("Example Labs", "Founder", "current"), EmployerFact("", "", "unknown")),
                           topics=("databases",), shared_context=(SharedContextFact("employer", "Synthetic project", "fixture"),))
        fam = replace(family(), facts=enriched, identifiers=(Identifier("phone", "+15550100"),))
        prompt = sol_identity.identity_prompt(fam, profile(), "research", (), "Synthetic owner")
        for text in ("Synthetic colleague", "Example Labs", "Example College", "databases", "Synthetic project", "+15550100"):
            self.assertIn(text, prompt)
        minimal = replace(fam, facts=replace(enriched, title="", employers=(), topics=(), location=""), identifiers=())
        prompt = sol_identity.identity_prompt(minimal, profile(), "research", (), "Synthetic owner")
        self.assertNotIn("my address-book contact handles", prompt)
        self.assertNotIn("work: ", prompt)

    def test_sol_prompt_contains_origin_specific_domain_policy_and_citations(self) -> None:
        research_prompt = sol_identity.identity_prompt(family(), profile(), "research",
                                                      ({"url": "https://example.com", "title": "Synthetic"},), "Synthetic owner")
        network_prompt = sol_identity.identity_prompt(family(), profile(), "linkedin_network", (), "Synthetic owner")
        self.assertIn("Synthetic owner", research_prompt)
        self.assertIn("independent source", research_prompt)
        self.assertIn("https://example.com", research_prompt)
        self.assertIn("DOMAIN matching", network_prompt)
        self.assertIn("Jordan Bravo", network_prompt)


if __name__ == "__main__":
    unittest.main()
