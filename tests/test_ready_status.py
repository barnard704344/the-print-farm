"""A finished printer whose plate is clear is shown as READY."""
import unittest

from src.bambu_client import PrintState, PrintStatus
from src.farm_manager import FarmManager


class Client:
    def __init__(self, status):
        self.state = PrintState()
        self.state.status = status
        self.host = "10.0.0.1"

    def is_connected(self):
        return True


class ReadyStatusTests(unittest.TestCase):
    def _state(self, status, needs_clear=False, required=True):
        farm = FarmManager([])
        farm._printers["P"] = Client(status)
        farm._printer_types["P"] = "bambulab"
        farm._plate_required["P"] = required
        if needs_clear:
            farm._plate_states["P"] = {"needs_clear": True, "finished_status": "FINISH"}
        return farm.get_all_states()["P"]

    def test_finished_with_plate_cleared_is_ready(self):
        state = self._state(PrintStatus.FINISH)
        self.assertEqual(state["status"], "FINISH")
        self.assertEqual(state["display_status"], "READY")

    def test_finished_with_print_on_plate_stays_finished(self):
        self.assertEqual(self._state(PrintStatus.FINISH, needs_clear=True)["display_status"], "FINISH")

    def test_plate_check_off_counts_as_ready(self):
        self.assertEqual(self._state(PrintStatus.FINISH, needs_clear=True, required=False)["display_status"], "READY")

    def test_idle_is_ready_and_running_is_unchanged(self):
        self.assertEqual(self._state(PrintStatus.IDLE)["display_status"], "READY")
        self.assertEqual(self._state(PrintStatus.RUNNING)["display_status"], "RUNNING")
