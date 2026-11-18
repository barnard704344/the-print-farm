import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock, ANY

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

    def test_create_and_connect_keeps_token_private_and_defaults_to_monitoring(self):
        client, manager = self.bambu_client()
        with patch("src.obico_client.ObicoClient") as obico:
            obico.return_value.ensure_printer.return_value = {"id": 17, "auth_token": "server-only-token"}
            response = client.post("/api/obico/create/P1S-1", json={
                "server": "https://obico.test", "username": "owner", "password": "account-password",
            })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("server-only-token", response.get_data(as_text=True))
        obico.return_value.ensure_printer.assert_called_once_with("P1S-1", ANY)
        cfg = load_config(self.config_path)["printers"][1]["obico"]
        self.assertEqual(cfg["auth_token"], "server-only-token")
        self.assertFalse(cfg["allow_remote_control"])
        manager.configure.assert_called_once_with("P1S-1", cfg)
        self.assertEqual(client.get("/api/obico/config/P1S-1").json["auth_token"], "********")

    def test_registration_uses_all_installed_names_and_preserves_other_printers(self):
        client, manager = self.bambu_client()
        names = [f"Workshop-{i}" for i in range(12)]
        self.config["printers"].extend({"name": name, "type": "bambulab", "host": f"192.0.2.{i+10}"} for i, name in enumerate(names))
        save_config(self.config_path, self.config)
        with patch("src.obico_client.ObicoClient") as obico:
            obico.return_value.ensure_printer.side_effect = lambda name, key: {"id": 100+names.index(name), "auth_token": "token-"+name}
            for name in names:
                response = client.post("/api/obico/create/"+name, json={
                    "server": "https://work-obico.test", "username": "owner", "password": "password",
                })
                self.assertEqual(response.status_code, 200, name)
        stored = load_config(self.config_path)["printers"]
        self.assertEqual(sum(bool(p.get("obico")) for p in stored), 13)
        self.assertEqual([c.args[0] for c in obico.return_value.ensure_printer.call_args_list], names)
        self.assertEqual(manager.configure.call_count, 12)
        keys = [c.args[1] for c in obico.return_value.ensure_printer.call_args_list]
        self.assertEqual(len(set(keys)), 12)

    def test_registration_rejects_unknown_and_klipper_and_missing_login(self):
        client, _ = self.bambu_client()
        with patch("src.obico_client.ObicoClient") as obico:
            self.assertEqual(client.post("/api/obico/create/missing", json={}).status_code, 404)
            self.assertEqual(client.post("/api/obico/create/Voron", json={}).status_code, 400)
            self.assertEqual(client.post("/api/obico/create/P1S-1", json={"server":"https://obico.test"}).status_code, 400)
            obico.assert_not_called()

    def test_registration_identity_is_scoped_to_the_installation(self):
        client, _ = self.bambu_client()
        payload = {"server": "https://shared-obico.test", "username": "owner", "password": "password"}
        with patch("src.obico_client.ObicoClient") as obico:
            obico.return_value.ensure_printer.return_value = {"id": 7, "auth_token": "private"}
            self.assertEqual(client.post("/api/obico/create/P1S-1", json=payload).status_code, 200)
            first_key = obico.return_value.ensure_printer.call_args.args[1]
            self.config["printers"][1].pop("obico")
            self.config["web"]["admin_api_key"] = "a-different-installation-key"
            save_config(self.config_path, self.config)
            self.assertEqual(client.post("/api/obico/create/P1S-1", json=payload).status_code, 200)
            second_key = obico.return_value.ensure_printer.call_args.args[1]
        self.assertNotEqual(first_key, second_key)

    def test_registration_requires_staff_and_never_calls_obico_for_student(self):
        client, _ = self.bambu_client()
        with client.session_transaction() as session:
            session.update(role="student", username="student")
        with patch("src.obico_client.ObicoClient") as obico:
            response = client.post("/api/obico/create/P1S-1", json={})
        self.assertEqual(response.status_code, 403)
        obico.assert_not_called()

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
