from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.common.gates import EXIT_NEEDS_APPROVAL
from packs.ingestion.primitives.share import share as share_cli
from packs.ingestion.primitives.share.evidence import ShareEvidence
from packs.ingestion.primitives.share.labels import (
    ACTIVE_P,
    PRIVATE_P,
    deterministic_labels,
    labels_from_answers,
    private_reason,
)
from packs.ingestion.primitives.share.models import JevLabels, MessageStats, PersonEvidence
from packs.ingestion.primitives.share.questions import NOUL_LABELS, build_questions, build_request
from packs.shared.csv_io import CsvIO

REFERENCE_DATE = "2026-09-24"

PEOPLE_HEADER = [
    "id",
    "public_identifier",
    "full_name",
    "headline",
    "current_title",
    "current_company",
    "city",
    "state",
    "country",
    "source_channels",
    "interaction_counts",
    "last_interaction",
    "superseded_person_ids",
]


class _Response:
    def __init__(self, status_code: int, payload: object = None, *, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = {"Retry-After": "0"}

    def json(self) -> object:
        return self._payload


class _Client:
    """The fake Jev transport from tests/test_jev_client.py, answering every question."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def post(self, endpoint: str, **kwargs) -> _Response:
        self.calls.append(kwargs["json"])
        return _Response(200, _payload(kwargs["json"]))

    async def aclose(self) -> None:
        return None


def _answer(question: dict) -> dict:
    if question["type"] == "noul":
        return {"type": "noul", "noul": 0.9}
    options = list(question["criteria"] if question["type"] == "choice" else map(str, range(len(question["criteria"]))))
    remainder = 0.3 / (len(options) - 1)
    return {
        "type": question["type"],
        "probabilities": {option: 0.7 if index == 0 else remainder for index, option in enumerate(options)},
    }


def _payload(request: dict) -> dict:
    return {
        "model": request["model"],
        "answers": {name: _answer(question) for name, question in request["questions"].items()},
        "usage": {"input_tokens": 2000, "output_tokens": 120},
    }


def _person(**overrides) -> PersonEvidence:
    fields = {
        "person_id": "person-a",
        "public_identifier": "jordan-bravo",
        "full_name": "Jordan Bravo",
        "headline": "Reliability engineer",
        "current_title": "Engineer",
        "current_company": "Example Systems",
        "city": "Springfield",
        "state": "CA",
        "country": "US",
        "source_channels": ("gmail_msgvault",),
        "interaction_counts": {"gmail": 4},
        "last_interaction": "2026-09-01T00:00:00+00:00",
        "superseded_person_ids": (),
        "network_worth": "yes",
        "dossier": "---\ngenerated_at: 2026-09-10T08:00:00+00:00\n---\n# Jordan Bravo\nWorked together on storage.",
        "evidence_date": "2026-09-10",
        "facts": {"canonical_name": "Jordan Bravo", "shared_context": [{"overlap": "employer"}]},
        "shared_overlaps": frozenset({"employer"}),
        "messages": MessageStats(
            first_at="2026-01-01T00:00:00+00:00",
            last_at="2026-09-01T00:00:00+00:00",
            from_me=1,
            from_them=9,
            group_count=0,
            channels=("gmail",),
        ),
    }
    fields.update(overrides)
    return PersonEvidence(**fields)


def _jev(*, kind: str = "acquaintance", **probabilities) -> JevLabels:
    questions = build_questions()
    answers = {name: {"type": "noul", "noul": probabilities.get(name, 0.0)} for name in NOUL_LABELS}
    for name, question in questions.items():
        if question["type"] != "noul":
            answers[name] = _answer(question)
    options = list(questions["relationship_kind"]["criteria"])
    remainder = 0.3 / (len(options) - 1)
    answers["relationship_kind"] = {
        "type": "choice",
        "probabilities": {option: 0.7 if option == kind else remainder for option in options},
    }
    return labels_from_answers(answers)


def _write_install(root: Path) -> ShareEvidence:
    """A synthetic install: one message-backed person and one LinkedIn-only person."""
    people_csv = root / "people.csv"
    CsvIO.write_dict_rows(
        people_csv,
        PEOPLE_HEADER,
        [
            {
                "id": "person-a",
                "public_identifier": "jordan-bravo",
                "full_name": "Jordan Bravo",
                "headline": "Reliability engineer",
                "current_title": "Engineer",
                "current_company": "Example Systems",
                "city": "Springfield",
                "country": "US",
                "source_channels": "gmail_msgvault",
                "interaction_counts": '{"gmail": 4}',
                "last_interaction": "2026-09-01T00:00:00+00:00",
                "superseded_person_ids": "",
            },
            {
                "id": "person-b",
                "public_identifier": "casey-delta",
                "full_name": "Casey Delta",
                "source_channels": "linkedin_csv",
                "interaction_counts": "{}",
                "superseded_person_ids": "",
            },
        ],
    )
    (root / "index.json").write_text(
        json.dumps(
            {
                "slugs": {"jordan-bravo-aaaa": {"person_id": "person-a", "name": "Jordan Bravo"}},
                "parents": {
                    "jordan-bravo-aaaa": {
                        "parent_id": "parent-aaaa",
                        "children": ["jordan-bravo-aaaa"],
                        "name": "Jordan Bravo",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "facts").mkdir()
    (root / "facts" / "parent-aaaa.jsonl").write_text(
        json.dumps(
            {
                "facts": {
                    "canonical_name": "Jordan Bravo",
                    "owned_identifiers": ["owner@example.com"],
                    "shared_context": [{"overlap": "school"}],
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "parents").mkdir()
    (root / "parents" / "jordan-bravo-aaaa.md").write_text(
        "---\ngenerated_at: 2026-09-10T08:00:00+00:00\n---\n# Jordan Bravo\nStorage work.", encoding="utf-8"
    )
    (root / "dossiers").mkdir()
    (root / "raw").mkdir()
    (root / "raw" / "parent-aaaa.json").write_text(
        json.dumps(
            {
                "person_id": "parent-aaaa",
                "groups": ["weekend-crew"],
                "messages": [
                    {
                        "at": "2026-01-01T00:00:00+00:00",
                        "channel": "gmail",
                        "direction": "from_them",
                        "subject": "SECRET-SUBJECT",
                        "text": "SECRET-BODY",
                    },
                    {
                        "at": "2026-09-01T00:00:00+00:00",
                        "channel": "gmail",
                        "direction": "from_me",
                        "subject": "SECRET-SUBJECT",
                        "text": "SECRET-BODY",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "review.csv").write_text("public_identifier,network_worth,llm_worth\n", encoding="utf-8")
    (root / "owner.json").write_text(
        json.dumps({"name": "Owner Person", "work": [{"company": "Powerset", "title": "Engineer"}]}),
        encoding="utf-8",
    )
    return ShareEvidence(
        people_csv=people_csv,
        index_json=root / "index.json",
        facts_dir=root / "facts",
        raw_dir=root / "raw",
        dossier_dir=root / "dossiers",
        parents_dir=root / "parents",
        overrides_csv=root / "review.csv",
        owner_json=root / "owner.json",
    )


class DeterministicLabelTests(unittest.TestCase):
    def test_shared_context_overlaps_become_employer_and_school_flags(self) -> None:
        labels = deterministic_labels(_person(), reference_date=REFERENCE_DATE)
        self.assertTrue(labels.shared_employer)
        self.assertFalse(labels.shared_school)

    def test_cadence_bands_run_stale_before_volume(self) -> None:
        bands = {
            ("2023-01-01T00:00:00+00:00", 500): "dormant",
            ("2025-01-01T00:00:00+00:00", 500): "stale",
            ("2026-09-01T00:00:00+00:00", 500): "frequent",
            ("2026-09-01T00:00:00+00:00", 40): "regular",
            ("2026-09-01T00:00:00+00:00", 4): "occasional",
        }
        for (last_interaction, count), expected in bands.items():
            person = _person(last_interaction=last_interaction, interaction_counts={"gmail": count})
            self.assertEqual(deterministic_labels(person, reference_date=REFERENCE_DATE).cadence, expected)

    def test_cadence_and_recency_are_absent_without_an_interaction(self) -> None:
        labels = deterministic_labels(_person(last_interaction=None), reference_date=REFERENCE_DATE)
        self.assertIsNone(labels.cadence)
        self.assertIsNone(labels.recency_days)

    def test_direction_follows_the_owner_share_of_messages(self) -> None:
        cases = {(1, 9): "they_initiate", (9, 1): "i_initiate", (5, 5): "mutual"}
        for (from_me, from_them), expected in cases.items():
            person = _person(
                messages=MessageStats(
                    first_at=None, last_at=None, from_me=from_me, from_them=from_them, group_count=0, channels=()
                )
            )
            self.assertEqual(deterministic_labels(person, reference_date=REFERENCE_DATE).direction, expected)

    def test_group_only_traffic_sets_group_chat_only(self) -> None:
        person = _person(
            messages=MessageStats(
                first_at=None, last_at=None, from_me=1, from_them=1, group_count=1, channels=("imessage_group",)
            )
        )
        self.assertTrue(deterministic_labels(person, reference_date=REFERENCE_DATE).group_chat_only)

    def test_a_person_without_facts_or_dossier_is_linkedin_only(self) -> None:
        person = _person(facts=None, dossier=None)
        self.assertTrue(deterministic_labels(person, reference_date=REFERENCE_DATE).linkedin_only)


class PrivateSuggestionTests(unittest.TestCase):
    def test_first_matching_rule_names_the_reason(self) -> None:
        deterministic = deterministic_labels(_person(), reference_date=REFERENCE_DATE)
        jev = _jev(is_family=PRIVATE_P, is_minor=PRIVATE_P, sensitive_context=PRIVATE_P)
        self.assertEqual(private_reason(deterministic, jev), "family")

    def test_family_fires_from_the_relationship_kind_alone(self) -> None:
        deterministic = deterministic_labels(_person(), reference_date=REFERENCE_DATE)
        self.assertEqual(private_reason(deterministic, _jev(kind="family")), "family")
        self.assertEqual(private_reason(deterministic, _jev(kind="romantic_partner")), "romantic_partner")

    def test_each_later_rule_fires_on_its_own(self) -> None:
        deterministic = deterministic_labels(_person(), reference_date=REFERENCE_DATE)
        for name, probability_key in (
            ("minor", "is_minor"),
            ("sensitive_context", "sensitive_context"),
            ("sensitive_provider", "is_healthcare_legal_or_financial_provider"),
        ):
            self.assertEqual(private_reason(deterministic, _jev(**{probability_key: PRIVATE_P})), name)

    def test_the_owner_rule_needs_no_jev_answers(self) -> None:
        person = _person(facts={"is_owner": True})
        deterministic = deterministic_labels(person, reference_date=REFERENCE_DATE)
        self.assertEqual(private_reason(deterministic, None), "owner")

    def test_nothing_is_suggested_for_an_ordinary_contact(self) -> None:
        deterministic = deterministic_labels(_person(), reference_date=REFERENCE_DATE)
        self.assertIsNone(private_reason(deterministic, _jev(is_professional=ACTIVE_P)))

    def test_confidential_dealings_is_a_label_not_a_private_rule(self) -> None:
        deterministic = deterministic_labels(_person(), reference_date=REFERENCE_DATE)
        self.assertIsNone(private_reason(deterministic, _jev(confidential_dealings=0.95)))


class QuestionContractTests(unittest.TestCase):
    def test_the_question_set_is_frozen(self) -> None:
        questions = build_questions()
        self.assertEqual(len(questions), 34)
        digest = hashlib.sha256(
            json.dumps(questions, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertEqual(digest, "7ee420f8599c34e543bab2c63a0c79f0c709b47b879d954cae4812a22baa3fe2")

    def test_answers_reduce_to_argmax_choice_score_and_probability(self) -> None:
        questions = build_questions()
        labels = labels_from_answers({name: _answer(question) for name, question in questions.items()})
        self.assertEqual(labels.choices["relationship_kind"], "family")
        self.assertAlmostEqual(labels.choice_p["relationship_kind"], 0.7)
        self.assertEqual(labels.scores["warmth"], 0)
        self.assertAlmostEqual(labels.probabilities["is_family"], 0.9)

    def test_the_request_carries_no_message_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = _write_install(Path(directory))
            person = next(p for p in evidence.load() if p.person_id == "person-a")
            request = build_request(
                dossier=person.dossier,
                facts=person.facts_state(),
                profile=person.profile_state(),
                channels=person.channel_state(),
                owner=evidence.owner_state(),
                reference_date=REFERENCE_DATE,
            )
        serialized = json.dumps(request)
        self.assertNotIn("SECRET-BODY", serialized)
        self.assertNotIn("SECRET-SUBJECT", serialized)
        self.assertFalse(_keys(request) & {"body", "text", "subject", "snippet"})

    def test_the_request_is_dated_by_its_evidence_not_by_today(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = _write_install(Path(directory))
            person = next(p for p in evidence.load() if p.person_id == "person-a")
        self.assertEqual(person.evidence_date, "2026-09-10")

    def test_owned_identifiers_never_reach_the_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = _write_install(Path(directory))
            person = next(p for p in evidence.load() if p.person_id == "person-a")
        self.assertNotIn("owned_identifiers", person.facts_state())
        self.assertIn("owned_identifiers", person.facts)


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


class LabelRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.evidence = _write_install(self.root)
        self.out = self.root / "share"

    def _run(self, **overrides):
        args = {
            "out_dir": self.out,
            "evidence": self.evidence,
            "api_key": "synthetic-key",
            "approve_spend": True,
        }
        args.update(overrides)
        with mock.patch.dict("os.environ", {"POWERPACKS_USAGE_LOG": str(self.out / "usage.jsonl")}):
            return share_cli.ShareLabels(**args).run()

    def test_estimate_prices_the_uncached_calls_and_writes_nothing(self) -> None:
        payload = self._run(estimate_only=True, approve_spend=False, client=_Client())
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["estimate"]["uncached_calls"], 1)
        self.assertGreater(payload["estimate"]["cost_usd"], 0)
        self.assertFalse((self.out / "labels.csv").exists())

    def test_spend_is_gated_until_approved(self) -> None:
        payload = self._run(approve_spend=False, client=_Client())
        self.assertEqual(payload["status"], "needs_approval")
        self.assertEqual(payload["needs_approval"]["estimated_calls"], 1)
        self.assertEqual(share_cli.exit_code_for_status(payload["status"]), EXIT_NEEDS_APPROVAL)
        self.assertFalse((self.out / "labels.csv").exists())

    def test_approved_run_labels_everyone_and_calls_jev_once(self) -> None:
        client = _Client()
        payload = self._run(client=client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(payload["counts"], {
            "people": 2,
            "jev_called": 1,
            "cached": 0,
            "deterministic_only": 1,
            "private_suggested": 1,
        })
        rows = {row["person_id"]: row for row in CsvIO.read_dict_rows_normalized(self.out / "labels.csv")}
        self.assertEqual(rows["person-a"]["relationship_kind"], "family")
        self.assertEqual(rows["person-a"]["private_reason"], "family")
        self.assertEqual(rows["person-b"]["linkedin_only"], "yes")
        self.assertEqual(rows["person-b"]["relationship_kind"], "")

    def test_a_cached_answer_costs_no_network_call(self) -> None:
        self._run(client=_Client())
        second = self._run(client=None, api_key=None)
        self.assertEqual(second["counts"]["cached"], 1)
        self.assertEqual(second["counts"]["jev_called"], 0)
        self.assertEqual(second["paid_usage"]["pairs"], 0)


class EvidenceJoinTests(unittest.TestCase):
    def test_facts_dossier_and_messages_join_through_the_parent_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            people = _write_install(Path(directory)).load()
        by_id = {person.person_id: person for person in people}
        self.assertEqual(by_id["person-a"].facts["canonical_name"], "Jordan Bravo")
        self.assertIn("Storage work", by_id["person-a"].dossier)
        self.assertEqual(by_id["person-a"].messages.from_me, 1)
        self.assertEqual(by_id["person-a"].messages.from_them, 1)
        self.assertEqual(by_id["person-a"].messages.group_count, 1)
        self.assertTrue(by_id["person-b"].linkedin_only)

    def test_absent_cells_stay_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            people = _write_install(Path(directory)).load()
        casey = next(person for person in people if person.person_id == "person-b")
        self.assertIsNone(casey.headline)
        self.assertIsNone(casey.last_interaction)
        self.assertEqual(casey.interaction_counts, {})


if __name__ == "__main__":
    unittest.main()
