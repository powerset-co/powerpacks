from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from deep_context_sqlite_test_helpers import message_payload, seed_identity
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    ProjectionStatus,
)
from packs.ingestion.primitives.deep_context.db.share_views import person_labels, share_decisions
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.share.evidence import ShareEvidence
from packs.ingestion.primitives.share.labels import (
    share_decision,
    ACTIVE_P,
    CONFIRM_P,
    confirm_flag,
    deterministic_labels,
    labels_from_answers,
)
from packs.ingestion.primitives.share.models import NO_MESSAGES, JevLabels, LabelRow, MessageStats, PersonEvidence
from packs.ingestion.primitives.share.questions import NOUL_LABELS, build_questions
from packs.ingestion.primitives.share.share_list import ShareList
from packs.shared.csv_io import CsvIO

REFERENCE_DATE = "2026-09-24"

PEOPLE_COLUMNS = [
    "id",
    "public_identifier",
    "full_name",
    "source_channels",
    "interaction_counts",
    "last_interaction",
    "superseded_person_ids",
]


def _answer(question: dict) -> dict:
    if question["type"] == "noul":
        return {"type": "noul", "noul": 0.9}
    options = list(question["criteria"] if question["type"] == "choice" else map(str, range(len(question["criteria"]))))
    remainder = 0.3 / (len(options) - 1)
    return {
        "type": question["type"],
        "probabilities": {option: 0.7 if index == 0 else remainder for index, option in enumerate(options)},
    }


def _person(**overrides) -> PersonEvidence:
    fields = {
        "person_id": "person-a",
        "public_identifier": "jordan-bravo",
        "full_name": "Jordan Bravo",
        "source_channels": ("gmail_msgvault",),
        "interaction_counts": {"gmail": 4},
        "last_interaction": "2026-09-01T00:00:00+00:00",
        "superseded_person_ids": (),
        "network_worth": "yes",
        "dossier": "---\ngenerated_at: 2026-09-10T08:00:00+00:00\n---\n# Jordan Bravo\nWorked together on storage.",
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


def _saved_labels(**overrides) -> dict:
    """Labels as synthesize saves them: every question answered, the given cells overridden."""
    saved: dict = {}
    for name, question in build_questions().items():
        if question["type"] == "noul":
            saved[name] = 0.0
        elif question["type"] == "score":
            saved[name] = 0
        else:
            saved[name] = "unknown"
            saved[f"{name}_p"] = 1.0
    saved.update(overrides)
    return saved


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
        self.assertAlmostEqual(labels.scores["warmth"], 0.75)  # 0.7·0 + 0.075·(1+2+3+4)
        self.assertAlmostEqual(labels.probabilities["is_family"], 0.9)


def _write_people(root: Path) -> Path:
    path = root / "people.csv"
    CsvIO.write_dict_rows(
        path,
        PEOPLE_COLUMNS,
        [
            {
                "id": "person-a",
                "public_identifier": "jordan-bravo",
                "full_name": "Jordan Bravo",
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
    return path


def _seed_store(root: Path, *, save_labels: bool) -> Db:
    """One message-backed person and one LinkedIn-only person in the canonical store."""
    db = Db(root / "deep-context.sqlite")
    work = root / "artifacts"
    work.mkdir(exist_ok=True)
    seed_identity(
        db,
        parent_id="parent-aaaa",
        person_id="person-a",
        row_key="jordan-bravo-aaaa",
        name="Jordan Bravo",
        machine_worth="yes",
        public_identifier="jordan-bravo",
        artifact_root=work,
        dossier_body="---\ngenerated_at: 2026-09-10T08:00:00+00:00\n---\n# Jordan Bravo\nStorage work.",
        labels=_saved_labels(relationship_kind="family") if save_labels else {},
    )
    bundle = {
        "person_id": "parent-aaaa",
        "name": "Jordan Bravo",
        "groups": ["weekend-crew"],
        "messages": [
            message_payload("SECRET-BODY", channel="gmail", at="2026-01-01T00:00:00+00:00",
                            direction="from_them", subject="SECRET-SUBJECT"),
            message_payload("SECRET-BODY", channel="gmail", at="2026-09-01T00:00:00+00:00",
                            direction="from_me", subject="SECRET-SUBJECT"),
        ],
    }
    bundle_path = work / "parent-aaaa.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    db.project_rows((
        ArtifactRow(
            "source-bundle:parent-aaaa",
            ArtifactKind.SOURCE_BUNDLE.value,
            "parent-aaaa",
            str(bundle_path),
            hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
            ProjectionStatus.PROJECTED.value,
            payload_json=json.dumps(bundle),
        ),
    ))
    seed_identity(
        db,
        parent_id="parent-bbbb",
        person_id="person-b",
        row_key="casey-delta-bbbb",
        name="Casey Delta",
        machine_worth="maybe",
        public_identifier="casey-delta",
    )
    return db


class ShareNodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "share"
        self.people_csv = _write_people(self.root)

    def _evidence(self, db: Db) -> ShareEvidence:
        return ShareEvidence(db, people_csv=self.people_csv)

    def _run(self, db: Db) -> dict:
        # The canonical inputs are declared external artifacts; the explicit db
        # and evidence point at the temp store, so only the readability precheck
        # needs the real files to stand in.
        return ShareList(db=db, out_dir=self.out, evidence=self._evidence(db)).run().to_payload()

    def test_share_requires_the_labels_synthesize_saved(self) -> None:
        db = _seed_store(self.root, save_labels=False)
        payload = self._run(db)
        self.assertEqual(payload["status"], "failed")
        self.assertIn("synthesize", payload["error"])
        self.assertEqual(person_labels(db), ())
        self.assertEqual(share_decisions(db), ())

    def test_one_pass_writes_labels_and_share_for_everyone(self) -> None:
        db = _seed_store(self.root, save_labels=True)
        payload = self._run(db)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual((payload["people"], payload["saved_labels"], payload["deterministic_only"]), (2, 1, 1))
        labels = {row.person_id: row for row in person_labels(db)}
        self.assertEqual(json.loads(labels["person-a"].labels_json)["relationship_kind"], "family")
        self.assertEqual(labels["person-a"].flag, "family")
        self.assertTrue(json.loads(labels["person-b"].labels_json)["linkedin_only"])
        share = {row.person_id: row for row in share_decisions(db)}
        # Worth yes plus a flag asks the human; the unjudged LinkedIn-only row stays home.
        self.assertEqual((share["person-a"].share, share["person-a"].reason), ("confirm", "family"))
        self.assertEqual((share["person-b"].share, share["person-b"].reason), ("no", "worth_maybe"))
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["source"], "share")
        self.assertEqual(manifest["by_reason"], {"family": 1, "worth_maybe": 1})
        self.assertEqual((manifest["confirm"], manifest["share_no"], manifest["share_yes"]), (1, 1, 0))


class EvidenceJoinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.people_csv = _write_people(self.root)

    def test_facts_dossier_and_messages_join_through_the_parent_id(self) -> None:
        db = _seed_store(self.root, save_labels=True)
        people = ShareEvidence(db, people_csv=self.people_csv).load()
        by_id = {person.person_id: person for person in people}
        self.assertEqual(by_id["person-a"].facts["canonical_name"], "Jordan Bravo")
        self.assertIn("Storage work", by_id["person-a"].dossier)
        self.assertEqual(by_id["person-a"].messages.from_me, 1)
        self.assertEqual(by_id["person-a"].messages.from_them, 1)
        self.assertEqual(by_id["person-a"].messages.group_count, 1)
        self.assertTrue(by_id["person-b"].linkedin_only)

    def test_absent_cells_stay_absent(self) -> None:
        db = _seed_store(self.root, save_labels=True)
        casey = next(
            person for person in ShareEvidence(db, people_csv=self.people_csv).load()
            if person.person_id == "person-b"
        )
        self.assertEqual(casey.public_identifier, "casey-delta")
        self.assertIsNone(casey.last_interaction)
        self.assertEqual(casey.interaction_counts, {})
        self.assertIsNone(casey.facts)
        self.assertIsNone(casey.dossier)
        self.assertEqual(casey.messages, NO_MESSAGES)


if __name__ == "__main__":
    unittest.main()
