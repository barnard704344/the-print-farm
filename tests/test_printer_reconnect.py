import os
import tempfile
import threading
import unittest
from unittest.mock import patch

import yaml

from src.web import create_app


class FakeClient:
    def __init__(self, host, connected=False, connect_result=True):
        self.host = host
        self.connected = connected
        self.connect_result = connect_result
        self.disconnected = False

    def connect(self, timeout=10):
        self.connected = self.connect_result
        return self.connect_result

    def disconnect(self):
        self.disconnected = True
        self.connected = False

    def is_connected(self):
        return self.connected


class FakeFarm:
    def __init__(self, client):
        self._printers = {"Workshop P1S": client}
        self._printer_types = {"Workshop P1S": "bambulab"}
        self._lock = threading.Lock()

    def set_failure_timeout(self, _seconds):
        pass

    def get_printer(self, name):
        return self._printers.get(name)


class FakeQueue:
    upload_dir = "/tmp"

    def get_active_jobs(self):
        return []


class PrinterReconnectTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.temp_dir.name, "config.yaml")
        self.config = {
            "web": {"secret_key": "test-secret", "orca_api_key": "orca-test-key-1234"},
            "printers": [{
                "name": "Workshop P1S",
                "type": "bambulab",
                "host": "192.168.1.20",
                "serial": "01P00SERIAL",
                "access_code": "87654321",
                "orca_port": 5007,
                "ams_serial": "AMS-CACHED",
                "staff_only": True,
                "tls_fingerprints": {"mqtt": "a" * 64},
            }],
        }
        with open(self.config_path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(self.config, handle, sort_keys=False)
        self.old_client = FakeClient("192.168.1.20")
        self.farm = FakeFarm(self.old_client)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _client(self):
        app = create_app(self.farm, FakeQueue(), config=self.config)
        client = app.test_client()
        with client.session_transaction() as session:
            session.update(role="staff", username="admin", display_name="Admin")
        return client

    def test_reconnect_scan_includes_configured_serial_at_new_address(self):
        discovered = [{
            "name": "Workshop P1S",
            "host": "192.168.1.77",
            "serial": "01P00SERIAL",
            "model": "P1S",
        }]
        with (
            patch("src.web.discover_printers", return_value=discovered),
            patch("src.web.get_local_subnets", return_value=["192.168.1"]),
            patch("src.web.scan_subnet", return_value=[]) as subnet_scan,
        ):
            response = self._client().post(
                "/api/discover/reconnect/scan",
                json={"name": "Workshop P1S", "timeout": 1, "port_scan": True},
            )

        self.assertEqual(response.status_code, 200)
        candidate = response.get_json()["candidates"][0]
        self.assertEqual(candidate["host"], "192.168.1.77")
        self.assertTrue(candidate["match"])
        subnet_scan.assert_called_once_with("192.168.1", timeout=1.0)

    def test_reconnect_scan_accepts_a_routed_printer_subnet(self):
        def scan_result(prefix, timeout=1.0):
            return ["10.136.36.99"] if prefix == "10.136.36" else []

        with (
            patch("src.web.discover_printers", return_value=[]),
            patch("src.web.get_local_subnets", return_value=["10.72.28"]),
            patch("src.web.scan_subnet", side_effect=scan_result) as subnet_scan,
            patch(
                "src.web.test_bambu_connection",
                return_value={"ok": True, "message": "Identity verified"},
            ) as identity_test,
        ):
            response = self._client().post(
                "/api/discover/reconnect/scan",
                json={
                    "name": "Workshop P1S",
                    "subnet": "10.136.32.0/21",
                    "timeout": 1,
                    "port_scan": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["scanned_subnets"],
            [f"10.136.{octet}" for octet in range(32, 40)],
        )
        self.assertEqual(response.get_json()["candidates"][0]["host"], "10.136.36.99")
        self.assertEqual(subnet_scan.call_count, 8)
        identity_test.assert_called_once_with(
            "10.136.36.99",
            "87654321",
            "01P00SERIAL",
            timeout=5.0,
            require_report=True,
        )

    def test_reconnect_scan_excludes_addresses_assigned_to_other_printers(self):
        self.config["printers"].append({
            "name": "P1S-ICT-AMS",
            "type": "bambulab",
            "host": "10.136.32.95",
            "serial": "OTHER-SERIAL",
            "access_code": "11112222",
        })
        with (
            patch("src.web.discover_printers", return_value=[]),
            patch("src.web.get_local_subnets", return_value=["10.72.28"]),
            patch("src.web.scan_subnet", return_value=["10.136.32.95"]),
            patch("src.web.test_bambu_connection") as identity_test,
        ):
            response = self._client().post(
                "/api/discover/reconnect/scan",
                json={
                    "name": "Workshop P1S",
                    "subnet": "10.136.32.0/24",
                    "timeout": 1,
                    "port_scan": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["candidates"], [])
        identity_test.assert_not_called()

    def test_reconnect_uses_configured_printer_vlan_when_field_is_blank(self):
        self.config["discovery"] = {"scan_subnets": ["10.136.32.0/21"]}
        with (
            patch("src.web.discover_printers", return_value=[]),
            patch("src.web.get_local_subnets", return_value=["10.72.28"]),
            patch("src.web.scan_subnet", return_value=[]) as subnet_scan,
        ):
            response = self._client().post(
                "/api/discover/reconnect/scan",
                json={"name": "Workshop P1S", "timeout": 1, "port_scan": True},
            )

        self.assertEqual(response.status_code, 200)
        scanned = response.get_json()["scanned_subnets"]
        self.assertEqual(scanned[:8], [f"10.136.{octet}" for octet in range(32, 40)])
        self.assertIn("192.168.1", scanned)
        self.assertIn("10.72.28", scanned)
        self.assertEqual(subnet_scan.call_count, 10)

    def test_dashboard_keeps_reconnect_control_for_offline_transitions(self):
        response = self._client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'data-field="reconnect"', response.data)
        self.assertIn(
            b"reconnectEl.style.display = connected ? 'none' : ''",
            response.data,
        )

    def test_reconnect_changes_only_host_and_replaces_live_client(self):
        new_client = FakeClient("192.168.1.77")
        with (
            patch.dict(os.environ, {"FARM_CONFIG": self.config_path}),
            patch(
                "src.web.test_bambu_connection",
                return_value={"ok": True, "message": "Connected"},
            ) as connection_test,
            patch("src.web.create_printer_client", return_value=new_client) as factory,
        ):
            response = self._client().post(
                "/api/discover/reconnect",
                json={"name": "Workshop P1S", "host": "192.168.1.77"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["connected"])
        connection_test.assert_called_once_with(
            "192.168.1.77", "87654321", "01P00SERIAL", require_report=True,
        )
        updated = factory.call_args.args[0]
        self.assertEqual(updated["host"], "192.168.1.77")
        self.assertEqual(updated["orca_port"], 5007)
        self.assertEqual(updated["ams_serial"], "AMS-CACHED")
        self.assertTrue(updated["staff_only"])
        self.assertEqual(updated["tls_fingerprints"], {"mqtt": "a" * 64})
        self.assertTrue(self.old_client.disconnected)
        self.assertIs(self.farm.get_printer("Workshop P1S"), new_client)

        with open(self.config_path, "r", encoding="utf-8") as handle:
            persisted = yaml.safe_load(handle)["printers"][0]
        expected = dict(self.config["printers"][0], host="192.168.1.77")
        self.assertEqual(persisted, expected)

    def test_reconnect_rejects_address_that_does_not_match_cached_settings(self):
        with (
            patch.dict(os.environ, {"FARM_CONFIG": self.config_path}),
            patch(
                "src.web.test_bambu_connection",
                return_value={"ok": False, "message": "Serial or access code did not match"},
            ),
            patch("src.web.create_printer_client") as factory,
        ):
            response = self._client().post(
                "/api/discover/reconnect",
                json={"name": "Workshop P1S", "host": "192.168.1.88"},
            )

        self.assertEqual(response.status_code, 409)
        factory.assert_not_called()
        self.assertFalse(self.old_client.disconnected)
        with open(self.config_path, "r", encoding="utf-8") as handle:
            persisted = yaml.safe_load(handle)["printers"][0]
        self.assertEqual(persisted["host"], "192.168.1.20")

    def test_reconnect_does_not_persist_when_live_connection_cannot_start(self):
        failed_client = FakeClient("192.168.1.77", connect_result=False)
        with (
            patch.dict(os.environ, {"FARM_CONFIG": self.config_path}),
            patch(
                "src.web.test_bambu_connection",
                return_value={"ok": True, "message": "Identity verified"},
            ),
            patch("src.web.create_printer_client", return_value=failed_client),
        ):
            response = self._client().post(
                "/api/discover/reconnect",
                json={"name": "Workshop P1S", "host": "192.168.1.77"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIs(self.farm.get_printer("Workshop P1S"), self.old_client)
        with open(self.config_path, "r", encoding="utf-8") as handle:
            persisted = yaml.safe_load(handle)["printers"][0]
        self.assertEqual(persisted["host"], "192.168.1.20")


if __name__ == "__main__":
    unittest.main()
