"""Identity judging sees elected dossier facts without changing research inputs."""

import json
from pathlib import Path
import tempfile
import unittest

from packs.ingestion.primitives.deep_context.db.models import ArtifactRow, FactRow, IdentityOrigin, ParentRow, PersonRow
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge import SYSTEM_PROMPT, identity_judge_prompt, judgment_fingerprint
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import JudgeProfile
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence


class IdentityDossierContextTests(unittest.TestCase):
    def test_identity_system_requires_actual_overlap_without_invented_base_rates(self):
        self.assertNotIn("1-in-100", SYSTEM_PROMPT)
        self.assertIn("one strong specific overlap OR two useful independent details", SYSTEM_PROMPT)
        self.assertIn("Name alone is insufficient", SYSTEM_PROMPT)
        self.assertIn("Do not invent geography", SYSTEM_PROMPT)
        self.assertIn("copies the supplied dossier is not independent corroboration", SYSTEM_PROMPT)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Db(Path(temporary.name) / "context.sqlite")
        self.db.project_rows((
            ParentRow("parent", "jordan-bravo", display_name="Jordan Bravo"),
            PersonRow("email", "parent"),
            PersonRow("phone", "parent"),
        ))
        self.profile = JudgeProfile(full_name="Jordan Bravo", linkedin_url="https://linkedin.com/in/jordan-bravo")

    def project(self, subject, facts, *, person_id=None):
        self.db.project_rows((
            ArtifactRow(f"facts:{subject}", "facts", "parent", f"/unused/{subject}.jsonl", "sha", "projected"),
            FactRow(subject, "parent", f"facts:{subject}", person_id=person_id, facts_json=json.dumps(facts)),
        ))

    def prompt(self, origin=IdentityOrigin.RESEARCH):
        return identity_judge_prompt(DossierEvidence.from_db(self.db, ("email",)), self.profile, origin, "")

    def test_elected_parent_details_reach_both_identity_origins_without_old_judgments(self):
        self.project("email", {"title": "STALE_CHILD_FACT"}, person_id="email")
        self.project("parent", {
            "canonical_name": "Jordan Avery Bravo",
            "aliases": [f"Jordan alias {i}" for i in range(10)],
            "employers": [{"name": "Oriel Robotics", "role": "Finance lead", "status": "past"}],
            "school": "Example University", "field_of_study": "Materials science",
            "topics": [f"specific topic {i}" for i in range(12)],
            "notable_events": [{"date": "2019-04", "summary": "Led the acquisition diligence work."}],
            "shared_context": [{"overlap": "employer", "detail": "Worked together", "evidence": "Reviewed the Oriel budget together."}],
            "identifiers": ["mentioned@example.test"],
            "owned_identifiers": {"emails": ["jordy@example.test"], "phones": ["+15550100222"], "urls": ["https://example.test/jordy"]},
            "confidence": 0.91,
            "network_worth": {"decision": "yes", "reason": "OLD_WORTH_JUDGMENT"},
            "identity_judgment": "OLD_IDENTITY_JUDGMENT",
        })
        for origin in (IdentityOrigin.ATTACHED, IdentityOrigin.RESEARCH):
            with self.subTest(origin=origin):
                prompt = self.prompt(origin)
                for detail in (
                    "Jordan Avery Bravo", "Jordan alias 9", "Employer (past)", "Finance lead",
                    "Materials science", "specific topic 11", "2019-04", "acquisition diligence",
                    "Reviewed the Oriel budget together", "mentioned@example.test", "jordy@example.test",
                    "+15550100222", "https://example.test/jordy",
                ):
                    self.assertIn(detail, prompt)
                for old in ("STALE_CHILD_FACT", "OLD_WORTH_JUDGMENT", "OLD_IDENTITY_JUDGMENT", "0.91"):
                    self.assertNotIn(old, prompt)

    def test_without_parent_facts_one_child_lookup_includes_other_child_timeline(self):
        self.project("email", {"notable_events": [{"date": "2017", "summary": "Built the first robot."}]}, person_id="email")
        self.project("phone", {"notable_events": [{"date": "2021", "summary": "Moved into research leadership."}]}, person_id="phone")
        prompt = self.prompt()
        self.assertIn("Built the first robot", prompt)
        self.assertIn("Moved into research leadership", prompt)

    def test_timeline_changes_identity_cache_but_not_research_bio(self):
        facts = {"relationship_to_owner": "former colleague", "employers": [{"name": "Oriel Robotics"}],
                 "notable_events": [{"date": "2019", "summary": "Started finance work."}]}

        def state():
            self.project("parent", facts)
            evidence = DossierEvidence.from_db(self.db, ("email",))
            return evidence.research_bio(), judgment_fingerprint(
                evidence, self.profile, IdentityOrigin.RESEARCH, "", model="test-model", effort="medium",
            )

        before = state()
        facts["notable_events"][0]["date"] = "2020"
        after = state()
        self.assertEqual(before[0], "My relationship: former colleague. Employers (from our messages): Oriel Robotics")
        self.assertEqual(before[0], after[0])
        self.assertNotEqual(before[1], after[1])
        facts["network_worth"] = {"decision": "no", "reason": "Old machine assessment"}
        facts["confidence"] = 0.99
        self.assertEqual(after, state())


if __name__ == "__main__":
    unittest.main()
