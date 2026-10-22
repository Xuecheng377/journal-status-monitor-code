"""Persistent manuscript storage, change detection and de-duplication."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterable, List

from config import Config, is_unknown_status

# Fields that are bookkeeping only. A run that changes nothing but these is
# "idle": it still updates the file, but it must not create a commit.
VOLATILE_FIELDS = {"last_checked"}


@dataclass
class PlanResult:
    """Outcome of comparing a fresh fetch against the stored state.

    Nothing is written until :meth:`ManuscriptStorage.apply` is called, which
    lets the caller send notifications first and retry on the next run when the
    notification could not be delivered.
    """

    changes: List[Dict] = field(default_factory=list)
    manuscripts: Dict = field(default_factory=dict)
    meaningful: bool = False
    fetched: int = 0
    stale: List[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)


class ManuscriptStorage:
    def __init__(self, data_file: str):
        self.data_file = data_file
        self._ensure_data_dir()

    # ------------------------------------------------------------------ files
    def _ensure_data_dir(self) -> None:
        data_dir = os.path.dirname(self.data_file)
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)

    def load_manuscripts(self) -> Dict:
        if not os.path.exists(self.data_file):
            return {}
        try:
            with open(self.data_file, "r", encoding="utf-8") as file:
                data = json.load(file)
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            print(f"Failed to read data file: {exc}")
            return {}

    def baseline_problem(self) -> str:
        """Why the saved snapshot cannot be trusted as a diff baseline.

        A missing or unreadable file silently degrades into "no manuscripts yet",
        which turns every fetched manuscript into a brand-new one. Callers that
        must not send that mail ask here first.
        """
        if not os.path.exists(self.data_file):
            return f"{self.data_file} does not exist"
        try:
            with open(self.data_file, "r", encoding="utf-8") as file:
                data = json.load(file)
        except Exception as exc:
            return f"{self.data_file} is unreadable ({exc})"
        if not isinstance(data, dict):
            return f"{self.data_file} is not a JSON object"
        return ""

    def save_manuscripts(self, manuscripts: Dict) -> None:
        cleaned = self._merge_duplicate_records(manuscripts)
        cleaned = self._cleanup_old_records(cleaned, Config.RETENTION_DAYS)
        tmp_file = f"{self.data_file}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as file:
            json.dump(cleaned, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
        os.replace(tmp_file, self.data_file)
        print(f"Saved manuscript data to {self.data_file}")

    def _meta_file(self) -> str:
        root, ext = os.path.splitext(self.data_file)
        return f"{root}.meta{ext or '.json'}"

    def load_meta(self) -> Dict:
        meta_file = self._meta_file()
        if not os.path.exists(meta_file):
            return {}
        try:
            with open(meta_file, "r", encoding="utf-8") as file:
                data = json.load(file)
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            print(f"Failed to read meta file: {exc}")
            return {}

    def save_meta(self, meta: Dict) -> None:
        meta_file = self._meta_file()
        tmp_file = f"{meta_file}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as file:
            json.dump(meta, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
        os.replace(tmp_file, meta_file)
        print(f"Saved monitor meta to {meta_file}")

    def daily_report_already_sent(self, today: str | None = None) -> bool:
        today = today or datetime.now().strftime("%Y-%m-%d")
        return self.load_meta().get("last_daily_report_date") == today

    def mark_daily_report_sent(self, today: str | None = None) -> None:
        today = today or datetime.now().strftime("%Y-%m-%d")
        meta = self.load_meta()
        meta["last_daily_report_date"] = today
        meta["last_daily_report_sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.save_meta(meta)

    # -------------------------------------------------------------- identities
    @staticmethod
    def _status_score(value: object) -> int:
        text = str(value or "").lower()
        if any(keyword in text for keyword in ("accept", "accepted", "published")):
            return 100
        if any(keyword in text for keyword in ("reject", "rejected", "withdrawn")):
            return 90
        if "decision" in text:
            return 70
        if "revision" in text:
            return 50
        if "review" in text:
            return 40
        if "submitted" in text:
            return 30
        return 0

    @staticmethod
    def _looks_like_date_token(value: object) -> bool:
        text = str(value or "").strip().lower()
        months = r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
        patterns = [
            rf"(?:{months})[-_ ]?\d{{2,4}}",
            rf"\d{{1,2}}[-_ ](?:{months})[-_ ]\d{{2,4}}",
            r"\d{4}[-_/]\d{1,2}[-_/]\d{1,2}",
        ]
        return any(re.fullmatch(pattern, text) for pattern in patterns)

    @staticmethod
    def _id_quality(value: object) -> int:
        text = str(value or "").strip()
        if not text:
            return 0
        if ManuscriptStorage._looks_like_date_token(text):
            return -10
        if re.search(r"[A-Za-z]", text) and re.search(r"\d", text):
            return 10
        if re.search(r"\d", text):
            return 5
        return 1

    @staticmethod
    def _strip_trailing_status_from_title(value: object) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        while True:
            match = re.search(r"\s+(\((?:[^()]|\([^()]*\))*\))\s*$", text)
            if not match:
                break
            suffix = match.group(1)
            if ManuscriptStorage._status_score(suffix) <= 0:
                break
            text = text[: match.start()].strip()
        return text

    @staticmethod
    def _identity_title(value: object) -> str:
        title = ManuscriptStorage._strip_trailing_status_from_title(value)
        title = re.sub(r"\s+", " ", title).strip().lower()
        title = re.sub(r"[^\w\s:;-]", "", title)
        return re.sub(r"\s+", " ", title).strip()

    @staticmethod
    def _display_title(value: object) -> str:
        return (
            ManuscriptStorage._strip_trailing_status_from_title(value)
            or str(value or "").strip()
            or "Untitled"
        )

    @staticmethod
    def _source(value: object) -> str:
        return str(value or "unknown").strip() or "unknown"

    @staticmethod
    def _record_key(record: Dict) -> str:
        """Stable key: manuscript id first, title only as a fallback.

        Keying on the title alone duplicated a record whenever the title gained a
        suffix (for example "(Revised)"), which then produced a fresh
        "first seen" notification and a 30 day ghost in the daily report.
        """
        source = ManuscriptStorage._source(record.get("source"))
        manuscript_id = str(record.get("id", "")).strip()
        if manuscript_id and ManuscriptStorage._id_quality(manuscript_id) >= 5:
            return f"{source}:id:{manuscript_id.lower()}"
        identity = ManuscriptStorage._identity_title(record.get("title"))
        return f"{source}:title:{identity or manuscript_id or 'unknown'}"

    def _build_index(self, data: Dict) -> tuple[Dict, Dict]:
        by_id: Dict[tuple[str, str], str] = {}
        by_title: Dict[tuple[str, str], str] = {}
        for key, record in data.items():
            if not isinstance(record, dict):
                continue
            source = self._source(record.get("source")).lower()
            manuscript_id = str(record.get("id", "")).strip()
            if manuscript_id:
                by_id.setdefault((source, manuscript_id.lower()), key)
            identity = self._identity_title(record.get("title"))
            if identity:
                by_title.setdefault((source, identity), key)
        return by_id, by_title

    # ----------------------------------------------------------------- records
    @staticmethod
    def _prefer_record(existing: Dict, candidate: Dict) -> Dict:
        existing_score = ManuscriptStorage._status_score(existing.get("status"))
        candidate_score = ManuscriptStorage._status_score(candidate.get("status"))
        chosen = candidate.copy() if candidate_score > existing_score else existing.copy()
        other = existing if chosen is candidate else candidate
        if ManuscriptStorage._id_quality(other.get("id")) > ManuscriptStorage._id_quality(chosen.get("id")):
            chosen["id"] = other.get("id", "")
        chosen["title"] = ManuscriptStorage._display_title(chosen.get("title"))
        if existing.get("first_seen") and candidate.get("first_seen"):
            chosen["first_seen"] = min(str(existing.get("first_seen")), str(candidate.get("first_seen")))
        suppressed = bool(existing.get("archive_suppressed") or candidate.get("archive_suppressed"))
        if suppressed:
            chosen["archive_suppressed"] = True
            for source_record in (existing, candidate):
                if source_record.get("suppress_until_status"):
                    chosen["suppress_until_status"] = source_record["suppress_until_status"]
        status_unchanged = str(chosen.get("suppress_until_status") or "") == str(chosen.get("status") or "")
        if not (suppressed and status_unchanged) and (
            existing.get("archived") or candidate.get("archived") or Config.is_terminal_status(chosen.get("status"))
        ):
            chosen["archived"] = True
            chosen.setdefault(
                "archived_at",
                existing.get("archived_at") or candidate.get("archived_at") or chosen.get("last_checked"),
            )
            chosen.setdefault("archive_reason", "terminal_status")
        return chosen

    def _merge_duplicate_records(self, manuscripts: Dict) -> Dict:
        merged: Dict = {}
        for _key, data in manuscripts.items():
            if not isinstance(data, dict):
                continue
            normalized = data.copy()
            normalized["title"] = self._display_title(normalized.get("title"))
            merged_key = self._record_key(normalized)
            if merged_key in merged:
                merged[merged_key] = self._prefer_record(merged[merged_key], normalized)
            else:
                merged[merged_key] = normalized

        # Legacy title-keyed records (written before the id was parsed) would
        # otherwise linger as a phantom twin of the id-keyed record with the same
        # title, and show up twice in the weekly report.
        by_title: Dict[tuple[str, str], str] = {}
        for key, record in merged.items():
            if ":id:" not in key:
                continue
            identity = self._identity_title(record.get("title"))
            if identity:
                by_title.setdefault((self._source(record.get("source")).lower(), identity), key)
        for key in list(merged):
            if ":id:" in key:
                continue
            record = merged[key]
            identity = self._identity_title(record.get("title"))
            twin = by_title.get((self._source(record.get("source")).lower(), identity)) if identity else None
            if twin and twin in merged:
                merged[twin] = self._prefer_record(merged[twin], record)
                merged.pop(key, None)
        return merged

    @staticmethod
    def _parse_time(value: str | None) -> datetime | None:
        if not value:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        return None

    def _cleanup_old_records(self, manuscripts: Dict, days: int) -> Dict:
        if days <= 0:
            return manuscripts
        threshold = datetime.now() - timedelta(days=days)
        cleaned = {}
        removed = 0
        for key, data in manuscripts.items():
            if data.get("archived"):
                cleaned[key] = data
                continue
            last_checked = self._parse_time(data.get("last_checked"))
            if last_checked is None or last_checked >= threshold:
                cleaned[key] = data
            else:
                removed += 1
        if removed:
            print(f"Removed {removed} stale active manuscript record(s).")
        return cleaned

    @staticmethod
    def _first_seen_old_status() -> str:
        return "\u9996\u6b21\u8bb0\u5f55\uff08\u65e0\u5386\u53f2\u72b6\u6001\uff09"

    @staticmethod
    def _is_silent_first_seen_status(status: object) -> bool:
        text = str(status or "").strip().lower()
        if not text or is_unknown_status(text):
            return True
        return any(keyword in text for keyword in Config.FIRST_SEEN_SILENT_STATUS_KEYWORDS)

    @staticmethod
    def _notify_on_first_seen(status: object) -> bool:
        """Every new manuscript is announced, including one already decided.

        Archiving decides whether the record is still tracked, never whether the
        owner hears about it: a manuscript first seen as "Submitted" or even
        "Accept" must produce one email, otherwise it silently disappears.
        """
        if not Config.NOTIFY_ON_FIRST_SEEN:
            return False
        return not ManuscriptStorage._is_silent_first_seen_status(status)

    @staticmethod
    def _change_item(
        manuscript: Dict,
        title: str,
        old_status: object,
        new_status: str,
        changed_at: str,
        is_new: bool = False,
    ) -> Dict:
        return {
            "id": manuscript.get("id", ""),
            "title": title,
            "source": manuscript.get("source", "Unknown"),
            "old_status": old_status,
            "new_status": new_status,
            "changed_at": changed_at,
            "url": manuscript.get("url", ""),
            "is_new": is_new,
        }

    # ------------------------------------------------------------- comparison
    @staticmethod
    def _meaningful_view(data: Dict) -> Dict:
        view = {}
        for key, record in data.items():
            if not isinstance(record, dict):
                view[key] = record
                continue
            cleaned = {k: v for k, v in record.items() if k not in VOLATILE_FIELDS}
            # Absent flag and an explicit false are the same state; without this,
            # a one-off normalization would look like a real change forever.
            for flag in ("archived", "archive_suppressed", "stale"):
                if not cleaned.get(flag):
                    cleaned.pop(flag, None)
            view[key] = cleaned
        return view

    def plan(self, new_manuscripts: List[Dict]) -> PlanResult:
        """Compare a fresh fetch with stored state without touching the disk."""
        old_data = self.load_manuscripts()
        by_id, by_title = self._build_index(old_data)

        updated: Dict = {key: value for key, value in old_data.items()}
        changes: List[Dict] = []
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        stale_ids: List[str] = []
        fetched = 0
        seen: set[str] = set()

        for manuscript in new_manuscripts:
            source = self._source(manuscript.get("source"))
            manuscript_id = str(manuscript.get("id", "")).strip()
            identity = self._identity_title(manuscript.get("title"))
            dedupe_key = f"{source.lower()}:{manuscript_id.lower() or identity}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            old_key = by_id.get((source.lower(), manuscript_id.lower())) if manuscript_id else None
            if old_key is None and not manuscript_id and identity:
                # Title matching is only a fallback for rows the portal renders
                # without a manuscript number. When the number is present it is
                # authoritative: 26-TIE-0002 is a NEW submission even though its
                # title is identical to the already-rejected 26-TIE-0001.
                old_key = by_title.get((source.lower(), identity))
            old_record = updated.get(old_key) if old_key else None

            raw_status = str(manuscript.get("status", "")).strip()
            title = self._display_title(manuscript.get("title", "Untitled"))

            if is_unknown_status(raw_status):
                # A parse miss is not a status change. Keep the last known status
                # and mark it stale instead of reporting "Under Review -> Unknown".
                if old_record:
                    record = {
                        **old_record,
                        "last_checked": current_time,
                        "stale": True,
                    }
                    new_key = self._record_key(record)
                    if old_key and old_key != new_key:
                        updated.pop(old_key, None)
                    updated[new_key] = record
                    stale_ids.append(str(old_record.get("id", "")) or title)
                    continue
                record = {
                    "id": manuscript_id or title[:80],
                    "title": title,
                    "status": "Unknown",
                    "source": source,
                    "url": manuscript.get("url", ""),
                    "last_checked": current_time,
                    "first_seen": current_time,
                    "archived": False,
                    "stale": True,
                }
                updated[self._record_key(record)] = record
                stale_ids.append(record["id"])
                continue

            fetched += 1
            current_status = raw_status
            # A manual unarchive means "keep watching this one". The suppression
            # only holds while the status stays what it was at unarchive time, so
            # a later change is evaluated by the normal rule again.
            suppressed_until = old_record.get("suppress_until_status") if old_record else None
            archive_suppressed = bool(
                old_record
                and old_record.get("archive_suppressed")
                and str(suppressed_until or "") == current_status
            )
            now_terminal = Config.is_terminal_status(current_status) and not archive_suppressed

            if old_record and old_record.get("archived"):
                # Archived manuscripts stay archived; only the last seen status moves.
                record = {
                    **old_record,
                    "last_seen_status": current_status,
                    "last_checked": current_time,
                }
                new_key = self._record_key(record)
                if old_key and old_key != new_key:
                    updated.pop(old_key, None)
                updated[new_key] = record
                continue

            if old_record:
                old_status = old_record.get("status")
                if str(old_status or "") != current_status:
                    changes.append(self._change_item(manuscript, title, old_status, current_status, current_time))
                    print(f"Status changed: {self.describe(manuscript_id, title)}: "
                          f"{self.mask(old_status)} -> {self.mask(current_status)}")
            else:
                print(f"New manuscript recorded: {self.describe(manuscript_id, title)} ({self.mask(current_status)})")
                if self._notify_on_first_seen(current_status):
                    changes.append(
                        self._change_item(
                            manuscript,
                            title,
                            self._first_seen_old_status(),
                            current_status,
                            current_time,
                            is_new=True,
                        )
                    )
                    print(f"First-seen status notification queued: {self.describe(manuscript_id, title)}")

            record = {
                "id": manuscript.get("id", ""),
                "title": title,
                "status": current_status,
                "source": source,
                "url": manuscript.get("url", ""),
                "last_checked": current_time,
                "first_seen": old_record.get("first_seen", current_time) if old_record else current_time,
                "archived": now_terminal,
            }
            if old_record and old_record.get("stale"):
                record.pop("stale", None)
            if archive_suppressed:
                record["archive_suppressed"] = True
                record["suppress_until_status"] = current_status
                if old_record and old_record.get("unarchived_at"):
                    record["unarchived_at"] = old_record["unarchived_at"]
            if now_terminal:
                record["archived_at"] = old_record.get("archived_at", current_time) if old_record else current_time
                record["archive_reason"] = "terminal_status"
                print(
                    f"Archived terminal manuscript: {self.describe(manuscript_id, title)} "
                    f"({self.mask(current_status)}) archived_at={record['archived_at']} "
                    f"first_seen={record['first_seen']}"
                )
            new_key = self._record_key(record)
            if old_key and old_key != new_key:
                updated.pop(old_key, None)
            updated[new_key] = record

        if stale_ids:
            print(
                f"{len(stale_ids)} manuscript(s) returned no parsable status; "
                "kept their previous status and marked them stale."
            )

        merged = self._merge_duplicate_records(updated)
        cleaned = self._cleanup_old_records(merged, Config.RETENTION_DAYS)
        meaningful = self._meaningful_view(cleaned) != self._meaningful_view(
            self._merge_duplicate_records(old_data)
        )
        print(f"Processed {len(updated)} manuscript key(s), {fetched} with a parsable status.")
        return PlanResult(
            changes=changes,
            manuscripts=cleaned,
            meaningful=meaningful,
            fetched=fetched,
            stale=stale_ids,
        )

    def apply(self, result: PlanResult) -> None:
        self.save_manuscripts(result.manuscripts)

    def compare_and_update(self, new_manuscripts: List[Dict]) -> List[Dict]:
        """Backwards compatible helper: plan and persist in one step."""
        result = self.plan(new_manuscripts)
        self.apply(result)
        return result.changes

    # ------------------------------------------------------------- inspection
    @staticmethod
    def mask(value: object, keep: int = 18) -> str:
        """Shorten a status for logs. Statuses are low risk but still shorten."""
        text = str(value or "").replace("\n", " ").strip()
        if len(text) <= keep * 2:
            return text
        return f"{text[:keep]}…{text[-keep:]}"

    @staticmethod
    def describe(manuscript_id: object, title: object) -> str:
        """Log identifier that does not print the full unpublished title."""
        manuscript_id = str(manuscript_id or "").strip()
        if Config.LOG_FULL_TITLES:
            return f"{manuscript_id} {title}".strip()
        title_text = str(title or "").strip()
        digest = 0
        for char in title_text:
            digest = (digest * 31 + ord(char)) % 100000
        label = f"title#{digest:05d}"
        return f"{manuscript_id or label} ({label}, {len(title_text)} chars)" if manuscript_id else label

    def get_all_manuscripts(self, include_archived: bool = False) -> List[Dict]:
        records = list(self.load_manuscripts().values())
        if include_archived:
            return records
        return [record for record in records if not record.get("archived")]

    def unarchive(self, needle: str) -> int:
        """Clear the archived flag for records matching id or title substring."""
        needle = str(needle or "").strip().lower()
        if not needle:
            raise ValueError("unarchive needs a manuscript id or title fragment")
        data = self.load_manuscripts()
        restored = 0
        for key, record in list(data.items()):
            haystack = f"{record.get('id', '')} {record.get('title', '')}".lower()
            if needle not in haystack:
                continue
            if not record.get("archived"):
                continue
            record.pop("archived", None)
            record.pop("archived_at", None)
            record.pop("archive_reason", None)
            record["archive_suppressed"] = True
            # Only hold off auto-archiving while the status is unchanged.
            record["suppress_until_status"] = record.get("status", "")
            record["unarchived_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            record["last_checked"] = record["unarchived_at"]
            data[key] = record
            restored += 1
            print(f"Unarchived {self.describe(record.get('id'), record.get('title'))}")
        if restored:
            self.save_manuscripts(data)
        return restored

    def clear_data(self) -> None:
        if os.path.exists(self.data_file):
            os.remove(self.data_file)
            print(f"Removed data file: {self.data_file}")


def iter_records(data: Dict) -> Iterable[Dict]:
    for record in data.values():
        if isinstance(record, dict):
            yield record