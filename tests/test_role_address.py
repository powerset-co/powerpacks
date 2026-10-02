from __future__ import annotations

import unittest

from packs.ingestion.primitives.common.contact_fields import is_role_address


class RoleAddressTests(unittest.TestCase):
    def test_role_word_must_be_the_whole_local_part(self) -> None:
        for email in ("ir@acme.com", "IR@acme.com", "ar@example.com", "info@example.com", "billing@example.com"):
            with self.subTest(email=email):
                self.assertTrue(is_role_address(email))
        for email in (
            "irene@acme.com", "shira@acme.com", "kirk.ir@acme.com",
            "mark@example.com", "ar.jones@example.com", "casey@example.com",
            "info.casey@example.com", "supporter@example.com",
        ):
            with self.subTest(email=email):
                self.assertFalse(is_role_address(email))

    def test_separated_spellings_of_multi_word_roles(self) -> None:
        for email in (
            "customer.service@example.com", "customer-service@example.com", "customer_service@example.com",
            "investor.relations@example.com", "accounts.receivable@example.com", "accounts.payable@example.com",
            "front.desk@example.com", "help.desk@example.com",
        ):
            with self.subTest(email=email):
                self.assertTrue(is_role_address(email))

    def test_back_office_inboxes_are_role_addresses(self) -> None:
        for email in ("backoffice@example.com", "back.office@example.com", "backoffice-ops@example.com"):
            with self.subTest(email=email):
                self.assertTrue(is_role_address(email))
        self.assertFalse(is_role_address("jordan.backoffice@example.com"))

    def test_short_words_are_never_rejoined_from_pieces(self) -> None:
        for email in ("i.r@acme.com", "a-r@example.com", "h_r@example.com", "o.ps@example.com", "t.ax@example.com"):
            with self.subTest(email=email):
                self.assertFalse(is_role_address(email))

    def test_personal_greeting_and_team_addresses_are_people(self) -> None:
        for email in ("hello@example.com", "hi@example.com", "team@example.com", "assistant@example.com"):
            with self.subTest(email=email):
                self.assertFalse(is_role_address(email))


if __name__ == "__main__":
    unittest.main()
