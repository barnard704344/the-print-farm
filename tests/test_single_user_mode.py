"""Login can be turned off for single-operator farms, and back on safely."""
import os
import tempfile
import unittest
from unittest import mock

from src.config_store import load_config, save_config
from src.web import create_app
from test_security import FakeFarm, FakeQueue


class SingleUserModeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.temp_dir.name, "config.yaml")
        self.env = mock.patch.dict(os.environ, {"FARM_CONFIG": self.config_path})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp_dir.cleanup()

    def _app(self, config):
        config.setdefault("web", {})["secret_key"] = "test-secret"
        config.setdefault("printers", [{"name": "Printer-1", "type": "klipper"}])
        config.setdefault("student_access", {"allowlist": [], "banlist": []})
        save_config(self.config_path, config)
        self.config = config
        return create_app(FakeFarm(), FakeQueue(self.temp_dir.name), config=config)

    def _staff(self, app):
        client = app.test_client()
        with client.session_transaction() as s:
            s.update(role="staff", admin=True, username="teacher", display_name="teacher")
        return client

    def test_disabled_login_gives_every_visitor_staff_access(self):
        app = self._app({"web": {"auth_enabled": False}})
        client = app.test_client()
        status = client.get("/api/auth/status").get_json()
        self.assertFalse(status["auth_enabled"])
        self.assertTrue(status["admin"])
        self.assertTrue(status["authenticated"])
        self.assertEqual(client.get("/api/auth/config").status_code, 200)
        self.assertEqual(client.post("/api/auth/logout").get_json()["ok"], True)
        self.assertTrue(client.get("/api/auth/status").get_json()["admin"])

    def test_login_required_by_default(self):
        app = self._app({"web": {}})
        status = app.test_client().get("/api/auth/status").get_json()
        self.assertTrue(status["auth_enabled"])
        self.assertFalse(status["authenticated"])
        self.assertEqual(app.test_client().get("/api/auth/config").status_code, 403)

    def test_staff_can_disable_login_and_it_is_saved(self):
        app = self._app({"web": {}, "local_users": [{"username": "teacher", "password_hash": "x", "role": "staff"}]})
        res = self._staff(app).post("/api/auth/config", json={"auth_enabled": False})
        self.assertEqual(res.status_code, 200)
        self.assertIs(load_config(self.config_path)["web"]["auth_enabled"], False)
        self.assertTrue(app.test_client().get("/api/auth/status").get_json()["admin"])

    def test_students_cannot_change_login_mode(self):
        app = self._app({"web": {}})
        client = app.test_client()
        with client.session_transaction() as s:
            s.update(role="student", username="carol")
        self.assertEqual(client.post("/api/auth/config", json={"auth_enabled": False}).status_code, 403)

    def test_reenabling_without_staff_login_requires_new_account(self):
        app = self._app({"web": {"auth_enabled": False}})
        client = app.test_client()
        refused = client.post("/api/auth/config", json={"auth_enabled": True})
        self.assertEqual(refused.status_code, 400)
        self.assertTrue(refused.get_json()["needs_account"])
        short = client.post("/api/auth/config", json={"auth_enabled": True, "username": "admin", "password": "short"})
        self.assertEqual(short.status_code, 400)

        ok = client.post("/api/auth/config", json={"auth_enabled": True, "username": "admin", "password": "a-long-password"})
        self.assertEqual(ok.status_code, 200)
        saved = load_config(self.config_path)
        self.assertIs(saved["web"]["auth_enabled"], True)
        self.assertEqual(saved["local_users"][0]["username"], "admin")
        self.assertNotIn("password", saved["local_users"][0])

        # The single-user session is gone; the new account can sign in.
        self.assertFalse(client.get("/api/auth/status").get_json()["authenticated"])
        login = client.post("/api/auth/login", json={"username": "admin", "password": "a-long-password"})
        self.assertEqual(login.status_code, 200)
        self.assertTrue(client.get("/api/auth/status").get_json()["admin"])

    def test_single_user_session_is_dropped_when_login_returns(self):
        app = self._app({"web": {"auth_enabled": False}})
        client = app.test_client()
        self.assertTrue(client.get("/api/auth/status").get_json()["admin"])
        self.config["web"]["auth_enabled"] = True
        self.assertFalse(client.get("/api/auth/status").get_json()["authenticated"])
