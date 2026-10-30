"""The failed attempt hands its window over instead of colouring the run red."""
import os
import unittest
from datetime import datetime

from retry_window import plan_handoff
from window_schedule import (
    attempt_minutes_from_wrangler,
    is_last_attempt,
    is_scheduled_window,
    next_attempt_minute,
    should_defer_failure_alert,
)

WRANGLER = """[triggers]
crons = [
  "17,27,37 0,4,8,12,16,20 * * *"
]
"""


class WindowScheduleTests(unittest.TestCase):
    def test_minutes_come_from_the_worker_cron(self):
        self.assertEqual(attempt_minutes_from_wrangler(WRANGLER), [17, 27, 37])

    def test_unparsable_cron_is_empty_so_callers_act_immediately(self):
        self.assertEqual(attempt_minutes_from_wrangler("crons = [\"* * * * *\"]"), [])
        self.assertEqual(attempt_minutes_from_wrangler("nothing here"), [])

    def test_scheduled_window_is_recognised(self):
        self.assertTrue(is_scheduled_window("2026-10-30T08"))
        self.assertFalse(is_scheduled_window("manual"))
        self.assertFalse(is_scheduled_window("manual-verify-3"))
        self.assertFalse(is_scheduled_window(""))

    def test_next_wake_up(self):
        self.assertEqual(next_attempt_minute([17, 27, 37], datetime(2026, 10, 30, 8, 20)), 27)
        self.assertEqual(next_attempt_minute([17, 27, 37], datetime(2026, 10, 30, 8, 17)), 27)
        self.assertIsNone(next_attempt_minute([17, 27, 37], datetime(2026, 10, 30, 8, 37)))
        self.assertIsNone(next_attempt_minute([17, 27, 37], datetime(2026, 10, 30, 8, 41)))

    def test_last_attempt(self):
        self.assertFalse(is_last_attempt([17, 27, 37], datetime(2026, 10, 30, 8, 27)))
        self.assertTrue(is_last_attempt([17, 27, 37], datetime(2026, 10, 30, 8, 37)))


class HandoffPlanTests(unittest.TestCase):
    def test_waits_for_the_next_minute(self):
        plan = plan_handoff("2026-10-30T08", [17, 27, 37], datetime(2026, 10, 30, 8, 20, 45))
        self.assertEqual(plan, (7 * 60 - 45, 27))

    def test_no_plan_on_the_last_wake_up(self):
        self.assertIsNone(plan_handoff("2026-10-30T08", [17, 27, 37], datetime(2026, 10, 30, 8, 37)))
        self.assertIsNone(plan_handoff("2026-10-30T08", [17, 27, 37], datetime(2026, 10, 30, 8, 45)))

    def test_manual_run_is_never_handed_off(self):
        self.assertIsNone(plan_handoff("manual", [17, 27, 37], datetime(2026, 10, 30, 8, 17)))

    def test_unknown_schedule_is_never_handed_off(self):
        self.assertIsNone(plan_handoff("2026-10-30T08", [], datetime(2026, 10, 30, 8, 17)))


class AlertDeferralTests(unittest.TestCase):
    def test_alert_is_held_back_while_a_wake_up_is_ahead(self):
        defer, reason = should_defer_failure_alert(
            "2026-10-30T08", [17, 27, 37], datetime(2026, 10, 30, 8, 17)
        )
        self.assertTrue(defer)
        self.assertIn(":27", reason)
        self.assertIn(":37", reason)

    def test_alert_fires_on_the_last_wake_up(self):
        defer, _ = should_defer_failure_alert(
            "2026-10-30T08", [17, 27, 37], datetime(2026, 10, 30, 8, 37)
        )
        self.assertFalse(defer)

    def test_alert_fires_when_the_policy_is_off(self):
        defer, _ = should_defer_failure_alert(
            "2026-10-30T08", [17, 27, 37], datetime(2026, 10, 30, 8, 17), enabled=False
        )
        self.assertFalse(defer)


if __name__ == "__main__":
    unittest.main()