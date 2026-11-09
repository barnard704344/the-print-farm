"""Klipper: upload clears stale layer counts; temperature routes validate input."""
import os
import tempfile
import unittest
from unittest import mock

from src.klipper_client import KlipperClient
from src.web import create_app
from test_security import FakeFarm, FakeQueue


class LayerCacheTests(unittest.TestCase):
    def test_upload_forgets_cached_layer_count_for_that_name(self):
        client = KlipperClient.__new__(KlipperClient)
        client.name = "Voron"
        client._base_url = "http://voron"
        client._layer_cache = {"Plate_for_42.gcode": 120, "other.gcode": 50}
        client._session = mock.Mock()
        client._session.post.return_value = mock.Mock(status_code=201)
        with tempfile.NamedTemporaryFile(suffix=".gcode") as fh:
            self.assertTrue(client.upload_file(fh.name, "Plate_for_42.gcode"))
        self.assertNotIn("Plate_for_42.gcode", client._layer_cache)
        self.assertEqual(client._layer_cache["other.gcode"], 50)


class TempRouteTests(unittest.TestCase):
    def test_non_numeric_temperatures_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"web": {"secret_key": "t"}, "printers": [{"name": "Printer-1", "type": "klipper"}],
                      "student_access": {"allowlist": [], "banlist": []}}
            app = create_app(FakeFarm(), FakeQueue(tmp), config=config)
            client = app.test_client()
            with client.session_transaction() as s:
                s.update(role="staff", admin=True, username="teacher")
            for endpoint in ("nozzle_temp", "bed_temp"):
                res = client.post(f"/api/printer/Printer-1/{endpoint}", json={"temp": "hot"})
                self.assertEqual(res.status_code, 400, endpoint)
