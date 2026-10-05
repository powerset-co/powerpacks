"""Source identifier blocking, affirmative judging, and merge-cache contracts.

Extracted identifiers remain context; source identifiers select compatible
names and establish an exact-name duplicate. All fixtures are synthetic.
"""
import json
import tempfile
import unittest
from csv import DictReader
from dataclasses import replace
from pathlib import Path
from unittest import mock

import packs.ingestion.primitives.deep_context.merge_candidates.judge as judge
import packs.ingestion.primitives.deep_context.merge_candidates.receipts as receipts
from packs.ingestion.primitives.common.contact_fields import identifier_phones
from packs.ingestion.primitives.deep_context.merge_candidates.cluster_merge_candidates import (
    ClusterMergeCandidates,
    build_parser,
)
from packs.ingestion.primitives.deep_context.shared.common import normalize_name
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    FactRow,
    MergeVerdictRow,
    ParentRow,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
    PersonRow,
)
from packs.ingestion.primitives.deep_context.db.merge_queries import merge_people
from packs.ingestion.primitives.deep_context.db.queries import imported_people
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.pipeline.contract import PeopleRow
from packs.ingestion.primitives.deep_context.merge_candidates.candidate_pairs import (
    SAME_FIRST_AND_LAST_NAME,
    SAME_FULL_NAME,
    generate_pairs,
    names_can_match,
    name_words,
    jaro_winkler,
    slam_dunk_verdict,
    same_name_reason,
)
from packs.ingestion.primitives.deep_context.merge_candidates.judge import (
    JUDGE_SYSTEM,
    decision_from_answers,
    judge_prompt,
    shared_identifier_note,
)
from packs.ingestion.primitives.deep_context.merge_candidates.models import (
    CachedMergeVerdict,
    MergeDecision,
    MergePair,
    MergePerson,
)
from packs.ingestion.primitives.deep_context.merge_candidates.receipts import (
    load_cached_verdicts,
    pair_sig,
    survey_pairs,
)
from packs.ingestion.primitives.deep_context.shared.openai_responses import (
    OpenAIResponse, OpenAIUsage, estimate_cost_usd,
)
from packs.ingestion.primitives.deep_context.merge_candidates.judge import MODEL_ID


def person(name, emails=(), extra_emails=(), phones=(), extra_phones=()):
    slug = name.lower().replace(" ", "-")
    return MergePerson(
        slug=slug,
        person_id=f"pid-{slug}",
        name=name,
        name_key=normalize_name(name),
        source_names=(name,),
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
    db.replace_imported_people((*imported_people(db), PeopleRow(id=person_id, full_name=name)))


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
    def test_identical_name_plus_shared_contact_phone_merges_in_code(self):
        a = person("Jordan Bravo", phones=["9145550466"])
        b = person("Jordan Bravo", phones=["9145550466"])
        verdict = slam_dunk_verdict(a, b)
        self.assertIsNotNone(verdict)
        self.assertTrue(verdict.same_person)
        self.assertGreaterEqual(verdict.confidence, 0.99)
        self.assertIn("slam dunk", verdict.reason)

    def test_identical_name_plus_shared_contact_email_merges_in_code(self):
        a = person("Jordan Bravo", emails=["jordan@example.com"])
        b = person("Jordan Bravo", emails=["jordan@example.com"])
        self.assertTrue(slam_dunk_verdict(a, b).same_person)

    def test_different_names_go_to_the_judge(self):
        a = person("Jordan Bravo", phones=["9145550466"])
        b = person("Casey Bravo", phones=["9145550466"])
        self.assertIsNone(slam_dunk_verdict(a, b))

    def test_same_full_name_without_a_source_identifier_is_not_free(self):
        a = person("Jordan Bravo", emails=["jordan@example.com"])
        b = person("Jordan Bravo", phones=["9145550466"])
        verdict = slam_dunk_verdict(a, b)
        self.assertIsNone(verdict)

    def test_same_full_name_in_another_word_order_is_compatible(self):
        for first, second in (
            ("Jordan Bravo", "Bravo, Jordan"),
            ("Jordan Bravo", "Bravo Jordan"),
            ("Jordan Bravo", "jordan  BRAVO"),
            ("Jordan O'Bravo", "Jordan O\u2019Bravo"),
            ("\u00c9mile Bravo", "E\u0301mile Bravo"),     # one accent, composed and decomposed
            ("Jordan Bravo Jr", "Bravo, Jordan Jr."),     # the same suffix on both sides
            ("Dr. Jordan Bravo", "Jordan Bravo"),         # a title is not part of the name
        ):
            with self.subTest(first=first, second=second):
                self.assertEqual(same_name_reason(name_words(first.lower()), name_words(second.lower())), SAME_FULL_NAME)

    def test_middle_name_on_one_side_or_agreeing_on_both_is_compatible(self):
        for first, second in (
            ("Jordan Bravo", "Jordan Alex Bravo"),
            ("Jordan A. Bravo", "Jordan Bravo"),
            ("Jordan A Bravo", "Jordan Alex Bravo"),
            ("Bravo, Jordan Alex", "Jordan Bravo"),
        ):
            with self.subTest(first=first, second=second):
                self.assertEqual(
                    same_name_reason(name_words(first.lower()), name_words(second.lower())), SAME_FIRST_AND_LAST_NAME,
                )

    def test_names_that_do_not_settle_it_go_to_the_judge(self):
        for first, second in (
            ("Jordan Alex Bravo", "Jordan Blake Bravo"),   # two different middle names
            ("Jordan Ann Bravo", "Jordan Anna Bravo"),     # one middle name only begins the other
            ("Jordan Bravo", "Jordan Bravo Jr"),           # a suffix on one side is another person
            ("Jordan Bravo", "Jordan Bravo, Sr."),
            ("Bravo, Jordan", "Bravo, Jordan Jr."),
            ("Jordan Bravo Jr", "Jordan Bravo Sr"),
            ("Jordan Bravo", "Jordan Bravo III"),
            ("Bravo, Dr Jordan", "Dr Bravo"),              # a title is not a first name
            ("O'Bravo, Jordan", "Jordan Bravo"),           # O'Bravo is one word, not a middle initial
            ("jordan.bravo@example.com", "jordan.bravo@example.com"),  # an address is not a name
            ("Jordan", "Jordan"),                          # one-word names
            ("Jordan B", "Jordan B"),                      # an initial is not a last name
            ("J Bravo", "J Bravo"),
            ("Jordan Bravo", "Casey Bravo"),
            ("Jordan Bravo", "Jordan Bravoski"),
        ):
            with self.subTest(first=first, second=second):
                self.assertIsNone(slam_dunk_verdict(person(first), person(second)))


class TestSharedIdentifierNote(unittest.TestCase):
    def test_shared_phone_is_rendered_normalized_with_provenance(self):
        a = person("Jordan Bravo", extra_phones=["9145550466"])
        b = person("Casey Bravo", phones=["9145550466"])
        note = shared_identifier_note(a, b)
        self.assertIn("SHARED IDENTIFIERS", note)
        self.assertIn("+1 (914) 555-0466", note)
        self.assertIn("A: attributed by message extraction", note)
        self.assertIn("B: contact record", note)

    def test_no_overlap_renders_nothing(self):
        a = person("Jordan Bravo", phones=["9145550466"])
        b = person("Casey Delta", phones=["3105550100"])
        self.assertEqual(shared_identifier_note(a, b), "")

    def test_shared_email_handle_across_domains_is_not_an_identifier(self):
        a = person("Kai Bravo", emails=["kbravo@example.com"])
        b = person("K Bravo", extra_emails=["kbravo@example.org"])
        self.assertEqual(shared_identifier_note(a, b), "")

    def test_shared_full_email_is_not_repeated_as_a_handle(self):
        a = person("Kai Bravo", emails=["kbravo@example.com"])
        b = person("K Bravo", emails=["kbravo@example.com"])
        note = shared_identifier_note(a, b)
        self.assertIn("- email kbravo@example.com is in BOTH records", note)
        self.assertNotIn("email handle", note)

    def test_judge_prompt_carries_the_section_only_on_overlap(self):
        a = person("Jordan Bravo", extra_phones=["9145550466"])
        b = person("Casey Bravo", phones=["9145550466"])
        self.assertIn("SHARED IDENTIFIERS", judge_prompt(a, b))
        c = person("Casey Delta", phones=["3105550100"])
        self.assertNotIn("SHARED IDENTIFIERS", judge_prompt(a, c))


class TestPairGeneration(unittest.TestCase):
    def test_jaro_winkler_matches_published_reference_values(self):
        # Standard examples reproduced in Winkler/Jaro implementation references.
        self.assertAlmostEqual(jaro_winkler("MARTHA", "MARHTA"), 0.9611111111)
        self.assertAlmostEqual(jaro_winkler("DIXON", "DICKSONX"), 0.8133333333)
        self.assertAlmostEqual(jaro_winkler("JELLYFISH", "SMELLYFISH"), 0.8962962963)

    def test_extracted_phone_does_not_create_a_pair(self):
        people = [person("Jordan Bravo", extra_phones=["9145550466"]),
                  person("JB", phones=["9145550466"])]
        self.assertEqual(generate_pairs(people), [])

    def test_same_name_pairs_with_nothing_else_in_common(self):
        people = [
            person("Bravo, Jordan", emails=["jb@example.com"]),
            person("Jordan Bravo", phones=["4155550100"]),
            person("Jordan Alex Bravo", emails=["alex@example.org"]),
            person("Casey Delta", emails=["casey@example.com"]),
        ]
        self.assertEqual(
            {(pair.first.name, pair.second.name) for pair in generate_pairs(people)},
            {("Bravo, Jordan", "Jordan Bravo"), ("Bravo, Jordan", "Jordan Alex Bravo"),
             ("Jordan Bravo", "Jordan Alex Bravo")},
        )

    def test_names_that_can_be_one_name(self):
        for first, second in (
            ("Jordan Bravo", "Jordan"),             # a one-word name that is the other's first name
            ("Jordan Bravo", "Bravo"),
            ("Jordan", "Jordan"),
            ("Jordan Bravo", "Jordan B"),           # a last initial
            ("Jordan Bravo", "J Bravo"),            # a first initial
            ("Benjamin Bravo", "Ben Bravo"),        # a short form that begins the name
            ("Jon Bravo", "John Bravo"),            # a spelling variant
            ("Jordan Bravo", "Jordan Bravoski"),
        ):
            with self.subTest(first=first, second=second):
                self.assertTrue(names_can_match(name_words(first.lower()), name_words(second.lower())))

    def test_names_that_cannot_be_one_name(self):
        for first, second in (
            ("Jordan Bravo", "Jordan Delta"),       # a shared first name alone
            ("Jordan Bravo", "Casey Bravo"),        # a shared last name alone
            ("Jordan Bravo", "Casey"),
            ("Jordan Bravo", "Riley Delta"),
            ("Jordan Bravo", ""),
            ("jordan@example.com", "casey@example.com"),
        ):
            with self.subTest(first=first, second=second):
                self.assertFalse(names_can_match(name_words(first.lower()), name_words(second.lower())))

    def test_one_word_name_meets_a_full_name_only_through_a_shared_handle_or_identifier(self):
        # "Jordan" alone could be any Jordan: nothing proposes the pair, so nothing is asked.
        self.assertEqual(generate_pairs([person("Jordan"), person("Jordan Bravo")]), [])

    def test_shared_email_handle_under_two_different_names_is_not_a_pair(self):
        people = [
            person("Jordan Bravo", emails=["jordan@example.com"]),
            person("Jordan Delta", emails=["jordan@example.org"]),
        ]
        self.assertEqual(generate_pairs(people), [])

    def test_shared_email_handle_under_a_short_form_of_the_name_is_a_pair(self):
        people = [
            person("Jordan Bravo", emails=["jordan@example.com"]),
            person("jordan", emails=["jordan@example.org"]),
        ]
        self.assertEqual(len(generate_pairs(people)), 1)

    def test_shared_first_name_and_a_different_last_name_is_not_a_pair(self):
        # Whole-name similarity once paired these: the long shared first name carried the score.
        people = [person("Christopher Bravo"), person("Christopher Brown")]
        self.assertGreaterEqual(jaro_winkler("christopher bravo", "christopher brown"), 0.85)
        self.assertEqual(generate_pairs(people), [])

    def test_oversized_blocking_bucket_is_reported_and_skipped(self):
        people = [
            person(f"Jordan Bravo {number}")
            for number in range(201)
        ]
        with mock.patch("sys.stderr") as stderr:
            self.assertEqual(generate_pairs(people), [])
        self.assertTrue(any("201 members (cap 200)" in str(call) for call in stderr.write.call_args_list))



class TestOwnedIdentifierLoading(unittest.TestCase):
    """Extracted identifiers remain evidence, not source identity keys."""

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
            return merge_people(db)

    def test_third_party_phone_in_untyped_identifiers_does_not_pair(self):
        people = self._load({
            "identifiers": ["Contact: Casey +1 415 555 0100"],
            "owned_identifiers": {"emails": [], "phones": [], "urls": []},
        })
        self.assertEqual(people[0].extra_phones, ())
        self.assertEqual(generate_pairs(people), [])

    def test_extracted_phone_does_not_pair_with_contact_record(self):
        people = self._load({
            "owned_identifiers": {"emails": [], "phones": ["+1 415 555 0100"], "urls": []},
        })
        self.assertEqual(people[0].extra_phones, ("4155550100",))
        self.assertEqual(generate_pairs(people), [])

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
            db.replace_imported_people((PeopleRow(id="child-a", full_name="Jordan A"),
                                        PeopleRow(id="child-b", full_name="Jordan B")))

            people = merge_people(db)

            self.assertEqual(len(people), 1)
            self.assertEqual(people[0].parent_id, parent_id)
            self.assertEqual(people[0].person_id, "child-a")
            self.assertEqual(people[0].member_person_ids, ("child-a", "child-b"))
            self.assertEqual(people[0].emails, ("a@example.com",))
            self.assertEqual(people[0].phone_digits, ("4155550100",))


class TestJudgeSystemRule(unittest.TestCase):
    def test_prompt_requires_owned_identifiers_instead_of_assuming_ownership(self):
        self.assertIn("overlap is neutral", JUDGE_SYSTEM)
        self.assertIn("ownership is unclear", JUDGE_SYSTEM)
        self.assertNotIn("confidence ~0.99", JUDGE_SYSTEM)

    def test_pair_signature_bytes_stay_pinned(self):
        first = person(
            "Jordan Bravo", emails=["jordan@example.com"], phones=["9145550466"],
        )
        second = person(
            "Jordan Bravo", extra_emails=["jordan@example.com"],
            extra_phones=["9145550466"],
        )
        self.assertEqual(len(pair_sig(first, second)), 64)


class TestCacheAndArtifacts(unittest.TestCase):
    def test_old_default_acceptance_cache_is_not_reused(self):
        first = person("Jordan Bravo", emails=["jordan@example.com"], phones=["9145550466"])
        second = replace(person("Jordan Bravo", extra_emails=["jordan@example.com"],
                                extra_phones=["9145550466"]), person_id="second-source")
        # The Oct 1 default-acceptance contract, with the same rendered evidence.
        old_signature = "00539eea0f94fbd7"
        cache = {frozenset((first.person_id, second.person_id)): CachedMergeVerdict(
            old_signature, MergeDecision(True, .95, True, "same full name", "slam_dunk"),
        )}
        reused, to_judge = receipts.split_cached_pairs([MergePair(first, second)], cache)
        self.assertEqual(reused, [])
        self.assertEqual(len(to_judge), 1)

    def test_no_legacy_cache_loader_remains(self):
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

    def test_shared_source_identifier_does_not_override_unrelated_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            for person_id, name in (("a", "Jordan Alpha"), ("b", "Casey Bravo")):
                seed_person(db, person_id=person_id, slug=person_id, name=name,
                            facts_path=root / f"{person_id}.jsonl", facts={}, phone="4155550100")
            survey = survey_pairs(db)
            self.assertEqual(survey.pairs, [])
            self.assertEqual(survey.to_judge, [])

    def test_near_identical_name_sharing_a_phone_is_judged_not_dropped(self):
        """The live case: one character apart, so slam-dunk equality misses it."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            seed_person(
                db, person_id="a", slug="jordan-bravo-a", name="Jordan Bravo",
                facts_path=root / "a.jsonl", facts={}, phone="4155550100",
            )
            seed_person(
                db, person_id="b", slug="jordanu-bravo-b", name="Jordanu Bravo",
                facts_path=root / "b.jsonl", facts={}, phone="4155550100",
            )

            survey = survey_pairs(db)

            self.assertEqual(survey.slam, [])
            self.assertEqual(len(survey.to_judge), 1)

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

    def test_slam_dunk_node_keeps_csv_contract(self):
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
                b"slam dunk: identical name + shared +1 (914) 555-0466\r\n"
            ))
            self.assertFalse(output.with_name("merge-verdicts.csv").exists())
            cached = canonical_snapshot(db).merge_verdicts
            self.assertEqual(len(cached), 1)
            self.assertEqual(len(cached[0].signature), 64)
            self.assertEqual(cached[0].accepted, 1)
            self.assertEqual(payload.pairs_slam_dunk, 1)


def scripted_caller(*, decision="same", fail_on=None, invalid=False, observed=None):
    """A synthetic Responses boundary; no SDK client or network is constructed."""
    class Caller:
        calls = []
        def __init__(self, config):
            self.config = config
            self.usage = OpenAIUsage()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def call(self, **request):
            self.calls.append(request)
            if fail_on and fail_on in request["user_prompt"]:
                raise RuntimeError("synthetic HTTP 503")
            if observed:
                observed(request)
            usage = OpenAIUsage(1000, 100, 70)
            self.usage = self.usage + usage
            payload = {"decision": decision, "confidence": .95, "tone_consistent": True,
                       "reason": "Synthetic decision", "identity_evidence": {"same": "explicit account linkage",
                           "different": "A identifies as Jordan Alpha; B explicitly identifies as a different Jordan Alpha in the same thread",
                           "uncertain": ""}[decision]}
            if invalid:
                del payload["decision"]
            return OpenAIResponse(payload, usage)
    return Caller


class TestSolJudge(unittest.TestCase):
    def _node(self, root: Path, *, limit=None) -> ClusterMergeCandidates:
        db = Db(root / "deep-context.sqlite")
        for person_id, slug, name, phone in (
            ("a", "jordan-alpha", "Jordan Alpha", "15550100100"),
            ("b", "j-alpha", "J Alpha", "15550100100"),
            ("c", "riley-charlie", "Riley Charlie", "15550100111"),
            ("d", "r-charlie", "R Charlie", "15550100111"),
        ):
            seed_person(db, person_id=person_id, slug=slug, name=name,
                        facts_path=root / f"{person_id}.jsonl", facts={}, phone=phone)
        return ClusterMergeCandidates(db=db, dossier_dir=root, limit=limit,
            out_csv=root / "merge-candidates.csv", out_md=root / "merge-candidates.md")

    def test_one_call_per_pair_preserves_reason_and_all_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self._node(Path(directory))
            fake = scripted_caller()
            with mock.patch.object(judge, "OpenAIResponsesCaller", fake):
                result = node.execute()
            self.assertEqual(len(fake.calls), 2)
            self.assertEqual(result.candidate_pairs, 2)
            self.assertEqual(result.model, "gpt-6.1-sol")
            self.assertEqual(result.reasoning_effort, "high")
            self.assertEqual(result.tokens, {"input_tokens": 2000, "output_tokens": 200, "reasoning_tokens": 140})
            self.assertAlmostEqual(result.estimated_cost_usd, estimate_cost_usd(2000, 200, MODEL_ID))
            for row in canonical_snapshot(node.db).merge_verdicts:
                self.assertEqual(row.judge, 'sol')
                self.assertIn('explicit account linkage', row.reason)
                self.assertTrue(row.accepted)
            self.assertIn('CONTACT A', fake.calls[0]['user_prompt'])
            self.assertEqual(fake.calls[0]['schema'], judge.SCHEMA)

    def test_uncertain_is_saved_reused_and_never_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self._node(Path(directory))
            fake = scripted_caller(decision='uncertain')
            with mock.patch.object(judge, 'OpenAIResponsesCaller', fake):
                first = node.execute()
                second = node.execute()
            self.assertEqual(len(fake.calls), 2)
            self.assertEqual((first.candidate_pairs, second.pairs_reused, second.pairs_judged), (0, 2, 0))
            self.assertEqual({(r.same_person, r.accepted) for r in canonical_snapshot(node.db).merge_verdicts}, {(None, False)})
            self.assertEqual(node.estimate()['estimated_cost_usd'], 0)

    def test_affirmative_different_remains_a_constraint(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self._node(Path(directory))
            with mock.patch.object(judge, 'OpenAIResponsesCaller', scripted_caller(decision='different')):
                node.execute()
            self.assertEqual({(r.same_person, r.accepted) for r in canonical_snapshot(node.db).merge_verdicts}, {(False, False)})
            self.assertEqual(node.survey().pairs, [])

    def test_failed_pair_is_retried_without_rebilling_success(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self._node(Path(directory))
            fake = scripted_caller(fail_on='J Alpha')
            with mock.patch.object(judge, 'OpenAIResponsesCaller', fake):
                first = node.execute()
            self.assertEqual((first.errors, first.status), (1, 'failed'))
            self.assertEqual([(r.person_a,r.person_b) for r in canonical_snapshot(node.db).merge_verdicts], [('c','d')])
            retry = scripted_caller()
            with mock.patch.object(judge, 'OpenAIResponsesCaller', retry):
                second = node.execute()
            self.assertEqual((len(retry.calls), second.pairs_reused, second.errors), (1, 1, 0))

    def test_invalid_schema_is_not_cached_or_accepted_but_usage_is_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self._node(Path(directory))
            with mock.patch.object(judge, 'OpenAIResponsesCaller', scripted_caller(invalid=True)):
                result = node.execute()
            self.assertEqual((result.errors, result.candidate_pairs), (2, 0))
            self.assertEqual(result.tokens['input_tokens'], 2000)
            self.assertEqual(canonical_snapshot(node.db).merge_verdicts, ())

    def test_same_requires_evidence(self):
        with self.assertRaises(ValueError):
            decision_from_answers({'decision': 'same', 'confidence': .99, 'reason': 'name alone',
                                   'identity_evidence': '', 'tone_consistent': True})

    def test_different_requires_affirmative_evidence(self):
        with self.assertRaises(ValueError):
            decision_from_answers({'decision': 'different', 'confidence': .99,
                                   'reason': 'There is not enough evidence linking these accounts.',
                                   'identity_evidence': '', 'tone_consistent': True})

    def test_changed_evidence_is_rejudged_and_old_acceptance_cannot_survive_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self._node(Path(directory))
            with mock.patch.object(judge, 'OpenAIResponsesCaller', scripted_caller()):
                node.execute()
            with mock.patch.object(judge, 'JUDGE_SYSTEM', judge.JUDGE_SYSTEM + '\nRevised contract'), mock.patch.object(
                judge, 'OpenAIResponsesCaller', scripted_caller(fail_on='CONTACT A')):
                result = node.execute()
            self.assertEqual(result.errors, 2)
            self.assertEqual({r.accepted for r in canonical_snapshot(node.db).merge_verdicts}, {False})

    def test_checkpoint_precedes_other_calls_and_never_accepts_early(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self._node(Path(directory))
            def observe(request):
                if 'R Charlie' in request['user_prompt']:
                    saved = canonical_snapshot(node.db).merge_verdicts
                    self.assertEqual([(r.person_a, r.person_b, r.accepted) for r in saved], [('a', 'b', False)])
            with mock.patch.object(judge, 'OpenAIResponsesCaller', scripted_caller(observed=observe)):
                node.execute()

    def test_limit_prices_and_executes_only_one_uncached_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self._node(Path(directory), limit=1)
            estimate = node.estimate()
            self.assertEqual((estimate['candidate_pairs_to_judge'], estimate['remaining']), (1, 1))
            fake = scripted_caller()
            with mock.patch.object(judge, 'OpenAIResponsesCaller', fake):
                result = node.execute()
            self.assertEqual((len(fake.calls), result.remaining, result.status), (1, 1, 'incomplete'))

    def test_dry_run_includes_output_estimate_without_spend(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self._node(Path(directory))
            with mock.patch.object(judge, 'OpenAIResponsesCaller', side_effect=AssertionError('network')):
                estimate = node.estimate()
            self.assertEqual(estimate['estimated_output_tokens'], 3000)
            self.assertEqual(estimate['model'], MODEL_ID)
            self.assertEqual(estimate['estimated_cost_usd'], estimate_cost_usd(
                estimate['estimated_input_tokens'], estimate['estimated_output_tokens'], MODEL_ID))

    def test_signature_binds_actual_request_contract(self):
        first, second = person('Jordan Alpha'), person('J Alpha')
        signature = pair_sig(first, second)
        for field, value in [('JUDGE_SYSTEM', 'changed'), ('MODEL_ID', 'other-model'),
                             ('REASONING_EFFORT', 'low'), ('SCHEMA', {**judge.SCHEMA, 'description': 'new'})]:
            with self.subTest(field=field), mock.patch.object(judge, field, value):
                self.assertNotEqual(signature, pair_sig(first, second))
        self.assertNotEqual(signature, pair_sig(first, second, owner_name='Casey Delta'))
        with_phone = replace(first, phone_digits=('15550100999',))
        self.assertNotEqual(signature, pair_sig(with_phone, second))
        self.assertIn('15550100999', judge.judge_prompt(with_phone, second))
        self.assertNotEqual(signature, pair_sig(replace(first, source_names=('Jordan Other',)), second))
        with mock.patch.dict('os.environ', {'POWERPACKS_DEEP_CONTEXT_MAX_OUTPUT_TOKENS': '4000'}):
            self.assertNotEqual(signature, pair_sig(first, second))

    def test_shared_mailbox_already_in_the_store_is_left_out_of_the_survey(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            for person_id, email in (("a", "ir@example.com"), ("b", "ir@example.org"), ("c", "casey@example.com")):
                parent_id = f"parent-{person_id}"
                db.project_rows((
                    ParentRow(parent_id, f"parent-worth:{parent_id}", "Investor Relations", person_id),
                    PersonRow(person_id, parent_id, person_id, person_id, "Investor Relations"),
                    PersonIdentifiersProjection(person_id, (PersonIdentifierRow(person_id, "email", email, email),)),
                    ArtifactRow(f"facts:{parent_id}", "facts", parent_id, str(root / f"{person_id}.jsonl"),
                                "0" * 64, "projected"),
                    FactRow(parent_id, parent_id, f"facts:{parent_id}", facts_json="{}"),
                ))
            db.replace_imported_people(tuple(PeopleRow(id=person_id, full_name="Investor Relations")
                                             for person_id in ("a", "b", "c")))

            survey = survey_pairs(db)

            self.assertEqual([person.person_id for person in survey.people], ["c"])
            self.assertEqual(survey.pairs, [])

    def test_cluster_has_no_confidence_override(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--confidence", "0.4"])



if __name__ == "__main__":
    unittest.main()
