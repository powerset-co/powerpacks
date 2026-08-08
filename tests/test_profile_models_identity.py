"""ProfileResult.from_payload identity-stamping regression tests.

Covers the provider-identity-mismatch path: a RapidAPI response that resolves
a renamed/redirected slug to a DIFFERENT profile than the one requested must
never be relabeled under the requested identity (that would silently file a
stranger's content under this candidate)."""

from __future__ import annotations

import unittest

from packs.ingestion.primitives.deep_context.enrich.profile_models import (
    PROFILE_IDENTITY_MISMATCH,
    ProfileResult,
)


def _content_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "state": "content",
        "from_cache": False,
        "fetched": True,
        "status_code": 200,
        "attempts": 1,
        "data": {"raw": "provider-response"},
        "normalized_profile": {
            "success": True,
            "public_identifier": "jordan-bravo",
            "linkedin_url": "https://www.linkedin.com/in/jordan-bravo",
            "full_name": "Jordan Bravo",
            "headline": "Product Manager at Example Co",
            "experiences": [{"title": "PM", "company": "Example Co"}],
            "education": [{"school": "State University"}],
        },
    }
    payload["normalized_profile"].update(overrides)
    return payload


class ProfileResultIdentityTest(unittest.TestCase):
    def test_agreement_stamps_requested_identity_as_before(self) -> None:
        result = ProfileResult.from_payload(
            "jordan-bravo",
            "https://www.linkedin.com/in/jordan-bravo",
            _content_payload(),
        )

        self.assertEqual(result.state, "content")
        self.assertTrue(result.normalized_profile.success)
        self.assertEqual(result.normalized_profile.public_identifier, "jordan-bravo")
        self.assertEqual(len(result.normalized_profile.experiences), 1)
        self.assertEqual(
            result.normalized_profile.experiences[0].company_name, "Example Co"
        )

    def test_no_echoed_identifier_is_treated_as_agreement(self) -> None:
        # A sparse success response with neither public_identifier nor
        # linkedin_url gives us nothing to distrust — same as today.
        payload = _content_payload(public_identifier="", linkedin_url="")
        result = ProfileResult.from_payload(
            "jordan-bravo", "https://www.linkedin.com/in/jordan-bravo", payload
        )

        self.assertTrue(result.normalized_profile.success)
        self.assertEqual(result.normalized_profile.public_identifier, "jordan-bravo")

    def test_mismatched_public_identifier_is_not_relabeled(self) -> None:
        # The provider silently resolved a renamed/redirected slug to a
        # DIFFERENT real profile than the one we requested.
        payload = _content_payload(
            public_identifier="not-jordan-bravo",
            linkedin_url="https://www.linkedin.com/in/not-jordan-bravo",
        )

        result = ProfileResult.from_payload(
            "jordan-bravo", "https://www.linkedin.com/in/jordan-bravo", payload
        )

        self.assertEqual(result.state, PROFILE_IDENTITY_MISMATCH)
        # Carries the PROVIDER's own identity — never the requested one.
        self.assertFalse(result.normalized_profile.success)
        self.assertEqual(result.normalized_profile.public_identifier, "not-jordan-bravo")
        self.assertEqual(
            result.normalized_profile.linkedin_url,
            "https://www.linkedin.com/in/not-jordan-bravo",
        )
        # No wrong-person content leaks through to consumers keyed off
        # `.present`/experiences/education (classify_queue, linkedin_view).
        self.assertEqual(result.normalized_profile.experiences, ())
        self.assertEqual(result.normalized_profile.education, ())
        self.assertEqual(result.normalized_profile.full_name, None)

    def test_mismatch_detected_from_url_when_pub_field_is_blank(self) -> None:
        payload = _content_payload(
            public_identifier="",
            linkedin_url="https://www.linkedin.com/in/not-jordan-bravo",
        )

        result = ProfileResult.from_payload(
            "jordan-bravo", "https://www.linkedin.com/in/jordan-bravo", payload
        )

        self.assertEqual(result.state, PROFILE_IDENTITY_MISMATCH)
        self.assertFalse(result.normalized_profile.success)
        self.assertEqual(result.normalized_profile.public_identifier, "not-jordan-bravo")

    def test_raw_payload_still_carries_the_untouched_provider_response(self) -> None:
        # `raw_payload()` is independent of the normalized_profile relabeling
        # — the original provider payload is never lost, mismatch or not.
        payload = _content_payload(
            public_identifier="not-jordan-bravo",
            linkedin_url="https://www.linkedin.com/in/not-jordan-bravo",
        )

        result = ProfileResult.from_payload(
            "jordan-bravo", "https://www.linkedin.com/in/jordan-bravo", payload
        )

        self.assertEqual(result.raw_payload(), {"raw": "provider-response"})

    def test_unsuccessful_provider_response_is_untouched(self) -> None:
        payload = {
            "state": "empty",
            "from_cache": False,
            "fetched": True,
            "normalized_profile": {"success": False},
        }

        result = ProfileResult.from_payload(
            "jordan-bravo", "https://www.linkedin.com/in/jordan-bravo", payload
        )

        self.assertEqual(result.state, "empty")
        self.assertFalse(result.normalized_profile.success)
        self.assertIsNone(result.normalized_profile.public_identifier)


if __name__ == "__main__":
    unittest.main()
