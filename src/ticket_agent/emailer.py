"""Email alerts over SMTP (STARTTLS on 587, or SSL on 465). Credentials live only in .env."""

from __future__ import annotations

import html
import json
import logging
import re
import smtplib
from email.message import EmailMessage

from .config import Settings
from .db import DB

log = logging.getLogger(__name__)


def to_plain(text_html: str) -> str:
    text = re.sub(r'<a href="([^"]+)">([^<]+)</a>', r"\2: \1", text_html)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


class Emailer:
    def __init__(self, settings: Settings, db: DB, web_base_url: str):
        self.s, self.db, self.web_base_url = settings, db, web_base_url

    @property
    def configured(self) -> bool:
        s = self.s
        return bool(s.smtp_host and s.email_from and s.email_to)

    def send(self, to: list[str], subject: str, body_html: str) -> bool:
        s = self.s
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, s.email_from, ", ".join(to)
        msg.set_content(to_plain(body_html))
        msg.add_alternative(f'<div style="font:15px/1.5 system-ui,sans-serif">'
                            f'{body_html.replace(chr(10), "<br>")}</div>', subtype="html")
        try:
            smtp_cls = smtplib.SMTP_SSL if s.smtp_port == 465 else smtplib.SMTP
            with smtp_cls(s.smtp_host, s.smtp_port, timeout=30) as smtp:
                if s.smtp_port != 465:
                    smtp.starttls()
                if s.smtp_user and s.smtp_password:
                    smtp.login(s.smtp_user, s.smtp_password)
                smtp.send_message(msg)
            return True
        except (smtplib.SMTPException, OSError) as e:
            log.error("email to %s failed: %s", to, type(e).__name__)  # never log credentials
            return False

    def send_pending_alerts(self) -> int:
        """Send queued alerts. Unsent ones stay queued (e.g. before SMTP is set up)."""
        if not self.configured:
            return 0
        sent = 0
        for a in self.db.unsent_alerts():
            ev = self.db.event(a["event_id"])
            name = ev["name"] if ev else "event"
            subject = f"🎟 {name}: " + ", ".join(
                {"target": "under your target", "low": "price dropped",
                 "listings": "more seats listed"}.get(k, k) for k in json.loads(a["kinds"]))
            body = (a["text"] + f'\n\n<a href="{html.escape(self.web_base_url)}/events/{a["event_id"]}">'
                    "Dashboard</a>")
            if self.send(self.s.email_to, subject, body):
                self.db.mark_sent(a["id"])
                sent += 1
        return sent

    def send_admin(self, text: str) -> None:
        if self.s.admin_email and self.s.smtp_host and self.s.email_from:
            self.send([self.s.admin_email], "ticket-agent: maintenance", html.escape(text))
