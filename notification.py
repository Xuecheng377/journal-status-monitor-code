"""Email notification helpers."""
from __future__ import annotations

import html
import smtplib
import ssl
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List

from config import Config


def c(value: str) -> str:
    return value.encode("ascii").decode("unicode_escape")


CN_FROM_NAME = c(r"\u671f\u520a\u72b6\u6001\u76d1\u63a7")
CN_TEST_SUBJECT = c(r"\u671f\u520a\u72b6\u6001\u76d1\u63a7\u6d4b\u8bd5\u90ae\u4ef6")
CN_CHANGE_SUBJECT = c(r"\u671f\u520a\u7a3f\u4ef6\u72b6\u6001\u66f4\u65b0\u901a\u77e5")
# A brand-new manuscript and a status change are different events for the owner,
# so they must be distinguishable from the inbox list alone.
CN_NEW_SUBJECT = c(r"\u671f\u520a\u7a3f\u4ef6\u65b0\u7a3f\u63d0\u9192")
CN_ALERT_SUBJECT = c(r"\u671f\u520a\u76d1\u63a7\u672a\u68c0\u67e5\u6210\u529f")
CN_NOT_CHECKED_NOTE = c(
    r"\u672c\u8f6e\u6ca1\u6709\u68c0\u67e5\uff0c\u4e0d\u4ee3\u8868\u7a3f\u4ef6\u6ca1\u6709\u53d8\u5316\u3002"
)
CN_DAILY_SUBJECT = c(r"\u671f\u520a\u7a3f\u4ef6\u6bcf\u5468\u72b6\u6001\u62a5\u544a")
CN_GENERATED_AT = c(r"\u751f\u6210\u65f6\u95f4")
CN_EMAIL_CONFIG_AVAILABLE = c(r"\u90ae\u4ef6\u901a\u77e5\u914d\u7f6e\u5df2\u7ecf\u53ef\u7528")
CN_TEST_RESULT = c(r"\u6d4b\u8bd5\u7ed3\u679c")
CN_SEND_SUCCESS = c(r"\u53d1\u9001\u6210\u529f")
CN_TEST_TEXT = c(r"\u5982\u679c\u4f60\u6536\u5230\u8fd9\u5c01\u90ae\u4ef6\uff0c\u8bf4\u660e\u90ae\u4ef6\u901a\u77e5\u914d\u7f6e\u5df2\u7ecf\u53ef\u7528\u3002")
CN_TEST_DETAIL = c(r"\u5982\u679c\u4f60\u6536\u5230\u8fd9\u5c01\u90ae\u4ef6\uff0c\u8bf4\u660e GitHub Actions \u4e2d\u7684\u90ae\u4ef6\u901a\u77e5\u914d\u7f6e\u5df2\u7ecf\u751f\u6548\u3002")
CN_PAPER_COUNT_UNIT = c(r"\u7bc7")
CN_SOURCE = c(r"\u6765\u6e90")
CN_MANUSCRIPT_ID = c(r"\u7a3f\u4ef6 ID")
CN_STATUS_CHANGE = c(r"\u72b6\u6001\u53d8\u5316")
CN_CHANGED_AT = c(r"\u53d8\u5316\u65f6\u95f4")
CN_CURRENT_STATUS = c(r"\u5f53\u524d\u72b6\u6001")
CN_CHECKED_AT = c(r"\u68c0\u67e5\u65f6\u95f4")
CN_SUBMISSION_SYSTEM = c(r"\u6295\u7a3f\u7cfb\u7edf")
CN_OPEN_SUBMISSION_SYSTEM = c(r"\u6253\u5f00\u6295\u7a3f\u7cfb\u7edf")
CN_CHANGE_SUBTITLE_PREFIX = c(r"\u68c0\u6d4b\u5230")
CN_CHANGE_SUBTITLE_SUFFIX = c(r"\u7bc7\u7a3f\u4ef6\u72b6\u6001\u53d1\u751f\u53d8\u5316")
CN_DAILY_SUBTITLE_PREFIX = c(r"\u5f53\u524d\u76d1\u63a7")
CN_DAILY_SUBTITLE_SUFFIX = c(r"\u7bc7\u7a3f\u4ef6")
CN_FOOTER_PREFIX = c(r"\u672c\u90ae\u4ef6\u7531\u671f\u520a\u72b6\u6001\u76d1\u63a7\u7a0b\u5e8f\u81ea\u52a8\u53d1\u9001\u3002\u751f\u6210\u65f6\u95f4")


class EmailNotifier:
    def __init__(self) -> None:
        self.sender = Config.EMAIL_SENDER
        self.password = Config.EMAIL_PASSWORD
        self.receivers = Config.EMAIL_RECEIVERS
        self.smtp_host, self.smtp_port = Config.get_smtp_config()

    def send_test_email(self) -> bool:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        subject = CN_TEST_SUBJECT
        text = "\n".join(
            [
                CN_TEST_SUBJECT,
                "=" * 24,
                f"{CN_GENERATED_AT}: {now}",
                f"SMTP: {self.smtp_host}:{self.smtp_port}",
                CN_TEST_TEXT,
            ]
        )
        body = (
            self._summary_card(
                CN_TEST_RESULT,
                CN_SEND_SUCCESS,
                CN_TEST_DETAIL,
            )
            + self._meta_grid(
                [
                    (CN_GENERATED_AT, now),
                    ("SMTP", f"{self.smtp_host}:{self.smtp_port}"),
                ]
            )
        )
        return self._send(subject, text, self._page(CN_TEST_SUBJECT, CN_EMAIL_CONFIG_AVAILABLE, body))

    def send_change_notification(self, changed_manuscripts: List[Dict]) -> bool:
        if not changed_manuscripts:
            print("No status changes to notify.")
            return True
        new_count = sum(1 for item in changed_manuscripts if item.get("is_new"))
        change_count = len(changed_manuscripts) - new_count
        if new_count and change_count:
            subject = (
                f"{CN_NEW_SUBJECT}\uff08{new_count}{CN_PAPER_COUNT_UNIT}"
                f" / {CN_STATUS_CHANGE}{change_count}{CN_PAPER_COUNT_UNIT}\uff09"
            )
        elif new_count:
            subject = f"{CN_NEW_SUBJECT}\uff08{new_count}{CN_PAPER_COUNT_UNIT}\uff09"
        else:
            subject = f"{CN_CHANGE_SUBJECT}\uff08{change_count}{CN_PAPER_COUNT_UNIT}\uff09"
        return self._send(
            subject,
            self._generate_change_text(changed_manuscripts),
            self._generate_change_html(changed_manuscripts),
        )

    def send_daily_report(self, all_manuscripts: List[Dict]) -> bool:
        if not all_manuscripts:
            # Returning True here used to mark the report as sent even though no
            # email left the process, silently swallowing that day's report.
            print("No manuscripts available for the daily report; nothing sent.")
            return False
        subject = f"{CN_DAILY_SUBJECT}\uff08{len(all_manuscripts)}{CN_PAPER_COUNT_UNIT}\uff09"
        return self._send(
            subject,
            self._generate_daily_text(all_manuscripts),
            self._generate_daily_html(all_manuscripts),
        )

    def send_failure_alert(self, problems: List[str]) -> bool:
        """Tell the user a monitoring run failed. Silence is the worst outcome."""
        if not problems:
            return True
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        subject = f"{CN_ALERT_SUBJECT}\uff08{len(problems)}\u9879\uff09"
        text = "\n".join(
            [subject, "=" * 28, CN_NOT_CHECKED_NOTE, f"{CN_GENERATED_AT}: {now}", ""]
            + [f"- {p}" for p in problems]
        )
        body = self._summary_card(
            CN_TEST_RESULT,
            "\u76d1\u63a7\u8fd0\u884c\u5931\u8d25",
            f"{CN_NOT_CHECKED_NOTE}\u4ee5\u4e0b\u73af\u8282\u6ca1\u6709\u5b8c\u6210\uff0c\u6700\u65b0\u72b6\u6001\u53ef\u80fd\u672a\u88ab\u68c0\u6d4b\u5230\u3002",
        )
        body += "".join(f'<div class="item"><div class="meta">{html.escape(str(p))}</div></div>' for p in problems)
        body += self._meta_grid([(CN_GENERATED_AT, now), ("SMTP", f"{self.smtp_host}:{self.smtp_port}")])
        return self._send(subject, text, self._page(subject, "\u76d1\u63a7\u5931\u8d25", body))

    def _send(self, subject: str, text_body: str, html_body: str) -> bool:
        message = MIMEMultipart("alternative")
        message["From"] = Header(f"{CN_FROM_NAME} <{self.sender}>", "utf-8")
        message["To"] = Header(", ".join(self.receivers), "utf-8")
        message["Subject"] = Header(subject, "utf-8")
        message.attach(MIMEText(text_body, "plain", "utf-8"))
        message.attach(MIMEText(html_body, "html", "utf-8"))

        try:
            print(f"Sending email to {', '.join(self.receivers)} through {self.smtp_host}:{self.smtp_port}...")
            if self.smtp_port == 465:
                with smtplib.SMTP_SSL(
                self.smtp_host, self.smtp_port, timeout=30, context=ssl.create_default_context()
            ) as server:
                    server.login(self.sender, self.password)
                    server.sendmail(self.sender, self.receivers, message.as_string())
            else:
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as server:
                    server.starttls(context=ssl.create_default_context())
                    server.login(self.sender, self.password)
                    server.sendmail(self.sender, self.receivers, message.as_string())
            print("Email sent.")
            return True
        except Exception as exc:
            print(f"Email send failed: {exc}")
            return False

    @staticmethod
    def _page(title: str, subtitle: str, body: str) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        footer = CN_FOOTER_PREFIX
        return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{ margin:0; padding:0; background:#eef3f1; color:#1f2933; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif; line-height:1.65; }}
    .wrap {{ max-width:880px; margin:0 auto; padding:28px 16px; }}
    .panel {{ background:#fff; border:1px solid #dce7e3; border-radius:8px; overflow:hidden; box-shadow:0 14px 36px rgba(15,82,76,.10); }}
    .hero {{ background:#0f766e; color:#fff; padding:28px 32px; }}
    .hero h1 {{ margin:0; font-size:24px; line-height:1.35; font-weight:760; }}
    .hero p {{ margin:8px 0 0; color:#d7fffa; font-size:15px; }}
    .content {{ padding:24px 28px 30px; }}
    .summary-card {{ background:#f4faf8; border:1px solid #cfe5df; border-radius:8px; padding:16px 18px; margin-bottom:18px; }}
    .summary-label {{ color:#52706b; font-size:13px; letter-spacing:.02em; }}
    .summary-value {{ margin-top:4px; color:#0f766e; font-size:22px; font-weight:760; }}
    .item {{ border:1px solid #dfe8e5; border-left:5px solid #0f766e; background:#fbfdfc; padding:18px 18px 16px; margin:14px 0; border-radius:8px; }}
    .title {{ color:#111827; font-size:16px; font-weight:740; margin-bottom:12px; }}
    .meta {{ color:#42526a; font-size:14px; margin:6px 0; }}
    .label {{ color:#6b7a90; display:inline-block; min-width:72px; }}
    .badge {{ display:inline-block; padding:2px 8px; border-radius:999px; background:#e6f4f1; color:#0f766e; font-size:12px; font-weight:700; vertical-align:middle; }}
    .status {{ color:#047857; font-weight:760; }}
    .old {{ color:#b42318; text-decoration:line-through; }}
    .new {{ color:#047857; font-weight:760; }}
    .button {{ display:inline-block; margin-top:10px; padding:8px 12px; color:#fff !important; background:#0f766e; text-decoration:none; border-radius:6px; font-size:14px; font-weight:700; }}
    .meta-grid div {{ background:#f8fbfa; border:1px solid #e0ebe8; border-radius:8px; padding:12px 14px; margin-bottom:10px; }}
    .meta-grid span {{ display:block; color:#6b7a90; font-size:13px; }}
    .meta-grid strong {{ display:block; color:#1f2933; margin-top:3px; word-break:break-word; }}
    .footer {{ color:#73837f; border-top:1px solid #e4ece9; margin-top:24px; padding-top:14px; font-size:12px; }}
  </style>
</head>
<body>
  <div class="wrap"><div class="panel">
    <div class="hero"><h1>{html.escape(title)}</h1><p>{html.escape(subtitle)}</p></div>
    <div class="content">{body}<div class="footer">{html.escape(footer)}: {html.escape(now)}</div></div>
  </div></div>
</body>
</html>"""

    @staticmethod
    def _summary_card(label: str, value: str, detail: str) -> str:
        return f"""<div class="summary-card">
  <div class="summary-label">{html.escape(label)}</div>
  <div class="summary-value">{html.escape(value)}</div>
  <p>{html.escape(detail)}</p>
</div>"""

    @staticmethod
    def _meta_grid(items: list[tuple[str, str]]) -> str:
        rows = [
            f"<div><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>"
            for label, value in items
        ]
        return '<div class="meta-grid">' + "".join(rows) + "</div>"

    def _generate_change_html(self, manuscripts: List[Dict]) -> str:
        rows = []
        for index, item in enumerate(manuscripts, 1):
            rows.append(
                f"""<div class="item">
  <div class="title">{index}. {html.escape(str(item.get("title", "Untitled")))}</div>
  {self._field(CN_SOURCE, f'<span class="badge">{html.escape(str(item.get("source", "")))}</span>', raw=True)}
  {self._field(CN_MANUSCRIPT_ID, html.escape(str(item.get("id", ""))), raw=True)}
  {self._field(CN_STATUS_CHANGE, f'<span class="old">{html.escape(str(item.get("old_status", "")))}</span> -> <span class="new">{html.escape(str(item.get("new_status", "")))}</span>', raw=True)}
  {self._field(CN_CHANGED_AT, html.escape(str(item.get("changed_at", ""))), raw=True)}
  {self._link_html(item.get("url"))}
</div>"""
            )
        subtitle = f"{CN_CHANGE_SUBTITLE_PREFIX} {len(manuscripts)} {CN_CHANGE_SUBTITLE_SUFFIX}"
        return self._page(CN_CHANGE_SUBJECT, subtitle, "\n".join(rows))

    def _generate_daily_html(self, manuscripts: List[Dict]) -> str:
        rows = []
        for index, item in enumerate(manuscripts, 1):
            rows.append(
                f"""<div class="item">
  <div class="title">{index}. {html.escape(str(item.get("title", "Untitled")))}</div>
  {self._field(CN_SOURCE, f'<span class="badge">{html.escape(str(item.get("source", "")))}</span>', raw=True)}
  {self._field(CN_MANUSCRIPT_ID, html.escape(str(item.get("id", ""))), raw=True)}
  {self._field(CN_CURRENT_STATUS, f'<span class="status">{html.escape(str(item.get("status", "")))}</span>', raw=True)}
  {self._field(CN_CHECKED_AT, html.escape(str(item.get("last_checked", ""))), raw=True)}
  {self._link_html(item.get("url"))}
</div>"""
            )
        subtitle = f"{CN_DAILY_SUBTITLE_PREFIX} {len(manuscripts)} {CN_DAILY_SUBTITLE_SUFFIX}"
        return self._page(CN_DAILY_SUBJECT, subtitle, "\n".join(rows))

    @staticmethod
    def _field(label: str, value: str, raw: bool = False) -> str:
        rendered = value if raw else html.escape(value)
        return f'<div class="meta"><span class="label">{html.escape(label)}</span>{rendered}</div>'

    @staticmethod
    def _link_html(url: object) -> str:
        if not url:
            return ""
        safe_url = html.escape(str(url), quote=True)
        return f'<a class="button" href="{safe_url}">{CN_OPEN_SUBMISSION_SYSTEM}</a>'

    @staticmethod
    def _generate_change_text(manuscripts: List[Dict]) -> str:
        lines = [CN_CHANGE_SUBJECT, "=" * 28, ""]
        for index, item in enumerate(manuscripts, 1):
            lines.extend(
                [
                    f"{index}. {item.get('title', 'Untitled')}",
                    f"   {CN_SOURCE}: {item.get('source', '')}",
                    f"   {CN_MANUSCRIPT_ID}: {item.get('id', '')}",
                    f"   {CN_STATUS_CHANGE}: {item.get('old_status', '')} -> {item.get('new_status', '')}",
                    f"   {CN_CHANGED_AT}: {item.get('changed_at', '')}",
                    f"   {CN_SUBMISSION_SYSTEM}: {item.get('url', '')}",
                    "",
                ]
            )
        return "\n".join(lines)

    @staticmethod
    def _generate_daily_text(manuscripts: List[Dict]) -> str:
        lines = [CN_DAILY_SUBJECT, "=" * 26, ""]
        for index, item in enumerate(manuscripts, 1):
            lines.extend(
                [
                    f"{index}. {item.get('title', 'Untitled')}",
                    f"   {CN_SOURCE}: {item.get('source', '')}",
                    f"   {CN_MANUSCRIPT_ID}: {item.get('id', '')}",
                    f"   {CN_CURRENT_STATUS}: {item.get('status', '')}",
                    f"   {CN_CHECKED_AT}: {item.get('last_checked', '')}",
                    f"   {CN_SUBMISSION_SYSTEM}: {item.get('url', '')}",
                    "",
                ]
            )
        return "\n".join(lines)
