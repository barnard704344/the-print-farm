import os
import tempfile
import unittest
from unittest.mock import patch

from src.config_store import load_config, save_config
from src.web import create_app


class FakeFarm:
    def get_printer(self, _name):
        return None


class FakeQueue:
    upload_dir = "/tmp"

    def get_active_jobs(self):
        return []


class NotificationSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmp.name, "config.yaml")
        self.webhook = "https://discord.com/api/webhooks/123/secret-token"
        self.config = {
            "web": {"secret_key": "test-secret"},
            "printers": [],
            "notifications": {
                "enabled": True,
                "discord": {
                    "enabled": True,
                    "webhook_url": self.webhook,
                },
            },
        }
        save_config(self.config_path, self.config)
        self.env = patch.dict(os.environ, {"FARM_CONFIG": self.config_path})
        self.env.start()
        app = create_app(FakeFarm(), FakeQueue(), config=self.config)
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session.update(role="staff", username="admin", display_name="Admin")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_get_returns_configured_webhook_to_admin(self):
        response = self.client.get("/api/notifications/config")

        self.assertEqual(response.status_code, 200)
        discord = response.get_json()["discord"]
        self.assertTrue(discord["enabled"])
        self.assertTrue(discord["webhook_configured"])
        self.assertEqual(discord["webhook_url"], self.webhook)

    def test_saving_mask_keeps_existing_webhook(self):
        response = self.client.post("/api/notifications/config", json={
            "enabled": True,
            "discord": {"enabled": True, "webhook_url": "********"},
        })

        self.assertEqual(response.status_code, 200)
        saved = load_config(self.config_path)["notifications"]["discord"]
        self.assertEqual(saved["webhook_url"], self.webhook)

    def test_dashboard_shows_configured_webhook_indicator(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="notifDiscordConfigured"', response.data)
        self.assertIn(
            b"discord.webhook_configured ? 'Configured' : 'Not configured'",
            response.data,
        )


if __name__ == "__main__":
    unittest.main()
