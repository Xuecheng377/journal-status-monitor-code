"""Regression tests for terminal-status detection.

These cases are the reason a real manuscript once stopped being monitored
forever: substring matching treated "Required Reviews Completed" as a finished
manuscript. Nothing here touches the network or the disk.
"""
import unittest
from unittest.mock import patch

from config import Config, normalize_status


NOT_TERMINAL_STATUSES = [
    "Under Review",
    "Required Reviews Completed",
    "Reviews Completed",
    "All Reviews Completed",
    "Awaiting Final Decision",
    "Awaiting Editor Decision",
    "Awaiting Reviewer Assignment",
    "Declined to Review",
    "With Editor",
    "Decision in Process",
    "Final Decision in Process",
    "Revision Required",
    "Major Revision",
    "Editor Review In Progress",
    "Pending Editor Assignment",
    "Submitted",
    "Incomplete",
    "",
    None,
    "Unknown",
    "N/A",
]

TERMINAL_STATUSES = [
    "Accept",
    "Accept (27-Mar-2026)",
    "Accepted",
    "Accepted for publication",
    "Accept in Principle",
    "Published",
    "Published Online",
    "Reject",
    "Rejected",
    # Owner rule: any status containing "Reject" is a rejection, including the
    # two wordings that a veto list used to keep alive forever.
    "Reject and Resubmit",
    "Reject with Possibility for Resubmission (16-Jul-2026)",
    "Reject - Resubmission Allowed",
    "Withdrawn",
    "Transferred",
    "Declined to Publish",
]


class TerminalStatusTests(unittest.TestCase):
    def test_working_states_are_not_terminal(self):
        for status in NOT_TERMINAL_STATUSES:
            with self.subTest(status=status):
                self.assertFalse(Config.is_terminal_status(status))

    def test_terminal_states_are_detected(self):
        for status in TERMINAL_STATUSES:
            with self.subTest(status=status):
                self.assertTrue(Config.is_terminal_status(status))

    def test_archive_can_be_disabled(self):
        with patch.object(Config, "ARCHIVE_TERMINAL", False):
            self.assertFalse(Config.is_terminal_status("Accepted"))

    def test_normalize_status_strips_dates_and_prefix(self):
        self.assertEqual(normalize_status("Current Status: Accept (27-Mar-2026)"), "accept")
        self.assertEqual(normalize_status("  UNDER   REVIEW "), "under review")


if __name__ == "__main__":
    unittest.main()