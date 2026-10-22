"""Parser regression tests driven by a fake WebDriver (no browser needed)."""
import unittest

from selenium.webdriver.common.by import By

from monitor import JournalMonitor


class FakeCell:
    def __init__(self, text):
        self.text = text

    def get_attribute(self, name):
        return ""


class FakeRow:
    def __init__(self, cells):
        self._cells = [FakeCell(text) for text in cells]

    def find_elements(self, by, selector):
        if by == By.TAG_NAME and selector == "td":
            return self._cells
        return []

    def find_element(self, by, selector):
        raise RuntimeError("no ancestor table in the fake row")


class FakeDriver:
    def __init__(self, rows):
        self._rows = [FakeRow(cells) for cells in rows]

    def find_elements(self, by, selector):
        if by == By.TAG_NAME and selector == "table":
            return [object()]
        if by == By.XPATH and selector == "//table//tr[td]":
            return self._rows
        return []

    def find_element(self, by, selector):
        class Body:
            text = ""

        return Body()


def parse(rows, source="IEEE"):
    monitor = JournalMonitor.__new__(JournalMonitor)
    monitor.driver = FakeDriver(rows)
    return monitor._parse_table_rows(source, "https://mc.manuscriptcentral.com/journal")


class ParsingTests(unittest.TestCase):
    def test_parses_title_id_and_status(self):
        manuscripts = parse(
            [
                ["26-TIE-0001", "A Study of Testable Things", "Under Review"],
            ]
        )
        self.assertEqual(len(manuscripts), 1)
        record = manuscripts[0]
        self.assertEqual(record["id"], "26-TIE-0001")
        self.assertEqual(record["status"], "Under Review")
        self.assertIn("A Study of Testable Things", record["title"])
        self.assertEqual(record["source"], "IEEE")

    def test_status_history_uses_the_top_line_as_current_status(self):
        manuscripts = parse(
            [
                [
                    "26-TIE-0001",
                    "A Study of Testable Things",
                    "Under Review\nAccept (02-Jan-2026)",
                ],
            ]
        )
        self.assertEqual(len(manuscripts), 1)
        self.assertEqual(manuscripts[0]["status"], "Under Review")
        self.assertNotIn("Accept", manuscripts[0]["title"])

    def test_navigation_rows_are_ignored(self):
        manuscripts = parse(
            [
                ["Log Out", "Help", "Instructions"],
                ["26-TIE-0001", "A Study of Testable Things", "Under Review"],
            ]
        )
        self.assertEqual(len(manuscripts), 1)
        self.assertEqual(manuscripts[0]["id"], "26-TIE-0001")

    def test_duplicate_rows_for_one_manuscript_are_merged(self):
        manuscripts = parse(
            [
                ["26-TIE-0001", "A Study of Testable Things", "Under Review"],
                ["26-TIE-0001", "A Study of Testable Things", "Decision in Process"],
            ]
        )
        self.assertEqual(len(manuscripts), 1)
        # The further-along status wins for a duplicated row.
        self.assertEqual(manuscripts[0]["status"], "Decision in Process")

    def test_resubmission_with_the_same_title_is_a_separate_manuscript(self):
        """26-TIE-0002 is a fresh 20-Oct submission of the rejected 26-TIE-0001.

        Keying the parse on the title collapsed the two rows into one, so the new
        manuscript never reached storage and never produced a notification.
        """
        title = "A Study of Testable Things with Payload"
        manuscripts = parse(
            [
                [
                    "26-TIE-0002 (REX-PROD-2-0000AAAA-0000-0000-0000-000000000001)",
                    title,
                    "Awaiting Admin Processing",
                    "Submitting Author: Chen, Xinkai",
                    "20-Oct-2026",
                ],
                [
                    "26-TIE-0001 (REX-PROD-2-0000BBBB-0000-0000-0000-000000000002)",
                    title,
                    "Reject with Possibility for Resubmission (03-Jan-2026)",
                    "Submitting Author: Chen, Xinkai",
                    "24-May-2026",
                ],
            ]
        )
        self.assertEqual(len(manuscripts), 2)
        ids = sorted(item["id"] for item in manuscripts)
        self.assertEqual(ids, ["26-TIE-0001", "26-TIE-0002"])
        by_id = {item["id"]: item for item in manuscripts}
        self.assertEqual(by_id["26-TIE-0002"]["status"], "Awaiting Admin Processing")

    def test_revision_row_collapses_into_the_base_manuscript(self):
        title = "Robust Adaptive Control of State-Constrained Hysteretic Nonlinear Systems"
        manuscripts = parse(
            [
                ["25-TIE-0003", title, "Major Revisions (01-Jan-2026)", "a revision has been submitted"],
                ["25-TIE-0003.R1", title, "Accept (02-Jan-2026)"],
            ]
        )
        self.assertEqual(len(manuscripts), 1)
        self.assertEqual(manuscripts[0]["status"], "Accept (02-Jan-2026)")

    def test_revision_suffix_helpers(self):
        self.assertEqual(JournalMonitor._manuscript_base_id("25-TIE-0003.R1"), "25-TIE-0003")
        self.assertEqual(JournalMonitor._manuscript_base_id("25-TIE-0003"), "25-TIE-0003")
        self.assertEqual(JournalMonitor._revision_number("25-TIE-0003.R2"), 2)
        self.assertEqual(JournalMonitor._revision_number("25-TIE-0003"), 0)

    def test_empty_table_returns_empty_list(self):
        self.assertEqual(parse([]), [])

    def test_missing_table_raises_instead_of_reporting_success(self):
        class NoTableDriver:
            def find_elements(self, by, selector):
                return []

            def find_element(self, by, selector):
                class Body:
                    text = ""

                return Body()

        monitor = JournalMonitor.__new__(JournalMonitor)
        monitor.driver = NoTableDriver()
        with self.assertRaises(RuntimeError):
            monitor._parse_table_rows("IEEE", "https://mc.manuscriptcentral.com/journal")

    def test_page_that_reports_no_submissions_is_not_an_error(self):
        class EmptyAccountDriver:
            def find_elements(self, by, selector):
                return []

            def find_element(self, by, selector):
                class Body:
                    text = "You have no manuscripts in this journal."

                return Body()

        monitor = JournalMonitor.__new__(JournalMonitor)
        monitor.driver = EmptyAccountDriver()
        self.assertEqual(monitor._parse_table_rows("IEEE", "https://mc.manuscriptcentral.com/journal"), [])

    def test_date_token_is_not_used_as_manuscript_id(self):
        self.assertEqual(JournalMonitor._pick_manuscript_id(["27-Mar-2026"]), "")
        self.assertTrue(JournalMonitor._looks_like_non_manuscript_id("2026"))


class ChallengeDetectionTests(unittest.TestCase):
    def _monitor_with_title(self, title):
        class TitleDriver:
            def __init__(self, value):
                self.title = value

        monitor = JournalMonitor.__new__(JournalMonitor)
        monitor.driver = TitleDriver(title)
        return monitor

    def test_cloudflare_interstitial_is_recognised(self):
        self.assertTrue(self._monitor_with_title("Just a moment...")._page_looks_like_challenge())
        self.assertTrue(self._monitor_with_title("Attention Required! | Cloudflare")._page_looks_like_challenge())
        self.assertTrue(self._monitor_with_title("Checking your browser before accessing")._page_looks_like_challenge())

    def test_real_portal_page_is_not_a_challenge(self):
        monitor = self._monitor_with_title("ScholarOne Manuscripts")
        self.assertFalse(monitor._page_looks_like_challenge())
        self.assertTrue(monitor._wait_out_challenge("IEEE"))

    def test_challenge_that_never_clears_reports_false(self):
        from config import Config

        monitor = self._monitor_with_title("Just a moment...")
        original = Config.CHALLENGE_WAIT_SECONDS
        Config.CHALLENGE_WAIT_SECONDS = 0
        try:
            self.assertFalse(monitor._wait_out_challenge("IEEE"))
        finally:
            Config.CHALLENGE_WAIT_SECONDS = original


if __name__ == "__main__":
    unittest.main()