"""Research rationale reaches the identity judge without becoming profile fact."""

import unittest

from packs.ingestion.primitives.deep_context.db.models import IdentityOrigin
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge import (
    identity_judge_prompt,
    prefer_cached_profile,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import JudgeProfile
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence


class IdentityResearchClaimsTests(unittest.TestCase):
    def test_cached_research_claims_survive_profile_hydration_in_judge_prompt(self):
        reason = "A public contact page lists +15550100 for Jordan Bravo; the email concerns a 2021 job interview."
        profile = prefer_cached_profile(
            JudgeProfile(reason=reason),
            JudgeProfile(full_name="Jordan Bravo", experiences=("Acme Robotics, Engineer, 2023-present",)),
        )
        prompt = identity_judge_prompt(DossierEvidence(name="Jordan Bravo"), profile, IdentityOrigin.RESEARCH, "")
        self.assertIn("Cached research claims (not independently verified):", prompt)
        self.assertIn(reason, prompt)
        self.assertIn("Missing information is not a contradiction.", prompt)
        self.assertIn("Interview or referral context does not prove employment; evaluate the dates.", prompt)

    def test_attached_profile_does_not_inherit_speculative_research_claims(self):
        prompt = identity_judge_prompt(
            DossierEvidence(name="Jordan Bravo"),
            JudgeProfile(reason="An unverified research claim."),
            IdentityOrigin.ATTACHED, "",
        )
        self.assertNotIn("An unverified research claim.", prompt)
        self.assertNotIn("Cached research claims", prompt)


if __name__ == "__main__":
    unittest.main()
