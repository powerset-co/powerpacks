from __future__ import annotations

import hashlib
import unittest

from packs.indexing.lib.job_descriptions import focused_description


class JobDescriptionFocusTest(unittest.TestCase):
    def test_short_role_does_not_restore_removed_perks(self) -> None:
        result = focused_description("Requirements\nExperience with Python.\nBenefits\nFree lunch.")
        self.assertIn("Experience with Python.", result)
        self.assertNotIn("Free lunch", result)

    def test_unheaded_requirements_survive_offer_removal(self) -> None:
        result = focused_description(
            "Build fault-tolerant storage engines.\n"
            "Experience diagnosing distributed consistency failures is required.\n"
            "Salary: $140,000 to $180,000.\n"
            "Apply now at https://example.com/apply."
        )
        self.assertIn("Build fault-tolerant storage engines.", result)
        self.assertIn("Experience diagnosing distributed consistency failures is required.", result)
        self.assertNotIn("$140,000", result)
        self.assertNotIn("https://example.com/apply", result)

    def test_unfamiliar_heading_after_perks_resumes_role_content(self) -> None:
        result = focused_description(
            "## Benefits\nFree meals and a gym membership.\n\n"
            "## Your first quarter\nDeliver a tested incident-response playbook."
        )
        self.assertNotIn("Free meals", result)
        self.assertIn("Deliver a tested incident-response playbook.", result)

    def test_people_operations_duties_are_not_employee_perks(self) -> None:
        duties = (
            "Manage payroll, employee benefits, and equity administration.\n"
            "Design compensation bands and audit salary equity."
        )
        result = focused_description("Responsibilities\n" + duties + "\nBenefits\nFree dental insurance.")
        for line in duties.splitlines():
            self.assertIn(line, result)
        self.assertNotIn("Free dental insurance", result)

    def test_role_budget_survives_salary_section(self) -> None:
        result = focused_description(
            "Responsibilities\nOwn a $5 million annual marketing budget.\n"
            "Compensation\nBase salary of $160,000 plus equity."
        )
        self.assertIn("Own a $5 million annual marketing budget.", result)
        self.assertNotIn("$160,000", result)

    def test_requirement_negations_and_preferences_are_preserved(self) -> None:
        result = focused_description(
            "Requirements\nNo college degree is required.\n"
            "Preferred qualifications\nRust experience is preferred, not required.\n"
            "Equivalent professional experience is accepted."
        )
        self.assertIn("No college degree is required.", result)
        self.assertIn("Rust experience is preferred, not required.", result)
        self.assertIn("Equivalent professional experience is accepted.", result)

    def test_location_eligibility_does_not_remove_geographic_expertise(self) -> None:
        result = focused_description(
            "Location\nSan Francisco, California\n"
            "Requirements\nKnowledge of California energy markets is required.\n"
            "Experience selling to enterprise buyers in Japan is preferred."
        )
        self.assertNotIn("San Francisco, California", result)
        self.assertIn("Knowledge of California energy markets is required.", result)
        self.assertIn("Experience selling to enterprise buyers in Japan is preferred.", result)

    def test_technology_stack_outside_standard_role_headings_survives(self) -> None:
        result = focused_description(
            "Responsibilities\nBuild and operate our event-processing platform.\n"
            "## Technology stack\nPython, PostgreSQL, Kafka, Kubernetes.\n"
            "## Benefits\nUnlimited PTO."
        )
        self.assertIn("Python, PostgreSQL, Kafka, Kubernetes.", result)
        self.assertNotIn("Unlimited PTO", result)

    def test_field_work_travel_is_retained_as_a_duty(self) -> None:
        result = focused_description(
            "Responsibilities\nTravel to customer sites to commission industrial equipment.\n"
            "Perform hands-on fault diagnosis and safety inspections in the field.\n"
            "Benefits\nRelocation assistance is available."
        )
        self.assertIn("Travel to customer sites to commission industrial equipment.", result)
        self.assertIn("Perform hands-on fault diagnosis and safety inspections in the field.", result)
        self.assertNotIn("Relocation assistance", result)

    def test_equity_perk_does_not_look_like_ownership_duty(self) -> None:
        result = focused_description(
            "Benefits\nOwn a piece of the company through our equity program.\n"
            "Responsibilities\nOwn quarterly planning and release management."
        )
        self.assertNotIn("equity program", result)
        self.assertIn("Own quarterly planning and release management.", result)

    def test_interview_paragraph_preserves_separate_real_duty(self) -> None:
        result = focused_description(
            "Interview process\nMeet the team in a final interview. "
            "You will build diagnostic tools for industrial equipment."
        )
        self.assertNotIn("final interview", result)
        self.assertIn("You will build diagnostic tools for industrial equipment.", result)

    def test_application_language_requirement_survives(self) -> None:
        result = focused_description(
            "How to apply\nPlease submit your application in English. "
            "English is our working language."
        )
        self.assertNotIn("submit your application", result)
        self.assertIn("English", result)
        self.assertIn("English is our working language.", result)

    def test_legal_jurisdiction_is_expertise_not_location_eligibility(self) -> None:
        requirement = "Experience interpreting California employment law and EU privacy regulations."
        result = focused_description("Requirements\n" + requirement)
        self.assertIn(requirement, result)

    def test_funding_sentence_removal_preserves_adjacent_product_fact(self) -> None:
        result = focused_description(
            "About us\nWe raised $30 million from Example Ventures. "
            "Our product automates warehouse inventory reconciliation."
        )
        self.assertNotIn("$30 million", result)
        self.assertIn("Our product automates warehouse inventory reconciliation.", result)

    def test_location_prose_removal_preserves_separate_responsibility(self) -> None:
        result = focused_description(
            "This role is based in San Francisco. "
            "You will design high-throughput query engines."
        )
        self.assertNotIn("San Francisco", result)
        self.assertIn("You will design high-throughput query engines.", result)

    def test_promotion_keywords_inside_explicit_experience_are_preserved(self) -> None:
        requirements = (
            "Experience hiring top-tier talent across engineering and design.",
            "Experience preparing Forbes AI award submissions for enterprise products.",
            "Experience managing international Olympiad events and evaluating submissions.",
        )
        for requirement in requirements:
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, focused_description("Requirements\n" + requirement))

    def test_compensation_software_product_is_not_a_salary_offer(self) -> None:
        description = "We build salary benchmarking software for compensation teams."
        self.assertIn(description, focused_description("About us\n" + description))

    def test_funding_sentence_preserves_company_stage_and_product(self) -> None:
        result = focused_description(
            "About us\nWe raised $20 million in Series A funding from Example Ventures "
            "to build nuclear reactor controls."
        )
        self.assertIn("Series A", result)
        self.assertIn("build nuclear reactor controls", result)

    def test_olympiad_experience_is_a_preference_when_explicit(self) -> None:
        qualification = "International Olympiad experience is preferred, not required."
        self.assertIn(qualification, focused_description("Preferred qualifications\n" + qualification))

    def test_explicit_architecture_duty_survives_unusual_benefits_subheading(self) -> None:
        duty = "Architect distributed systems and mentor junior engineers."
        result = focused_description("Benefits\nCareer Development\n" + duty)
        self.assertIn(duty, result)

    def test_management_scope_survives_following_salary_offer(self) -> None:
        result = focused_description(
            "Requirements\nManage a team of 5.\nBenefits\nWe offer competitive salary."
        )
        self.assertIn("Manage a team of 5.", result)
        self.assertNotIn("We offer competitive salary", result)


class SemanticRemovalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = "A once-in-a-lifetime adventure.\nRequirements\nExperience building storage systems."
        self.digest = hashlib.sha256(focused_description(self.source).encode()).hexdigest()

    def test_reviewed_quote_removal_preserves_requirement(self) -> None:
        result = focused_description(self.source, source_sha256=self.digest,
                                     removal_quotes=["A once-in-a-lifetime adventure."])
        self.assertEqual(result, "Requirements\nExperience building storage systems.")
        self.assertEqual(focused_description(self.source, source_sha256=self.digest,
                                           removal_quotes=[]), self.source)

    def test_stale_or_missing_source_digest_rejected(self) -> None:
        for digest in (None, "old-source"):
            with self.subTest(digest=digest), self.assertRaisesRegex(ValueError, "match focused source"):
                focused_description(self.source, source_sha256=digest,
                                    removal_quotes=["A once-in-a-lifetime adventure."])

    def test_absent_empty_or_duplicate_quotes_rejected(self) -> None:
        for quotes in (["invented requirement"], [""], [" "], ["adventure", "adventure"], "adventure"):
            with self.subTest(quotes=quotes), self.assertRaises(ValueError):
                focused_description(self.source, source_sha256=self.digest, removal_quotes=quotes)

    def test_ambiguous_occurrence_rejected(self) -> None:
        source = "Repeated. Repeated.\nRequirements\nExperience building storage systems."
        digest = hashlib.sha256(focused_description(source).encode()).hexdigest()
        with self.assertRaisesRegex(ValueError, "exactly one"):
            focused_description(source, source_sha256=digest, removal_quotes=["Repeated."])

    def test_overlapping_quotes_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "overlap"):
            focused_description(self.source, source_sha256=self.digest,
                                removal_quotes=["once-in-a-lifetime", "lifetime adventure"])

    def test_entire_description_cannot_be_removed(self) -> None:
        with self.assertRaisesRegex(ValueError, "entire description"):
            focused_description(self.source, source_sha256=self.digest, removal_quotes=[self.source])


if __name__ == "__main__":
    unittest.main()
