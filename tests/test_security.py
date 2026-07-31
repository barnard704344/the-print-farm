import hashlib
import os
import ssl
import stat
import tempfile
import unittest
import zipfile
from enum import Enum
from unittest.mock import patch

from flask import Flask

from src.api_v1 import create_api_v1
from src.camera import BambuCamera
from src.config_store import load_config, save_config
from src.file_validation import InvalidPrintFile, validate_print_file
from src.image_validation import save_normalized_image
from src.main import _resolve_web_bind
from src.tls_trust import verify_peer_certificate
from src.web import create_app


class Status(Enum):
    IDLE = "IDLE"
    FAILED = "FAILED"


class FakeState:
    def __init__(self, status=Status.IDLE):
        self.status = status
        self.mmu = {}


class FakePrinter:
    def __init__(self):
        self.state = FakeState()
        self._has_mmu = True
        self.sent_gcode = []

    def is_connected(self):
        return True

    def pause_print(self):
        return True

    def send_gcode(self, command):
        self.sent_gcode.append(command)
        return True


class FakeFarm:
    def __init__(self):
        self.printer = FakePrinter()

    def get_printer(self, name):
        return self.printer if name == "Printer-1" else None

    def get_printer_type(self, name):
        return "klipper"

    def get_all_states(self):
        return {
            "Printer-1": {
                "name": "Printer-1",
                "connected": True,
                "status": "IDLE",
                "klipper_tools": [],
            }
        }

    def get_farm_summary(self):
        return {"total": 1, "connected": 1, "printing": 0, "idle": 1}

    def get_all_printers(self):
        return {"Printer-1": self.printer}


class FakeQueue:
    def __init__(self, upload_dir):
        self.upload_dir = upload_dir
        self.jobs = [
            {
                "id": 1,
                "status": "queued",
                "submitted_by": "alice",
                "file_path": "/secret/alice.gcode",
                "filename": "internal-alice.gcode",
                "original_name": "alice.gcode",
            },
            {
                "id": 2,
                "status": "queued",
                "submitted_by": "bob",
                "file_path": "/secret/bob.gcode",
                "filename": "internal-bob.gcode",
                "original_name": "bob.gcode",
            },
        ]

    def get_active_jobs(self):
        return []

    def get_queued_jobs(self):
        return list(self.jobs)

    def get_all_jobs(self, limit=100):
        return list(self.jobs[:limit])

    def get_job(self, job_id):
        return next((job for job in self.jobs if job["id"] == job_id), None)

    def get_stats(self):
        return {"queued": len(self.jobs)}


class FakeCameraManager:
    def get_frame(self, name):
        return b"\xff\xd8frame\xff\xd9"

    def is_streaming(self, name):
        return True


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.farm = FakeFarm()
        self.queue = FakeQueue(self.temp_dir.name)
        self.config = {
            "web": {"secret_key": "test-secret"},
            "printers": [{"name": "Printer-1", "type": "klipper"}],
            "student_access": {"allowlist": ["alice"], "banlist": []},
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def _api_app(self, api_key="integration-secret"):
        app = Flask(__name__)
        app.secret_key = "test-secret"
        app.register_blueprint(
            create_api_v1(
                self.farm,
                self.queue,
                camera_manager=FakeCameraManager(),
                api_key=api_key,
                config=self.config,
            )
        )
        return app

    def test_api_fails_closed_without_key_or_session(self):
        client = self._api_app(api_key="").test_client()
        response = client.get("/api/v1/server")
        self.assertEqual(response.status_code, 401)

    def test_student_cannot_control_printer(self):
        client = self._api_app().test_client()
        with client.session_transaction() as session:
            session.update(role="student", username="alice", display_name="Alice")
        response = client.post(
            "/api/v1/printers/Printer-1/command",
            json={"command": "pause"},
        )
        self.assertEqual(response.status_code, 403)

    def test_student_job_listing_is_owned_and_redacted(self):
        client = self._api_app().test_client()
        with client.session_transaction() as session:
            session.update(role="student", username="alice", display_name="Alice")
        response = client.get("/api/v1/jobs")
        self.assertEqual(response.status_code, 200)
        jobs = response.get_json()["data"]
        self.assertEqual([job["id"] for job in jobs], [1])
        self.assertNotIn("file_path", jobs[0])
        self.assertNotIn("filename", jobs[0])

    def test_happy_hare_rejects_parameter_injection(self):
        client = self._api_app().test_client()
        response = client.post(
            "/api/v1/printers/Printer-1/happyhare/run",
            headers={"X-Api-Key": "integration-secret"},
            json={"macro": "MMU_HOME", "params": {"TOOL": "0\nM112"}},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.farm.printer.sent_gcode, [])

    def test_camera_snapshot_uses_supported_camera_api(self):
        client = self._api_app().test_client()
        response = client.get(
            "/api/v1/cameras/Printer-1/snapshot",
            headers={"X-Api-Key": "integration-secret"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")

    def test_dashboard_does_not_disclose_api_key(self):
        app = create_app(
            self.farm,
            self.queue,
            api_key="sentinel-master-key",
            config=self.config,
        )
        response = app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"sentinel-master-key", response.data)

    def test_dashboard_javascript_arguments_escape_html_attribute_quotes(self):
        app = create_app(
            self.farm,
            self.queue,
            api_key="integration-secret",
            config=self.config,
        )
        response = app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"'&': '&amp;', '\"': '&quot;'", response.data)
        self.assertNotIn(
            b"return escapeHtml(JSON.stringify(String(value ?? '')))",
            response.data,
        )

    def test_dashboard_has_teacher_dispatch_lane_and_details_drawer(self):
        app = create_app(
            self.farm,
            self.queue,
            api_key="integration-secret",
            config=self.config,
        )
        response = app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="teacherDispatchLane"', response.data)
        self.assertIn(b'id="teacherDispatchList"', response.data)
        self.assertIn(b'class="details-drawer"', response.data)
        self.assertIn(b"function dispatchFromLane(jobId)", response.data)
        self.assertNotIn(
            b'class="modal-overlay" id="printerDetailsOverlay"',
            response.data,
        )

    def test_legacy_job_list_requires_login(self):
        app = create_app(
            self.farm,
            self.queue,
            api_key="sentinel-master-key",
            config=self.config,
        )
        response = app.test_client().get("/api/jobs")
        self.assertEqual(response.status_code, 401)

    def test_farm_telemetry_requires_login(self):
        app = create_app(
            self.farm,
            self.queue,
            api_key="sentinel-master-key",
            config=self.config,
        )
        response = app.test_client().get("/api/farm/status")
        self.assertEqual(response.status_code, 401)

    def test_update_refuses_to_restart_a_busy_printer(self):
        app = create_app(
            self.farm,
            self.queue,
            api_key="sentinel-master-key",
            config=self.config,
        )
        self.farm.get_all_states = lambda: {
            "Printer-1": {"connected": True, "status": "RUNNING"}
        }
        client = app.test_client()
        with client.session_transaction() as session:
            session.update(role="staff", username="admin", display_name="Admin")
        response = client.post("/api/update/apply")
        self.assertEqual(response.status_code, 409)

    def test_update_refuses_to_restart_an_unverified_printer(self):
        app = create_app(
            self.farm,
            self.queue,
            api_key="sentinel-master-key",
            config=self.config,
        )
        self.farm.get_all_states = lambda: {
            "Printer-1": {"connected": False, "status": "UNKNOWN"}
        }
        client = app.test_client()
        with client.session_transaction() as session:
            session.update(role="staff", username="admin", display_name="Admin")
        response = client.post("/api/update/apply")
        self.assertEqual(response.status_code, 409)
        self.assertIn("offline", response.get_json()["message"])

    def test_integration_key_cannot_invoke_deployment_update(self):
        app = create_app(
            self.farm,
            self.queue,
            api_key="sentinel-master-key",
            config=self.config,
        )
        response = app.test_client().post(
            "/api/update/apply",
            headers={"X-Api-Key": "sentinel-master-key"},
        )
        self.assertEqual(response.status_code, 403)

    def test_student_stats_only_count_owned_jobs(self):
        app = create_app(
            self.farm,
            self.queue,
            api_key="sentinel-master-key",
            config=self.config,
        )
        client = app.test_client()
        with client.session_transaction() as session:
            session.update(role="student", username="alice", display_name="Alice")
        response = client.get("/api/jobs")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["stats"]["total"], 1)
        self.assertEqual(payload["stats"]["queued"], 1)

    def test_plaintext_local_password_is_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.yaml")
            config = {
                "web": {"secret_key": "test-secret"},
                "local_users": [
                    {
                        "username": "admin",
                        "display_name": "Admin",
                        "role": "staff",
                        "password": "not-a-real-password",
                    }
                ],
            }
            save_config(path, config)
            with patch.dict(os.environ, {"FARM_CONFIG": path}):
                create_app(self.farm, self.queue, config=config)

            persisted = load_config(path)
            user = persisted["local_users"][0]
            self.assertNotIn("password", user)
            self.assertTrue(user["password_hash"].startswith("scrypt:"))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


class ResourceLimitTests(unittest.TestCase):
    def test_remote_web_bind_requires_explicit_opt_in(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                _resolve_web_bind({"host": "0.0.0.0"}),
                "127.0.0.1",
            )
        with patch.dict(
            os.environ,
            {
                "FARM_WEB_HOST": "0.0.0.0",
                "FARM_ALLOW_REMOTE_BACKEND": "true",
            },
            clear=True,
        ):
            self.assertEqual(_resolve_web_bind({"host": "127.0.0.1"}), "0.0.0.0")

    def test_valid_gcode_is_accepted(self):
        with tempfile.NamedTemporaryFile(suffix=".gcode") as handle:
            handle.write(b"G28\n")
            handle.flush()
            validate_print_file(handle.name)

    def test_high_ratio_archive_is_rejected(self):
        with tempfile.NamedTemporaryFile(suffix=".3mf") as handle:
            with zipfile.ZipFile(handle.name, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("Metadata/plate_1.gcode", b"A" * (2 * 1024 * 1024))
            with self.assertRaises(InvalidPrintFile):
                validate_print_file(handle.name)

    def test_atomic_config_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.yaml")
            save_config(path, {"web": {"api_key": "secret"}})
            self.assertEqual(load_config(path)["web"]["api_key"], "secret")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_atomic_config_preserves_runtime_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "runtime.yaml")
            link = os.path.join(directory, "config.yaml")
            save_config(target, {"value": "before"})
            os.symlink(target, link)
            save_config(link, {"value": "after"})
            self.assertTrue(os.path.islink(link))
            self.assertEqual(load_config(target)["value"], "after")

    def test_fragmented_camera_read(self):
        class FragmentedSocket:
            def __init__(self):
                self.parts = [b"a", TimeoutError(), b"bc", b"def"]

            def recv(self, size):
                part = self.parts.pop(0)
                if isinstance(part, Exception):
                    raise part
                return part

        camera = BambuCamera("127.0.0.1", "code")
        self.assertEqual(camera._recv_exact(FragmentedSocket(), 6), b"abcdef")

    def test_tofu_rejects_a_changed_live_certificate(self):
        first = b"first-certificate"
        second = b"second-certificate"
        with tempfile.TemporaryDirectory() as directory:
            trust_path = os.path.join(directory, "trust.json")
            with patch.dict(os.environ, {"FARM_TLS_TRUST": trust_path}):
                fingerprint = verify_peer_certificate("printer", 8883, first)
                self.assertEqual(fingerprint, hashlib.sha256(first).hexdigest())
                verify_peer_certificate("printer", 8883, first)
                with self.assertRaises(ssl.SSLError):
                    verify_peer_certificate("printer", 8883, second)

    def test_explicit_fingerprint_is_validated_on_the_live_certificate(self):
        certificate = b"printer-certificate"
        expected = hashlib.sha256(certificate).hexdigest()
        self.assertEqual(
            verify_peer_certificate("printer", 8883, certificate, expected),
            expected,
        )
        with self.assertRaises(ValueError):
            verify_peer_certificate("printer", 8883, certificate, "not-a-pin")

    def test_thumbnail_payload_must_decode_as_a_supported_image(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, "thumbnail.png")
            with self.assertRaises(ValueError):
                save_normalized_image(
                    b"<svg onload=alert(1)>",
                    destination,
                    max_bytes=1024,
                )
            self.assertFalse(os.path.exists(destination))


if __name__ == "__main__":
    unittest.main()
