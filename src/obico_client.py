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
from urllib.parse import urljoin, urlsplit

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
        self._last_error = ""

    @property
    def last_error(self) -> str:
        return self._last_error

    def _login(self) -> bool:
        """Authenticate via Django session login."""
        try:
            s = requests.Session()
            r = s.get(f"{self.server_url}/accounts/login/", timeout=10)
            if r.status_code != 200:
                self._last_error = f"Obico login page returned HTTP {r.status_code}"
                logger.warning(f"Obico login page returned {r.status_code}")
                return False

            csrf_match = re.search(
                r'csrfmiddlewaretoken.*?value=["\x27]([^"\x27]+)', r.text
            )
            if not csrf_match:
                self._last_error = "Obico login page did not provide a CSRF token"
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
                self._last_error = f"Obico login was rejected (HTTP {r2.status_code})"
                logger.warning(f"Obico login failed: {r2.status_code}")
                return False

            self._session = s
            self._last_error = ""
            logger.info(f"Obico: authenticated to {self.server_url}")
            return True
        except Exception as e:
            self._last_error = f"Obico login error: {e}"
            logger.warning(f"Obico login error: {e}")
            return False

    def _ensure_session(self) -> bool:
        with self._lock:
            if self._session is None:
                return self._login()
        return True

    def ensure_printer(self, name: str, registration_key: str) -> dict:
        """Register a farm printer, reusing this installation/device on retry.

        The caller keeps the returned agent token private in server configuration.
        This never issues a command to the physical printer.
        """
        if not self._ensure_session():
            raise ValueError("Could not sign into Obico; check the address and login")
        try:
            endpoint = self.server_url + "/api/v1/printers/"
            next_url, seen, matches = endpoint, set(), []
            while next_url:
                parts, expected = urlsplit(next_url), urlsplit(endpoint)
                if ((parts.scheme, parts.netloc, parts.path) != (expected.scheme, expected.netloc, expected.path)
                        or next_url in seen):
                    raise ValueError("Obico returned an invalid printer-list page")
                seen.add(next_url)
                r = self._session.get(next_url, timeout=10, allow_redirects=False)
                if r.status_code != 200:
                    raise ValueError(f"Could not list Obico printers (HTTP {r.status_code})")
                data = r.json()
                printers = data.get("results", []) if isinstance(data, dict) else data
                if not isinstance(printers, list):
                    raise ValueError("Obico returned an invalid printer list")
                matches.extend(p for p in printers
                               if p.get("agent_name") == "the_print_farm"
                               and str(p.get("agent_version") or "").partition("+")[2] == registration_key)
                next_page = data.get("next") if isinstance(data, dict) else None
                next_url = urljoin(endpoint, next_page) if next_page else None
            if len(matches) > 1:
                raise ValueError("Duplicate registration records found; select the correct ID in manual settings")
            if matches:
                printer = matches[0]
                if printer.get("agent_connected") and printer.get("agent_name") != "the_print_farm":
                    raise ValueError("That Obico printer is already connected to another agent")
            else:
                settings = {"name": name, "action_on_failure": "NONE",
                            "watching_enabled": True, "agent_name": "the_print_farm",
                            "agent_version": "1.0+" + registration_key}
                r = self._session.post(self.server_url + "/api/v1/printers/",
                                       json=settings,
                                       timeout=15, allow_redirects=False)
                if r.status_code == 405:
                    printer = self._register_with_linking_code(settings)
                elif r.status_code == 201:
                    printer = r.json()
                else:
                    raise ValueError(f"Could not create the Obico printer (HTTP {r.status_code})")
            if not printer.get("auth_token") or not printer.get("id"):
                raise ValueError("Obico did not return a printer ID and agent token")
            return printer
        except requests.RequestException:
            raise ValueError("Could not reach Obico; check the server address") from None

    def _register_with_linking_code(self, settings):
        """Compatibility with servers whose printer list does not support POST.

        Only request a NEW unassigned code in the signed-in account. Never verify
        a code attached to a printer: verification would rotate its live token.
        """
        r = self._session.get(self.server_url + "/api/v1/onetimeverificationcodes/",
                              timeout=10, allow_redirects=False)
        if r.status_code != 200:
            raise ValueError(f"Could not start Obico registration (HTTP {r.status_code})")
        data = r.json()
        code = str(data.get("code") or "")
        if data.get("printer") or data.get("verified_at") or len(code) != 6 or not code.isdigit():
            raise ValueError("Obico did not provide a new unassigned linking code")
        # This is the anonymous AGENT endpoint, not an account/session API.
        # Sending the signed-in cookie makes DRF enforce CSRF and reject the
        # agent request. Keep account cookies only for obtaining the code and
        # updating the owned printer; never disable server CSRF protection.
        r = requests.post(self.server_url + "/api/v1/octo/verify/",
                          params={"code": code}, timeout=15, allow_redirects=False)
        if r.status_code != 200:
            raise ValueError(f"Could not finish Obico registration (HTTP {r.status_code})")
        printer = r.json().get("printer") or {}
        if not printer.get("id") or not printer.get("auth_token"):
            raise ValueError("Obico did not return the registered printer")
        r = self._session.patch(self.server_url + f"/api/v1/printers/{int(printer['id'])}/",
                                json=settings, timeout=10, allow_redirects=False)
        if r.status_code != 200:
            raise ValueError(f"Obico printer was created, but its settings could not be applied (HTTP {r.status_code})")
        # Older servers acknowledge PATCH without returning a serialized printer.
        # Read the canonical record rather than treating the acknowledgement as
        # the ID/token payload. This also verifies the created record is readable.
        r = self._session.get(self.server_url + f"/api/v1/printers/{int(printer['id'])}/",
                              timeout=10, allow_redirects=False)
        if r.status_code != 200:
            raise ValueError(f"Could not read the registered Obico printer (HTTP {r.status_code})")
        return r.json()

    def fetch_status(self) -> dict:
        """Fetch printer status and latest failure detection score from Obico.

        Returns a dict with keys: connected, watching, normalized_p,
        snapshot_url, state, current_print.
        """
        if not self._ensure_session():
            return {"connected": False, "error": self._last_error or "Obico authentication failed"}

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
                    return {"connected": False, "error": self._last_error or "Obico authentication failed"}
                r = self._session.get(
                    f"{self.server_url}/api/v1/printers/{self.printer_id}/",
                    timeout=10,
                )

            if r.status_code != 200:
                if r.status_code == 404:
                    error = f"Obico printer ID {self.printer_id} was not found"
                else:
                    error = f"Obico printer request failed (HTTP {r.status_code})"
                self._last_error = error
                return {"connected": False, "error": error}

            data = r.json()
            status = data.get("status") or {}
            pic = data.get("pic") or {}
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
                raw_fn = current_print.get("filename", "")
                # Strip UUID prefix (32 hex chars + underscore) added during upload
                if len(raw_fn) > 33 and raw_fn[32] == "_" and all(c in "0123456789abcdef" for c in raw_fn[:32]):
                    raw_fn = raw_fn[33:]
                result["current_print"] = {
                    "id": print_id,
                    "filename": raw_fn,
                    "started_at": current_print.get("started_at", ""),
                }
                if print_id:
                    result.update(self._fetch_prediction(print_id))
            else:
                result["normalized_p"] = 0
                result["current_print"] = None

            self._last_data = result
            self._last_error = ""
            return result

        except Exception as e:
            self._last_error = f"Obico request error: {e}"
            logger.debug(f"Obico fetch error: {e}")
            if self._last_data:
                return {**self._last_data, "connected": False, "error": self._last_error}
            return {"connected": False, "error": self._last_error}

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
