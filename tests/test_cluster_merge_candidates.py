"""Deterministic identifier matching in the merge-candidate clusterer.

The regression these lock in: two records for the same human, carrying the same
phone number in different formats ('(m)/(c) 914-555-0466' in a signature vs
'+19145550466' on a contact record), must meet as the SAME normalized key —
paired by blocking, merged in code when the names are identical, and surfaced
to the judge as a computed SHARED IDENTIFIERS section when they are not.
All names/identifiers here are synthetic.
"""
import json
import tempfile
import unittest
from csv import DictReader
from pathlib import Path
from unittest import mock

import packs.ingestion.primitives.deep_context.merge_candidates.receipts as receipts
from packs.ingestion.primitives.deep_context.cluster_merge_candidates import ClusterMergeCandidates
from packs.ingestion.primitives.deep_context.common import normalize_name
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    FactRow,
    MergeVerdictRow,
    ParentRow,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
    PersonRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.merge_candidates.blocking import (
    generate_pairs,
    slam_dunk_verdict,
)
from packs.ingestion.primitives.deep_context.merge_candidates.judge import (
    JUDGE_SYSTEM,
    judge_prompt,
    shared_identifier_note,
)
from packs.ingestion.primitives.deep_context.merge_candidates.models import (
    MergePerson,
    identifier_phones,
    load_people,
)
from packs.ingestion.primitives.deep_context.merge_candidates.receipts import (
    load_cached_verdicts,
    pair_sig,
    person_sig,
    survey_pairs,
)


def person(name, emails=(), extra_emails=(), phones=(), extra_phones=()):
    slug = name.lower().replace(" ", "-")
    return MergePerson(
        slug=slug,
        person_id=f"pid-{slug}",
        name=name,
        name_key=normalize_name(name),
        emails=tuple(emails),
        extra_emails=tuple(extra_emails),
        phone_digits=tuple(phones),
        extra_phones=tuple(extra_phones),
    )


def seed_person(
    db, *, person_id, slug, name, facts_path, facts, phone="",
):
    parent_id = f"parent-{person_id}"
    rows = [
        ParentRow(parent_id, f"parent-worth:{parent_id}", name, slug),
        PersonRow(person_id, parent_id, slug, slug, name),
    ]
    if phone:
        rows.append(PersonIdentifiersProjection(person_id, (
            PersonIdentifierRow(person_id, "phone", phone, phone),
        )))
    rows.extend((
        ArtifactRow(
            f"facts:{parent_id}", "facts", parent_id, str(facts_path),
            "0" * 64, "projected",
        ),
        FactRow(
            parent_id, parent_id, f"facts:{parent_id}",
            facts_json=json.dumps(facts),
        ),
    ))
    db.project_rows(tuple(rows))


class TestIdentifierPhones(unittest.TestCase):
    def test_signature_and_e164_meet_as_one_key(self):
        mined = identifier_phones(["(m)/(c) 914-555-0466", "+19145550466"])
        self.assertEqual(mined, {"9145550466"})

    def test_emails_urls_and_short_digits_are_skipped(self):
        self.assertEqual(identifier_phones([
            "casey@example.com",
            "https://example.com/in/casey-91455504",
            "example.com/casey",
            "ext 12345",
        ]), set())

    def test_non_us_number_keeps_full_digits(self):
        self.assertEqual(identifier_phones(["+44 20 7946 0958"]), {"442079460958"})


class TestSlamDunkVerdict(unittest.TestCase):
    def test_identical_name_plus_shared_phone_merges_in_code(self):
        a = person("Jordan Bravo", extra_phones=["9145550466"])
        b = person("Jordan Bravo", phones=["9145550466"])
        verdict = slam_dunk_verdict(a, b)
        self.assertIsNotNone(verdict)
        self.assertTrue(verdict["same_person"])
        self.assertGreaterEqual(verdict["confidence"], 0.99)
        self.assertIn("deterministic", verdict["reason"])

    def test_identical_name_plus_shared_email_merges_in_code(self):
        a = person("Jordan Bravo", emails=["jordan@example.com"])
        b = person("Jordan Bravo", extra_emails=["jordan@example.com"])
        self.assertTrue(slam_dunk_verdict(a, b)["same_person"])

    def test_different_names_go_to_the_judge(self):
        a = person("Jordan Bravo", phones=["9145550466"])
        b = person("Casey Bravo", phones=["9145550466"])
        self.assertIsNone(slam_dunk_verdict(a, b))

    def test_identical_name_without_shared_identifier_goes_to_the_judge(self):
        a = person("Jordan Bravo", emails=["jordan@example.com"])
        b = person("Jordan Bravo", phones=["9145550466"])
        self.assertIsNone(slam_dunk_verdict(a, b))


class TestSharedIdentifierNote(unittest.TestCase):
    def test_shared_phone_is_rendered_normalized_with_provenance(self):
        a = person("Jordan Bravo", extra_phones=["9145550466"])
        b = person("Casey Bravo", phones=["9145550466"])
        note = shared_identifier_note(a, b)
        self.assertIn("SHARED IDENTIFIERS", note)
        self.assertIn("+1 (914) 555-0466", note)
        self.assertIn("A: owned message evidence", note)
        self.assertIn("B: contact record", note)

    def test_no_overlap_renders_nothing(self):
        a = person("Jordan Bravo", phones=["9145550466"])
        b = person("Casey Delta", phones=["3105550100"])
        self.assertEqual(shared_identifier_note(a, b), "")

    def test_judge_prompt_carries_the_section_only_on_overlap(self):
        a = person("Jordan Bravo", extra_phones=["9145550466"])
        b = person("Casey Bravo", phones=["9145550466"])
        self.assertIn("SHARED IDENTIFIERS", judge_prompt(a, b))
        c = person("Casey Delta", phones=["3105550100"])
        self.assertNotIn("SHARED IDENTIFIERS", judge_prompt(a, c))


class TestPairGeneration(unittest.TestCase):
    def test_owned_message_phone_pairs_across_different_names(self):
        people = [
            person("Jordan Bravo", extra_phones=["9145550466"]),
            person("JB", phones=["9145550466"]),
            person("Casey Delta", phones=["3105550100"]),
        ]
        pairs = generate_pairs(people)
        self.assertIn((0, 1), pairs)
        self.assertNotIn((0, 2), pairs)

    def test_person_sig_changes_when_an_owned_phone_appears(self):
        before = person_sig(person("Jordan Bravo"))
        after = person_sig(person("Jordan Bravo", extra_phones=["9145550466"]))
        self.assertNotEqual(before, after)


class TestOwnedIdentifierLoading(unittest.TestCase):
    """Only ownership-qualified message identifiers may create an identity edge."""

    def _load(self, facts):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dossiers, raw, facts_dir = base / "dossiers", base / "raw", base / "facts"
            for path in (dossiers, raw, facts_dir):
                path.mkdir()
            for slug, person_id, name in (("jordan-a", "a", "Jordan Alpha"), ("casey-b", "b", "Casey Bravo")):
                (dossiers / f"{slug}.md").write_text(
                    f'---\nname: "{name}"\nemails: []\nphones: []\n---\n', encoding="utf-8")
                (raw / f"{person_id}.json").write_text('{"messages": []}', encoding="utf-8")
            (facts_dir / "a.jsonl").write_text(json.dumps({"facts": facts}) + "\n", encoding="utf-8")
            db = Db(base / "deep-context.sqlite")
            seed_person(
                db, person_id="a", slug="jordan-a", name="Jordan Alpha",
                facts_path=facts_dir / "a.jsonl", facts=facts,
            )
            seed_person(
                db, person_id="b", slug="casey-b", name="Casey Bravo",
                facts_path=facts_dir / "b.jsonl", facts={}, phone="4155550100",
            )
            return load_people(db)

    def test_third_party_phone_in_untyped_identifiers_does_not_pair(self):
        people = self._load({
            "identifiers": ["Contact: Casey +1 415 555 0100"],
            "owned_identifiers": {"emails": [], "phones": [], "urls": []},
        })
        self.assertEqual(people[0].extra_phones, ())
        self.assertNotIn((0, 1), generate_pairs(people))

    def test_owned_message_phone_still_pairs_with_contact_record(self):
        people = self._load({
            "identifiers": ["+1 415 555 0100"],
            "owned_identifiers": {"emails": [], "phones": ["+1 415 555 0100"], "urls": []},
        })
        self.assertEqual(people[0].extra_phones, ("4155550100",))
        self.assertIn((0, 1), generate_pairs(people))

    def test_loads_one_merge_person_per_parent_with_union_identifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            parent_id = "parent-family"
            db.project_rows((
                ParentRow(parent_id, f"parent-worth:{parent_id}", "Jordan Bravo", "jordan-family"),
                PersonRow("child-b", parent_id, "jordan-b", "jordan-family", "Jordan B"),
                PersonRow("child-a", parent_id, "jordan-a", "jordan-family", "Jordan A"),
                PersonIdentifiersProjection("child-a", (
                    PersonIdentifierRow("child-a", "email", "a@example.com", "a@example.com"),
                )),
                PersonIdentifiersProjection("child-b", (
                    PersonIdentifierRow("child-b", "phone", "4155550100", "+1 415 555 0100"),
                )),
                ArtifactRow(
                    f"facts:{parent_id}", "facts", parent_id, str(root / "facts.jsonl"),
                    "0" * 64, "projected",
                ),
                FactRow(
                    parent_id, parent_id, f"facts:{parent_id}",
                    facts_json=json.dumps({"canonical_name": "Jordan Bravo"}),
                ),
            ))

            people = load_people(db)

            self.assertEqual(len(people), 1)
            self.assertEqual(people[0].parent_id, parent_id)
            self.assertEqual(people[0].person_id, "child-a")
            self.assertEqual(people[0].member_person_ids, ("child-a", "child-b"))
            self.assertEqual(people[0].emails, ("a@example.com",))
            self.assertEqual(people[0].phone_digits, ("4155550100",))


class TestJudgeSystemRule(unittest.TestCase):
    def test_prompt_states_the_shared_phone_rule(self):
        self.assertIn("SHARED PHONE NUMBER", JUDGE_SYSTEM)
        self.assertIn("0.99", JUDGE_SYSTEM)

    def test_pair_signature_bytes_stay_pinned(self):
        first = person(
            "Jordan Bravo", emails=["jordan@example.com"], phones=["9145550466"],
        )
        second = person(
            "Jordan Bravo", extra_emails=["jordan@example.com"],
            extra_phones=["9145550466"],
        )
        self.assertEqual(pair_sig(first, second), "ab7993a775baa257")


class TestCacheAndArtifacts(unittest.TestCase):
    def test_pre_signature_verdict_is_not_adopted(self):
        self.assertEqual(load_cached_verdicts([
            MergeVerdictRow("a", "b", "a", "b", "", "llm", 0, 0, 0),
        ]), {})
        self.assertFalse(hasattr(receipts, "load_legacy_verdicts"))

    def test_cache_key_resolves_representative_children_to_current_parents(self):
        cache = load_cached_verdicts((
            MergeVerdictRow(
                "child-a", "child-b", "a", "b", "evidence-v1", "llm",
                1, 0.9, 1, updated_at="2026-08-06T00:00:00Z",
            ),
        ), {"child-a": "parent-a", "child-b": "parent-b"})
        self.assertEqual(
            set(cache), {frozenset({"parent-a", "parent-b"})},
        )

    def test_shared_observed_identifier_never_enters_paid_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            seed_person(
                db, person_id="a", slug="jordan-a", name="Jordan Alpha",
                facts_path=root / "a.jsonl", facts={}, phone="4155550100",
            )
            seed_person(
                db, person_id="b", slug="casey-b", name="Casey Bravo",
                facts_path=root / "b.jsonl", facts={}, phone="4155550100",
            )

            survey = survey_pairs(db)

            self.assertEqual(len(survey.shared_unsettled), 1)
            self.assertEqual(survey.to_judge, [])

    def test_survey_splits_cache_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dossiers = root / "dossiers"
            raw = root / "raw"
            facts = root / "facts"
            for path in (dossiers, raw, facts):
                path.mkdir()
            db = Db(root / "deep-context.sqlite")
            with mock.patch.object(
                receipts, "split_cached_pairs", wraps=receipts.split_cached_pairs,
            ) as split:
                receipts.survey_pairs(db)
            split.assert_called_once()

    def test_deterministic_node_keeps_csv_contract_and_zero_legacy_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dossiers = root / "dossiers"
            raw = root / "raw"
            facts = root / "facts"
            for path in (dossiers, raw, facts):
                path.mkdir()
            db = Db(root / "deep-context.sqlite")
            for slug, person_id in (("jordan-a", "a"), ("jordan-b", "b")):
                (dossiers / f"{slug}.md").write_text(
                    '---\nname: "Jordan Bravo"\nemails: []\nphones: []\n---\n',
                    encoding="utf-8",
                )
                (raw / f"{person_id}.json").write_text('{"messages": []}', encoding="utf-8")
                facts_path = facts / f"{person_id}.jsonl"
                facts_path.write_text(json.dumps({"facts": {}}) + "\n", encoding="utf-8")
                seed_person(
                    db, person_id=person_id, slug=slug, name="Jordan Bravo",
                    facts_path=facts_path, facts={}, phone="9145550466",
                )
            output = root / "merge-candidates.csv"
            payload = ClusterMergeCandidates(
                db=db,
                dossier_dir=dossiers,
                out_csv=output,
                out_md=root / "merge-candidates.md",
                deterministic_only=True,
            ).run()

            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(DictReader(handle))
            self.assertEqual(list(rows[0]), [
                "slug_a", "name_a", "slug_b", "name_b", "confidence",
                "tone_consistent", "reason",
            ])
            self.assertEqual((rows[0]["slug_a"], rows[0]["slug_b"]), ("jordan-a", "jordan-b"))
            self.assertEqual(output.read_bytes(), (
                b"slug_a,name_a,slug_b,name_b,confidence,tone_consistent,reason\r\n"
                b"jordan-a,Jordan Bravo,jordan-b,Jordan Bravo,0.99,True,"
                b"deterministic: identical name + shared +1 (914) 555-0466\r\n"
            ))
            self.assertFalse(output.with_name("merge-verdicts.csv").exists())
            cached = canonical_snapshot(db).merge_verdicts
            self.assertEqual(len(cached), 1)
            self.assertEqual(cached[0].signature, "eeadbe96795d2bec")
            self.assertEqual(cached[0].accepted, 1)
            self.assertEqual(payload.pairs_legacy_adopted, 0)
            self.assertEqual(payload.pairs_deterministic, 1)


if __name__ == "__main__":
    unittest.main()
