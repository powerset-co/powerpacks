from __future__ import annotations

import unittest

from packs.ingestion.schemas.linkedin_profile_normalizer import detect_linkedin_schema, normalize_linkedin_profile


class LinkedinProfileNormalizerTests(unittest.TestCase):
    def assertStableKeys(self, profile: dict) -> None:
        expected = {
            "success",
            "error",
            "public_identifier",
            "member_id",
            "first_name",
            "last_name",
            "full_name",
            "headline",
            "summary",
            "location_str",
            "city",
            "state",
            "country",
            "profile_pic_url",
            "linkedin_url",
            "connections",
            "skills",
            "languages",
            "certifications",
            "education",
            "experiences",
        }
        self.assertEqual(set(profile), expected)

    def test_error_payloads_always_return_dict(self) -> None:
        for payload in ({"success": False, "message": "  Bad profile  "}, {"error": "not found"}, None):
            profile = normalize_linkedin_profile(payload)  # type: ignore[arg-type]
            self.assertStableKeys(profile)
            self.assertFalse(profile["success"])
            self.assertTrue(profile["error"])
            self.assertEqual(profile["experiences"], [])
            self.assertEqual(profile["education"], [])

    def test_unrecognized_payload_always_returns_dict(self) -> None:
        profile = normalize_linkedin_profile({"foo": "bar"})
        self.assertStableKeys(profile)
        self.assertFalse(profile["success"])
        self.assertIn("unrecognized", profile["error"])

    def test_already_normalized_payload_preserved(self) -> None:
        payload = normalize_linkedin_profile(
            {
                "full_name": "Jane Example",
                "public_identifier": "jane-example",
                "experiences": [{"title": "Founder", "starts_at": {"year": 2020, "month": 1, "day": 2}}],
            }
        )
        payload["error"] = ""
        schema = detect_linkedin_schema(payload)
        self.assertEqual(schema, "normalized")
        profile = normalize_linkedin_profile(payload)
        self.assertStableKeys(profile)
        self.assertTrue(profile["success"])
        self.assertEqual(profile["public_identifier"], "jane-example")
        self.assertEqual(profile["experiences"][0]["starts_at"], {"year": 2020, "month": 1, "day": 2})

    def test_nested_data_and_rapidapi_experiences(self) -> None:
        payload = {
            "data": {
                "public_identifier": "jane-example",
                "fullName": "Jane Example",
                "headline": "Builder",
                "location": {"city": "San Francisco", "state": "CA", "country": "US"},
                "profilePicUrl": "https://example.test/pic.jpg",
                "experiences": [
                    {
                        "title": "CEO",
                        "companyName": "Acme",
                        "start_date": {"year": 2021, "month": 3, "day": 4},
                        "end_date": {"bad": "date"},
                    }
                ],
                "educations": [{"schoolName": "State", "start_date": {"year": 2010}}],
                "skills": ["python"],
            }
        }
        self.assertEqual(detect_linkedin_schema(payload), "rapidapi_parsed")
        profile = normalize_linkedin_profile(payload)
        self.assertTrue(profile["success"])
        self.assertEqual(profile["first_name"], "Jane")
        self.assertEqual(profile["last_name"], "Example")
        self.assertEqual(profile["city"], "San Francisco")
        self.assertEqual(profile["profile_pic_url"], "https://example.test/pic.jpg")
        self.assertEqual(profile["experiences"][0]["company_name"], "Acme")
        self.assertEqual(profile["experiences"][0]["starts_at"], {"year": 2021, "month": 3, "day": 4})
        self.assertIsNone(profile["experiences"][0]["ends_at"])
        self.assertEqual(profile["education"][0]["school"], "State")

    def test_work_experience_variant(self) -> None:
        profile = normalize_linkedin_profile(
            {
                "first_name": "Jane",
                "last_name": "Example",
                "work_experience": [{"position": "Engineer", "company": {"name": "Widgets"}}],
            }
        )
        self.assertTrue(profile["success"])
        self.assertEqual(profile["full_name"], "Jane Example")
        self.assertEqual(profile["experiences"][0]["title"], "Engineer")
        self.assertEqual(profile["experiences"][0]["company_name"], "Widgets")

    def test_linkedin_native_full_positions(self) -> None:
        payload = {
            "firstName": "Native",
            "lastName": "Person",
            "geo": {"city": "San Francisco, California", "country": "United States", "full": "San Francisco, California, United States"},
            "fullPositions": {"values": [{"title": "VP", "companyName": "NativeCo"}]},
            "education": {"values": [{"school": "University"}]},
        }
        self.assertEqual(detect_linkedin_schema(payload), "linkedin_native")
        profile = normalize_linkedin_profile(payload)
        self.assertTrue(profile["success"])
        self.assertEqual(profile["location_str"], "San Francisco, California, United States")
        self.assertEqual(profile["city"], "San Francisco")
        self.assertEqual(profile["state"], "California")
        self.assertEqual(profile["country"], "United States")
        self.assertEqual(profile["experiences"][0]["title"], "VP")
        self.assertEqual(profile["education"][0]["school"], "University")

    def test_rapidapi_converted_camel_case(self) -> None:
        payload = {
            "firstName": "Cam",
            "lastName": "Case",
            "profileURL": "https://www.linkedin.com/in/cam-case",
            "profilePicture": "pic",
            "position": [{"title": "Lead"}],
        }
        self.assertEqual(detect_linkedin_schema(payload), "rapidapi_converted")
        profile = normalize_linkedin_profile(payload)
        self.assertTrue(profile["success"])
        self.assertEqual(profile["linkedin_url"], "https://www.linkedin.com/in/cam-case")
        self.assertEqual(profile["profile_pic_url"], "pic")


if __name__ == "__main__":
    unittest.main()
