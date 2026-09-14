"""Identity prompts retain observed contact handles and message-shared links."""

import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    FactRow,
    IdentityOrigin,
    ParentRow,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
    PersonRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge import (
    identity_judge_prompt,
    judgment_fingerprint,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import JudgeProfile
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence


class IdentityEvidenceIdentifiersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Db(Path(self.temp.name) / "context.sqlite")
        self.db.project_rows((
            ParentRow("jordan", "jordan-bravo", display_name="Jordan Bravo"),
            ParentRow("casey", "casey-delta", display_name="Casey Delta"),
            PersonRow("jordan-email", "jordan"),
            PersonRow("jordan-phone", "jordan"),
            PersonRow("casey-email", "casey"),
            PersonIdentifiersProjection("jordan-email", (
                PersonIdentifierRow("jordan-email", "email", "jordan@example.com"),
            )),
            PersonIdentifiersProjection("jordan-phone", (
                PersonIdentifierRow("jordan-phone", "phone", "+15550100"),
            )),
            PersonIdentifiersProjection("casey-email", (
                PersonIdentifierRow("casey-email", "email", "casey@example.com"),
            )),
            ArtifactRow("facts:jordan", "facts", "jordan", "/facts/jordan.jsonl", "sha", "projected"),
            FactRow("jordan", "jordan", "facts:jordan", facts_json=json.dumps({
                "identifiers": ["jordan@example.com"],
                "owned_identifiers": {"urls": ["https://www.linkedin.com/in/jordan-bravo/"]},
            })),
        ))

    def test_parent_handles_and_shared_link_reach_actual_judge_prompt(self):
        evidence = DossierEvidence.from_db(self.db, ("jordan-email",))
        profile = JudgeProfile(linkedin_url="https://linkedin.com/in/jordan-bravo")
        for origin in (IdentityOrigin.ATTACHED, IdentityOrigin.RESEARCH):
            with self.subTest(origin=origin):
                prompt = identity_judge_prompt(evidence, profile, origin, "")
                self.assertIn("jordan@example.com", prompt)
                self.assertIn("+15550100", prompt)
                self.assertNotIn("casey@example.com", prompt)
                self.assertIn("MATCHES", prompt)
                self.assertIn("work-email DOMAIN", prompt)

    def test_different_shared_link_is_visible_without_claiming_ownership(self):
        evidence = DossierEvidence.from_parent_db(self.db, "jordan")
        prompt = identity_judge_prompt(
            evidence, JudgeProfile(linkedin_url="https://linkedin.com/in/jordan-namesake"),
            IdentityOrigin.RESEARCH, "",
        )
        self.assertIn("DIFFERS", prompt)
        self.assertIn("third party", prompt)
        self.assertIn("https://www.linkedin.com/in/jordan-bravo", prompt)

    def test_legacy_shared_link_remains_available_when_no_owned_url_exists(self):
        self.db.project_rows((FactRow("jordan", "jordan", "facts:jordan", facts_json=json.dumps({
            "identifiers": ["https://www.linkedin.com/in/jordan-bravo/"],
        })),))
        evidence = DossierEvidence.from_parent_db(self.db, "jordan")
        self.assertEqual(evidence.self_linkedin_url, "https://www.linkedin.com/in/jordan-bravo")

    def test_owned_profile_url_precedes_a_legacy_mentioned_url(self):
        self.db.project_rows((FactRow("jordan", "jordan", "facts:jordan", facts_json=json.dumps({
            "identifiers": ["https://www.linkedin.com/in/casey-delta/"],
            "owned_identifiers": {"urls": [
                "https://example.com/jordan", "https://www.linkedin.com/in/jordan-bravo/",
            ]},
        })),))
        evidence = DossierEvidence.from_parent_db(self.db, "jordan")
        self.assertEqual(evidence.self_linkedin_url, "https://www.linkedin.com/in/jordan-bravo")

    def test_changed_contact_handle_invalidates_judge_cache(self):
        profile = JudgeProfile(linkedin_url="https://linkedin.com/in/jordan-bravo")
        def fingerprint():
            return judgment_fingerprint(
                DossierEvidence.from_parent_db(self.db, "jordan"), profile,
                IdentityOrigin.RESEARCH, "", model="test-model", effort="medium",
            )
        before = fingerprint()
        self.db.project_rows((PersonIdentifiersProjection("jordan-email", (
            PersonIdentifierRow("jordan-email", "email", "jordan@new-company.example"),
        )),))
        self.assertNotEqual(before, fingerprint())

    def test_attached_link_is_not_presented_as_message_shared_evidence(self):
        self.db.project_rows((PersonIdentifiersProjection("casey-email", (
            PersonIdentifierRow("casey-email", "email", "casey@example.com"),
            PersonIdentifierRow("casey-email", "linkedin", "casey-delta"),
        )),))
        prompt = identity_judge_prompt(
            DossierEvidence.from_parent_db(self.db, "casey"),
            JudgeProfile(linkedin_url="https://linkedin.com/in/casey-delta"),
            IdentityOrigin.ATTACHED, "",
        )
        self.assertIn("casey@example.com", prompt)
        self.assertNotIn("own messages", prompt)
        self.assertNotIn("MATCHES", prompt)


if __name__ == "__main__":
    unittest.main()
