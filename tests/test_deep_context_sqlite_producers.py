from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context import apply_retargets
from packs.ingestion.primitives.deep_context import profile_projection
from packs.ingestion.primitives.deep_context.apply_retargets import ApplyRetargets
from packs.ingestion.primitives.deep_context.db.models import (
    IdentityMachineProjection,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
    PersonSourceRow,
    PersonSourcesProjection,
    ReviewSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.db.identity_views import linkedin_review
from packs.ingestion.primitives.deep_context.db.identity_policy import IdentityPolicy
import packs.ingestion.primitives.deep_context.identity_reconcile.settlement as identity_settlement
from packs.ingestion.primitives.deep_context.identity_reconcile.results import (
    upsert_retargets,
    write_overrides,
)
from packs.ingestion.primitives.deep_context.judge_models import (
    IdentityTask,
    IdentityVerdict,
    JudgeProfile,
)
from packs.ingestion.primitives.deep_context.identity_reconcile.results import (
    RetargetProposal,
)
from packs.ingestion.primitives.deep_context.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.db.projectors import project_parent_fact
from packs.shared.csv_io import CsvIO
from deep_context_sqlite_test_helpers import query, seed_identity


def reconcile_task(
    *,
    verdict: str = "confirmed",
    confidence: float = 0.99,
    reason: str = "matches",
    action: str = "confirm",
    fingerprint: str = "fixture-judge-input",
) -> IdentityTask:
    return IdentityTask(
        candidate_key="alice",
        person_ids=("person-1",),
        evidence=DossierEvidence(name="Alice Example"),
        linkedin=JudgeProfile.from_payload({
            "linkedin_url": "https://www.linkedin.com/in/alice",
        }),
        verdict=IdentityVerdict.from_payload({
            "verdict": verdict,
            "confidence": confidence,
            "reason": reason,
        }),
        action=action,
        judgment_fingerprint=fingerprint,
    )


class SqliteProducerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Db(self.root / "deep-context.sqlite")
        seed_identity(
            self.db,
            parent_id="parent-1",
            person_id="person-1",
            row_key="alice",
            name="Alice Example",
            machine_worth="maybe",
            parent_public_identifier="parent-1",
            linkedin_url="https://www.linkedin.com/in/alice",
            candidate_people=True,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_reconcile_projects_machine_identity_without_touching_human(self) -> None:
        task = reconcile_task()
        write_overrides(self.db, [task])
        row = query(self.db, "SELECT * FROM links WHERE row_key='alice'")[0]
        self.assertEqual(row["machine_action"], "verify")
        self.assertEqual(row["machine_approved"], "auto")

        self.db.decide_identity("alice", "verify", source=ReviewSource.REVIEW.value)
        task = replace(
            task,
            verdict=IdentityVerdict.from_payload({
                "verdict": "wrong_person",
                "confidence": 1.0,
                "reason": "different",
            }),
            action="detach",
        )
        self.assertEqual(write_overrides(self.db, [task]).preserved_user_rows, 1)
        row = query(self.db, "SELECT * FROM links WHERE row_key='alice'")[0]
        self.assertEqual(row["decision_action"], "verify")
        self.assertEqual(row["machine_action"], "verify")

    def test_reconcile_snapshots_identity_once_per_projection_batch(self) -> None:
        task = reconcile_task()
        with mock.patch.object(
            identity_settlement,
            "identity_snapshot",
            wraps=identity_settlement.identity_snapshot,
        ) as snapshot:
            write_overrides(self.db, [task, replace(task)])
        snapshot.assert_called_once_with(self.db)

    def test_retarget_and_downstream_baton_are_sqlite_derived(self) -> None:
        upsert_retargets(
            self.db,
            [
                RetargetProposal(
                    candidate_key="alice",
                    new_linkedin_url="https://www.linkedin.com/in/alice-correct",
                    confidence=0.9,
                    judge_fingerprint="fixture-research-judge-input",
                )
            ],
        )
        row = query(self.db, "SELECT * FROM links WHERE row_key='alice'")[0]
        self.assertEqual(row["machine_action"], "retarget")
        self.assertEqual(row["machine_proposed_public_identifier"], "alice-correct")

    def test_effective_identity_decision_precedence(self) -> None:
        common = {
            "linkedin_url": "https://www.linkedin.com/in/alice",
            "public_identifier": "alice",
            "machine_action": "retarget",
            "machine_proposed_url": "https://www.linkedin.com/in/alice-machine",
            "machine_proposed_public_identifier": "alice-machine",
        }

        human = IdentityPolicy.effective_decision(
            **common,
            decision_action="retarget",
            decision_approved="yes",
            replacement_url="https://www.linkedin.com/in/alice-human",
            replacement_public_identifier="alice-human",
            machine_approved="auto",
        )
        self.assertEqual(
            (human.action, human.approved, human.url),
            ("retarget", "yes", "https://www.linkedin.com/in/alice-human"),
        )

        machine = IdentityPolicy.effective_decision(
            **common,
            decision_action=None,
            decision_approved=None,
            replacement_url=None,
            replacement_public_identifier=None,
            machine_approved="auto",
        )
        self.assertEqual(
            (machine.action, machine.approved, machine.url),
            ("retarget", "auto", "https://www.linkedin.com/in/alice-machine"),
        )

        pending = IdentityPolicy.effective_decision(
            **common,
            decision_action=None,
            decision_approved=None,
            replacement_url=None,
            replacement_public_identifier=None,
            machine_approved=None,
        )
        self.assertEqual(
            (pending.action, pending.approved, pending.url, pending.new_url),
            (
                "",
                "",
                "https://www.linkedin.com/in/alice-machine",
                "https://www.linkedin.com/in/alice-machine",
            ),
        )

    def test_cleared_retarget_is_recorded_as_machine_accepted(self) -> None:
        upsert_retargets(
            self.db,
            [
                RetargetProposal(
                    candidate_key="alice",
                    new_linkedin_url="https://www.linkedin.com/in/alice-correct",
                    llm_reject="",
                    llm_reject_confidence="0.910",
                    has_reject_fields=True,
                    judge_fingerprint="fixture-research-judge-input",
                )
            ],
        )
        row = query(self.db, "SELECT * FROM links WHERE row_key='alice'")[0]
        self.assertEqual(
            (row["machine_action"], row["machine_approved"], row["machine_reject"]),
            ("retarget", "auto", None),
        )
        out = self.root / "retarget.csv"
        with mock.patch.object(
            profile_projection,
            "hydrate_profiles",
            side_effect=AssertionError("realize must not hydrate profiles"),
        ):
            result = ApplyRetargets(
                db=self.db,
                profile_cache_dir=self.root / "cache",
                out_csv=out,
            ).run()
        self.assertEqual((result["approved_retargets"], result["rows"]), (1, 1))
        self.assertEqual(
            CsvIO.read_dict_rows(out)[0]["public_identifier"],
            "alice-correct",
        )

    def test_uncleared_retarget_stays_pending_and_is_not_realized(self) -> None:
        facts_dir = self.root / "facts"
        facts_dir.mkdir()
        facts_path = facts_dir / "parent-1.jsonl"
        facts_path.write_text(
            json.dumps({
                "final_confidence": 0.9,
                "facts": {
                    "network_worth": {"decision": "yes", "reason": "known"},
                },
            }) + "\n",
            encoding="utf-8",
        )
        project_parent_fact(self.db, facts_path, "parent-1")
        upsert_retargets(
            self.db,
            [
                RetargetProposal(
                    candidate_key="alice",
                    new_linkedin_url="https://www.linkedin.com/in/alice-uncertain",
                    llm_reject="yes",
                    llm_reject_confidence="0.790",
                    has_reject_fields=True,
                    judge_fingerprint="fixture-rejected-research-judge-input",
                )
            ],
        )

        row = query(self.db, "SELECT * FROM links WHERE row_key='alice'")[0]
        self.assertEqual(
            (row["machine_action"], row["machine_approved"], row["machine_reject"]),
            ("retarget", None, "yes"),
        )
        (parent,) = linkedin_review(self.db, "queue")
        self.assertEqual(parent.candidates[0].action, "")
        self.assertEqual(
            parent.candidates[0].new_url,
            "https://www.linkedin.com/in/alice-uncertain",
        )

        result = ApplyRetargets(
            db=self.db,
            profile_cache_dir=self.root / "cache",
            out_csv=self.root / "retarget.csv",
        ).run()
        self.assertEqual((result["approved_retargets"], result["rows"]), (0, 0))

    def test_machine_settlement_rejects_a_missing_judge_fingerprint(self) -> None:
        with self.assertRaisesRegex(StoreError, "lacks judge fingerprint"):
            write_overrides(self.db, [reconcile_task(fingerprint="")])

        baton = self.root / "review.csv"
        result = ApplyRetargets(
            db=self.db,
            profile_cache_dir=self.root / "cache",
            out_csv=self.root / "retarget.csv",
        ).run()
        self.assertFalse(baton.exists())
        self.assertTrue((self.root / "retarget.csv").exists())
        self.assertEqual(result["approved_retargets"], 0)

    def test_approved_retarget_carries_contact_identity_from_sqlite(self) -> None:
        self.db.project_rows((
            PersonIdentifiersProjection("person-1", (
                PersonIdentifierRow("person-1", "email", "alice@example.com"),
            )),
            PersonSourcesProjection("person-1", (
                PersonSourceRow("person-1", "gmail"),
            )),
        ))
        self.db.project_rows((IdentityMachineProjection(
            "alice",
            machine_action="retarget",
            machine_approved="auto",
            machine_proposed_url="https://www.linkedin.com/in/alice-correct",
            machine_proposed_public_identifier="alice-correct",
        ),))
        cache_dir = self.root / "cache"
        raw_profile = {
            "public_identifier": "alice-correct",
            "linkedin_url": "https://www.linkedin.com/in/alice-correct",
            "full_name": "Alice Correct",
            "experiences": [
                {"title": "Founder", "company_name": "Correct Robotics"}
            ],
        }
        profile_projection.project_profile_results(
            self.db,
            [(
                {
                    "public_identifier": "alice-correct",
                    "linkedin_url": "https://www.linkedin.com/in/alice-correct",
                    "candidate_key": "alice",
                    "parent_id": "parent-1",
                },
                {
                    "state": "content",
                    "normalized_profile": {
                        "success": True,
                        "full_name": "Alice Correct",
                        "experiences": raw_profile["experiences"],
                    },
                    "data": raw_profile,
                    "from_cache": False,
                },
            )],
            cache_dir,
        )
        captured = {}

        def build(url, pub, raw, carry):
            captured.update(carry)
            return {"public_identifier": pub, "linkedin_url": url}

        with (
            mock.patch.object(
                profile_projection,
                "hydrate_profiles",
                side_effect=AssertionError("realize must not hydrate profiles"),
            ),
            mock.patch.object(apply_retargets, "build_retarget_row", side_effect=build),
        ):
            result = ApplyRetargets(
                db=self.db,
                profile_cache_dir=cache_dir,
                out_csv=self.root / "retarget.csv",
            ).run()

        self.assertEqual((result["approved_retargets"], result["enriched"]), (1, 1))
        self.assertEqual(captured["primary_email"], "alice@example.com")
        self.assertEqual(captured["source_channels"], "gmail")

    def test_human_retarget_projects_without_profile_spend(self) -> None:
        profile_projection.project_profile_results(
            self.db,
            [(
                {
                    "public_identifier": "alice",
                    "linkedin_url": "https://www.linkedin.com/in/alice",
                    "candidate_key": "alice",
                    "parent_id": "parent-1",
                },
                {
                    "state": "content",
                    "normalized_profile": {
                        "success": True,
                        "public_identifier": "alice",
                        "full_name": "Wrong Alice",
                    },
                    "data": {
                        "public_identifier": "alice",
                        "full_name": "Wrong Alice",
                    },
                },
            )],
            self.root / "cache",
        )
        self.db.decide_identity(
            "alice",
            "retarget",
            replacement_url="https://www.linkedin.com/in/alice-human-choice",
            replacement_public_identifier="alice-human-choice",
        )
        out = self.root / "retarget.csv"
        with mock.patch.object(
            profile_projection,
            "hydrate_profiles",
            side_effect=AssertionError("realize must not hydrate profiles"),
        ):
            result = ApplyRetargets(
                db=self.db,
                profile_cache_dir=self.root / "cache",
                out_csv=out,
            ).run()

        self.assertEqual((result["approved_retargets"], result["rows"]), (1, 1))
        (row,) = CsvIO.read_dict_rows(out)
        self.assertEqual(row["public_identifier"], "alice-human-choice")
        self.assertEqual(
            row["linkedin_url"],
            "https://www.linkedin.com/in/alice-human-choice",
        )
        self.assertEqual(row["full_name"], "")

    def test_synthesis_projects_fixed_facts_artifact(self) -> None:
        facts_dir = self.root / "facts"
        facts_dir.mkdir()
        record = {
            "final_confidence": 0.88,
            "facts": {"network_worth": {"decision": "maybe", "reason": "uncertain"}},
        }
        path = facts_dir / "parent-1.jsonl"
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        result = project_parent_fact(self.db, path, "parent-1")
        self.assertEqual(result["parent_id"], "parent-1")
        fact = query(self.db, "SELECT * FROM facts WHERE subject_key='parent-1'")[0]
        self.assertIsNone(fact["person_id"])
        self.assertEqual(fact["machine_worth"], "maybe")
        self.assertEqual(fact["confidence"], 0.88)


if __name__ == "__main__":
    unittest.main()
