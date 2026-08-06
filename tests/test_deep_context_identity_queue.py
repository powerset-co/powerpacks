from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.deep_context import identity_evidence, profile_projection
from packs.ingestion.primitives.deep_context.db.identity_views import linkedin_review
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    FactRow,
    LinkRow,
    ParentRow,
    PersonRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.people_views import person_detail
from packs.ingestion.primitives.deep_context.identity_reconcile import healing, queue
from packs.ingestion.primitives.deep_context.identity_reconcile.judgment_policy import (
    NO_PROFILE_REASON,
)


@dataclass(frozen=True)
class Candidate:
    parent_id: str
    parent_slug: str
    name: str
    candidate_key: str
    pub: str
    url: str


class IdentityQueueWorthGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Db(self.root / "deep-context.sqlite")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_parent(
        self,
        key: str,
        machine_worth: str,
        *,
        human_worth: str | None = None,
        **link_updates: object,
    ) -> None:
        parent_id = f"parent-{key}"
        person_id = f"person-{key}"
        artifact_key = f"facts:{person_id}"
        link_values: dict[str, object] = {
            "linkedin_url": f"https://www.linkedin.com/in/{key}",
            "display_name": f"Jordan {key.title()}",
            "machine_judgment": "needs_review",
            "machine_confidence": 0.0,
            "machine_reason": NO_PROFILE_REASON,
            "paid_profile": 1,
        }
        link_values.update(link_updates)
        self.db.project_rows((
            ParentRow(parent_id, key, f"Jordan {key.title()}", key),
            PersonRow(person_id, parent_id, display_name=f"Jordan {key.title()}"),
            ArtifactRow(
                artifact_key,
                "facts",
                parent_id,
                f"/facts/{person_id}.jsonl",
                f"sha-{key}",
                "projected",
                person_id=person_id,
            ),
            FactRow(
                person_id,
                parent_id,
                artifact_key,
                person_id=person_id,
                machine_worth=machine_worth,
                facts_json=json.dumps({"canonical_name": f"Jordan {key.title()}"}),
            ),
            LinkRow(key, parent_id, key, "pub", **link_values),
        ))
        if human_worth is not None:
            self.db.decide_worth(parent_id, human_worth)

    def test_attached_queue_uses_human_then_machine_worth_precedence(self) -> None:
        self.add_parent("machine-no", "no")
        self.add_parent("human-no", "yes", human_worth="no")
        self.add_parent("human-yes", "no", human_worth="yes")
        self.add_parent("maybe", "maybe")

        tasks = queue.build_tasks(self.db)
        selected, skipped, uncapped = healing.select_candidates(
            self.db, None, Candidate, lambda _line: None,
        )

        self.assertEqual(
            {task["candidate_key"] for task in tasks},
            {"human-yes", "maybe"},
        )
        self.assertEqual(
            {candidate.candidate_key for candidate in selected},
            {"human-yes", "maybe"},
        )
        self.assertEqual((skipped, uncapped), (0, 2))

    def test_effective_no_never_reaches_hydration_judging_or_heal(self) -> None:
        self.add_parent("machine-no", "no")
        self.add_parent("human-no", "yes", human_worth="no")

        tasks = queue.build_tasks(self.db)
        candidates, skipped, uncapped = healing.select_candidates(
            self.db, None, Candidate, lambda _line: None,
        )
        self.assertEqual(tasks, [])
        self.assertEqual(candidates, [])
        self.assertEqual((skipped, uncapped), (0, 0))

        with (
            patch.object(profile_projection, "hydrate_profiles") as hydrate,
            patch.object(identity_evidence, "judge_batch") as judge,
        ):
            queue.fetch_missing_profiles(self.db, tasks, self.root / "profiles")
            healing.fetch_states(
                self.db,
                candidates,
                self.root / "profiles",
                max_workers=1,
                say=lambda _line: None,
            )
            healing.rejudge(self.db, candidates, concurrency=1)

        hydrate.assert_not_called()
        judge.assert_not_called()

    def test_factsless_parent_is_absent_until_synthesis_runs(self) -> None:
        self.db.project_rows((
            ParentRow("parent-factsless", "factsless", "Jordan Factsless", "factsless"),
            PersonRow("person-factsless", "parent-factsless"),
            LinkRow(
                "factsless",
                "parent-factsless",
                "factsless",
                "pub",
                linkedin_url="https://www.linkedin.com/in/factsless",
                machine_judgment="needs_review",
                machine_confidence=0.0,
                machine_reason=NO_PROFILE_REASON,
            ),
        ))

        tasks = queue.build_tasks(self.db)
        selected, skipped, uncapped = healing.select_candidates(
            self.db, None, Candidate, lambda _line: None,
        )

        self.assertEqual(tasks, [])
        self.assertEqual(selected, [])
        self.assertEqual((skipped, uncapped), (0, 0))
        self.assertEqual(linkedin_review(self.db, "parents"), [])
        self.assertIsNone(person_detail(self.db, "parent-factsless"))

    def test_research_queue_keeps_its_effective_yes_only_gate(self) -> None:
        research = {
            "machine_judgment": "wrong_person",
            "machine_confidence": 0.91,
            "machine_reason": "wrong attached profile",
            "judgment_payload_json": json.dumps({"recommend_deep_research": True}),
        }
        self.add_parent("yes", "yes", **research)
        self.add_parent("maybe", "maybe", **research)
        self.add_parent("human-no", "yes", human_worth="no", **research)
        self.add_parent("human-yes", "no", human_worth="yes", **research)

        rows = linkedin_review(self.db, "enrichment")

        self.assertEqual(
            {row["candidate_key"] for row in rows},
            {"yes", "human-yes"},
        )


if __name__ == "__main__":
    unittest.main()
