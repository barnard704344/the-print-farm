import os
import tempfile
import unittest
from unittest.mock import patch

from src.config_store import load_config, save_config
from src.web import create_app


class FakeKlipper:
    def __init__(self):
        self.obico_configs = []

    def configure_obico(self, config):
        self.obico_configs.append(config)


class FakeFarm:
    def __init__(self):
        self.printer = FakeKlipper()

    def get_printer(self, name):
        return self.printer if name == "Voron" else None


class FakeQueue:
    upload_dir = "/tmp"

    def get_active_jobs(self):
        return []


class ObicoSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmp.name, "config.yaml")
        self.config = {
            "web": {"secret_key": "test-secret"},
            "printers": [{
                "name": "Voron",
                "type": "klipper",
                "host": "192.168.1.66",
                "obico": {
                    "server": "http://obico.test",
                    "printer_id": 3,
                    "username": "operator@example.com",
                    "password": "saved-secret",
                },
            }],
        }
        save_config(self.config_path, self.config)
        self.farm = FakeFarm()
        self.env = patch.dict(os.environ, {"FARM_CONFIG": self.config_path})
        self.env.start()
        app = create_app(self.farm, FakeQueue(), config=self.config)
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session.update(role="staff", username="admin", display_name="Admin")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_saved_config_loads_as_enabled_with_masked_password(self):
        response = self.client.get("/api/obico/config/Voron")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["enabled"])
        self.assertEqual(response.get_json()["password"], "********")

    def test_save_preserves_masked_password_and_applies_immediately(self):
        response = self.client.post("/api/obico/config/Voron", json={
            "enabled": True,
            "server": "http://obico-new.test",
            "printer_id": 8,
            "username": "operator@example.com",
            "password": "********",
        })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["enabled"])
        saved = load_config(self.config_path)["printers"][0]["obico"]
        self.assertEqual(saved["password"], "saved-secret")
        self.assertEqual(saved["server"], "http://obico-new.test")
        self.assertEqual(self.farm.printer.obico_configs[-1], saved)

    def test_disable_clears_config_and_live_client(self):
        response = self.client.post(
            "/api/obico/config/Voron",
            json={"enabled": False},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["enabled"])
        self.assertNotIn("obico", load_config(self.config_path)["printers"][0])
        self.assertIsNone(self.farm.printer.obico_configs[-1])

    def test_enabled_config_requires_connection_fields(self):
        response = self.client.post(
            "/api/obico/config/Voron",
            json={"enabled": True, "server": "http://obico.test"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("printer ID", response.get_json()["message"])

    def test_dashboard_restores_selection_and_saved_checkbox_state(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"const previousSelection = select.value", response.data)
        self.assertIn(b"await loadObicoForPrinter()", response.data)
        self.assertIn(
            b"obicoEnabled').checked = Boolean(data.enabled)",
            response.data,
        )

    def test_settings_are_grouped_into_remembered_category_tabs(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        for section in (b"access", b"printers", b"integrations", b"system"):
            self.assertIn(b'data-settings-section="' + section + b'"', response.data)
            self.assertIn(b'data-settings-panel="' + section + b'"', response.data)
        self.assertIn(b"function setSettingsSection(section)", response.data)
        self.assertIn(b"farm-settings-section", response.data)


if __name__ == "__main__":
    unittest.main()
