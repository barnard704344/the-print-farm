"""Klipper: errors that stop a print reach the farm as FAILED with a reason."""
import unittest
from unittest import mock

from src.bambu_client import PrintStatus
from src.klipper_client import KlipperClient, clean_klipper_error, find_print_error

BLOCKED = (
    "!! Error evaluating 'gcode_macro _PRINT_AREA_SAFETY:gcode': gcode.CommandError: "
    "PRINT BLOCKED: model reaches Y108.66; StealthChanger dock keep-out is Y120.0. "
    "Move the plate rearward or reduce _PRINT_AREA_SAFETY front_keepout_y after clearance testing."
)


def cmd(msg):
    return {"time": 1.0, "type": "command", "message": msg}


def resp(msg):
    return {"time": 1.0, "type": "response", "message": msg}


# What the Voron's console showed when job #335 was blocked.
BLOCKED_LOG = [
    resp("!! Unknown command: FOO"),  # from an earlier print; must be ignored
    resp("Done printing file"),
    cmd('SDCARD_PRINT_FILE FILENAME="ESP32-2432S028CaseUSB-C.gcode"'),
    resp("File opened:ESP32-2432S028CaseUSB-C.gcode Size:3674427"),
    resp("File selected"),
    resp(BLOCKED),
    resp(BLOCKED),
    resp('echo: "extruder" not hot enough'),
]


class CleanErrorTests(unittest.TestCase):
    def test_strips_macro_wrapper(self):
        self.assertTrue(clean_klipper_error(BLOCKED).startswith(
            "PRINT BLOCKED: model reaches Y108.66; StealthChanger dock keep-out is Y120.0."))

    def test_plain_error(self):
        self.assertEqual(clean_klipper_error("!! Move out of range: 0.000 400.000"),
                         "Move out of range: 0.000 400.000")


class FindPrintErrorTests(unittest.TestCase):
    def test_finds_macro_error_after_print_start(self):
        self.assertTrue(find_print_error(BLOCKED_LOG).startswith("PRINT BLOCKED"))

    def test_ignores_errors_from_before_the_print(self):
        log = [resp("!! Unknown command: FOO"), cmd("SDCARD_PRINT_FILE FILENAME=a.gcode"),
               resp("File opened:a.gcode Size:1")]
        self.assertEqual(find_print_error(log), "")

    def test_user_cancel_after_error_is_not_an_error(self):
        log = BLOCKED_LOG[:6] + [cmd("CANCEL_PRINT")]
        self.assertEqual(find_print_error(log), "")


def make_client(status, gcode_store=None):
    client = KlipperClient("Voron", "voron")
    client._session = mock.Mock()

    def get(url, params=None, timeout=None):
        if url.endswith("/printer/objects/query"):
            return mock.Mock(status_code=200, json=lambda: {"result": {"status": status}})
        if url.endswith("/server/gcode_store"):
            return mock.Mock(status_code=200,
                             json=lambda: {"result": {"gcode_store": gcode_store or []}})
        return mock.Mock(status_code=404, json=lambda: {})

    client._session.get.side_effect = get
    return client


def print_stats(state, filename="ESP32-2432S028CaseUSB-C.gcode", message=""):
    return {"state": state, "filename": filename, "total_duration": 0.02,
            "print_duration": 0.0, "message": message, "info": {}}


class FetchStatusTests(unittest.TestCase):
    def test_blocked_print_reports_reason_but_stays_ready(self):
        # Nothing was printed, so the printer must still accept the next job.
        client = make_client({"webhooks": {"state": "ready"},
                              "print_stats": print_stats("cancelled")}, BLOCKED_LOG)
        client._fetch_status()
        self.assertEqual(client.state.status, PrintStatus.IDLE)
        self.assertTrue(client.state.error_message.startswith("PRINT BLOCKED"))

    def test_console_log_read_once_per_print(self):
        client = make_client({"webhooks": {"state": "ready"},
                              "print_stats": print_stats("cancelled")}, BLOCKED_LOG)
        client._fetch_status()
        client._fetch_status()
        store_calls = [c for c in client._session.get.call_args_list
                       if c.args[0].endswith("/server/gcode_store")]
        self.assertEqual(len(store_calls), 1)

    def test_plain_cancel_stays_idle(self):
        log = [cmd("SDCARD_PRINT_FILE FILENAME=a.gcode"), resp("File opened:a.gcode Size:1"),
               cmd("CANCEL_PRINT")]
        client = make_client({"webhooks": {"state": "ready"},
                              "print_stats": print_stats("cancelled")}, log)
        client._fetch_status()
        self.assertEqual(client.state.status, PrintStatus.IDLE)
        self.assertEqual(client.state.error_message, "")

    def test_print_stats_error_uses_its_message(self):
        client = make_client({"webhooks": {"state": "ready"},
                              "print_stats": print_stats("error", message="Heater extruder not heating at expected rate")})
        client._fetch_status()
        self.assertEqual(client.state.status, PrintStatus.FAILED)
        self.assertIn("not heating", client.state.error_message)

    def test_klipper_shutdown_is_failed(self):
        client = make_client({"webhooks": {"state": "shutdown",
                                           "state_message": "MCU 'mcu' shutdown: Timer too close\nOnce the underlying issue is corrected..."},
                              "print_stats": print_stats("printing")})
        client._fetch_status()
        self.assertEqual(client.state.status, PrintStatus.FAILED)
        self.assertEqual(client.state.error_message, "MCU 'mcu' shutdown: Timer too close")

    def test_printing_has_no_error(self):
        client = make_client({"webhooks": {"state": "ready"},
                              "print_stats": print_stats("printing")})
        client._fetch_status()
        self.assertEqual(client.state.status, PrintStatus.RUNNING)
        self.assertEqual(client.state.error_message, "")


if __name__ == "__main__":
    unittest.main()
