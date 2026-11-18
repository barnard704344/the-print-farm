import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

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
            "server": "http://obico.test",
            "printer_id": 8,
            "username": "operator@example.com",
            "password": "********",
        })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["enabled"])
        saved = load_config(self.config_path)["printers"][0]["obico"]
        self.assertEqual(saved["password"], "saved-secret")
        self.assertEqual(saved["server"], "http://obico.test")
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

    def bambu_client(self):
        self.config["printers"].append({"name": "P1S-1", "type": "bambulab", "host": "192.0.2.1"})
        save_config(self.config_path, self.config)
        manager = MagicMock()
        app = create_app(self.farm, FakeQueue(), config=self.config, obico_manager=manager)
        client = app.test_client()
        with client.session_transaction() as session:
            session.update(role="staff", username="admin")
        return client, manager

    def test_bambu_saves_token_masked_and_applies_and_disables(self):
        client, manager = self.bambu_client()
        data = dict(enabled=True, server="http://obico.test", printer_id=4,
                    username="operator", password="password", auth_token="private-token")
        response = client.post("/api/obico/config/P1S-1", json=data)
        self.assertEqual(response.status_code, 200)
        manager.configure.assert_called_once()
        self.assertFalse(manager.configure.call_args.args[1]["allow_remote_control"])
        response = client.get("/api/obico/config/P1S-1")
        self.assertEqual(response.json["password"], "********")
        self.assertEqual(response.json["auth_token"], "********")
        self.assertEqual(response.json["printer_type"], "bambulab")
        stored = load_config(self.config_path)["printers"]
        self.assertEqual(stored[0]["obico"]["printer_id"], 3)
        self.assertEqual(stored[1]["obico"]["auth_token"], "private-token")
        response = client.post("/api/obico/config/P1S-1", json={"enabled": False})
        self.assertEqual(response.status_code, 200)
        manager.configure.assert_called_with("P1S-1", None)
        self.assertNotIn("obico", load_config(self.config_path)["printers"][1])

    def test_bambu_missing_token_and_duplicate_printer_are_rejected(self):
        client, manager = self.bambu_client()
        data = dict(enabled=True, server="http://obico.test", printer_id=4,
                    username="operator", password="password")
        self.assertEqual(client.post("/api/obico/config/P1S-1", json=data).status_code, 400)
        data.update(auth_token="token", printer_id=3)
        self.assertEqual(client.post("/api/obico/config/P1S-1", json=data).status_code, 400)
        manager.configure.assert_not_called()

    def test_bambu_connection_test_only_validates_and_does_not_start_agent(self):
        client, manager = self.bambu_client()
        data = dict(printer_name="P1S-1", server="http://obico.test", printer_id=4,
                    username="operator", password="password", auth_token="token")
        with patch("src.bambu_obico.linked_printer") as token_check, patch("src.obico_client.ObicoClient") as reader:
            reader.return_value.fetch_status.return_value = {"connected": True, "state": "Operational"}
            response = client.post("/api/obico/test", json=data)
        self.assertEqual(response.status_code, 200)
        token_check.assert_called_once()
        manager.configure.assert_not_called()

    def test_masked_password_cannot_be_forwarded_to_new_server(self):
        response = self.client.post("/api/obico/config/Voron", json={
            "enabled": True, "server": "https://other.test", "printer_id": 3,
            "username": "operator@example.com", "password": "********",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(load_config(self.config_path)["printers"][0]["obico"]["server"], "http://obico.test")

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
