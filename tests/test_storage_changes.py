"""Storage behaviour: change detection, retry-safety and record identity."""
import json
import os
import tempfile
import unittest

from config import Config
from storage import ManuscriptStorage


def manuscript(title="A study of testable things", status="Under Review", manuscript_id="26-TIE-0001", source="IEEE"):
    return {
        "id": manuscript_id,
        "title": title,
        "status": status,
        "source": source,
        "url": "https://mc.manuscriptcentral.com/journal",
    }


class BaselineProblemTests(unittest.TestCase):
    """REQUIRE_BASELINE hinges on telling "no records yet" from "state lost"."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(setattr, Config, "REQUIRE_BASELINE", Config.REQUIRE_BASELINE)
        self.path = os.path.join(self._tmp.name, "data", "manuscripts.json")
        self.storage = ManuscriptStorage(self.path)

    def test_missing_file_is_reported(self):
        self.assertIn("does not exist", self.storage.baseline_problem())

    def test_corrupt_file_is_reported(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertIn("unreadable", self.storage.baseline_problem())

    def test_valid_file_has_no_problem(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"k": {}}, handle)
        self.assertEqual(self.storage.baseline_problem(), "")

    def test_empty_object_is_a_valid_baseline(self):
        # A first run legitimately stores {} once everything is pruned; that must
        # not be confused with a missing file.
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({}, handle)
        self.assertEqual(self.storage.baseline_problem(), "")


class StorageChangeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_file = os.path.join(self._tmp.name, "data", "manuscripts.json")
        self.storage = ManuscriptStorage(self.data_file)

    def _write(self, records):
        os.makedirs(os.path.dirname(self.data_file), exist_ok=True)
        with open(self.data_file, "w", encoding="utf-8") as handle:
            json.dump(records, handle)

    def _read(self):
        with open(self.data_file, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_first_seen_queues_one_notification_and_persists(self):
        result = self.storage.plan([manuscript()])
        self.assertEqual(len(result.changes), 1)
        self.assertTrue(result.meaningful)
        self.storage.apply(result)
        stored = self._read()
        self.assertEqual(len(stored), 1)
        record = next(iter(stored.values()))
        self.assertEqual(record["status"], "Under Review")

    def test_plan_does_not_write_so_a_failed_email_can_be_retried(self):
        first = self.storage.plan([manuscript()])
        # The email failed, so nothing was applied. The next run must see the
        # exact same change instead of silently losing it.
        second = self.storage.plan([manuscript()])
        self.assertEqual(len(first.changes), 1)
        self.assertEqual(len(second.changes), 1)
        self.assertFalse(os.path.exists(self.data_file))

    def test_retry_after_applying_old_status_still_reports_change(self):
        self.storage.apply(self.storage.plan([manuscript(status="Under Review")]))
        changed = self.storage.plan([manuscript(status="Decision in Process")])
        self.assertEqual(len(changed.changes), 1)
        self.storage.apply(changed)
        self.assertEqual(next(iter(self._read().values()))["status"], "Decision in Process")
        again = self.storage.plan([manuscript(status="Decision in Process")])
        self.assertEqual(again.changes, [])
        self.assertFalse(again.meaningful, "an idle refresh must not create a commit")

    def test_unknown_status_keeps_previous_status_and_is_not_a_change(self):
        self.storage.apply(self.storage.plan([manuscript(status="Under Review")]))
        result = self.storage.plan([manuscript(status="Unknown")])
        self.assertEqual(result.changes, [])
        self.assertEqual(len(result.stale), 1)
        self.storage.apply(result)
        record = next(iter(self._read().values()))
        self.assertEqual(record["status"], "Under Review")
        self.assertTrue(record["stale"])

    def test_terminal_status_archives_and_stays_archived(self):
        self.storage.apply(self.storage.plan([manuscript(status="Under Review")]))
        accepted = self.storage.plan([manuscript(status="Accept (02-Jan-2026)")])
        self.assertEqual(len(accepted.changes), 1)
        self.storage.apply(accepted)
        record = next(iter(self._read().values()))
        self.assertTrue(record["archived"])

        back = self.storage.plan([manuscript(status="Under Review")])
        self.assertEqual(back.changes, [])
        self.storage.apply(back)
        record = next(iter(self._read().values()))
        self.assertTrue(record["archived"])
        self.assertEqual(record["last_seen_status"], "Under Review")

    def test_unarchive_restores_monitoring(self):
        self.storage.apply(self.storage.plan([manuscript(status="Accept")]))
        self.assertEqual(self.storage.unarchive("26-TIE-0001"), 1)
        record = next(iter(self._read().values()))
        self.assertFalse(record.get("archived"))
        self.assertEqual(self.storage.unarchive("26-TIE-0001"), 0)

    def test_unarchived_record_is_not_archived_again(self):
        self.storage.apply(self.storage.plan([manuscript(status="Under Review")]))
        self.storage.apply(self.storage.plan([manuscript(status="Accept")]))
        self.assertTrue(next(iter(self._read().values()))["archived"])
        self.storage.unarchive("26-TIE-0001")
        result = self.storage.plan([manuscript(status="Accept")])
        self.storage.apply(result)
        record = next(iter(self._read().values()))
        self.assertFalse(record.get("archived"))
        self.assertTrue(record.get("archive_suppressed"))

    def test_resubmission_status_archives_per_owner_rule(self):
        self.storage.apply(self.storage.plan([manuscript(status="Under Review")]))
        self.storage.apply(self.storage.plan([manuscript(status="Reject with Possibility for Resubmission")]))
        record = next(iter(self._read().values()))
        self.assertTrue(record.get("archived"))

    def test_record_is_keyed_by_id_so_a_title_edit_does_not_duplicate_it(self):
        self.storage.apply(self.storage.plan([manuscript(title="Original title of the paper")]))
        renamed = self.storage.plan([manuscript(title="Original title of the paper (Revised)")])
        self.assertEqual(renamed.changes, [], "same manuscript id must not look like a new record")
        self.storage.apply(renamed)
        self.assertEqual(len(self._read()), 1)

    def test_new_number_with_an_old_title_is_a_new_manuscript(self):
        # A resubmission keeps the title but gets a fresh number (6325 vs 3091).
        # Storage used to fall back to the title index even when the row carried an
        # id, so the 20-Oct resubmission was folded into the rejected record and no
        # notification was ever sent.
        self._write(
            {
                "IEEE:id:26-tie-0001": {
                    "id": "26-TIE-0001",
                    "title": "A Study of Testable Things",
                    "status": "Reject with Possibility for Resubmission (03-Jan-2026)",
                    "source": "IEEE",
                    "url": "https://mc.manuscriptcentral.com/tie-ieee",
                    "last_checked": "2026-10-19 11:20:00",
                    "first_seen": "2026-05-24 11:20:00",
                    "archived": True,
                    "archived_at": "2026-10-19 11:20:00",
                    "archive_reason": "terminal_status",
                }
            }
        )
        fresh = manuscript(
            manuscript_id="26-TIE-0002",
            title="A Study of Testable Things",
            status="Awaiting Admin Processing",
        )
        result = self.storage.plan([fresh])
        self.assertEqual(len(result.changes), 1, "the new number must be reported as a new manuscript")
        self.assertTrue(result.changes[0].get("is_new"))
        self.storage.apply(result)
        stored = self._read()
        self.assertEqual(len(stored), 2)
        self.assertIn("IEEE:id:26-tie-0002", stored)
        self.assertTrue(stored["IEEE:id:26-tie-0001"]["archived"])

    def test_legacy_title_keyed_twin_is_folded_into_the_id_keyed_record(self):
        title = "Robust Adaptive Control of State-Constrained Hysteretic Systems"
        self._write(
            {
                "IEEE:id:25-tie-0003": {
                    "id": "25-TIE-0003",
                    "title": title,
                    "status": "Accept (02-Jan-2026)",
                    "source": "IEEE",
                    "url": "https://mc.manuscriptcentral.com/tie-ieee",
                    "last_checked": "2026-10-21 17:02:48",
                    "first_seen": "2025-12-23 11:20:00",
                    "archived": True,
                    "archived_at": "2026-10-21 17:02:48",
                },
                "IEEE:robust adaptive control of state constrained hysteretic systems": {
                    "id": "",
                    "title": title,
                    "status": "Accept (02-Jan-2026)",
                    "source": "IEEE",
                    "url": "https://mc.manuscriptcentral.com/tie-ieee",
                    "last_checked": "2026-10-21 15:16:54",
                    "first_seen": "2026-05-26 12:55:25",
                    "archived": True,
                },
            }
        )
        self.storage.apply(self.storage.plan([]))
        stored = self._read()
        self.assertEqual(len(stored), 1, "the title-keyed twin must not survive")
        self.assertIn("IEEE:id:25-tie-0003", stored)

    def test_legacy_title_keyed_record_is_migrated_without_a_notification_flood(self):
        legacy = {
            "IEEE:a study of testable things": {
                "id": "26-TIE-0001",
                "title": "A study of testable things",
                "status": "Under Review",
                "source": "IEEE",
                "url": "https://mc.manuscriptcentral.com/journal",
                "last_checked": "2026-10-20 11:20:00",
                "first_seen": "2026-10-01 11:20:00",
                "archived": False,
            }
        }
        self._write(legacy)
        result = self.storage.plan([manuscript()])
        self.assertEqual(result.changes, [])
        self.storage.apply(result)
        stored = self._read()
        self.assertEqual(len(stored), 1)
        self.assertIn(":id:", next(iter(stored)))

    def test_empty_fetch_does_not_delete_records(self):
        self.storage.apply(self.storage.plan([manuscript()]))
        result = self.storage.plan([])
        self.storage.apply(result)
        self.assertEqual(len(self._read()), 1)

    def test_idle_run_is_not_a_change_even_when_flags_were_absent(self):
        # Older records have no "archived"/"stale" key at all; writing false must
        # not look like a real change, or every run would commit.
        record = dict(manuscript())
        record["first_seen"] = "2026-01-01 00:00:00"
        record["last_checked"] = "2026-10-20 11:20:00"
        self._write({"IEEE:id:26-TIE-0001": record})
        result = self.storage.plan([manuscript()])
        self.assertFalse(result.meaningful)


class ArchiveKeywordConfigTests(unittest.TestCase):
    def test_configured_vetoes_win_over_keywords(self):
        with unittest.mock.patch.object(Config, "TERMINAL_STATUS_VETOES", ("completed",)):
            self.assertFalse(Config.is_terminal_status("Required Reviews Completed"))
            self.assertTrue(Config.is_terminal_status("Accepted"))


if __name__ == "__main__":
    unittest.main()