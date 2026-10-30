"""Configuration for the journal status monitor."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from typing import Iterable


TRUE_VALUES = {"1", "true", "yes", "y", "on"}
MAX_PLATFORM_SLOTS = 5

# Status words that mean "the manuscript is finished; stop monitoring it".
# Matching is phrase based and always checked against TERMINAL_STATUS_VETOES first.
DEFAULT_TERMINAL_KEYWORDS = (
    "accept",
    "accepted",
    "acceptance",
    "published",
    "online",
    "reject",
    "rejected",
    "withdrawn",
    "transferred",
    "declined to publish",
)

# Phrases that look terminal but are NOT. These caused permanent false archival
# (for example "Required Reviews Completed") before this list existed.
DEFAULT_TERMINAL_VETOES = (
    "required reviews completed",
    "reviews completed",
    "awaiting",
    "in process",
    "in progress",
    "resubmit",
    "resubmission",
    "decline to review",
    "declined to review",
    "under review",
    "with editor",
    "with reviewer",
    "pending",
    "decision",
    "not accepted",
    "no decision",
)

# Checked before the veto list: the owner's rule is that "reject" always wins.
# Without this, "Reject with Possibility for Resubmission" (veto: resubmission)
# and "Reject and Resubmit" (veto: resubmit) would never be archived.
DEFAULT_TERMINAL_OVERRIDES = ("reject",)

# Hosts the monitor is allowed to type a real password into.
DEFAULT_ALLOWED_URL_HOSTS = (
    "manuscriptcentral.com",
    "scholarone.com",
    "editorialmanager.com",
    "elsevier.com",
    "springer.com",
    "wiley.com",
    "nature.com",
    "ieee.org",
    "acm.org",
    "tandfonline.com",
    "sagepub.com",
    "iop.org",
    "aip.org",
    "aps.org",
    "hindawi.com",
    "frontiersin.org",
    "mdpi.com",
    "plos.org",
    "biorxiv.org",
)

UNKNOWN_STATUS_TOKENS = {"", "unknown", "n/a", "na", "none", "null", "-"}


def first_env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    # An unset secret arrives as an empty string in GitHub Actions. Treating ""
    # as False silently disabled safety flags such as FAIL_ON_EMPTY.
    if value is None or not value.strip():
        return default
    return value.strip().lower() in TRUE_VALUES


def int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc


def split_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip().lower() for item in value.replace(";", ",").split(",") if item.strip()]


def split_recipients(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]


split_statuses = split_list


def normalize_status(value: object) -> str:
    """Lower-case, de-duplicated status text with dates/parentheticals removed."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"^(?:current\s+)?status\s*:?\s*", "", text)
    text = re.sub(r"\((?:[^()]*)\)", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .:-")


def is_unknown_status(value: object) -> bool:
    return normalize_status(value) in UNKNOWN_STATUS_TOKENS


def host_allowed(url: str | None, allowed_hosts: Iterable[str]) -> bool:
    """True when the URL is HTTPS and its host is an allowed suffix."""
    if not url:
        return False
    try:
        parts = urlsplit(str(url).strip())
    except ValueError:
        return False
    if parts.scheme.lower() != "https":
        return False
    host = (parts.hostname or "").lower().strip(".")
    if not host:
        return False
    for suffix in allowed_hosts:
        suffix = str(suffix or "").strip().lower().lstrip(".")
        if not suffix:
            continue
        if host == suffix or host.endswith(f".{suffix}"):
            return True
    return False


@dataclass(frozen=True)
class PlatformAccount:
    name: str
    email: str | None
    password: str | None
    url: str | None

    @property
    def configured(self) -> bool:
        return bool(self.email and self.password and self.url)

    @property
    def partially_configured(self) -> bool:
        return any([self.email, self.password, self.url]) and not self.configured


def platform_slot_names(prefix: str, slot: int) -> tuple[str, str, str]:
    env_prefix = prefix if slot == 1 else f"{prefix}_{slot}"
    return f"{env_prefix}_EMAIL", f"{env_prefix}_PASSWORD", f"{env_prefix}_URL"


def read_platform_account(name: str, prefix: str, slot: int) -> PlatformAccount:
    email_name, password_name, url_name = platform_slot_names(prefix, slot)
    if slot == 1:
        email = first_env(email_name, f"{prefix}_USERNAME")
    else:
        email = first_env(email_name, f"{prefix}_{slot}_USERNAME")
    return PlatformAccount(
        name if slot == 1 else f"{name}#{slot}",
        email,
        first_env(password_name),
        first_env(url_name),
    )


class Config:
    # Accept both documented *_EMAIL names and older *_USERNAME aliases.
    IEEE_EMAIL = first_env("IEEE_EMAIL", "IEEE_USERNAME")
    IEEE_USERNAME = IEEE_EMAIL
    IEEE_PASSWORD = first_env("IEEE_PASSWORD")
    IEEE_URL = first_env("IEEE_URL")

    ELSEVIER_EMAIL = first_env("ELSEVIER_EMAIL", "ELSEVIER_USERNAME")
    ELSEVIER_USERNAME = ELSEVIER_EMAIL
    ELSEVIER_PASSWORD = first_env("ELSEVIER_PASSWORD")
    ELSEVIER_URL = first_env("ELSEVIER_URL")

    EMAIL_SENDER = first_env("EMAIL_SENDER")
    EMAIL_PASSWORD = first_env("EMAIL_PASSWORD")
    EMAIL_RECEIVER = first_env("EMAIL_RECEIVER", "EMAIL_RECIPIENTS")
    EMAIL_RECEIVERS = split_recipients(EMAIL_RECEIVER)

    SMTP_SERVER = first_env("SMTP_SERVER", "SMTP_HOST")
    SMTP_PORT = int_env("SMTP_PORT", 0)

    DATA_FILE = first_env("DATA_FILE", default="data/manuscripts.json") or "data/manuscripts.json"
    RETENTION_DAYS = int_env("RETENTION_DAYS", 30)
    HEADLESS = bool_env("HEADLESS", True)
    # A run that fetched nothing is a failure, not a quiet success.
    FAIL_ON_EMPTY = bool_env("FAIL_ON_EMPTY", True)
    # Failure alerts are throttled: a Cloudflare challenge that repeats every
    # window must not bury the inbox, but it must still be visible once.
    ALERT_COOLDOWN_HOURS = int_env("ALERT_COOLDOWN_HOURS", 6)
    # A blocked window is woken again on the fallback minutes (:27, :37). Mailing
    # on the first failure paged the inbox for a challenge that cleared ten
    # minutes later, so only the last wake-up of a window may alert.
    ALERT_ON_LAST_ATTEMPT_ONLY = bool_env("ALERT_ON_LAST_ATTEMPT_ONLY", True)
    # Where the Worker's cron lives. Its minute list is the source of truth for
    # "how many chances does this window get", so the monitor never has to guess.
    WINDOW_CRON_FILE = first_env(
        "WINDOW_CRON_FILE", default="cloudflare-scheduler/wrangler.toml"
    ) or "cloudflare-scheduler/wrangler.toml"
    SAVE_DEBUG_ARTIFACTS = bool_env("SAVE_DEBUG_ARTIFACTS", True)
    # The code repository is public, so debug artifacts are public too: redact
    # identifiers and titles before they are uploaded.
    REDACT_DEBUG_ARTIFACTS = bool_env("REDACT_DEBUG_ARTIFACTS", False)
    # Fail instead of treating a missing baseline as "everything is new".
    REQUIRE_BASELINE = bool_env("REQUIRE_BASELINE", False)
    DEBUG_DIR = first_env("DEBUG_DIR", default="debug") or "debug"
    WAIT_SECONDS = int_env("WAIT_SECONDS", 20)
    PAGE_LOAD_TIMEOUT = int_env("PAGE_LOAD_TIMEOUT", 60)
    # Bot-protection interstitials ("Just a moment...") usually clear by
    # themselves; waiting is cheaper than failing a whole window. 40s covers the
    # JS challenge on a slow runner; the observed failures never cleared inside
    # 20s but the next attempt ten minutes later always did.
    CHALLENGE_WAIT_SECONDS = int_env("CHALLENGE_WAIT_SECONDS", 40)
    # The login page is JavaScript rendered: title='' with a form and no inputs
    # is a page still painting, not a broken login. Give it time to appear.
    LOGIN_WAIT_SECONDS = int_env("LOGIN_WAIT_SECONDS", 30)
    # Two retries per account (three attempts) absorb a transient challenge
    # without turning the monitor into a blind feeder that reports success on
    # nothing.
    ACCOUNT_RETRIES = int_env("ACCOUNT_RETRIES", 2)
    # Keep the automation-flag suppression for site compatibility, but never fake
    # the browser user agent: a mismatched UA is what sites flag first.
    STEALTH_MODE = bool_env("STEALTH_MODE", True)

    ARCHIVE_TERMINAL = bool_env("ARCHIVE_TERMINAL", True)
    INCLUDE_ARCHIVED_IN_REPORT = bool_env("INCLUDE_ARCHIVED_IN_REPORT", False)
    TERMINAL_STATUS_KEYWORDS = tuple(
        split_list(
            first_env(
                "TERMINAL_STATUS_KEYWORDS",
                default=",".join(DEFAULT_TERMINAL_KEYWORDS),
            )
        )
    )
    TERMINAL_STATUS_VETOES = tuple(
        split_list(
            first_env(
                "TERMINAL_STATUS_VETOES",
                default=",".join(DEFAULT_TERMINAL_VETOES),
            )
        )
    )
    TERMINAL_STATUS_OVERRIDES = tuple(
        split_list(first_env("TERMINAL_STATUS_OVERRIDES", default=",".join(DEFAULT_TERMINAL_OVERRIDES)))
    )

    NOTIFY_ON_FIRST_SEEN = bool_env("NOTIFY_ON_FIRST_SEEN", True)
    # A brand-new manuscript must always be announced; only never-submitted
    # drafts stay quiet (they are not manuscripts yet and would only be noise).
    FIRST_SEEN_SILENT_STATUS_KEYWORDS = tuple(
        split_list(
            first_env(
                "FIRST_SEEN_SILENT_STATUS_KEYWORDS",
                default="draft,incomplete,unsubmitted",
            )
        )
    )
    # An account that logged in but produced no rows is usually a broken parse,
    # not an account without submissions. Name an account here to allow it.
    ALLOW_EMPTY_ACCOUNTS = tuple(split_list(first_env("ALLOW_EMPTY_ACCOUNTS", default="")))

    ALLOWED_URL_HOSTS = tuple(
        split_list(
            first_env("ALLOWED_URL_HOSTS", default=",".join(DEFAULT_ALLOWED_URL_HOSTS))
        )
    )
    REQUIRE_HTTPS = bool_env("REQUIRE_HTTPS", True)
    # Emit full manuscript titles into Actions logs. Off by default so a log copy
    # cannot leak unpublished titles; emails always contain the full title.
    LOG_FULL_TITLES = bool_env("LOG_FULL_TITLES", False)

    @classmethod
    def ieee_account(cls) -> PlatformAccount:
        return read_platform_account("IEEE", "IEEE", 1)

    @classmethod
    def elsevier_account(cls) -> PlatformAccount:
        return read_platform_account("Elsevier", "ELSEVIER", 1)

    @classmethod
    def platform_accounts(cls) -> list[PlatformAccount]:
        accounts: list[PlatformAccount] = []
        for slot in range(1, MAX_PLATFORM_SLOTS + 1):
            accounts.append(read_platform_account("IEEE", "IEEE", slot))
        for slot in range(1, MAX_PLATFORM_SLOTS + 1):
            accounts.append(read_platform_account("Elsevier", "ELSEVIER", slot))
        return accounts

    @classmethod
    def configured_platforms(cls) -> list[PlatformAccount]:
        return [account for account in cls.platform_accounts() if account.configured]

    @classmethod
    def get_smtp_config(cls) -> tuple[str, int]:
        host = cls.SMTP_SERVER or cls.guess_smtp_host(cls.EMAIL_SENDER)
        port = cls.SMTP_PORT or cls.default_smtp_port(host)
        return host, port

    @staticmethod
    def guess_smtp_host(sender: str | None) -> str:
        if not sender or "@" not in sender:
            return ""
        domain = sender.rsplit("@", 1)[1].lower()
        known = {
            "qq.com": "smtp.qq.com",
            "foxmail.com": "smtp.qq.com",
            "163.com": "smtp.163.com",
            "126.com": "smtp.126.com",
            "yeah.net": "smtp.yeah.net",
            "gmail.com": "smtp.gmail.com",
            "outlook.com": "smtp.office365.com",
            "hotmail.com": "smtp.office365.com",
            "live.com": "smtp.office365.com",
            "sina.com": "smtp.sina.com",
        }
        return known.get(domain, f"smtp.{domain}")

    @staticmethod
    def default_smtp_port(host: str) -> int:
        if host in {"smtp.qq.com", "smtp.163.com", "smtp.126.com", "smtp.gmail.com"}:
            return 465
        return 587

    @classmethod
    def is_terminal_status(cls, status: object) -> bool:
        if not cls.ARCHIVE_TERMINAL:
            return False
        text = normalize_status(status)
        if not text or text in UNKNOWN_STATUS_TOKENS:
            return False
        # Owner rule: anything that says "reject" is a rejection, including
        # "Reject and Resubmit" and "Reject with Possibility for Resubmission".
        if any(override in text for override in cls.TERMINAL_STATUS_OVERRIDES):
            return True
        if any(veto in text for veto in cls.TERMINAL_STATUS_VETOES):
            return False
        return any(keyword in text for keyword in cls.TERMINAL_STATUS_KEYWORDS)

    @classmethod
    def url_is_allowed(cls, url: str | None) -> bool:
        if cls.REQUIRE_HTTPS:
            return host_allowed(url, cls.ALLOWED_URL_HOSTS)
        text = str(url or "")
        return host_allowed(text, cls.ALLOWED_URL_HOSTS) or text.startswith("http://")

    @classmethod
    def url_problem(cls, url: str | None) -> str | None:
        """Return a human readable reason when a login URL must not be used."""
        if not url:
            return "URL is empty"
        try:
            parts = urlsplit(str(url).strip())
        except ValueError:
            return "URL cannot be parsed"
        scheme = parts.scheme.lower()
        if cls.REQUIRE_HTTPS and scheme != "https":
            return f"URL scheme must be https, got {scheme or 'none'!r}"
        if not host_allowed(url, cls.ALLOWED_URL_HOSTS):
            host = (parts.hostname or "").lower()
            return (
                f"host {host!r} is not in ALLOWED_URL_HOSTS; "
                "add it deliberately if this journal portal is expected"
            )
        return None

    @classmethod
    def validate(cls, mode: str) -> None:
        errors: list[str] = []

        if mode in {"normal", "daily_report"}:
            if not cls.configured_platforms():
                errors.append(
                    "Configure at least one full platform account: IEEE_EMAIL/IEEE_PASSWORD/IEEE_URL "
                    "or ELSEVIER_EMAIL/ELSEVIER_PASSWORD/ELSEVIER_URL."
                )
            for account in cls.platform_accounts():
                if account.partially_configured:
                    errors.append(f"{account.name} settings are incomplete; email, password, and URL are all required.")
            for account in cls.configured_platforms():
                problem = cls.url_problem(account.url)
                if problem:
                    errors.append(f"{account.name} URL rejected ({problem}). Refusing to type a password there.")

        if mode in {"test", "normal", "daily_report"}:
            if not cls.EMAIL_SENDER:
                errors.append("EMAIL_SENDER is required.")
            if not cls.EMAIL_PASSWORD:
                errors.append("EMAIL_PASSWORD is required.")
            if not cls.EMAIL_RECEIVERS:
                errors.append("EMAIL_RECEIVER is required.")
            smtp_host, _ = cls.get_smtp_config()
            if not smtp_host:
                errors.append("SMTP_SERVER/SMTP_HOST is required when SMTP cannot be inferred from EMAIL_SENDER.")

        if errors:
            raise RuntimeError("Configuration is invalid:\n - " + "\n - ".join(errors))

    @classmethod
    def summary(cls, accounts: Iterable[PlatformAccount]) -> str:
        names = ", ".join(account.name for account in accounts) or "none"
        smtp_host, smtp_port = cls.get_smtp_config()
        return (
            f"platforms={names}; smtp={smtp_host}:{smtp_port}; data_file={cls.DATA_FILE}; "
            f"archive_terminal={cls.ARCHIVE_TERMINAL}"
        )