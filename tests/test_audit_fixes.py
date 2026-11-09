"""Regression tests for the November 2026 security audit fixes."""
import importlib
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.web import create_app
from test_security import FakeFarm, FakeQueue


class AuditFixTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.queue = FakeQueue(self.temp_dir.name)
        config = {
            "web": {"secret_key": "test-secret", "orca_api_key": "orca-upload-key"},
            "printers": [{"name": "Printer-1", "type": "klipper"}],
            "student_access": {"allowlist": ["alice", "mallory"], "banlist": ["mallory"]},
        }
        self.app = create_app(FakeFarm(), self.queue, config=config)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _client(self, username=None, role="student"):
        client = self.app.test_client()
        if username:
            with client.session_transaction() as session:
                session.update(role=role, username=username, display_name=username)
        return client

    # Orca upload key must not reach students without print access (#7)
    def test_unapproved_student_cannot_fetch_orca_key(self):
        response = self._client("carol").get("/api/orca/api-key")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(b"orca-upload-key", response.data)

    def test_banned_student_cannot_fetch_orca_key(self):
        response = self._client("mallory").get("/api/orca/api-key")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(b"orca-upload-key", response.data)

    def test_approved_student_and_staff_receive_orca_key(self):
        for client in (self._client("alice"), self._client("teacher", role="staff")):
            response = client.get("/api/orca/api-key")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["api_key"], "orca-upload-key")

    # check_filament previously had no authentication at all (#6)
    def test_check_filament_requires_login(self):
        response = self._client().post("/api/jobs/1/check_filament", json={"printer": "Printer-1"})
        self.assertEqual(response.status_code, 401)

    def test_check_filament_rejects_other_students_jobs(self):
        response = self._client("carol").post("/api/jobs/1/check_filament", json={"printer": "Printer-1"})
        self.assertEqual(response.status_code, 403)


class ConfigPathTests(unittest.TestCase):
    """Startup must load the same file the web UI saves to (#4)."""

    def _default_config_arg(self, env):
        with patch.dict(os.environ, env, clear=False), \
                patch.object(sys, "argv", ["print-farm", "status"]), \
                patch("src.main.load_config", side_effect=SystemExit("stop")) as load:
            main = importlib.import_module("src.main")
            with self.assertRaises(SystemExit):
                main.main()
            return load.call_args.args[0]

    def test_startup_uses_farm_config_env(self):
        self.assertEqual(
            self._default_config_arg({"FARM_CONFIG": "/etc/the-print-farm/config.yaml"}),
            "/etc/the-print-farm/config.yaml",
        )

    def test_startup_falls_back_to_repo_config(self):
        env = dict(os.environ)
        env.pop("FARM_CONFIG", None)
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self._default_config_arg({}), "config/config.yaml")


if __name__ == "__main__":
    unittest.main()
