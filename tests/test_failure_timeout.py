import unittest
from unittest.mock import patch

from src.bambu_client import PrintStatus
from src.farm_manager import FarmManager


class _State:
    def __init__(self):
        self.status = PrintStatus.FAILED


class _Printer:
    def __init__(self):
        self.state = _State()


class FailureTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.farm = FarmManager()
        self.farm._printers["Printer-1"] = _Printer()

    def test_failed_printer_becomes_effectively_idle_after_timeout(self):
        self.farm.set_failure_timeout(300)
        with patch("src.farm_manager.time.monotonic", side_effect=[100, 401]):
            self.assertEqual(
                self.farm.get_effective_status("Printer-1"), PrintStatus.FAILED
            )
            self.assertEqual(
                self.farm.get_effective_status("Printer-1"), PrintStatus.IDLE
            )

    def test_zero_timeout_disables_automatic_clearing(self):
        self.farm.set_failure_timeout(0)
        with patch("src.farm_manager.time.monotonic", side_effect=[100, 1000]):
            self.assertEqual(
                self.farm.get_effective_status("Printer-1"), PrintStatus.FAILED
            )
            self.assertEqual(
                self.farm.get_effective_status("Printer-1"), PrintStatus.FAILED
            )

    def test_non_failed_state_resets_failure_timer(self):
        self.farm.set_failure_timeout(300)
        with patch("src.farm_manager.time.monotonic", return_value=100):
            self.farm.get_effective_status("Printer-1")
        self.farm._printers["Printer-1"].state.status = PrintStatus.IDLE
        self.assertEqual(
            self.farm.get_effective_status("Printer-1"), PrintStatus.IDLE
        )
        self.assertNotIn("Printer-1", self.farm._failure_first_seen)


if __name__ == "__main__":
    unittest.main()
