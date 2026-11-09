"""Filament load/unload commands match Bambu Studio / Bambuddy, and are guarded."""
import tempfile
import threading
import unittest

from src.bambu_client import BambuClient, PrintState, PrintStatus, ams_global_tray_id
from src.web import create_app
from test_security import FakeFarm, FakeQueue


def make_client(status=PrintStatus.IDLE, tray_now=255, nozzle=28.0, trays=None, connected=True):
    client = BambuClient.__new__(BambuClient)
    client.name = "P1S-Test"
    client._state_lock = threading.Lock()
    client._state = PrintState()
    client._state.status = status
    client._state.ams_tray_now = tray_now
    client._state.nozzle_temper = nozzle
    client._state.ams_trays = trays or []
    client.is_connected = lambda: connected
    client.sent = []

    def capture(payload):
        client.sent.append(payload["print"])
        return True

    client._send_command = capture
    return client


def tray(unit, slot):
    return {"id": ams_global_tray_id(unit, slot), "unit": unit, "slot": slot}


class CommandShapeTests(unittest.TestCase):
    def test_load_regular_ams_slot(self):
        client = make_client(trays=[tray(1, 2)])
        self.assertTrue(client.ams_load_tray(6))
        cmd = client.sent[-1]
        self.assertEqual(cmd["command"], "ams_change_filament")
        self.assertEqual((cmd["ams_id"], cmd["slot_id"], cmd["target"]), (1, 2, 6))
        self.assertEqual((cmd["curr_temp"], cmd["tar_temp"]), (-1, -1))

    def test_load_ams_ht_uses_unit_id_as_target(self):
        client = make_client(trays=[tray(128, 0)])
        client.ams_load_tray(128)
        cmd = client.sent[-1]
        self.assertEqual((cmd["ams_id"], cmd["slot_id"], cmd["target"]), (128, 0, 128))

    def test_load_external_spool(self):
        client = make_client()
        client.load_filament()
        cmd = client.sent[-1]
        self.assertEqual((cmd["ams_id"], cmd["slot_id"], cmd["target"]), (255, 254, 254))
        self.assertEqual((cmd["curr_temp"], cmd["tar_temp"]), (-1, -1))
        self.assertNotIn("param", cmd)  # no raw G-code

    def test_unload_from_ams_uses_source_unit_and_nozzle_temp(self):
        client = make_client(tray_now=5, nozzle=245.4, trays=[tray(1, 1)])
        client.unload_filament()
        cmd = client.sent[-1]
        self.assertEqual((cmd["ams_id"], cmd["slot_id"], cmd["target"]), (1, 255, 255))
        self.assertEqual((cmd["curr_temp"], cmd["tar_temp"]), (245, 245))

    def test_unload_external_with_cold_nozzle_defaults_to_210(self):
        client = make_client(tray_now=254, nozzle=30)
        client.ams_load_tray(255)
        cmd = client.sent[-1]
        self.assertEqual((cmd["ams_id"], cmd["target"], cmd["curr_temp"]), (255, 255, 210))

    def test_only_a_running_print_blocks(self):
        self.assertIsNotNone(make_client(status=PrintStatus.RUNNING).filament_change_blocked())
        self.assertIsNone(make_client(status=PrintStatus.PAUSED).filament_change_blocked())
        self.assertIsNone(make_client(status=PrintStatus.PAUSE_FILAMENT).filament_change_blocked())
        self.assertIsNotNone(make_client(connected=False).filament_change_blocked())


class FilamentFarm(FakeFarm):
    def __init__(self, client, ptype="bambulab"):
        super().__init__()
        self.printer = client
        self.ptype = ptype

    def get_printer_type(self, name):
        return self.ptype


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _staff(self, client, ptype="bambulab"):
        config = {"web": {"secret_key": "t"}, "printers": [{"name": "Printer-1", "type": ptype}],
                  "student_access": {"allowlist": [], "banlist": []}}
        app = create_app(FilamentFarm(client, ptype), FakeQueue(self.temp_dir.name), config=config)
        http = app.test_client()
        with http.session_transaction() as s:
            s.update(role="staff", username="teacher", display_name="teacher")
        return http

    def test_running_print_refuses_load_and_unload(self):
        client = make_client(status=PrintStatus.RUNNING, trays=[tray(0, 0)])
        http = self._staff(client)
        for url, body in [("/api/printer/Printer-1/ams_load", {"tray_id": 0}),
                          ("/api/printer/Printer-1/load_filament", None),
                          ("/api/printer/Printer-1/unload_filament", None)]:
            res = http.post(url, json=body)
            self.assertEqual(res.status_code, 409, url)
        self.assertEqual(client.sent, [])

    def test_idle_load_sends_command(self):
        client = make_client(trays=[tray(0, 3)])
        res = self._staff(client).post("/api/printer/Printer-1/ams_load", json={"tray_id": 3})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(client.sent[-1]["target"], 3)

    def test_invalid_tray_rejected(self):
        res = self._staff(make_client()).post("/api/printer/Printer-1/ams_load", json={"tray_id": 99})
        self.assertEqual(res.status_code, 400)

    def test_klipper_printer_gets_clear_error(self):
        res = self._staff(object(), ptype="klipper").post("/api/printer/Printer-1/load_filament")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Bambu Lab", res.get_json()["message"])
