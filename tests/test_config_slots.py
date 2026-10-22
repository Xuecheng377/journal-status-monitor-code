"""Multi-account slot reading. Uses the function API so it never needs a module reload."""
import os
import unittest
from unittest.mock import patch

from config import MAX_PLATFORM_SLOTS, platform_slot_names, read_platform_account

ENV = {
    "IEEE_EMAIL": "first@example.com",
    "IEEE_PASSWORD": "first-password",
    "IEEE_URL": "https://mc.manuscriptcentral.com/first",
    "IEEE_2_EMAIL": "second@example.com",
    "IEEE_2_PASSWORD": "second-password",
    "IEEE_2_URL": "https://mc.manuscriptcentral.com/second",
    "ELSEVIER_2_EMAIL": "elsevier@example.com",
    "ELSEVIER_2_PASSWORD": "elsevier-password",
    "ELSEVIER_2_URL": "https://www.editorialmanager.com/example",
}


class PlatformSlotTests(unittest.TestCase):
    def test_numbered_slots_are_read_independently(self):
        with patch.dict(os.environ, ENV, clear=True):
            accounts = [read_platform_account("IEEE", "IEEE", slot) for slot in (1, 2)]
            elsevier = read_platform_account("Elsevier", "ELSEVIER", 2)
        self.assertEqual([account.email for account in accounts], ["first@example.com", "second@example.com"])
        self.assertTrue(all(account.configured for account in accounts))
        self.assertEqual(elsevier.email, "elsevier@example.com")

    def test_unused_slot_is_not_configured(self):
        with patch.dict(os.environ, ENV, clear=True):
            account = read_platform_account("IEEE", "IEEE", 3)
        self.assertFalse(account.configured)
        self.assertFalse(account.partially_configured)

    def test_partial_slot_is_flagged(self):
        with patch.dict(os.environ, {"IEEE_2_EMAIL": "only-email@example.com"}, clear=True):
            account = read_platform_account("IEEE", "IEEE", 2)
        self.assertTrue(account.partially_configured)
        self.assertFalse(account.configured)

    def test_slot_names(self):
        self.assertEqual(platform_slot_names("IEEE", 1), ("IEEE_EMAIL", "IEEE_PASSWORD", "IEEE_URL"))
        self.assertEqual(platform_slot_names("IEEE", 2), ("IEEE_2_EMAIL", "IEEE_2_PASSWORD", "IEEE_2_URL"))
        self.assertEqual(MAX_PLATFORM_SLOTS, 5)


if __name__ == "__main__":
    unittest.main()