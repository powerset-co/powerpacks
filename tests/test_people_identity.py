"""People-side identity contract: one LinkedIn slug/URL normalizer, one key.

Mirrors tests/test_company_identity.py's company-slug coverage. The company
normalizer already percent-decoded and was tested; the people side was the
outlier, and a second non-decoding copy in the Gmail discover stage split one
person into two rows at the fan-in merge.
"""

from __future__ import annotations

import unittest

from packs.ingestion.primitives.discover.gmail import extract_gmail
from packs.ingestion.schemas.people_schema import (
    extract_public_identifier,
    generate_person_id,
    normalize_linkedin_url,
    normalize_people_row,
    row_public_identifier,
    stable_linkedin_key,
)

# Synthetic "Jordan Bravo" written in Arabic script, and the percent-encoded
# form a Gmail resolution row stores for the same profile.
UNICODE_SLUG = "جوردن-bravo-42a7b1"
ENCODED_SLUG = "%D8%AC%D9%88%D8%B1%D8%AF%D9%86-bravo-42a7b1"
UNICODE_URL = f"https://www.linkedin.com/in/{UNICODE_SLUG}"
ENCODED_URL = f"https://www.linkedin.com/in/{ENCODED_SLUG}"


class PeopleSlugNormalizationTests(unittest.TestCase):
    def test_percent_encoded_slug_decodes(self) -> None:
        self.assertEqual(extract_public_identifier(ENCODED_URL), UNICODE_SLUG)
        self.assertEqual(normalize_linkedin_url(ENCODED_URL), UNICODE_URL)

    def test_raw_unicode_slug_passes_through(self) -> None:
        self.assertEqual(extract_public_identifier(UNICODE_URL), UNICODE_SLUG)
        self.assertEqual(normalize_linkedin_url(UNICODE_URL), UNICODE_URL)

    def test_mixed_ascii_and_non_ascii_slug(self) -> None:
        self.assertEqual(
            extract_public_identifier("https://www.linkedin.com/in/jordan-%D8%A8%D8%B1%D8%A7%D9%81%D9%88-9b2c"),
            "jordan-برافو-9b2c",
        )

    def test_trailing_slash_query_string_and_uppercase(self) -> None:
        self.assertEqual(extract_public_identifier("https://www.linkedin.com/in/jordan-bravo/"), "jordan-bravo")
        self.assertEqual(
            extract_public_identifier("https://www.linkedin.com/in/jordan-bravo?trk=public_profile"),
            "jordan-bravo",
        )
        self.assertEqual(extract_public_identifier("https://WWW.LinkedIn.com/in/Jordan-Bravo"), "jordan-bravo")
        self.assertEqual(
            normalize_linkedin_url("www.linkedin.com/in/Jordan-Bravo/?trk=public#about"),
            "https://www.linkedin.com/in/jordan-bravo",
        )
        self.assertEqual(
            normalize_linkedin_url(f"linkedin.com/in/{ENCODED_SLUG}/?trk=public"),
            UNICODE_URL,
        )

    def test_non_profile_and_empty_urls(self) -> None:
        self.assertEqual(extract_public_identifier(""), "")
        self.assertEqual(extract_public_identifier("https://www.linkedin.com/company/acme"), "")
        self.assertEqual(normalize_linkedin_url(""), "")
        self.assertEqual(normalize_linkedin_url("https://example.com/jordan"), "https://example.com/jordan")


class StableLinkedinKeyTests(unittest.TestCase):
    def test_percent_encoded_and_unicode_forms_share_one_key(self) -> None:
        encoded_row = {"public_identifier": ENCODED_SLUG, "linkedin_url": ENCODED_URL}
        unicode_row = {"public_identifier": UNICODE_SLUG, "linkedin_url": UNICODE_URL}
        self.assertEqual(stable_linkedin_key(encoded_row), stable_linkedin_key(unicode_row))
        self.assertEqual(stable_linkedin_key(encoded_row), f"linkedin:{UNICODE_SLUG}")
        # Same key => same deterministic person id => one merged row, not two.
        self.assertEqual(
            generate_person_id(row_public_identifier(encoded_row)),
            generate_person_id(row_public_identifier(unicode_row)),
        )

    def test_url_wins_over_a_contradictory_stored_public_identifier(self) -> None:
        row = {"public_identifier": "stale-slug", "linkedin_url": "https://www.linkedin.com/in/jordan-bravo"}
        self.assertEqual(stable_linkedin_key(row), "linkedin:jordan-bravo")

    def test_stored_slug_is_used_and_canonicalized_when_there_is_no_url(self) -> None:
        self.assertEqual(stable_linkedin_key({"public_identifier": ENCODED_SLUG}), f"linkedin:{UNICODE_SLUG}")
        self.assertEqual(stable_linkedin_key({"public_identifier": "Jordan-Bravo"}), "linkedin:jordan-bravo")
        # A non-profile URL cannot yield a slug, so the stored one still applies.
        self.assertEqual(
            stable_linkedin_key({"public_identifier": "jordan-bravo", "linkedin_url": "https://example.com/jordan"}),
            "linkedin:jordan-bravo",
        )

    def test_row_without_linkedin_has_no_key(self) -> None:
        self.assertEqual(stable_linkedin_key({"primary_email": "jordan@example.com"}), "")


class NormalizePeopleRowTests(unittest.TestCase):
    def test_normalized_row_is_internally_consistent(self) -> None:
        row = normalize_people_row(
            {
                "public_identifier": ENCODED_SLUG,
                "linkedin_url": ENCODED_URL,
                "full_name": "Jordan Bravo",
                "primary_email": "jordan@example.com",
            }
        )
        self.assertEqual(row["linkedin_url"], UNICODE_URL)
        self.assertEqual(row["public_identifier"], UNICODE_SLUG)
        self.assertEqual(row["public_identifier"], extract_public_identifier(row["linkedin_url"]))
        self.assertEqual(stable_linkedin_key(row), f"linkedin:{UNICODE_SLUG}")

    def test_normalization_is_idempotent_across_encodings(self) -> None:
        encoded = normalize_people_row({"public_identifier": ENCODED_SLUG, "linkedin_url": ENCODED_URL})
        decoded = normalize_people_row({"public_identifier": UNICODE_SLUG, "linkedin_url": UNICODE_URL})
        self.assertEqual(encoded, decoded)
        self.assertEqual(normalize_people_row(encoded), encoded)

    def test_slug_only_row_keeps_its_slug_and_gains_no_url(self) -> None:
        row = normalize_people_row({"public_identifier": "Jordan-Bravo", "full_name": "Jordan Bravo"})
        self.assertEqual(row["public_identifier"], "jordan-bravo")
        self.assertEqual(row["linkedin_url"], "")


class GmailExtractorUsesTheSchemaNormalizerTests(unittest.TestCase):
    def test_extract_gmail_no_longer_ships_its_own_pair(self) -> None:
        self.assertIs(extract_gmail.extract_public_identifier, extract_public_identifier)
        self.assertIs(extract_gmail.normalize_linkedin_url, normalize_linkedin_url)

    def test_applied_resolution_slug_matches_every_other_writer(self) -> None:
        # apply_resolutions stamps id/public_identifier/linkedin_url from the
        # resolution URL; a percent-encoded one must land on the same identity a
        # LinkedIn-CSV or directory row would produce for the same profile.
        applied_url = extract_gmail.normalize_linkedin_url(ENCODED_URL)
        applied_pub = extract_gmail.extract_public_identifier(applied_url)
        self.assertEqual(applied_pub, UNICODE_SLUG)
        self.assertEqual(
            stable_linkedin_key({"public_identifier": applied_pub, "linkedin_url": applied_url}),
            stable_linkedin_key(normalize_people_row({"linkedin_url": UNICODE_URL})),
        )


if __name__ == "__main__":
    unittest.main()
