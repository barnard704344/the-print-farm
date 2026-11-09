"""Klipper macros: discovery, safe running, pinning and permissions."""
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from src.config_store import save_config
from src.klipper_client import KlipperClient
from src.web import create_app
from test_security import FakeFarm, FakeQueue

OBJECTS = {"result": {"objects": [
    "gcode_macro PURGE_LINE", "gcode_macro _HELPER", "gcode_macro PAUSE", "gcode_macro PRINT_START",
    "gcode_macro MMU_HOME", "gcode_macro preheat", "extruder",
]}}
SETTINGS = {"result": {"status": {"configfile": {"settings": {
    "gcode_macro purge_line": {"description": "Purge a line at the bed edge", "gcode": "G1 X10"},
    "gcode_macro preheat": {"description": "G-Code macro",
                            "gcode": "M104 S{params.TEMP|default(220)} M140 S{params.BED}"},
}}}}}


def response(payload=None, status=200):
    resp = mock.Mock(status_code=status)
    resp.json.return_value = payload or {}
    resp.raise_for_status = mock.Mock()
    return resp


def make_client():
    client = KlipperClient.__new__(KlipperClient)
    client.name = "Voron"
    client._base_url = "http://voron"
    client._macro_cache, client._macro_cache_at = [], 0.0
    client._connected = threading.Event()
    client._connected.set()
    client._session = mock.Mock()
    client._session.get.side_effect = lambda url, **kw: response(OBJECTS if url.endswith("/list") else SETTINGS)
    client._session.post.return_value = response()
    return client


class MacroClientTests(unittest.TestCase):
    def test_lists_user_macros_with_description_and_params(self):
        macros = {m["name"]: m for m in make_client().list_macros()}
        self.assertEqual(set(macros), {"PURGE_LINE", "PREHEAT"})
        self.assertEqual(macros["PURGE_LINE"]["description"], "Purge a line at the bed edge")
        self.assertEqual(macros["PREHEAT"]["description"], "")
        self.assertEqual(macros["PREHEAT"]["params"], [{"name": "TEMP", "default": "220"}, {"name": "BED", "default": ""}])

    def test_runs_macro_with_params(self):
        client = make_client()
        ok, message = client.run_macro("preheat", {"TEMP": "240", "BED": ""})
        self.assertTrue(ok, message)
        self.assertEqual(client._session.post.call_args.kwargs["params"], {"script": "PREHEAT TEMP=240"})

    def test_rejects_unknown_macros_and_unsafe_values(self):
        client = make_client()
        self.assertFalse(client.run_macro("FIRMWARE_RESTART")[0])
        self.assertFalse(client.run_macro("PAUSE")[0])
        self.assertFalse(client.run_macro("PREHEAT", {"TEMP": "240\nM112"})[0])
        self.assertFalse(client.run_macro("PREHEAT", {"TEMP": "240 M112"})[0])
        client._session.post.assert_not_called()

    def test_reports_klipper_errors(self):
        client = make_client()
        client._session.post.return_value = response({"error": {"message": "Must home axis first"}}, 400)
        self.assertEqual(client.run_macro("PURGE_LINE"), (False, "Must home axis first"))

    def test_long_macro_returns_started_without_waiting(self):
        client = make_client()
        client._session.post.side_effect = lambda *a, **k: (time.sleep(0.5), response())[1]
        start = time.monotonic()
        ok, message = client.run_macro("PURGE_LINE", wait=0.05)
        self.assertTrue(ok)
        self.assertIn("started", message)
        self.assertLess(time.monotonic() - start, 0.4)


class KlipperFarm(FakeFarm):
    def __init__(self, client):
        super().__init__()
        self.printer = client


class MacroRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmp.name, "config.yaml")
        self.env = mock.patch.dict(os.environ, {"FARM_CONFIG": self.config_path})
        self.env.start()
        self.config = {"web": {"secret_key": "t"},
                       "printers": [{"name": "Printer-1", "type": "klipper"}],
                       "student_access": {"allowlist": ["carol"], "banlist": []}}
        save_config(self.config_path, self.config)
        self.client = make_client()
        self.app = create_app(KlipperFarm(self.client), FakeQueue(self.tmp.name), config=self.config)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _http(self, role, username):
        http = self.app.test_client()
        with http.session_transaction() as s:
            s.update(role=role, username=username, admin=role == "staff")
        return http

    def test_approved_student_can_list_and_run(self):
        http = self._http("student", "carol")
        self.assertEqual(len(http.get("/api/printer/Printer-1/macros").get_json()["macros"]), 2)
        res = http.post("/api/printer/Printer-1/macros/run", json={"macro": "PURGE_LINE"})
        self.assertEqual(res.status_code, 200, res.get_json())

    def test_unapproved_student_cannot_run(self):
        res = self._http("student", "dave").post("/api/printer/Printer-1/macros/run", json={"macro": "PURGE_LINE"})
        self.assertEqual(res.status_code, 403)

    def test_only_staff_can_pin_and_pins_show_in_status(self):
        student = self._http("student", "carol")
        self.assertEqual(student.post("/api/printer/Printer-1/macros/pinned", json={"pinned": ["PURGE_LINE"]}).status_code, 403)
        staff = self._http("staff", "teacher")
        res = staff.post("/api/printer/Printer-1/macros/pinned", json={"pinned": ["purge_line", "PURGE_LINE"]})
        self.assertEqual(res.get_json()["pinned"], ["PURGE_LINE"])
        self.assertEqual(staff.get("/api/printer/Printer-1/macros").get_json()["pinned"], ["PURGE_LINE"])
        self.assertEqual(staff.post("/api/printer/Printer-1/macros/pinned", json={"pinned": ["BAD NAME"]}).status_code, 400)
