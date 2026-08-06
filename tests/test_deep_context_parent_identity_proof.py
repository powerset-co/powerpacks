"""Smoke the copy-first parent-identity proof on synthetic legacy state."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.tools.parent_identity_proof import (
    ParentIdentityProof,
)


class ParentIdentityProofSmokeTest(unittest.TestCase):
    def test_facts_only_legacy_person_survives_migration_and_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / ".powerpacks"
            deep_context = base / "deep-context"
            facts = deep_context / "facts"
            facts.mkdir(parents=True)
            (facts / "person-a.jsonl").write_text(
                json.dumps(
                    {
                        "facts": {
                            "canonical_name": "Jordan Bravo",
                            "network_worth": {
                                "decision": "maybe",
                                "reason": "Synthetic fixture",
                            },
                        },
                        "final_confidence": 0.7,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            report = ParentIdentityProof(deep_context).run()

        self.assertEqual(report.status, "completed", report.failures)
        self.assertTrue(report.migration_completed)
        self.assertEqual(report.migrated_people, 1)
        self.assertEqual((report.parents_checked, report.parents_preserved), (1, 1))
        self.assertEqual((report.pairs_checked, report.pairs_preserved), (1, 1))
        self.assertEqual((report.pairs_changed, report.pairs_lost), (0, 0))
        self.assertTrue(report.identity_invariants_ok)


if __name__ == "__main__":
    unittest.main()
