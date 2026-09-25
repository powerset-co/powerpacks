from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.identity_views import (
    enrichment_queue,
    linkedin_parents,
)
from packs.ingestion.primitives.deep_context.db.models import (
    LinkRow,
    ParentRow,
    PersonRow,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.people_views import person_detail
from deep_context_sqlite_test_helpers import seed_identity


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
        link_values: dict[str, object] = {
            "machine_action": "review",
            "paid_profile": 1,
        }
        link_values.update(link_updates)
        seed_identity(
            self.db,
            parent_id=parent_id,
            person_id=person_id,
            row_key=key,
            name=f"Jordan {key.title()}",
            machine_worth=machine_worth,
            display_slug=key,
            parent_public_identifier=key,
            linkedin_url=f"https://www.linkedin.com/in/{key}",
            human_worth=human_worth,
            link_updates=link_values,
        )

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
                machine_action="review",
                source=WriterSource.RECONCILE.value,
            ),
        ))

        self.assertEqual(linkedin_parents(self.db), [])
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

        rows = enrichment_queue(self.db)

        self.assertEqual(
            {row.row_key for row in rows},
            {"yes", "human-yes"},
        )


if __name__ == "__main__":
    unittest.main()
