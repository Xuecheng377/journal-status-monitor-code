"""Notification behaviour that used to lose information silently."""
import unittest
from unittest.mock import patch

from config import Config
from notification import CN_ALERT_SUBJECT, CN_CHANGE_SUBJECT, CN_NEW_SUBJECT, CN_NOT_CHECKED_NOTE, EmailNotifier


def make_notifier():
    with patch.multiple(
        Config,
        EMAIL_SENDER="sender@example.com",
        EMAIL_PASSWORD="smtp-secret",
        EMAIL_RECEIVERS=["receiver@example.com"],
        SMTP_SERVER="smtp.example.com",
        SMTP_PORT=465,
    ):
        return EmailNotifier()


class NotificationTests(unittest.TestCase):
    def test_empty_daily_report_is_not_reported_as_sent(self):
        notifier = make_notifier()
        with patch.object(notifier, "_send") as send:
            self.assertFalse(notifier.send_daily_report([]))
        send.assert_not_called()

    def test_empty_change_list_is_a_successful_no_op(self):
        notifier = make_notifier()
        with patch.object(notifier, "_send") as send:
            self.assertTrue(notifier.send_change_notification([]))
        send.assert_not_called()

    def test_failure_alert_is_actually_sent(self):
        notifier = make_notifier()
        with patch.object(notifier, "_send", return_value=True) as send:
            self.assertTrue(notifier.send_failure_alert(["IEEE fetch failed: login rejected"]))
        send.assert_called_once()
        subject = send.call_args[0][0]
        self.assertIn(CN_ALERT_SUBJECT, subject)
        self.assertNotIn(CN_CHANGE_SUBJECT, subject)
        # The reader must not conclude "no email means nothing changed".
        self.assertIn(CN_NOT_CHECKED_NOTE, send.call_args[0][1])

    def test_new_manuscript_uses_its_own_subject(self):
        notifier = make_notifier()
        with patch.object(notifier, "_send", return_value=True) as send:
            notifier.send_change_notification(
                [
                    {
                        "id": "26-TIE-0004",
                        "title": "A brand new study",
                        "source": "IEEE",
                        "old_status": "首次记录（无历史状态）",
                        "new_status": "Submitted",
                        "changed_at": "2026-10-21 11:20:00",
                        "url": "https://mc.manuscriptcentral.com/journal",
                        "is_new": True,
                    }
                ]
            )
        self.assertIn(CN_NEW_SUBJECT, send.call_args[0][0])

    def test_failure_alert_without_problems_does_nothing(self):
        notifier = make_notifier()
        with patch.object(notifier, "_send") as send:
            self.assertTrue(notifier.send_failure_alert([]))
        send.assert_not_called()

    def test_change_email_contains_title_and_status_transition(self):
        notifier = make_notifier()
        with patch.object(notifier, "_send", return_value=True) as send:
            notifier.send_change_notification(
                [
                    {
                        "id": "26-TIE-0001",
                        "title": "A study of testable things",
                        "source": "IEEE",
                        "old_status": "Under Review",
                        "new_status": "Decision in Process",
                        "changed_at": "2026-10-21 22:00:00",
                        "url": "https://mc.manuscriptcentral.com/journal",
                    }
                ]
            )
        text_body = send.call_args[0][1]
        self.assertIn("A study of testable things", text_body)
        self.assertIn("Under Review -> Decision in Process", text_body)


if __name__ == "__main__":
    unittest.main()