"""
Spoolman Client — HTTP client for the Spoolman filament management REST API.

Connects to a Spoolman instance to track filament spools, vendors, and
usage. When a print completes, filament consumption is reported back to
Spoolman so spool weights stay accurate.

Spoolman API docs: https://donkie.github.io/Spoolman/
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10  # seconds


class SpoolmanClient:
    """HTTP client for the Spoolman REST API v1."""

    def __init__(self, base_url: str):
        """
        Args:
            base_url: Root URL of the Spoolman instance, e.g. "http://localhost:7912"
        """
        # Ensure base URL ends with /api/v1/
        self.base_url = base_url.rstrip("/")
        self._api = self.base_url + "/api/v1/"
        self._session = requests.Session()
        self._session.headers["Content-Type"] = "application/json"

    # ── Helpers ───────────────────────────────────────────

    def _url(self, path: str) -> str:
        return urljoin(self._api, path.lstrip("/"))

    def _get(self, path: str, params: dict = None) -> Optional[dict | list]:
        try:
            r = self._session.get(self._url(path), params=params, timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            logger.error(f"Spoolman GET {path} failed: {e}")
            return None

    def _post(self, path: str, json: dict = None) -> Optional[dict]:
        try:
            r = self._session.post(self._url(path), json=json, timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            logger.error(f"Spoolman POST {path} failed: {e}")
            return None

    def _patch(self, path: str, json: dict = None) -> Optional[dict]:
        try:
            r = self._session.patch(self._url(path), json=json, timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            logger.error(f"Spoolman PATCH {path} failed: {e}")
            return None

    def _put(self, path: str, json: dict = None) -> Optional[dict]:
        try:
            r = self._session.put(self._url(path), json=json, timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            logger.error(f"Spoolman PUT {path} failed: {e}")
            return None

    def _delete(self, path: str) -> bool:
        try:
            r = self._session.delete(self._url(path), timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            return True
        except requests.RequestException as e:
            logger.error(f"Spoolman DELETE {path} failed: {e}")
            return False

    # ── Health / Info ─────────────────────────────────────

    def health(self) -> Optional[dict]:
        """Check Spoolman health status."""
        return self._get("health")

    def info(self) -> Optional[dict]:
        """Get Spoolman server info."""
        return self._get("info")

    # ── Vendors ───────────────────────────────────────────

    def get_vendors(self, **kwargs) -> Optional[list]:
        """List vendors. Optional params: name, sort, limit, offset."""
        return self._get("vendor", params=kwargs or None)

    def get_vendor(self, vendor_id: int) -> Optional[dict]:
        return self._get(f"vendor/{vendor_id}")

    def create_vendor(self, data: dict) -> Optional[dict]:
        return self._post("vendor", json=data)

    def update_vendor(self, vendor_id: int, data: dict) -> Optional[dict]:
        return self._patch(f"vendor/{vendor_id}", json=data)

    def delete_vendor(self, vendor_id: int) -> bool:
        return self._delete(f"vendor/{vendor_id}")

    # ── Filaments ─────────────────────────────────────────

    def get_filaments(self, **kwargs) -> Optional[list]:
        """List filaments. Optional params: name, material, vendor.name, sort, limit, offset."""
        return self._get("filament", params=kwargs or None)

    def get_filament(self, filament_id: int) -> Optional[dict]:
        return self._get(f"filament/{filament_id}")

    def create_filament(self, data: dict) -> Optional[dict]:
        return self._post("filament", json=data)

    def update_filament(self, filament_id: int, data: dict) -> Optional[dict]:
        return self._patch(f"filament/{filament_id}", json=data)

    def delete_filament(self, filament_id: int) -> bool:
        return self._delete(f"filament/{filament_id}")

    # ── Spools ────────────────────────────────────────────

    def get_spools(self, **kwargs) -> Optional[list]:
        """
        List spools.
        Optional params: filament.name, filament.material, filament.vendor.name,
                         location, allow_archived, sort, limit, offset.
        """
        # Spoolman expects lowercase 'true'/'false' for boolean query params
        params = {}
        for k, v in kwargs.items():
            params[k] = str(v).lower() if isinstance(v, bool) else v
        return self._get("spool", params=params or None)

    def get_spool(self, spool_id: int) -> Optional[dict]:
        return self._get(f"spool/{spool_id}")

    def create_spool(self, data: dict) -> Optional[dict]:
        return self._post("spool", json=data)

    def update_spool(self, spool_id: int, data: dict) -> Optional[dict]:
        return self._patch(f"spool/{spool_id}", json=data)

    def delete_spool(self, spool_id: int) -> bool:
        return self._delete(f"spool/{spool_id}")

    def use_spool(self, spool_id: int, use_weight: float = None, use_length: float = None) -> Optional[dict]:
        """
        Consume filament from a spool.
        Specify either use_weight (grams) or use_length (mm), not both.
        """
        payload = {}
        if use_weight is not None:
            payload["use_weight"] = use_weight
        if use_length is not None:
            payload["use_length"] = use_length
        return self._put(f"spool/{spool_id}/use", json=payload)

    # ── Convenience ───────────────────────────────────────

    def get_spools_by_location(self, location: str) -> Optional[list]:
        """Get all spools at a given location (e.g. a printer name).

        Wraps the name in quotes to force an exact match in Spoolman's
        partial-search location filter.
        """
        return self.get_spools(location=f'"{location}"')

    def get_active_spools(self) -> Optional[list]:
        """Get all non-archived spools."""
        return self.get_spools(allow_archived=False)

    def find_matching_spool(self, material: str, color_hex: str = None) -> Optional[dict]:
        """
        Find a spool that matches the given material (and optionally color).
        Returns the spool with the most remaining filament, or None.
        """
        params = {"filament.material": material, "allow_archived": False}
        spools = self.get_spools(**params)
        if not spools:
            return None

        # Filter by color if requested
        if color_hex:
            color_hex = color_hex.lstrip("#").upper()
            spools = [
                s for s in spools
                if s.get("filament", {}).get("color_hex", "").upper() == color_hex
            ]

        if not spools:
            return None

        # Pick spool with most remaining weight
        return max(spools, key=lambda s: s.get("remaining_weight") or 0)
