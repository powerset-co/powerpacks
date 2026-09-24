from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
import tempfile
import unittest
from datetime import date
from pathlib import Path

from packs.ingestion.primitives.share.share_list import ShareList
from packs.ingestion.primitives.share.evidence import ShareEvidence
from packs.ingestion.primitives.share.labels import (
    share_decision,
    ACTIVE_P,
    CONFIRM_P,
    confirm_flag,
    deterministic_labels,
    labels_from_answers,
)
from packs.ingestion.primitives.share.models import JevLabels, LabelRow, MessageStats, PersonEvidence
from packs.ingestion.primitives.share.questions import NOUL_LABELS, build_questions
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
                    "network_worth": {"decision": "yes"},
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
    return ShareEvidence(
        people_csv=people_csv,
        index_json=root / "index.json",
        facts_dir=root / "facts",
        raw_dir=root / "raw",
        dossier_dir=root / "dossiers",
        parents_dir=root / "parents",
        overrides_csv=root / "review.csv",
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


class ConfirmFlagTests(unittest.TestCase):
    def test_first_matching_rule_names_the_flag(self) -> None:
        jev = _jev(is_family=CONFIRM_P, is_minor=CONFIRM_P, sensitive_context=CONFIRM_P,
                   is_automated_sender=ACTIVE_P, is_stranger=ACTIVE_P)
        self.assertEqual(confirm_flag(jev), "family")

    def test_a_sensitive_rule_beats_automated_sender_and_stranger(self) -> None:
        jev = _jev(is_minor=CONFIRM_P, is_automated_sender=ACTIVE_P, is_stranger=ACTIVE_P)
        self.assertEqual(confirm_flag(jev), "minor")
        self.assertEqual(confirm_flag(_jev(is_automated_sender=ACTIVE_P, is_stranger=ACTIVE_P)), "automated_sender")

    def test_family_fires_from_the_relationship_kind_alone(self) -> None:
        self.assertEqual(confirm_flag(_jev(kind="family")), "family")
        self.assertEqual(confirm_flag(_jev(kind="romantic_partner")), "romantic_partner")

    def test_each_later_rule_fires_on_its_own(self) -> None:
        for name, probability_key, probability in (
            ("minor", "is_minor", CONFIRM_P),
            ("sensitive_context", "sensitive_context", CONFIRM_P),
            ("sensitive_provider", "is_healthcare_legal_or_financial_provider", CONFIRM_P),
            ("automated_sender", "is_automated_sender", ACTIVE_P),
            ("stranger", "is_stranger", ACTIVE_P),
        ):
            self.assertEqual(confirm_flag(_jev(**{probability_key: probability})), name)

    def test_a_flagged_owner_is_no_by_the_owner_rule_not_a_confirm(self) -> None:
        row = LabelRow(person_id="person-a", public_identifier="jordan-bravo", is_owner=True,
                       worth="yes", flag="family", probabilities={})
        decision = share_decision(row, None, updated_at="2026-09-24T00:00:00Z")
        self.assertEqual((decision.share, decision.reason), ("no", "owner"))

    def test_a_person_jev_never_saw_raises_no_flag(self) -> None:
        self.assertIsNone(confirm_flag(None))

    def test_nothing_is_flagged_for_an_ordinary_contact(self) -> None:
        self.assertIsNone(confirm_flag(_jev(is_professional=ACTIVE_P)))

    def test_confidential_dealings_is_a_label_not_a_flag(self) -> None:
        self.assertIsNone(confirm_flag(_jev(confidential_dealings=0.95)))


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

    def test_the_request_is_dated_by_its_evidence_not_by_today(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = _write_install(Path(directory))
            facts_file = next(evidence.facts_dir.glob("*.jsonl"))
            synthesized = datetime(2026, 9, 10, 8, 0).timestamp()
            os.utime(facts_file, (synthesized, synthesized))
            person = next(p for p in evidence.load() if p.person_id == "person-a")
        self.assertEqual(person.evidence_date, "2026-09-10")



def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


class ShareNodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.evidence = _write_install(self.root)
        self.out = self.root / "share"

    def _save_labels(self):
        path = next(self.evidence.facts_dir.glob("*.jsonl"))
        rec = json.loads(path.read_text().splitlines()[-1])
        answers = {name: _answer(question) for name, question in build_questions().items()}
        labels = labels_from_answers(answers)
        rec["facts"]["labels"] = {
            **labels.choices, **{f"{name}_p": value for name, value in labels.choice_p.items()},
            **labels.scores, **labels.probabilities,
        }
        path.write_text(json.dumps(rec) + "\n")

    def test_share_requires_the_labels_synthesize_saved(self):
        payload = ShareList(out_dir=self.out, evidence=self.evidence).run().to_payload()
        self.assertEqual(payload["status"], "failed")
        self.assertIn("synthesize", payload["error"])
        self.assertFalse((self.out / "labels.csv").exists())
        self.assertFalse((self.out / "share.csv").exists())

    def test_one_pass_writes_labels_and_share_for_everyone(self):
        self._save_labels()
        payload = ShareList(out_dir=self.out, evidence=self.evidence).run().to_payload()
        self.assertEqual(payload["status"], "completed")
        self.assertEqual((payload["people"], payload["saved_labels"], payload["deterministic_only"]), (2, 1, 1))
        labels = {row["person_id"]: row for row in CsvIO.read_dict_rows_normalized(self.out / "labels.csv")}
        self.assertEqual(labels["person-a"]["relationship_kind"], "family")
        self.assertEqual(labels["person-a"]["flag"], "family")
        self.assertEqual(labels["person-b"]["linkedin_only"], "yes")
        share = {row["person_id"]: row for row in CsvIO.read_dict_rows_normalized(self.out / "share.csv")}
        # Worth yes plus a flag asks the human; the unjudged LinkedIn-only row stays home.
        self.assertEqual((share["person-a"]["share"], share["person-a"]["reason"]), ("confirm", "family"))
        self.assertEqual((share["person-b"]["share"], share["person-b"]["reason"]), ("no", "worth_maybe"))
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["stage"] if "stage" in manifest else manifest["source"], "share")
        self.assertEqual(manifest["by_reason"], {"family": 1, "worth_maybe": 1})
        self.assertEqual((manifest["confirm"], manifest["share_no"], manifest["share_yes"]), (1, 1, 0))


class EvidenceJoinTests(unittest.TestCase):
    def test_parentless_review_uses_public_identifier_for_worth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = _write_install(Path(directory))
            CsvIO.write_dict_rows(
                evidence.overrides_csv,
                ["public_identifier", "network_worth", "llm_worth"],
                [{"public_identifier": "casey-delta", "network_worth": "no"}],
            )
            casey = next(person for person in evidence.load() if person.person_id == "person-b")
        self.assertEqual(casey.network_worth, "no")

    def test_facts_without_dossier_use_facts_file_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = _write_install(Path(directory))
            (evidence.parents_dir / "jordan-bravo-aaaa.md").unlink()
            jordan = next(person for person in evidence.load() if person.person_id == "person-a")
            expected = date.fromtimestamp((evidence.facts_dir / "parent-aaaa.jsonl").stat().st_mtime).isoformat()
        self.assertEqual(jordan.evidence_date, expected)

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
