"""Research rationale reaches the identity judge without becoming profile fact."""

import hashlib
import unittest

from parallel.types import TaskRunJsonOutput

from packs.ingestion.primitives.deep_context.db.models import IdentityOrigin
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge import (
    identity_judge_prompt,
    judgment_fingerprint,
    prefer_cached_profile,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import JudgeProfile
from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence


class IdentityResearchClaimsTests(unittest.TestCase):
    def research_profile(self):
        return ResearchResult.from_output(TaskRunJsonOutput(
            type="json",
            content={
                "real_name": "Jordan Bravo",
                "linkedin_url": "https://www.linkedin.com/in/jordan-bravo",
                "work_experience": [{"title": "CFO", "company_name": "Oriel Robotics"}],
                "education": [],
            },
            basis=[{
                "field": "linkedin_url",
                "reasoning": "The Oriel CFO role comes from the supplied contact. A public contact page links jordan@oriel.example to the proposed URL.",
                "citations": [],
            }],
        )).identity_profile()

    def test_research_positions_are_claims_not_independent_profile_employment(self):
        research = self.research_profile()
        selected = prefer_cached_profile(research, JudgeProfile())
        evidence = DossierEvidence(name="Jordan Bravo", emails=("jordan@oriel.example",))
        prompt = identity_judge_prompt(evidence, selected, IdentityOrigin.RESEARCH, "")
        self.assertEqual(selected.source, "research")
        self.assertIn("RESEARCH-DERIVED CANDIDATE CLAIMS (not fetched LinkedIn profile evidence):", prompt)
        self.assertNotIn("\n\nLINKEDIN:", prompt)
        self.assertIn("CFO @ Oriel Robotics", prompt)
        self.assertIn(research.linkedin_url, prompt)
        self.assertIn(research.reason, prompt)
        self.assertIn("copied contact facts are not corroboration", prompt)
        self.assertNotIn("DOMAIN matching the profile's employer is strong identity proof", prompt)

        def fingerprint(profile):
            return judgment_fingerprint(evidence, profile, IdentityOrigin.RESEARCH, "", model="fixture", effort="high")

        self.assertNotEqual(fingerprint(research), fingerprint(selected))
        self.assertEqual(fingerprint(selected), fingerprint(prefer_cached_profile(research, JudgeProfile())))

    def test_hydrated_research_profile_keeps_existing_prompt_and_fingerprint(self):
        research = self.research_profile()
        cached = JudgeProfile.from_payload({
            "linkedin_url": research.linkedin_url,
            "full_name": "Jordan Bravo",
            "experiences": ["Engineer @ Bravo Robotics"],
            "source": "cache",
            "reason": research.reason,
        })
        selected = prefer_cached_profile(research, cached)
        evidence = DossierEvidence(name="Jordan Bravo", emails=("jordan@oriel.example",))
        self.assertEqual(selected.as_judge_dict(), cached.as_judge_dict())
        self.assertEqual(selected.source, "cache")
        self.assertEqual(
            identity_judge_prompt(evidence, selected, IdentityOrigin.RESEARCH, ""),
            identity_judge_prompt(evidence, cached, IdentityOrigin.RESEARCH, ""),
        )
        self.assertIn("\n\nLINKEDIN:", identity_judge_prompt(evidence, selected, IdentityOrigin.RESEARCH, ""))
        self.assertEqual(
            hashlib.sha256(identity_judge_prompt(evidence, selected, IdentityOrigin.RESEARCH, "").encode()).hexdigest(),
            "b1f0c59073a0d6fdede8c7fde3c75f6e1f3a78cd49828760ce443a6553ea5477",
        )
        self.assertEqual(
            judgment_fingerprint(evidence, selected, IdentityOrigin.RESEARCH, "", model="fixture", effort="high"),
            judgment_fingerprint(evidence, cached, IdentityOrigin.RESEARCH, "", model="fixture", effort="high"),
        )

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
