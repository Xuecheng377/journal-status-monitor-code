"""Everything that decides when a window gets another chance.

The Worker wakes one window up to three times (:17 first, :27 and :37 fallback).
Three places need that same knowledge: the monitor (may it mail now?), the
hand-off step (may it sleep until the next wake-up?) and the tests. Keeping it
in one dependency-free module means the cron file stays the only source of
truth and no copy drifts.
"""
from __future__ import annotations

import re
from datetime import datetime

WINDOW_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}$")


def attempt_minutes_from_wrangler(text: str) -> list[int]:
    """Minute-of-hour values the Worker can use to wake a window.

    Anything that is not a plain comma list is ignored, and an empty result
    makes the caller act immediately instead of staying silent.
    """
    section = text.split("[triggers]", 1)[-1] if "[triggers]" in text else text
    minutes = set()
    for expression in re.findall(r'"([^"]+)"', section):
        fields = expression.split()
        if len(fields) != 5:
            continue
        minute_field, _hour, dom, month, dow = fields
        if (dom, month, dow) != ("*", "*", "*"):
            continue
        parts = [part.strip() for part in minute_field.split(",") if part.strip().isdigit()]
        if not parts or len(parts) != len(minute_field.split(",")):
            continue
        minutes.update(int(part) for part in parts if 0 <= int(part) <= 59)
    return sorted(minutes)


def is_scheduled_window(window: str) -> bool:
    """True for a Worker dispatch (``2026-10-24T08``), false for a manual run."""
    return bool(WINDOW_PATTERN.fullmatch((window or "").strip()))


def next_attempt_minute(attempt_minutes: list[int], now: datetime) -> int | None:
    """Minute of the next wake-up still ahead in this hour, or None if none is."""
    ahead = [minute for minute in attempt_minutes if minute > now.minute]
    return min(ahead) if ahead else None


def is_last_attempt(attempt_minutes: list[int], now: datetime) -> bool:
    return next_attempt_minute(attempt_minutes, now) is None


def should_defer_failure_alert(
    window: str,
    attempt_minutes: list[int],
    now: datetime,
    enabled: bool = True,
) -> tuple[bool, str]:
    """Hold a failure alert back while the window still has a fallback wake-up."""
    if not enabled:
        return False, ""
    if not is_scheduled_window(window):
        return False, ""  # manual or test dispatch: no retry is coming
    if not attempt_minutes:
        return False, ""  # unknown schedule: alert rather than stay silent
    next_minute = next_attempt_minute(attempt_minutes, now)
    if next_minute is None:
        return False, ""
    ahead = "、".join(
        f":{minute:02d}" for minute in sorted(set(attempt_minutes)) if minute > now.minute
    )
    return True, (
        f"窗口 {window} 还有兜底唤醒（{ahead}），现在才 :{now.minute:02d}；"
        "最后一次唤醒仍然失败时才发送告警邮件。"
    )