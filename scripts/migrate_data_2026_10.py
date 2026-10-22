"""One-off migration: re-key records to id-first and undo the false archival.

"Reject with Possibility for Resubmission" used to match the "reject" keyword and
was archived on 2026-07-17, which silently stopped monitoring that manuscript.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage import ManuscriptStorage  # noqa: E402

DATA_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "manuscripts.json")
FALSE_ARCHIVE_ID = "26-TIE-0001"

with open(DATA_FILE, "r", encoding="utf-8") as handle:
    raw = json.load(handle)

storage = ManuscriptStorage(DATA_FILE)
migrated = {}
restored = []
for key, record in raw.items():
    record = dict(record)
    if str(record.get("id", "")).strip() == FALSE_ARCHIVE_ID and record.get("archived"):
        record.pop("archived", None)
        record.pop("archived_at", None)
        record.pop("archive_reason", None)
        record["unarchived_at"] = "2026-10-21 23:10:00"
        record["unarchive_reason"] = "false_archival_by_substring_match"
        restored.append(record.get("id"))
    new_key = ManuscriptStorage._record_key(record)
    if new_key in migrated:
        migrated[new_key] = ManuscriptStorage._prefer_record(migrated[new_key], record)
    else:
        migrated[new_key] = record

with open(DATA_FILE + ".tmp", "w", encoding="utf-8") as handle:
    json.dump(migrated, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
os.replace(DATA_FILE + ".tmp", DATA_FILE)

print("keys:")
for key in migrated:
    print(" -", key)
print("unarchived:", restored)