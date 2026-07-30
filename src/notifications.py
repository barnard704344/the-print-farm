"""
Notification system for The Print Farm.
Supports email (SMTP) and Discord (webhook) notifications.
"""

import logging
import smtplib
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

logger = logging.getLogger(__name__)


class NotificationManager:
    """Sends notifications via email and/or Discord based on config."""

    def __init__(self, config: dict):
        self._config = config

    @property
    def _notif_cfg(self):
        return self._config.get("notifications", {})

    def _email_cfg(self):
        return self._notif_cfg.get("email", {})

    def _discord_cfg(self):
        return self._notif_cfg.get("discord", {})

    def _event_enabled(self, event: str) -> bool:
        events = self._notif_cfg.get("events", {})
        return events.get(event, False)

    def notify(self, event: str, subject: str, message: str):
        """Send notification for an event (non-blocking)."""
        if not self._notif_cfg.get("enabled", False):
            return
        if not self._event_enabled(event):
            return
        # Fire in background thread to avoid blocking the main loop
        t = threading.Thread(
            target=self._send_all, args=(subject, message), daemon=True
        )
        t.start()

    def _send_all(self, subject: str, message: str):
        email_cfg = self._email_cfg()
        if email_cfg.get("enabled", False):
            try:
                self._send_email(email_cfg, subject, message)
            except Exception as e:
                logger.error(f"Email notification failed: {e}")

        discord_cfg = self._discord_cfg()
        if discord_cfg.get("enabled", False):
            try:
                self._send_discord(discord_cfg, subject, message)
            except Exception as e:
                logger.error(f"Discord notification failed: {e}")

    def _send_email(self, cfg: dict, subject: str, body: str):
        smtp_host = cfg.get("smtp_host", "")
        smtp_port = cfg.get("smtp_port", 587)
        username = cfg.get("username", "")
        password = cfg.get("password", "")
        from_addr = cfg.get("from_address", username)
        to_addrs = cfg.get("to_addresses", [])
        use_tls = cfg.get("use_tls", True)

        if not smtp_host or not to_addrs:
            return

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = ", ".join(to_addrs)
        msg.attach(MIMEText(body, "plain"))

        if use_tls:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
            server.starttls()
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)

        if username and password:
            server.login(username, password)
        server.sendmail(from_addr, to_addrs, msg.as_string())
        server.quit()
        logger.info(f"Email sent: {subject}")

    def _send_discord(self, cfg: dict, subject: str, message: str):
        webhook_url = cfg.get("webhook_url", "")
        if not webhook_url:
            return

        payload = {
            "embeds": [{
                "title": subject,
                "description": message,
                "color": 0x00B894,  # green accent
            }]
        }

        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info(f"Discord notification sent: {subject}")

    def test_email(self) -> dict:
        """Send a test email. Returns {ok, message}."""
        cfg = self._email_cfg()
        if not cfg.get("enabled"):
            return {"ok": False, "message": "Email notifications not enabled"}
        try:
            self._send_email(cfg, "The Print Farm — Test Email", "This is a test notification from The Print Farm.")
            return {"ok": True, "message": "Test email sent successfully"}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def test_discord(self) -> dict:
        """Send a test Discord message. Returns {ok, message}."""
        cfg = self._discord_cfg()
        if not cfg.get("enabled"):
            return {"ok": False, "message": "Discord notifications not enabled"}
        try:
            self._send_discord(cfg, "The Print Farm — Test", "This is a test notification from The Print Farm.")
            return {"ok": True, "message": "Test Discord notification sent"}
        except Exception as e:
            return {"ok": False, "message": str(e)}
