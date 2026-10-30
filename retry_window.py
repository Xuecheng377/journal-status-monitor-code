"""Hand a failed window over to its next wake-up instead of failing the run.

The Worker wakes a window at :17 and retries at :27 and :37. A Cloudflare
challenge at :17 used to turn the whole run red, which mails the repository
owner even when the retry ten minutes later succeeds. So the run now ends
green after it has *queued* the retry itself: it sleeps until the next wake-up
minute and dispatches the workflow again.

Exit codes are the contract with the workflow step:

  0  the retry is queued (the run is green, nobody is mailed)
  2  this was the last wake-up of the window (the run should fail and alert)
  1  the retry could not be queued (fail so the Worker's own fallback still runs)
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from window_schedule import attempt_minutes_from_wrangler, is_scheduled_window, next_attempt_minute

DEFAULT_CRON_FILE = "cloudflare-scheduler/wrangler.toml"
WORKFLOW_FILE = "monitor.yml"
MAX_SLEEP_SECONDS = 20 * 60


def plan_handoff(window: str, attempt_minutes: list[int], now: datetime):
    """Return ``(sleep_seconds, target_minute)`` or None when no wake-up is left."""
    if not is_scheduled_window(window):
        return None
    if not attempt_minutes:
        return None
    target = next_attempt_minute(attempt_minutes, now)
    if target is None:
        return None
    seconds = (target - now.minute) * 60 - now.second
    if seconds < 0:
        seconds = 0
    return seconds, target


def _api(method: str, url: str, token: str, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "journal-status-monitor")
    if data:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body.strip() else {}


def _queued_retry_exists(repo: str, token: str, window: str, dispatched_after: float) -> bool:
    """Check the API really shows the run we just asked for."""
    try:
        runs = _api(
            "GET",
            f"https://api.github.com/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/runs?per_page=10",
            token,
        )
    except Exception:
        return False
    for run in runs.get("workflow_runs", []):
        created = run.get("created_at")
        if not created or window not in str(run.get("display_title", "")):
            continue
        try:
            stamp = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if stamp >= dispatched_after - 60:
            return True
    return False


def main() -> int:
    window = os.environ.get("RUN_WINDOW", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    mode = os.environ.get("RUN_MODE", "normal").strip() or "normal"
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    cron_file = Path(os.environ.get("WINDOW_CRON_FILE", DEFAULT_CRON_FILE))

    try:
        minutes = attempt_minutes_from_wrangler(cron_file.read_text(encoding="utf-8"))
    except OSError:
        minutes = []

    now = datetime.now()
    plan = plan_handoff(window, minutes, now)
    if plan is None:
        print(
            f"No wake-up left in {window or 'this window'} "
            f"(now :{now.minute:02d}, wake-ups {minutes or 'unknown'}); this run reports the failure."
        )
        return 2

    seconds, target = plan
    if seconds > MAX_SLEEP_SECONDS:
        print(f"Next wake-up :{target:02d} is {seconds}s away, further than {MAX_SLEEP_SECONDS}s; giving up.")
        return 1
    print(f"Holding this run until :{target:02d} ({seconds}s), then dispatching the retry.")
    time.sleep(seconds)

    if not repo or not token:
        print("GITHUB_REPOSITORY or token is missing; cannot dispatch the retry.")
        return 1
    dispatched_after = time.time()
    try:
        _api(
            "POST",
            f"https://api.github.com/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/dispatches",
            token,
            {"ref": os.environ.get("GITHUB_REF_NAME", "main"), "inputs": {"mode": mode, "window": window}},
        )
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:400]
        print(f"Dispatching the retry failed ({error.code}): {detail}")
        return 1
    except Exception as error:
        print(f"Dispatching the retry failed: {error}")
        return 1

    print("Retry dispatched; waiting for it to appear in the run list.")
    deadline = time.time() + 90
    while time.time() < deadline:
        if _queued_retry_exists(repo, token, window, dispatched_after):
            print("Retry is queued. This run ends green on purpose.")
            return 0
        time.sleep(5)
    print("The retry never showed up in the run list.")
    return 1


if __name__ == "__main__":
    sys.exit(main())