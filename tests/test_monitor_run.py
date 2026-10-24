"""End-to-end run order without a browser.

The important contract: a status change is only persisted after the email that
announces it was sent, so a broken SMTP configuration retries instead of losing
the change forever.
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from config import Config, PlatformAccount
from storage import ManuscriptStorage

try:  # selenium is only needed to import monitor, not to exercise run()
    from monitor import JournalMonitor

    SELENIUM_AVAILABLE = True
except Exception:  # pragma: no cover - depends on the environment
    SELENIUM_AVAILABLE = False


class FakeNotifier:
    def __init__(self, send_result=True):
        self.send_result = send_result
        self.changes = []
        self.alerts = []
        self.reports = []

    def send_change_notification(self, changed):
        self.changes.append(changed)
        return self.send_result

    def send_daily_report(self, manuscripts):
        self.reports.append(manuscripts)
        return self.send_result

    def send_failure_alert(self, problems):
        self.alerts.append(problems)
        return True


MANUSCRIPT = {
    "id": "26-TIE-0001",
    "title": "A study of testable things",
    "status": "Under Review",
    "source": "IEEE",
    "url": "https://mc.manuscriptcentral.com/journal",
}


@unittest.skipUnless(SELENIUM_AVAILABLE, "selenium is not installed")
class MonitorRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_file = os.path.join(self._tmp.name, "data", "manuscripts.json")
        validate = patch.object(Config, "validate", classmethod(lambda cls, mode: None))
        validate.start()
        self.addCleanup(validate.stop)

    def make_monitor(self, notifier, manuscripts=(MANUSCRIPT,), problems=()):
        monitor = JournalMonitor.__new__(JournalMonitor)
        monitor.storage = ManuscriptStorage(self.data_file)
        monitor.notifier = notifier
        monitor.driver = None
        monitor.collect_manuscripts = lambda: (list(manuscripts), list(problems))
        return monitor

    def stored(self):
        with open(self.data_file, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_failed_notification_leaves_state_unchanged_and_fails_the_run(self):
        notifier = FakeNotifier(send_result=False)
        monitor = self.make_monitor(notifier)
        self.assertEqual(monitor.run("normal"), 1)
        self.assertFalse(os.path.exists(self.data_file))
        # Same change is offered again on the next run.
        self.assertEqual(monitor.run("normal"), 1)
        self.assertEqual(len(notifier.changes), 2)

    def test_successful_notification_persists_the_new_status(self):
        notifier = FakeNotifier(send_result=True)
        monitor = self.make_monitor(notifier)
        self.assertEqual(monitor.run("normal"), 0)
        self.assertEqual(len(notifier.changes), 1)
        record = next(iter(self.stored().values()))
        self.assertEqual(record["status"], "Under Review")

    def test_empty_fetch_fails_the_run_and_does_not_pretend_success(self):
        notifier = FakeNotifier()
        monitor = self.make_monitor(notifier, manuscripts=())
        with patch.object(Config, "FAIL_ON_EMPTY", True):
            self.assertEqual(monitor.run("normal"), 1)

    def test_account_failure_alerts_and_fails_but_still_notifies_changes(self):
        notifier = FakeNotifier()
        monitor = self.make_monitor(notifier, problems=["IEEE fetch failed: login rejected"])
        self.assertEqual(monitor.run("normal"), 1)
        self.assertEqual(len(notifier.alerts), 1)
        self.assertIn("login rejected", notifier.alerts[0][0])
        self.assertEqual(len(notifier.changes), 1)

    def test_failure_alert_emits_data_changed_so_the_cooldown_gets_committed(self):
        # The commit step is gated on this output. Emitting "false" from the
        # later _apply() skipped the commit, so the alert cooldown reset and the
        # next window mailed the same failure again.
        notifier = FakeNotifier()
        monitor = self.make_monitor(notifier, problems=["IEEE fetch failed: Just a moment..."])
        output_path = os.path.join(self._tmp.name, "github_output")
        with open(output_path, "w", encoding="utf-8"):
            pass
        with patch.dict(os.environ, {"GITHUB_OUTPUT": output_path}):
            self.assertEqual(monitor.run("normal"), 1)
        with open(output_path, "r", encoding="utf-8") as handle:
            emitted = [line.strip() for line in handle if line.strip()]
        self.assertIn("data_changed=true", emitted)
        self.assertEqual(emitted[-1], "data_changed=true", "the last emitted value wins")

    def test_run_without_changes_and_without_problems_does_not_ask_for_a_commit(self):
        notifier = FakeNotifier()
        monitor = self.make_monitor(notifier)
        output_path = os.path.join(self._tmp.name, "github_output_quiet")
        with open(output_path, "w", encoding="utf-8"):
            pass
        with patch.dict(os.environ, {"GITHUB_OUTPUT": output_path}):
            self.assertEqual(monitor.run("normal"), 0)  # first run announces the new paper
            with open(output_path, "w", encoding="utf-8"):
                pass
            self.assertEqual(monitor.run("normal"), 0)  # nothing changed this time
        with open(output_path, "r", encoding="utf-8") as handle:
            emitted = [line.strip() for line in handle if line.strip()]
        self.assertEqual(emitted[-1], "data_changed=false")

    def test_repeated_failures_are_alerted_once_per_cooldown(self):
        notifier = FakeNotifier()
        monitor = self.make_monitor(notifier, problems=["IEEE fetch failed: Just a moment..."])
        monitor.run("normal")
        monitor.run("normal")
        monitor.run("normal")
        self.assertEqual(len(notifier.alerts), 1, "a repeating fault must not spam the inbox")

    def test_daily_report_with_no_active_manuscripts_is_not_a_failure(self):
        notifier = FakeNotifier()
        monitor = self.make_monitor(
            notifier, manuscripts=({**MANUSCRIPT, "status": "Accept"},)
        )
        self.assertEqual(monitor.run("normal"), 0)  # archived on first sight
        self.assertEqual(monitor.run("daily_report"), 0)
        self.assertEqual(notifier.reports, [])
        meta_file = os.path.join(os.path.dirname(self.data_file), "manuscripts.meta.json")
        meta = {}
        if os.path.exists(meta_file):
            with open(meta_file, "r", encoding="utf-8") as handle:
                meta = json.load(handle)
        # Nothing active is a normal state: the report is skipped and recorded as
        # sent so the following windows do not retry it all day.
        self.assertIn("last_daily_report_date", meta)

    def test_first_seen_manuscript_is_announced_even_when_submitted(self):
        notifier = FakeNotifier()
        monitor = self.make_monitor(
            notifier, manuscripts=({**MANUSCRIPT, "status": "Submitted"},)
        )
        self.assertEqual(monitor.run("normal"), 0)
        self.assertEqual(len(notifier.changes), 1)
        self.assertTrue(notifier.changes[0][0].get("is_new"))

    def test_first_seen_terminal_manuscript_is_announced_and_archived(self):
        notifier = FakeNotifier()
        monitor = self.make_monitor(
            notifier, manuscripts=({**MANUSCRIPT, "status": "Reject with Possibility for Resubmission"},)
        )
        self.assertEqual(monitor.run("normal"), 0)
        self.assertEqual(len(notifier.changes), 1)
        data_file = self.data_file
        with open(data_file, "r", encoding="utf-8") as handle:
            record = next(iter(json.load(handle).values()))
        self.assertTrue(record["archived"])

    def test_account_that_parses_zero_rows_is_reported_as_a_problem(self):
        notifier = FakeNotifier()
        monitor = self.make_monitor(notifier, manuscripts=(), problems=())
        monitor._init_driver = lambda: None
        monitor._close_driver = lambda: None
        monitor._reset_session = lambda: None
        monitor.fetch_ieee_manuscripts = lambda account: []
        accounts = [
            PlatformAccount("IEEE", "a@example.com", "pw", "https://mc.manuscriptcentral.com/tie-ieee")
        ]
        with patch.object(Config, "configured_platforms", classmethod(lambda cls: accounts)):
            manuscripts, problems = JournalMonitor.collect_manuscripts(monitor)
        self.assertEqual(manuscripts, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("parsed 0 manuscripts", problems[0])

    def test_allow_empty_accounts_permits_an_empty_account(self):
        notifier = FakeNotifier()
        monitor = self.make_monitor(notifier, manuscripts=(), problems=())
        monitor._init_driver = lambda: None
        monitor._close_driver = lambda: None
        monitor._reset_session = lambda: None
        monitor.fetch_ieee_manuscripts = lambda account: []
        accounts = [
            PlatformAccount("IEEE", "a@example.com", "pw", "https://mc.manuscriptcentral.com/tie-ieee")
        ]
        with patch.object(Config, "configured_platforms", classmethod(lambda cls: accounts)):
            with patch.object(Config, "ALLOW_EMPTY_ACCOUNTS", ("IEEE",)):
                manuscripts, problems = JournalMonitor.collect_manuscripts(monitor)
        self.assertEqual(manuscripts, [])
        self.assertEqual(problems, [])

    def test_daily_report_uses_active_manuscripts_only(self):
        notifier = FakeNotifier()
        monitor = self.make_monitor(notifier)
        monitor.run("normal")
        self.assertEqual(monitor.run("daily_report"), 0)
        self.assertEqual(len(notifier.reports), 1)
        self.assertEqual(len(notifier.reports[0]), 1)


if __name__ == "__main__":
    unittest.main()