import unittest
from unittest.mock import Mock

from src.obico_client import ObicoClient


class ObicoClientTests(unittest.TestCase):
    def test_nullable_obico_fields_are_supported(self):
        client = ObicoClient("http://obico.test", "user", "secret", 3)
        response = Mock(status_code=200)
        response.json.return_value = {
            "status": None,
            "pic": None,
            "current_print": None,
            "watching_enabled": True,
        }
        client._session = Mock()
        client._session.get.return_value = response

        status = client.fetch_status()

        self.assertTrue(status["connected"])
        self.assertTrue(status["watching"])
        self.assertEqual(status["state"], "")
        self.assertEqual(status["snapshot_url"], "")

    def test_missing_printer_id_is_reported(self):
        client = ObicoClient("http://obico.test", "user", "secret", 2)
        client._session = Mock()
        client._session.get.return_value = Mock(status_code=404)

        status = client.fetch_status()

        self.assertFalse(status["connected"])
        self.assertEqual(status["error"], "Obico printer ID 2 was not found")

    def test_request_failure_marks_cached_data_unavailable(self):
        client = ObicoClient("http://obico.test", "user", "secret", 3)
        client._last_data = {"connected": True, "state": "Printing"}
        client._session = Mock()
        client._session.get.side_effect = RuntimeError("network unavailable")

        status = client.fetch_status()

        self.assertFalse(status["connected"])
        self.assertEqual(status["state"], "Printing")
        self.assertIn("network unavailable", status["error"])


if __name__ == "__main__":
    unittest.main()
