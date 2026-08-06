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
        profile={},
    )


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
            index = {
                "slugs": {"jordan-a": {"person_id": "a"}, "casey-b": {"person_id": "b"}},
                "by_phone": {"4155550100": ["casey-b"]},
            }
            return load_people(index, dossiers, raw, facts_dir)

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
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "merge-verdicts.csv"
            path.write_text(
                "name_a,name_b,same_person,confidence\nJordan Bravo,Jordan B,true,0.9\n",
                encoding="utf-8",
            )
            self.assertEqual(load_cached_verdicts(path), {})
            self.assertFalse(hasattr(receipts, "load_legacy_verdicts"))

    def test_survey_splits_cache_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dossiers = root / "dossiers"
            raw = root / "raw"
            facts = root / "facts"
            for path in (dossiers, raw, facts):
                path.mkdir()
            index = root / "index.json"
            index.write_text('{"slugs": {}, "by_phone": {}}', encoding="utf-8")
            with mock.patch.object(
                receipts, "split_cached_pairs", wraps=receipts.split_cached_pairs,
            ) as split:
                receipts.survey_pairs(
                    index_json=index,
                    dossier_dir=dossiers,
                    raw_dir=raw,
                    facts_dir=facts,
                    verdicts_csv=root / "merge-verdicts.csv",
                    refresh=False,
                )
            split.assert_called_once()

    def test_deterministic_node_keeps_csv_contract_and_zero_legacy_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dossiers = root / "dossiers"
            raw = root / "raw"
            facts = root / "facts"
            for path in (dossiers, raw, facts):
                path.mkdir()
            index = root / "index.json"
            index.write_text(json.dumps({
                "slugs": {
                    "jordan-a": {"person_id": "a", "name": "Jordan Bravo"},
                    "jordan-b": {"person_id": "b", "name": "Jordan Bravo"},
                },
                "by_phone": {"9145550466": ["jordan-a", "jordan-b"]},
            }), encoding="utf-8")
            for slug, person_id in (("jordan-a", "a"), ("jordan-b", "b")):
                (dossiers / f"{slug}.md").write_text(
                    '---\nname: "Jordan Bravo"\nemails: []\nphones: []\n---\n',
                    encoding="utf-8",
                )
                (raw / f"{person_id}.json").write_text('{"messages": []}', encoding="utf-8")
            output = root / "merge-candidates.csv"
            payload = ClusterMergeCandidates(
                dossier_dir=dossiers,
                index_json=index,
                raw_dir=raw,
                facts_dir=facts,
                out_csv=output,
                out_md=root / "merge-candidates.md",
                no_llm=True,
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
            self.assertEqual(output.with_name("merge-verdicts.csv").read_bytes(), (
                b"slug_a,slug_b,name_a,name_b,same_person,confidence,tone_consistent,"
                b"reason,sig,judge\r\n"
                b"jordan-a,jordan-b,Jordan Bravo,Jordan Bravo,True,0.99,True,"
                b"deterministic: identical name + shared +1 (914) 555-0466,"
                b"eeadbe96795d2bec,slam_dunk\r\n"
            ))
            self.assertEqual(payload.pairs_legacy_adopted, 0)
            self.assertEqual(payload.pairs_deterministic, 1)


if __name__ == "__main__":
    unittest.main()
