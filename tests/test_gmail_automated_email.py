import unittest

from packs.ingestion.primitives.discover.gmail.msgvault.util import is_automated_email


class AutomatedEmailTests(unittest.TestCase):
    def test_people_at_service_companies_are_kept(self):
        for email in (
            "casey@airbnb.com",
            "jordan@uber.com",
            "sam@delta.com",
            "alex@alaska.edu",
            "taylor@zendesk.com",
            "morgan@unitedway.org",
            "riley@enterprise-ai.io",
            "jamie@bookingpartners.co",
        ):
            with self.subTest(email=email):
                self.assertEqual(is_automated_email(email), (False, ""))

    def test_service_senders_are_still_automated(self):
        for email in (
            "noreply@uber.com",
            "no-reply@airbnb.com",
            "notifications@github.com",
            "reply+abc@notifications.github.com",
            "casey@bounce.mailer.example.com",
            "help@acme.zendesk.com",
            "12345@reply.airbnb.com",
        ):
            with self.subTest(email=email):
                self.assertTrue(is_automated_email(email)[0])


if __name__ == "__main__":
    unittest.main()
