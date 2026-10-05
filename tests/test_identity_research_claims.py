"""Identity judges receive research citations, never synthesized research claims."""

import json
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
                "reasoning": "INVENTED email-to-employer bridge from the supplied contact.",
                "citations": [{"url": "https://example.com/jordan", "title": "Jordan Bravo bio",
                               "excerpts": ["Jordan Bravo founded Oriel; contact jordan@oriel.example."]}],
            }],
        )).identity_profile()

    def test_repeated_citations_are_emitted_once_without_losing_distinct_excerpts(self):
        first = {"url": "https://example.com/jordan", "title": "Jordan bio", "excerpts": ["Founded Oriel."]}
        second = {"url": first["url"], "title": first["title"], "excerpts": ["Contact jordan@oriel.example."]}
        result = ResearchResult.from_output(TaskRunJsonOutput(type="json",
            content={"work_experience": [], "education": []},
            basis=[{"field": "summary", "reasoning": "", "citations": [first, first]},
                   {"field": "linkedin_url", "reasoning": "", "citations": [second, first, second]}]))
        self.assertEqual(result.identity_citations(), [first, second])
        self.assertEqual(json.loads(result.identity_profile().reason), [first, second])

    def test_contact_only_employer_cannot_confirm_an_empty_attached_profile(self):
        from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge import SYSTEM_PROMPT
        evidence = DossierEvidence(name="Jordan Bravo", emails=("jordan@acme.example",),
            employers=("Acme",), dossier="Employer: Acme")
        profile = JudgeProfile(full_name="Jordan Bravo")
        prompt = identity_judge_prompt(evidence, profile, IdentityOrigin.ATTACHED, "")
        self.assertIn("experience:\n  (none)", prompt)
        self.assertIn("An employer appearing only in the contact dossier cannot corroborate the profile.", SYSTEM_PROMPT)

    def test_research_positions_are_claims_not_independent_profile_employment(self):
        research = self.research_profile()
        selected = prefer_cached_profile(research, JudgeProfile())
        evidence = DossierEvidence(name="Jordan Bravo", emails=("jordan@oriel.example",))
        prompt = identity_judge_prompt(evidence, selected, IdentityOrigin.RESEARCH, "")
        self.assertEqual(selected.source, "research")
        self.assertIn("RESEARCH PROPOSAL (no fetched LinkedIn profile evidence):", prompt)
        self.assertNotIn("\n\nLINKEDIN:", prompt)
        self.assertNotIn("CFO @ Oriel Robotics", prompt)
        self.assertNotIn("INVENTED email-to-employer bridge", prompt)
        self.assertIn("Jordan Bravo founded Oriel; contact jordan@oriel.example.", prompt)
        self.assertIn(research.linkedin_url, prompt)
        self.assertIn(research.reason, prompt)
        self.assertIn("copied contact facts are not corroboration", prompt)
        self.assertNotIn("DOMAIN matching the profile's employer is strong identity proof", prompt)

        def fingerprint(profile):
            return judgment_fingerprint(evidence, profile, IdentityOrigin.RESEARCH, "", model="fixture", effort="high")

        self.assertNotEqual(fingerprint(research), fingerprint(selected))
        self.assertEqual(fingerprint(selected), fingerprint(prefer_cached_profile(research, JudgeProfile())))

    def test_jev_requests_omit_uncited_research_content_and_reasoning(self):
        from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import jev_judge
        from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import IdentityTask
        selected = prefer_cached_profile(self.research_profile(), JudgeProfile())
        request = json.dumps(jev_judge._requests(IdentityTask(DossierEvidence(name="Jordan Bravo"), selected), ()))
        self.assertNotIn("INVENTED email-to-employer bridge", request)
        self.assertNotIn("CFO @ Oriel Robotics", request)
        self.assertIn("https://example.com/jordan", request)
        self.assertIn("Jordan Bravo founded Oriel; contact jordan@oriel.example.", request)

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
            judgment_fingerprint(evidence, selected, IdentityOrigin.RESEARCH, "", model="fixture", effort="high"),
            judgment_fingerprint(evidence, cached, IdentityOrigin.RESEARCH, "", model="fixture", effort="high"),
        )

    def test_actual_citations_survive_profile_hydration_in_judge_prompt(self):
        research = self.research_profile()
        reason = research.reason
        profile = prefer_cached_profile(
            research,
            JudgeProfile(full_name="Jordan Bravo", experiences=("Acme Robotics, Engineer, 2023-present",)),
        )
        prompt = identity_judge_prompt(DossierEvidence(name="Jordan Bravo"), profile, IdentityOrigin.RESEARCH, "")
        self.assertIn("Research source citations (URLs, titles and excerpts):", prompt)
        self.assertNotIn("INVENTED email-to-employer bridge", prompt)
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
