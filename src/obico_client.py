"""
Obico Server Client — pulls failure detection data from a self-hosted Obico instance.

Uses Django session auth (CSRF + cookie) to talk to the Obico REST API.
Caches the session and refreshes on 401/403.
"""

import logging
import re
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class ObicoClient:
    """Polls a self-hosted Obico server for AI failure detection data for a printer."""

    def __init__(self, server_url: str, username: str, password: str, printer_id: int):
        self.server_url = server_url.rstrip("/")
        self.username = username
        self.password = password
        self.printer_id = printer_id

        self._session: Optional[requests.Session] = None
        self._lock = threading.Lock()
        self._last_data: dict = {}

    def _login(self) -> bool:
        """Authenticate via Django session login."""
        try:
            s = requests.Session()
            r = s.get(f"{self.server_url}/accounts/login/", timeout=10)
            if r.status_code != 200:
                logger.warning(f"Obico login page returned {r.status_code}")
                return False

            csrf_match = re.search(
                r'csrfmiddlewaretoken.*?value=["\x27]([^"\x27]+)', r.text
            )
            if not csrf_match:
                logger.warning("Obico: no CSRF token found on login page")
                return False

            r2 = s.post(
                f"{self.server_url}/accounts/login/",
                data={
                    "csrfmiddlewaretoken": csrf_match.group(1),
                    "login": self.username,
                    "password": self.password,
                },
                headers={"Referer": f"{self.server_url}/accounts/login/"},
                timeout=10,
                allow_redirects=False,
            )
            if r2.status_code not in (301, 302):
                logger.warning(f"Obico login failed: {r2.status_code}")
                return False

            self._session = s
            logger.info(f"Obico: authenticated to {self.server_url}")
            return True
        except Exception as e:
            logger.warning(f"Obico login error: {e}")
            return False

    def _ensure_session(self) -> bool:
        with self._lock:
            if self._session is None:
                return self._login()
        return True

    def fetch_status(self) -> dict:
        """Fetch printer status and latest failure detection score from Obico.

        Returns a dict with keys: connected, watching, normalized_p,
        snapshot_url, state, current_print.
        """
        if not self._ensure_session():
            return {"connected": False}

        try:
            r = self._session.get(
                f"{self.server_url}/api/v1/printers/{self.printer_id}/",
                timeout=10,
            )
            if r.status_code in (401, 403):
                # Session expired, re-login
                with self._lock:
                    self._session = None
                if not self._ensure_session():
                    return {"connected": False}
                r = self._session.get(
                    f"{self.server_url}/api/v1/printers/{self.printer_id}/",
                    timeout=10,
                )

            if r.status_code != 200:
                return {"connected": False}

            data = r.json()
            status = data.get("status", {})
            pic = data.get("pic", {})
            current_print = data.get("current_print")

            result = {
                "connected": True,
                "watching": data.get("watching_enabled", False),
                "action_on_failure": data.get("action_on_failure", ""),
                "sensitivity": data.get("detective_sensitivity", 1.0),
                "state": status.get("state", {}).get("text", ""),
                "snapshot_url": pic.get("img_url", ""),
            }

            # Get the latest prediction score from the current/latest print
            if current_print:
                print_id = current_print.get("id")
                result["current_print"] = {
                    "id": print_id,
                    "filename": current_print.get("filename", ""),
                    "started_at": current_print.get("started_at", ""),
                }
                if print_id:
                    result.update(self._fetch_prediction(print_id))
            else:
                result["normalized_p"] = 0
                result["current_print"] = None

            self._last_data = result
            return result

        except Exception as e:
            logger.debug(f"Obico fetch error: {e}")
            return self._last_data if self._last_data else {"connected": False}

    def _fetch_prediction(self, print_id: int) -> dict:
        """Get the latest AI detection score for a print."""
        try:
            r = self._session.get(
                f"{self.server_url}/api/v1/prints/{print_id}/prediction_json/",
                timeout=5,
            )
            if r.status_code == 200:
                predictions = r.json()
                if predictions:
                    latest = predictions[-1]
                    fields = latest.get("fields", latest)
                    return {
                        "normalized_p": fields.get("normalized_p", 0),
                        "current_p": fields.get("current_p", 0),
                        "ewm_mean": fields.get("ewm_mean", 0),
                    }
        except Exception as e:
            logger.debug(f"Obico prediction fetch error: {e}")
        return {"normalized_p": 0}
