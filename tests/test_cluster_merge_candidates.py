"""Deterministic identifier matching in the merge-candidate clusterer.

The regression these lock in: two records for the same human, carrying the same
phone number in different formats ('(m)/(c) 914-555-0466' in a signature vs
'+19145550466' on a contact record), must meet as the SAME normalized key —
paired by blocking, merged in code when the names are identical, and surfaced
to the judge as a computed SHARED IDENTIFIERS section when they are not.
All names/identifiers here are synthetic.
"""
import unittest

from packs.ingestion.primitives.deep_context import cluster_merge_candidates as cmc
from packs.ingestion.primitives.deep_context.common import normalize_name


def person(name, emails=(), extra_emails=(), phones=(), extra_phones=()):
    return {
        "slug": name.lower().replace(" ", "-"),
        "person_id": f"pid-{name.lower().replace(' ', '-')}",
        "name": name,
        "name_key": normalize_name(name),
        "emails": list(emails),
        "extra_emails": list(extra_emails),
        "phone_digits": list(phones),
        "extra_phones": list(extra_phones),
        "profile": {},
        "from_me": [],
        "from_them": [],
    }


class TestIdentifierPhones(unittest.TestCase):
    def test_signature_and_e164_meet_as_one_key(self):
        mined = cmc.identifier_phones(["(m)/(c) 914-555-0466", "+19145550466"])
        self.assertEqual(mined, {"9145550466"})

    def test_emails_urls_and_short_digits_are_skipped(self):
        self.assertEqual(cmc.identifier_phones([
            "casey@example.com",
            "https://example.com/in/casey-91455504",
            "example.com/casey",
            "ext 12345",
        ]), set())

    def test_non_us_number_keeps_full_digits(self):
        self.assertEqual(cmc.identifier_phones(["+44 20 7946 0958"]), {"442079460958"})


class TestSlamDunkVerdict(unittest.TestCase):
    def test_identical_name_plus_shared_phone_merges_in_code(self):
        a = person("Jordan Bravo", extra_phones=["9145550466"])
        b = person("Jordan Bravo", phones=["9145550466"])
        verdict = cmc.slam_dunk_verdict(a, b)
        self.assertIsNotNone(verdict)
        self.assertTrue(verdict["same_person"])
        self.assertGreaterEqual(verdict["confidence"], 0.99)
        self.assertIn("deterministic", verdict["reason"])

    def test_identical_name_plus_shared_email_merges_in_code(self):
        a = person("Jordan Bravo", emails=["jordan@example.com"])
        b = person("Jordan Bravo", extra_emails=["jordan@example.com"])
        self.assertTrue(cmc.slam_dunk_verdict(a, b)["same_person"])

    def test_different_names_go_to_the_judge(self):
        a = person("Jordan Bravo", phones=["9145550466"])
        b = person("Casey Bravo", phones=["9145550466"])
        self.assertIsNone(cmc.slam_dunk_verdict(a, b))

    def test_identical_name_without_shared_identifier_goes_to_the_judge(self):
        a = person("Jordan Bravo", emails=["jordan@example.com"])
        b = person("Jordan Bravo", phones=["9145550466"])
        self.assertIsNone(cmc.slam_dunk_verdict(a, b))


class TestSharedIdentifierNote(unittest.TestCase):
    def test_shared_phone_is_rendered_normalized_with_provenance(self):
        a = person("Jordan Bravo", extra_phones=["9145550466"])
        b = person("Casey Bravo", phones=["9145550466"])
        note = cmc.shared_identifier_note(a, b)
        self.assertIn("SHARED IDENTIFIERS", note)
        self.assertIn("+1 (914) 555-0466", note)
        self.assertIn("A: seen in messages", note)
        self.assertIn("B: contact record", note)

    def test_no_overlap_renders_nothing(self):
        a = person("Jordan Bravo", phones=["9145550466"])
        b = person("Casey Delta", phones=["3105550100"])
        self.assertEqual(cmc.shared_identifier_note(a, b), "")

    def test_judge_prompt_carries_the_section_only_on_overlap(self):
        a = person("Jordan Bravo", extra_phones=["9145550466"])
        b = person("Casey Bravo", phones=["9145550466"])
        self.assertIn("SHARED IDENTIFIERS", cmc.judge_prompt(a, b))
        c = person("Casey Delta", phones=["3105550100"])
        self.assertNotIn("SHARED IDENTIFIERS", cmc.judge_prompt(a, c))


class TestPairGeneration(unittest.TestCase):
    def test_message_discovered_phone_pairs_across_different_names(self):
        people = [
            person("Jordan Bravo", extra_phones=["9145550466"]),
            person("JB", phones=["9145550466"]),
            person("Casey Delta", phones=["3105550100"]),
        ]
        pairs = cmc.generate_pairs(people)
        self.assertIn((0, 1), pairs)
        self.assertNotIn((0, 2), pairs)

    def test_person_sig_changes_when_a_mined_phone_appears(self):
        before = cmc._person_sig(person("Jordan Bravo"))
        after = cmc._person_sig(person("Jordan Bravo", extra_phones=["9145550466"]))
        self.assertNotEqual(before, after)


class TestJudgeSystemRule(unittest.TestCase):
    def test_prompt_states_the_shared_phone_rule(self):
        self.assertIn("SHARED PHONE NUMBER", cmc.JUDGE_SYSTEM)
        self.assertIn("0.99", cmc.JUDGE_SYSTEM)


if __name__ == "__main__":
    unittest.main()
