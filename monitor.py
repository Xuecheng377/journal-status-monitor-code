"""Journal manuscript status monitor."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List
from urllib.parse import urlsplit

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from config import Config, PlatformAccount
from notification import EmailNotifier
from storage import ManuscriptStorage
# Re-exported so callers and tests keep using one import path.
from window_schedule import attempt_minutes_from_wrangler, should_defer_failure_alert

# Markers used to tell a publisher's bot-protection interstitial from a real
# page. Both the title and the rendered body matter: a challenge that has not
# run its script yet has neither, which is why waiting is part of the check.
CHALLENGE_TITLE_MARKERS = (
    "just a moment",
    "attention required",
    "checking your browser",
    "please wait",
    "verifying you are human",
)
CHALLENGE_SELECTORS = (
    "#challenge-form",
    "#challenge-running",
    "#cf-challenge-running",
    "iframe[src*='challenges.cloudflare.com']",
    "input[name='cf-turnstile-response']",
)
CHALLENGE_BODY_MARKERS = (
    "just a moment",
    "checking your browser",
    "verifying you are human",
    "enable javascript and cookies",
)


class JournalMonitor:
    def __init__(self) -> None:
        self.storage = ManuscriptStorage(Config.DATA_FILE)
        self.notifier = EmailNotifier()
        self.driver: webdriver.Chrome | None = None
        self._current_account = ""

    def _init_driver(self) -> None:
        options = Options()
        if Config.HEADLESS:
            options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
        if Config.STEALTH_MODE:
            # Some portals refuse automation-flagged browsers outright. This does
            # not hide the real user agent: a spoofed UA is what gets accounts
            # flagged, so the browser reports its true identity.
            options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--window-size=1440,1200")
        self.driver = webdriver.Chrome(options=options)
        self.driver.set_page_load_timeout(Config.PAGE_LOAD_TIMEOUT)
        self.driver.implicitly_wait(2)
        self._apply_stealth_flags()
        self._normalize_user_agent()

    def _normalize_user_agent(self) -> None:
        """Remove the "HeadlessChrome" token, keep everything else untouched.

        ScholarOne (behind Cloudflare) serves a challenge page instead of the
        login form when the UA announces itself as headless. Replacing only that
        token keeps the real version, platform and client hints consistent, which
        a fabricated Windows UA on a Linux runner would not.
        """
        if not self.driver:
            return
        try:
            user_agent = self.driver.execute_script("return navigator.userAgent") or ""
            if "HeadlessChrome" not in user_agent:
                print(f"User agent left as is: {user_agent[:60]}…")
                return
            self.driver.execute_cdp_cmd(
                "Network.setUserAgentOverride",
                {"userAgent": user_agent.replace("HeadlessChrome", "Chrome")},
            )
            print("User agent: removed the HeadlessChrome marker.")
        except Exception as exc:
            print(f"Could not normalize the user agent: {exc}")

    def _apply_stealth_flags(self) -> None:
        """Small, honest browser-profile fixes that reduce bot-challenge rate."""
        if not self.driver or not Config.STEALTH_MODE:
            return
        try:
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"},
            )
        except Exception as exc:
            print(f"Could not patch navigator.webdriver: {exc}")

    def _page_looks_like_challenge(self) -> bool:
        """True when the page is a bot-protection interstitial, not the portal.

        The title alone is not enough: a managed challenge paints an empty
        document with a form and no inputs first, and only sets "Just a moment..."
        once its script runs. That blank window is what the old check walked
        straight into and reported as "login failed".
        """
        if not self.driver:
            return False
        try:
            title = (self.driver.title or "").strip().lower()
        except Exception:
            return False
        if any(marker in title for marker in CHALLENGE_TITLE_MARKERS):
            return True
        try:
            for selector in CHALLENGE_SELECTORS:
                if self.driver.find_elements(By.CSS_SELECTOR, selector):
                    return True
            body = (self.driver.find_element(By.TAG_NAME, "body").text or "").strip().lower()
        except Exception:
            return False
        return any(marker in body for marker in CHALLENGE_BODY_MARKERS)

    def _login_form_ready(self) -> bool:
        """A password field is the one field every ScholarOne login page has."""
        if not self.driver:
            return False
        try:
            return bool(self.driver.find_elements(By.CSS_SELECTOR, "input[type='password']"))
        except Exception:
            return False

    def _open_login_page(self, account: PlatformAccount, slug: str) -> None:
        """Open the portal and wait until it is really a login form.

        Waiting here instead of failing on the first look is what turns the
        "blank page" failures into a normal run: the page is JavaScript
        rendered, so title='' password_inputs=0 forms=1 is a page in flight,
        not a broken login.
        """
        if not self.driver:
            raise RuntimeError("Browser driver is not initialized.")
        self.driver.get(account.url)
        time.sleep(2)
        deadline = time.time() + Config.LOGIN_WAIT_SECONDS
        while True:
            if self._login_form_ready():
                return
            if self._page_looks_like_challenge():
                if self._wait_out_challenge(account.name):
                    continue
                self._save_debug_artifacts(f"{slug}_challenge")
                raise RuntimeError(
                    "blocked by bot protection before the login form (challenge never cleared)"
                )
            if time.time() >= deadline:
                break
            time.sleep(2)
        self._log_page_diagnosis(f"{account.name} page")
        self._save_debug_artifacts(f"{slug}_login_failed")
        raise RuntimeError(
            f"login form never rendered within {Config.LOGIN_WAIT_SECONDS}s "
            "(blank page or unknown interstitial, not a credential problem)"
        )

    def _wait_out_challenge(self, label: str) -> bool:
        """Wait for an interstitial to clear. Returns True when a real page is up."""
        if not self._page_looks_like_challenge():
            return True
        print(f"{label}: bot-protection challenge detected, waiting up to {Config.CHALLENGE_WAIT_SECONDS}s.")
        deadline = time.time() + Config.CHALLENGE_WAIT_SECONDS
        while time.time() < deadline:
            time.sleep(3)
            if not self._page_looks_like_challenge():
                print(f"{label}: challenge cleared.")
                return True
        print(f"{label}: challenge did not clear in time.")
        return False

    def _log_page_diagnosis(self, label: str) -> None:
        """Print what the runner actually saw, without leaking page content."""
        if not self.driver:
            return
        try:
            url = self.driver.current_url
            host = urlsplit(url).hostname or "?"
            title = (self.driver.title or "")[:80]
            password_fields = len(self.driver.find_elements(By.CSS_SELECTOR, "input[type='password']"))
            text_fields = len(
                self.driver.find_elements(
                    By.CSS_SELECTOR, "input[type='text'], input[type='email'], input[name='login']"
                )
            )
            forms = len(self.driver.find_elements(By.TAG_NAME, "form"))
            print(
                f"{label}: host={host} title={title!r} password_inputs={password_fields} "
                f"text_inputs={text_fields} forms={forms}"
            )
        except Exception as exc:
            print(f"{label}: could not inspect the page: {exc}")

    def _assert_account_url(self, account: PlatformAccount) -> None:
        problem = Config.url_problem(account.url)
        if problem:
            raise RuntimeError(
                f"{account.name} URL rejected ({problem}). Refusing to type a password there."
            )

    def _reset_session(self) -> None:
        """Never let one account's cookies leak into the next account's session."""
        if not self.driver:
            return
        try:
            self.driver.delete_all_cookies()
        except Exception as exc:
            print(f"Could not clear cookies: {exc}")

    def _close_driver(self) -> None:
        if self.driver:
            self.driver.quit()
            self.driver = None

    @property
    def wait(self) -> WebDriverWait:
        if not self.driver:
            raise RuntimeError("Browser driver is not initialized.")
        return WebDriverWait(self.driver, Config.WAIT_SECONDS)

    def _save_debug_artifacts(self, prefix: str) -> None:
        if not Config.SAVE_DEBUG_ARTIFACTS or not self.driver:
            return
        if not hasattr(self.driver, "save_screenshot"):
            return
        try:
            Path(Config.DEBUG_DIR).mkdir(parents=True, exist_ok=True)
            safe_prefix = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in prefix)
            if Config.REDACT_DEBUG_ARTIFACTS:
                # A public repository makes every uploaded artifact public, so keep
                # only a hashed text snapshot: enough to compare runs, useless to
                # anyone who is not the owner.
                (Path(Config.DEBUG_DIR) / f"{safe_prefix}.txt").write_text(
                    self._redacted_snapshot(prefix), encoding="utf-8"
                )
                print(f"Saved redacted debug snapshot for {prefix}.")
                return
            self.driver.save_screenshot(str(Path(Config.DEBUG_DIR) / f"{safe_prefix}.png"))
            (Path(Config.DEBUG_DIR) / f"{safe_prefix}.html").write_text(
                self._redact_page_source(self.driver.page_source),
                encoding="utf-8",
            )
            print(f"Saved debug artifacts for {prefix}.")
        except Exception as exc:
            print(f"Failed to save debug artifacts: {exc}")

    @staticmethod
    def _digest(value: object) -> str:
        text = str(value or "").strip()
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10] if text else "-"

    def _redacted_snapshot(self, prefix: str) -> str:
        """What the list page showed, with every identifier replaced by a digest.

        Two runs can still be diffed by digest (a missing row is a missing line),
        while the artifact reveals nothing about the manuscripts themselves.
        """
        rows = getattr(self, "_snapshot_rows", None) or []
        lines = [f"# redacted snapshot: {prefix}", f"# rows: {len(rows)}"]
        for row in rows:
            lines.append(
                "id={id} status={status!r} title={title} revision={revision}".format(
                    id=self._digest(row.get("id")),
                    status=row.get("status"),
                    title=f"title#{self._digest(row.get('title'))}({len(str(row.get('title') or ''))} chars)",
                    revision=row.get("revision"),
                )
            )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _redact_page_source(page_source: str) -> str:
        """Strip typed credential values before a page snapshot touches the disk."""
        if not page_source:
            return ""
        redacted = re.sub(
            r"(<input[^>]*type=[\"']?password[\"']?[^>]*?value=[\"'])([^\"']*)",
            r"\1***",
            page_source,
            flags=re.IGNORECASE,
        )
        redacted = re.sub(
            r"(<input[^>]*value=[\"'])([^\"']*)([\"'][^>]*type=[\"']?password)",
            r"\1***\3",
            redacted,
            flags=re.IGNORECASE,
        )
        return redacted

    def _find_first(self, candidates: list[tuple[str, str]], timeout: int = 5):
        if not self.driver:
            raise RuntimeError("Browser driver is not initialized.")
        last_error: Exception | None = None
        for by, selector in candidates:
            try:
                return WebDriverWait(self.driver, timeout).until(EC.presence_of_element_located((by, selector)))
            except Exception as exc:
                last_error = exc
        raise TimeoutException(f"No element matched candidates: {candidates}") from last_error

    def _click_first(self, candidates: list[tuple[str, str]], timeout: int = 5) -> bool:
        if not self.driver:
            raise RuntimeError("Browser driver is not initialized.")
        for by, selector in candidates:
            try:
                element = WebDriverWait(self.driver, timeout).until(EC.element_to_be_clickable((by, selector)))
                element.click()
                return True
            except Exception:
                continue
        return False

    def fetch_ieee_manuscripts(self, account: PlatformAccount) -> List[Dict]:
        if not self.driver:
            raise RuntimeError("Browser driver is not initialized.")
        self._assert_account_url(account)
        print(f"Opening IEEE ScholarOne: {account.url}")
        self._current_account = account.name
        self._open_login_page(account, "ieee")

        try:
            email_input = self._find_first(
                [
                    (By.ID, "login"),
                    (By.NAME, "login"),
                    (By.CSS_SELECTOR, "input[type='email']"),
                    (By.CSS_SELECTOR, "input[type='text']"),
                    (By.XPATH, "//input[contains(@placeholder, 'User') or contains(@placeholder, 'Email') or contains(@placeholder, 'ID')]"),
                ]
            )
            password_input = self._find_first(
                [
                    (By.ID, "password"),
                    (By.NAME, "password"),
                    (By.CSS_SELECTOR, "input[type='password']"),
                ]
            )
            email_input.clear()
            email_input.send_keys(account.email or "")
            password_input.clear()
            password_input.send_keys(account.password or "")

            clicked = self._click_first(
                [
                    (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'log in')]"),
                    (By.XPATH, "//input[contains(translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'log in')]"),
                    (By.CSS_SELECTOR, "button[type='submit']"),
                    (By.CSS_SELECTOR, "input[type='submit']"),
                ]
            )
            if not clicked:
                password_input.send_keys("\n")

            time.sleep(6)
            self._click_first(
                [
                    (By.LINK_TEXT, "Author"),
                    (By.PARTIAL_LINK_TEXT, "Author"),
                    (By.XPATH, "//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'author')]"),
                ],
                timeout=8,
            )
            time.sleep(4)
        except Exception as exc:
            self._log_page_diagnosis("IEEE login page")
            self._save_debug_artifacts("ieee_login_failed")
            raise RuntimeError(f"IEEE login failed: {exc}") from exc

        manuscripts = self._parse_table_rows("IEEE", self.driver.current_url)
        if not manuscripts:
            self._save_debug_artifacts("ieee_no_manuscripts")
        return manuscripts

    def fetch_elsevier_manuscripts(self, account: PlatformAccount) -> List[Dict]:
        if not self.driver:
            raise RuntimeError("Browser driver is not initialized.")
        self._assert_account_url(account)
        print(f"Opening Elsevier Editorial Manager: {account.url}")
        self._current_account = account.name
        self._open_login_page(account, "elsevier")

        try:
            email_input = self._find_first(
                [
                    (By.CSS_SELECTOR, "input[type='email']"),
                    (By.NAME, "username"),
                    (By.NAME, "login"),
                    (By.ID, "username"),
                    (By.ID, "login"),
                    (By.CSS_SELECTOR, "input[type='text']"),
                ],
                timeout=8,
            )
            email_input.clear()
            email_input.send_keys(account.email or "")
            self._click_first(
                [
                    (By.CSS_SELECTOR, "button[type='submit']"),
                    (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continue')]"),
                    (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'next')]"),
                ],
                timeout=4,
            )
            time.sleep(2)

            password_input = self._find_first([(By.CSS_SELECTOR, "input[type='password']")], timeout=10)
            password_input.clear()
            password_input.send_keys(account.password or "")
            clicked = self._click_first(
                [
                    (By.CSS_SELECTOR, "button[type='submit']"),
                    (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sign in')]"),
                    (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'login')]"),
                ],
                timeout=5,
            )
            if not clicked:
                password_input.send_keys("\n")
            time.sleep(8)
        except Exception as exc:
            self._log_page_diagnosis("Elsevier login page")
            self._save_debug_artifacts("elsevier_login_failed")
            raise RuntimeError(f"Elsevier login failed: {exc}") from exc

        manuscripts = self._parse_table_rows("Elsevier", self.driver.current_url)
        if not manuscripts:
            self._save_debug_artifacts("elsevier_no_manuscripts")
        return manuscripts

    def _parse_table_rows(self, source: str, url: str) -> List[Dict]:
        if not self.driver:
            return []

        if not self.driver.find_elements(By.TAG_NAME, "table"):
            # "zero rows" and "the page changed" used to look identical. Only a
            # page that explicitly says there is nothing counts as empty.
            body_text = ""
            try:
                body_text = (self.driver.find_element(By.TAG_NAME, "body").text or "").lower()
            except Exception as exc:
                print(f"{source}: could not read page text: {exc}")
            empty_markers = ("no manuscript", "no submission", "no items", "nothing to display", "no records")
            if any(marker in body_text for marker in empty_markers):
                print(f"{source}: page reports no submissions.")
                return []
            self._save_debug_artifacts(f"{source.lower()}_no_table")
            raise RuntimeError(
                f"{source}: no manuscript table found on the page; the layout may have changed"
            )

        rows = self.driver.find_elements(By.XPATH, "//table//tr[td]")
        manuscripts_by_title: dict[str, dict] = {}
        seen: set[str] = set()
        dropped: list[str] = []

        for row in rows:
            raw_cells = [cell.text or "" for cell in row.find_elements(By.TAG_NAME, "td")]
            cells = [self._clean_cell(cell) for cell in raw_cells]
            cells = [cell for cell in cells if cell]
            if len(cells) < 3:
                continue

            row_text = " | ".join(cells)
            lowered = row_text.lower()
            if self._looks_like_navigation_row(lowered):
                continue

            status_index = self._find_status_cell_index(row, len(raw_cells))
            status = self._pick_status_from_raw_cells(raw_cells, status_index=status_index) or self._pick_status(cells)
            title = self._pick_title(cells, status=status)
            manuscript_id = self._pick_manuscript_id(cells)
            if not self._looks_like_manuscript_row(cells, manuscript_id, title, status):
                continue
            if not manuscript_id:
                dropped.append(row_text[:120])

            identity_title = self._manuscript_identity_title(title)
            base_id = self._manuscript_base_id(manuscript_id)
            if base_id:
                # The manuscript number is authoritative. Two different numbers
                # with the same title are two different manuscripts (a rejected
                # paper and its fresh resubmission), while 25-TIE-0003 and its
                # 25-TIE-0003.R1 revision are one paper.
                unique_key = f"{source}:id:{base_id}".lower()
            else:
                unique_key = f"{source}:{identity_title}".lower()
            if unique_key in seen:
                current = {
                    "id": manuscript_id or title[:80],
                    "title": self._display_title(title),
                    "status": status or "Unknown",
                    "source": source,
                    "url": url,
                    "revision": self._revision_number(manuscript_id),
                }
                manuscripts_by_title[unique_key] = self._prefer_current_record(manuscripts_by_title[unique_key], current)
                continue
            seen.add(unique_key)

            manuscripts_by_title[unique_key] = {
                "id": manuscript_id or title[:80],
                "title": self._display_title(title),
                "status": status or "Unknown",
                "source": source,
                "url": url,
                "revision": self._revision_number(manuscript_id),
            }

        manuscripts = list(manuscripts_by_title.values())
        print(f"Parsed {len(manuscripts)} {source} manuscript(s).")
        # Always leave an untruncated, credential-free snapshot of what the list
        # actually showed. Titles and IDs stay visible so "did the new paper ever
        # appear?" can be answered from the artifact alone. The account name is
        # part of the file name because two ScholarOne accounts would otherwise
        # overwrite each other's snapshot.
        slug = re.sub(r"[^a-z0-9]+", "_", str(getattr(self, "_current_account", "") or source).lower()).strip("_")
        self._snapshot_rows = manuscripts
        self._save_debug_artifacts(f"{slug or source.lower()}_list")
        if dropped:
            # A row that survives the "looks like a manuscript" filter but yields
            # no id is exactly how a fresh submission slips by unnoticed, so say
            # which rows were skipped instead of failing silently.
            print(
                f"{source}: {len(rows)} table row(s) seen, {dropped} could not be turned into a manuscript: "
                + "; ".join(dropped[:5])
            )
        if not manuscripts:
            print(f"{source}: table present but no manuscript rows were recognised; check the parser.")
        return manuscripts

    @staticmethod
    def _clean_cell(value: str) -> str:
        return re.sub(r"\s+", " ", value or "").strip()

    @staticmethod
    def _cell_lines(value: str) -> list[str]:
        return [JournalMonitor._clean_cell(line) for line in (value or "").splitlines() if JournalMonitor._clean_cell(line)]

    @staticmethod
    def _looks_like_navigation_row(text: str) -> bool:
        blocked = [
            "logout",
            "log out",
            "help",
            "instructions",
            "privacy",
            "terms",
            "search",
            "create new submission",
        ]
        return any(token in text for token in blocked)

    @staticmethod
    def _looks_like_manuscript_row(cells: list[str], manuscript_id: str, title: str, status: str) -> bool:
        joined = " ".join(cells).lower()
        signals = [
            "submitted",
            "review",
            "decision",
            "revision",
            "editor",
            "manuscript",
            "accept",
            "reject",
            "awaiting",
            "processing",
        ]
        return bool((manuscript_id or len(title) > 20) and (status or any(signal in joined for signal in signals)))

    @staticmethod
    def _pick_manuscript_id(cells: list[str]) -> str:
        strong_id_patterns = [
            r"\b\d{2}-[A-Z]{1,8}-\d{2,}[-_A-Z0-9]*\b",
        ]
        id_patterns = [
            r"\b[A-Z]{1,8}[-_ ]?\d{2,}[-_A-Z0-9]*\b",
            r"\b\d{5,}[-_A-Z0-9]*\b",
        ]
        for cell in cells:
            compact = cell.strip()
            for pattern in strong_id_patterns:
                match = re.search(pattern, compact, flags=re.IGNORECASE)
                if match and not JournalMonitor._looks_like_non_manuscript_id(match.group(0)):
                    return match.group(0)
            if len(compact) > 60:
                continue
            for pattern in id_patterns:
                match = re.search(pattern, compact, flags=re.IGNORECASE)
                if match and not JournalMonitor._looks_like_non_manuscript_id(match.group(0)):
                    return match.group(0)
        fallback = cells[0].strip() if cells else ""
        if fallback and len(fallback) <= 60 and not JournalMonitor._looks_like_non_manuscript_id(fallback):
            return fallback
        return ""

    @staticmethod
    def _looks_like_date_token(value: str) -> bool:
        text = str(value or "").strip().lower()
        months = r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
        patterns = [
            rf"(?:{months})[-_ ]?\d{{2,4}}",
            rf"\d{{1,2}}[-_ ](?:{months})[-_ ]\d{{2,4}}",
            r"\d{4}[-_/]\d{1,2}[-_/]\d{1,2}",
        ]
        return any(re.fullmatch(pattern, text) for pattern in patterns)

    @staticmethod
    def _looks_like_non_manuscript_id(value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return True
        if JournalMonitor._looks_like_date_token(text):
            return True
        if re.fullmatch(r"(?:19|20)\d{2}", text):
            return True
        if JournalMonitor._status_score(text):
            return True
        return False

    @staticmethod
    def _normalize_title(value: str) -> str:
        text = JournalMonitor._clean_cell(value)
        action_markers = [
            r"\bView Submission\b.*$",
            r"\bSubmitting Author\b.*$",
            r"\bSubmitted Manuscripts?\b.*$",
            r"\bManuscript Files\b.*$",
        ]
        for pattern in action_markers:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()
        return text.strip(" -|")

    @staticmethod
    def _strip_trailing_status_from_title(value: str) -> str:
        text = JournalMonitor._normalize_title(value)
        while True:
            match = re.search(r"\s+(\((?:[^()]|\([^()]*\))*\))\s*$", text)
            if not match:
                break
            suffix = match.group(1)
            if not JournalMonitor._status_score(suffix):
                break
            text = text[: match.start()].strip()
        return text

    @staticmethod
    def _display_title(value: str) -> str:
        return JournalMonitor._strip_trailing_status_from_title(value) or JournalMonitor._normalize_title(value) or "Untitled manuscript"

    @staticmethod
    def _manuscript_identity_title(value: str) -> str:
        title = JournalMonitor._strip_trailing_status_from_title(value)
        title = re.sub(r"\s+", " ", title).strip().lower()
        title = re.sub(r"[^\w\s:;-]", "", title)
        return re.sub(r"\s+", " ", title).strip()

    @staticmethod
    def _status_priority(status: object) -> int:
        text = str(status or "").lower()
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
        return JournalMonitor._status_score(text)

    @staticmethod
    def _manuscript_base_id(value: object) -> str:
        """Strip a revision suffix: 25-TIE-0003.R1 and 25-TIE-0003 are one paper.

        A genuinely new submission has a new number (26-TIE-0002), so it must NOT
        collapse into the older record just because the title is identical - that
        is how a resubmitted paper used to disappear from the dashboard.
        """
        text = str(value or "").strip()
        return re.sub(r"\s*\.\s*R\d+$", "", text, flags=re.IGNORECASE).strip()

    @staticmethod
    def _revision_number(value: object) -> int:
        """0 for the original submission, N for .RN revisions."""
        match = re.search(r"\.\s*R(\d+)\s*$", str(value or "").strip(), flags=re.IGNORECASE)
        return int(match.group(1)) if match else 0

    @staticmethod
    def _id_quality(manuscript_id: object) -> int:
        text = str(manuscript_id or "").strip()
        if not text:
            return 0
        if JournalMonitor._looks_like_non_manuscript_id(text):
            return -10
        if re.search(r"[A-Za-z]", text) and re.search(r"\d", text):
            return 10
        if re.search(r"\d", text):
            return 5
        return 1

    @staticmethod
    def _prefer_current_record(existing: dict, candidate: dict) -> dict:
        # A revision row (25-TIE-0003.R1) carries the current state of the paper,
        # so it wins over the original row when both are on the page.
        existing_revision = int(existing.get("revision") or 0)
        candidate_revision = int(candidate.get("revision") or 0)
        if candidate_revision != existing_revision:
            return candidate if candidate_revision > existing_revision else existing
        existing_score = JournalMonitor._status_priority(existing.get("status"))
        candidate_score = JournalMonitor._status_priority(candidate.get("status"))
        if candidate_score > existing_score:
            chosen = {**candidate}
            if JournalMonitor._id_quality(existing.get("id")) > JournalMonitor._id_quality(candidate.get("id")):
                chosen["id"] = existing.get("id", "")
            return chosen
        if candidate_score == existing_score and JournalMonitor._id_quality(candidate.get("id")) > JournalMonitor._id_quality(existing.get("id")):
            chosen = {**existing}
            chosen["id"] = candidate.get("id", "")
            return chosen
        return existing

    @staticmethod
    def _pick_title(cells: list[str], status: str = "") -> str:
        normalized = [JournalMonitor._normalize_title(cell) for cell in cells]
        blocked = {"contact journal", status.lower().strip()}
        candidates = []
        for cell in normalized:
            lower = cell.lower()
            if not cell or lower in blocked:
                continue
            if lower.startswith(("eic:", "adm:")):
                continue
            if JournalMonitor._looks_like_identifier_cell(cell):
                continue
            if JournalMonitor._status_score(cell):
                continue
            if len(cell) > 20:
                candidates.append(cell)
        if candidates:
            return max(candidates, key=len)
        long_cells = [cell for cell in normalized if len(cell) > 20]
        return max(long_cells, key=len) if long_cells else (normalized[-1] if normalized else "")

    @staticmethod
    def _looks_like_identifier_cell(value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        if re.search(r"\bREX-PROD\b", text, flags=re.IGNORECASE):
            return True
        if re.search(r"\b[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\b", text, flags=re.IGNORECASE):
            return True
        if re.fullmatch(r"\d{2}-[A-Z]{1,8}-\d{2,}.*", text, flags=re.IGNORECASE):
            return True
        return False

    @staticmethod
    def _status_score(value: str) -> int:
        text = value.lower()
        keywords = [
            "submitted",
            "review",
            "decision",
            "revision",
            "editor",
            "awaiting",
            "processing",
            "accept",
            "reject",
            "incomplete",
            "withdrawn",
            "published",
            "with editor",
            "with reviewer",
            "with associate editor",
            "with editor-in-chief",
        ]
        return sum(1 for keyword in keywords if keyword in text)

    @staticmethod
    def _line_is_status_candidate(line: str) -> bool:
        lower = line.lower().strip()
        if not lower:
            return False
        ignored_prefixes = (
            "contact journal",
            "eic:",
            "adm:",
            "view submission",
            "submitting author",
            "manuscript files",
            "action",
        )
        if lower.startswith(ignored_prefixes):
            return False
        return bool(JournalMonitor._status_score(line))

    @staticmethod
    def _status_date_key(line: str) -> tuple[int, int, int] | None:
        month_map = {
            "jan": 1,
            "feb": 2,
            "mar": 3,
            "apr": 4,
            "may": 5,
            "jun": 6,
            "jul": 7,
            "aug": 8,
            "sep": 9,
            "sept": 9,
            "oct": 10,
            "nov": 11,
            "dec": 12,
        }
        match = re.search(r"\b(\d{1,2})[-/\s]([A-Za-z]{3,9})[-/\s](\d{2,4})\b", line)
        if match:
            day = int(match.group(1))
            month = month_map.get(match.group(2).lower()[:3])
            year = int(match.group(3))
            if year < 100:
                year += 2000
            if month:
                return (year, month, day)
        match = re.search(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b", line)
        if match:
            return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        return None

    @staticmethod
    def _status_lines_from_cell(raw_cell: str) -> list[str]:
        status_lines: list[str] = []
        lines = JournalMonitor._cell_lines(raw_cell)
        index = 0
        while index < len(lines):
            line = lines[index]
            cleaned = re.sub(r"^(current\s+)?status\s*:?\s*", "", line, flags=re.IGNORECASE).strip()
            if index + 1 < len(lines) and JournalMonitor._should_join_status_lines(cleaned, lines[index + 1]):
                cleaned = f"{cleaned} {lines[index + 1]}".strip()
                index += 1
            if JournalMonitor._line_is_status_candidate(cleaned):
                status_lines.append(cleaned)
            index += 1
        return status_lines

    @staticmethod
    def _should_join_status_lines(current: str, next_line: str) -> bool:
        current_text = str(current or "").strip()
        next_text = str(next_line or "").strip()
        if not current_text or not next_text:
            return False
        lower_next = next_text.lower()
        if current_text.count("(") > current_text.count(")"):
            return True
        if lower_next in {"processing"}:
            return True
        if re.fullmatch(r"\(?\d{1,2}[-/\s][A-Za-z]{3,9}[-/\s]?\d{2,4}\)?", next_text):
            return True
        if re.fullmatch(r"\d{2,4}\)?", next_text) and re.search(r"\([A-Za-z]{3,9}[-/\s]*$", current_text):
            return True
        return False

    @staticmethod
    def _choose_current_status(status_lines: list[str]) -> str:
        if not status_lines:
            return ""
        # ScholarOne renders the STATUS history newest -> oldest.
        # The visual top line is therefore the current status even if older
        # lines contain later-looking dates from revision activity.
        return status_lines[0]

    @staticmethod
    def _status_cell_score(raw_cell: str) -> int:
        lines = JournalMonitor._status_lines_from_cell(raw_cell)
        if not lines:
            return 0
        joined = " ".join(JournalMonitor._cell_lines(raw_cell)).lower()
        score = 10 + len(lines) * 5
        if re.search(r"\b(current\s+)?status\b", joined):
            score += 25
        if len(lines) > 1:
            score += 15
        if JournalMonitor._status_date_key(lines[0]) is not None:
            score += 10
        if any(JournalMonitor._status_date_key(line) is not None for line in lines):
            score += 5
        if re.search(r"\b(view submission|contact journal|manuscript files|submitting author)\b", joined):
            score -= 25
        if len(JournalMonitor._clean_cell(raw_cell)) > 500:
            score -= 10
        return score

    @staticmethod
    def _pick_status_from_raw_cells(raw_cells: list[str], status_index: int | None = None) -> str:
        if status_index is not None and 0 <= status_index < len(raw_cells):
            status = JournalMonitor._choose_current_status(JournalMonitor._status_lines_from_cell(raw_cells[status_index]))
            if status:
                return status

        scored_cells = [
            (JournalMonitor._status_cell_score(raw_cell), index, raw_cell)
            for index, raw_cell in enumerate(raw_cells)
        ]
        scored_cells = [item for item in scored_cells if item[0] > 0]
        if scored_cells:
            scored_cells.sort(key=lambda item: (-item[0], item[1]))
            return JournalMonitor._choose_current_status(JournalMonitor._status_lines_from_cell(scored_cells[0][2]))

        candidates: list[str] = []
        for raw_cell in raw_cells:
            candidates.extend(JournalMonitor._status_lines_from_cell(raw_cell))
        if not candidates:
            return ""
        return JournalMonitor._choose_current_status(candidates)

    @staticmethod
    def _find_status_cell_index(row: object, cell_count: int) -> int | None:
        try:
            cells = row.find_elements(By.TAG_NAME, "td")
            for index, cell in enumerate(cells):
                labels = [
                    cell.get_attribute("data-label") or "",
                    cell.get_attribute("aria-label") or "",
                    cell.get_attribute("headers") or "",
                ]
                if any(re.search(r"\bstatus\b", label, flags=re.IGNORECASE) for label in labels):
                    return index

            table = row.find_element(By.XPATH, "./ancestor::table[1]")
            header_rows = table.find_elements(By.XPATH, ".//tr[th]")
            for header_row in reversed(header_rows):
                headers = [JournalMonitor._clean_cell(th.text) for th in header_row.find_elements(By.TAG_NAME, "th")]
                if len(headers) != cell_count:
                    continue
                for index, header in enumerate(headers):
                    if re.search(r"\bstatus\b", header, flags=re.IGNORECASE):
                        return index
        except Exception:
            return None
        return None

    @staticmethod
    def _pick_status(cells: list[str]) -> str:
        scored = [(JournalMonitor._status_score(cell), len(cell), cell) for cell in cells if len(cell) <= 120]
        scored = [item for item in scored if item[0] > 0]
        if scored:
            scored.sort(key=lambda item: (-item[0], item[1]))
            return scored[0][2]
        return cells[1] if len(cells) > 1 and len(cells[1]) <= 120 else "Unknown"

    def collect_manuscripts(self) -> tuple[List[Dict], List[str]]:
        """Fetch every configured account and report failures per account.

        Returns (manuscripts, problems). An empty list is ambiguous on its own
        (locked account vs. genuinely no submissions), so every account failure
        is surfaced instead of only printed.
        """
        manuscripts: list[dict] = []
        problems: list[str] = []
        accounts = Config.configured_platforms()
        print("Configuration:", Config.summary(accounts))
        account_rows: dict[str, int] = {}

        self._init_driver()
        try:
            for account in accounts:
                fetched: list[dict] = []
                last_error = ""
                for attempt in range(Config.ACCOUNT_RETRIES + 1):
                    if attempt:
                        # A fresh session often gets past a transient challenge.
                        print(f"{account.name}: retry {attempt}/{Config.ACCOUNT_RETRIES} after a short pause.")
                        self._reset_session()
                        time.sleep(8)
                    try:
                        if account.name.startswith("IEEE"):
                            fetched = self.fetch_ieee_manuscripts(account)
                        elif account.name.startswith("Elsevier"):
                            fetched = self.fetch_elsevier_manuscripts(account)
                        else:
                            break
                        last_error = ""
                        break
                    except Exception as exc:
                        last_error = str(exc)
                        print(f"{account.name} fetch failed (attempt {attempt + 1}): {exc}")
                        if self.driver:
                            self._log_page_diagnosis(f"{account.name} page")
                if last_error:
                    problems.append(f"{account.name} fetch failed: {last_error}")
                    account_rows[account.name] = -1
                    continue
                account_rows[account.name] = len(fetched)
                if not fetched and account.name not in Config.ALLOW_EMPTY_ACCOUNTS:
                    # Logging in and then seeing zero rows is usually a broken
                    # parse, and a silently missed manuscript is the exact fault
                    # this monitor exists to prevent. Say so out loud.
                    problems.append(
                        f"{account.name} parsed 0 manuscripts；页面有表格但没有识别到稿件，"
                        "可能有新稿被漏掉。确认该账号确实没有在投稿件时，把账号名加入 "
                        "ALLOW_EMPTY_ACCOUNTS（GitHub Secret）即可。"
                    )
                    print(f"{account.name}: parsed 0 manuscripts - treated as a parse problem.")
                manuscripts.extend(fetched)
        finally:
            self._close_driver()
        self._account_rows = account_rows
        return manuscripts, problems

    def _emit_output(self, name: str, value: str) -> None:
        path = os.getenv("GITHUB_OUTPUT")
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"{name}={value}\n")
        except Exception as exc:
            print(f"Could not write step output {name}: {exc}")

    def _apply(self, result) -> None:
        self.storage.apply(result)
        # A failure alert rewrites the meta file (the cooldown timestamp), and a
        # partial run can still carry a real change from the account that did
        # load. Both must be committed: emitting "false" afterwards used to skip
        # the commit step entirely, so the throttle reset every window and the
        # same change was mailed again on the next successful run.
        changed = bool(getattr(result, "meaningful", False)) or bool(getattr(self, "_alert_sent", False))
        self._emit_output("data_changed", "true" if changed else "false")

    def _attempt_minutes(self) -> List[int]:
        """Minute list of the Worker cron, read from the checked-out config."""
        try:
            text = Path(Config.WINDOW_CRON_FILE).read_text(encoding="utf-8")
        except OSError:
            return []
        return attempt_minutes_from_wrangler(text)

    def _alert(self, problems: List[str]) -> None:
        """Send a failure alert, throttled so a recurring fault cannot spam."""
        if not problems:
            return
        defer, reason = should_defer_failure_alert(
            os.getenv("RUN_WINDOW", ""),
            self._attempt_minutes(),
            datetime.now(),
            enabled=Config.ALERT_ON_LAST_ATTEMPT_ONLY,
        )
        if defer:
            print(f"Failure alert held back: {reason}")
            self._summary["note"] = reason
            return
        cooldown = Config.ALERT_COOLDOWN_HOURS
        meta = self.storage.load_meta()
        last = meta.get("last_failure_alert_at")
        if cooldown > 0 and last:
            try:
                last_at = datetime.strptime(str(last), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                last_at = None
            if last_at and datetime.now() - last_at < timedelta(hours=cooldown):
                print(f"Failure alert suppressed (last sent at {last}, cooldown {cooldown}h).")
                return
        if self.notifier.send_failure_alert(problems):
            meta["last_failure_alert_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.storage.save_meta(meta)
            # The meta file must be committed even when the run fails, otherwise
            # the cooldown resets on the next attempt. _apply() re-emits this at
            # the end of the run, so remember it here as well.
            self._alert_sent = True
            self._emit_output("data_changed", "true")

    def _write_step_summary(self) -> None:
        """Publish a short, human-readable result to the Actions run summary.

        This is what makes "was I actually checked?" answerable from a phone
        without reading raw logs.
        """
        path = os.getenv("GITHUB_STEP_SUMMARY")
        summary = getattr(self, "_summary", None)
        if not path or not summary:
            return
        accounts = summary.get("accounts") or {}
        rows = "、".join(
            f"{name}: {'失败' if count < 0 else str(count) + ' 篇'}" for name, count in accounts.items()
        ) or "无"
        lines = [
            f"## 期刊监控 · {summary.get('mode', '')}",
            "",
            f"- 结论：**{summary.get('result') or '未知'}**",
            f"- 抓取到稿件：{summary.get('fetched', 0)} 篇（{rows}）",
            f"- 本次变化：新稿件 {summary.get('new', 0)} 篇，状态变化 {summary.get('changed', 0)} 项",
        ]
        if summary.get("archived"):
            lines.append(f"- 归档：{summary['archived']} 篇（状态判定为终态）")
        problems = summary.get("problems") or []
        if problems:
            lines.append("")
            lines.append("### 异常")
            lines.extend(f"- {problem}" for problem in problems)
        if summary.get("note"):
            lines.append("")
            lines.append(f"> {summary['note']}")
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
        except Exception as exc:
            print(f"Could not write the step summary: {exc}")

    def run(self, mode: str) -> int:
        self._summary = {
            "mode": mode,
            "fetched": 0,
            "accounts": {},
            "problems": [],
            "new": 0,
            "changed": 0,
            "archived": 0,
            "result": "",
            "note": "",
        }
        try:
            code = self._run(mode)
        except Exception as exc:
            self._summary["result"] = "异常退出"
            self._summary["problems"] = [str(exc)]
            self._write_step_summary()
            raise
        self._summary["result"] = "成功" if code == 0 else "失败"
        self._write_step_summary()
        return code

    def _run(self, mode: str) -> int:
        Config.validate(mode)
        if mode == "test":
            return 0 if self.notifier.send_test_email() else 1

        # Say out loud when this window's last chance is, so "why was no mail
        # sent" or "why am I only hearing about this now" is answerable from the
        # log alone.
        window = os.getenv("RUN_WINDOW", "").strip() or "manual"
        minutes = self._attempt_minutes()
        print(
            f"Alert policy: window={window} wake-ups="
            f"{','.join(f':{minute:02d}' for minute in minutes) or 'unknown'}"
            f" mail_only_on_last={Config.ALERT_ON_LAST_ATTEMPT_ONLY}"
        )

        manuscripts, problems = self.collect_manuscripts()
        print(f"Fetched {len(manuscripts)} manuscript(s).")
        self._summary["fetched"] = len(manuscripts)
        self._summary["accounts"] = getattr(self, "_account_rows", {}) or {}
        self._summary["problems"] = list(problems)

        if problems:
            # Alert first: the user must know that monitoring did not fully run.
            self._alert(problems)

        if Config.REQUIRE_BASELINE:
            baseline_problem = self.storage.baseline_problem()
            if baseline_problem:
                # With no baseline every manuscript looks new: one bogus "N new
                # manuscripts" mail, and the real diff history is gone. Fail loudly
                # and leave the state untouched instead.
                message = (
                    f"Baseline unavailable ({baseline_problem}); refusing to treat every "
                    "manuscript as new. Is the state repository mounted at DATA_FILE?"
                )
                print(message)
                self._summary["problems"] = list(problems) + [message]
                self._summary["note"] = message
                return 1

        if not manuscripts:
            message = "No manuscripts were fetched. Check credentials, URLs, page structure, or debug artifacts."
            print(message)
            self._summary["note"] = message
            if Config.FAIL_ON_EMPTY:
                return 1
            return 1 if problems else 0

        result = self.storage.plan(manuscripts)
        current = list(result.manuscripts.values())
        self._summary["new"] = sum(1 for item in result.changes if item.get("is_new"))
        self._summary["changed"] = sum(1 for item in result.changes if not item.get("is_new"))
        self._summary["archived"] = sum(
            1 for record in current if record.get("archived") and record.get("archive_reason") == "terminal_status"
        )

        if mode == "daily_report":
            if self.storage.daily_report_already_sent():
                # A fallback run after the weekly report went out. It must still
                # announce a change it happens to see: applying the snapshot here
                # without a mail would lose that change forever.
                print("Daily report already sent today; skipping the weekly summary.")
                if result.has_changes:
                    if not self.notifier.send_change_notification(result.changes):
                        print("Change notification failed; stored state left unchanged for retry.")
                        return 1
                self._apply(result)
                return 1 if problems else 0
            active = current if Config.INCLUDE_ARCHIVED_IN_REPORT else [
                record for record in current if not record.get("archived")
            ]
            if not active:
                # Everything is archived: that is a normal state, not a failure.
                # Say so once per day instead of leaving a red run behind.
                print("Daily report skipped: every manuscript is archived, nothing to report.")
                self._summary["note"] = "全部稿件均已归档，本周没有需要汇报的活跃稿件。"
                self.storage.mark_daily_report_sent()
                self._apply(result)
                return 1 if problems else 0
            # The weekly window is also a normal check window, so a new manuscript
            # can land here. Send the summary first (it is the half guarded by
            # mark_daily_report_sent, so a failure retries cleanly), then the
            # dedicated change mail, then persist. If the change mail fails, the
            # next run re-enters the guard above and sends only that mail.
            sent = self.notifier.send_daily_report(active)
            if not sent:
                # Keep the previous snapshot so the report and the status changes
                # it contains can be retried on the next run.
                print("Daily report could not be sent; stored state left unchanged for retry.")
                return 1
            self.storage.mark_daily_report_sent()
            if result.has_changes:
                if not self.notifier.send_change_notification(result.changes):
                    print("Change notification failed; stored state left unchanged for retry.")
                    return 1
            self._apply(result)
            return 1 if problems else 0

        if result.has_changes:
            if not self.notifier.send_change_notification(result.changes):
                # Do not persist the new status: the next run compares against the
                # old one again and retries the notification instead of losing it.
                print("Change notification failed; stored state left unchanged for retry.")
                return 1

        self._apply(result)
        if problems:
            return 1
        if not result.has_changes:
            print("No status changes detected.")
        return 0


def resolve_mode(cli_mode: str | None) -> str:
    if cli_mode:
        return cli_mode
    if os.getenv("DAILY_REPORT", "").lower() == "true":
        return "daily_report"
    return os.getenv("RUN_MODE", "normal").strip().lower()


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor journal manuscript status.")
    parser.add_argument("--mode", choices=["normal", "test", "daily_report"], help="Run mode.")
    parser.add_argument(
        "--unarchive",
        metavar="ID_OR_TITLE",
        help="Clear the archived flag for matching manuscript(s) and exit. "
        "Use it when a status was archived by mistake.",
    )
    args = parser.parse_args()

    if args.unarchive:
        storage = ManuscriptStorage(Config.DATA_FILE)
        restored = storage.unarchive(args.unarchive)
        print(f"Unarchived {restored} record(s).")
        return 0 if restored else 1

    mode = resolve_mode(args.mode)
    print(f"Run mode: {mode}")
    return JournalMonitor().run(mode)


if __name__ == "__main__":
    sys.exit(main())
