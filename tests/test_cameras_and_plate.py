"""Shared cameras (idle stop, 304s, stream cap) and build plate confirmation."""
import os
import tempfile
import time
import unittest

from src.bambu_client import PrintStatus
from src.camera import CameraManager
from src.farm_manager import FarmManager
from src.job_queue import JobQueue
from src.web import create_app
from test_security import FakeFarm, FakeQueue


# ── Fakes ──────────────────────────────────────────────

class FakeCam:
    def __init__(self):
        self.stopped = False
        self.frame = b"\xff\xd8frame\xff\xd9"
        self.frame_at = 123.456
        self.is_streaming = True
        self.is_starting = False

    @property
    def latest_frame(self):
        return self.frame

    @property
    def latest_frame_with_time(self):
        return self.frame, self.frame_at

    def stop(self):
        self.stopped = True


class FakeState:
    def __init__(self, status=PrintStatus.IDLE):
        self.status = status


class FakeClient:
    def __init__(self, status=PrintStatus.IDLE, connected=True):
        self.state = FakeState(status)
        self.connected = connected
        self.host = "10.0.0.1"

    def is_connected(self):
        return self.connected


def make_farm(names, required=None):
    farm = FarmManager([])
    for name in names:
        farm._printers[name] = FakeClient()
        farm._printer_types[name] = "bambulab"
        farm._plate_required[name] = True if required is None else required
    return farm


# ── Camera manager ─────────────────────────────────────

class CameraManagerTests(unittest.TestCase):
    def test_idle_camera_is_stopped_and_viewed_camera_kept(self):
        manager = CameraManager(idle_seconds=60)
        watched, idle = FakeCam(), FakeCam()
        manager._cameras = {"watched": watched, "idle": idle}
        now = time.monotonic()
        manager._last_viewed = {"watched": now - 100, "idle": now - 100}
        manager.touch("watched")
        self.assertEqual(manager.reap_idle(), ["idle"])
        self.assertTrue(idle.stopped)
        self.assertFalse(watched.stopped)
        self.assertIn("watched", manager._cameras)
        manager.stop_all()


class CameraEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cameras = CameraManager(idle_seconds=0)
        self.cam = FakeCam()
        self.cameras._cameras = {"Printer-1": self.cam, "Staff-Printer": FakeCam()}
        farm = FakeFarm()
        config = {
            "web": {"secret_key": "test-secret"},
            "printers": [
                {"name": "Printer-1", "type": "klipper"},
                {"name": "Staff-Printer", "type": "klipper", "staff_only": True},
            ],
            "student_access": {"allowlist": [], "banlist": []},
        }
        self.app = create_app(farm, FakeQueue(self.temp_dir.name),
                              camera_manager=self.cameras, config=config)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _client(self, role="student", username="carol"):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session.update(role=role, username=username, display_name=username)
        return client

    def test_snapshot_returns_304_when_frame_unchanged(self):
        client = self._client()
        first = client.get("/api/camera/Printer-1/snapshot")
        self.assertEqual(first.status_code, 200)
        etag = first.headers["ETag"]
        again = client.get("/api/camera/Printer-1/snapshot", headers={"If-None-Match": etag})
        self.assertEqual(again.status_code, 304)
        self.cam.frame_at = 999.0
        changed = client.get("/api/camera/Printer-1/snapshot", headers={"If-None-Match": etag})
        self.assertEqual(changed.status_code, 200)

    def test_snapshot_marks_camera_as_viewed(self):
        self._client().get("/api/camera/Printer-1/snapshot")
        self.assertIn("Printer-1", self.cameras._last_viewed)

    def test_students_cannot_view_staff_only_cameras(self):
        self.assertEqual(self._client().get("/api/camera/Staff-Printer/snapshot").status_code, 403)
        staff = self._client(role="staff", username="teacher")
        self.assertEqual(staff.get("/api/camera/Staff-Printer/snapshot").status_code, 200)

    def test_only_staff_can_force_stop_a_shared_camera(self):
        self.assertEqual(self._client().post("/api/camera/Printer-1/stop").status_code, 403)
        self.assertFalse(self.cam.stopped)
        staff = self._client(role="staff", username="teacher")
        self.assertEqual(staff.post("/api/camera/Printer-1/stop").status_code, 200)
        self.assertTrue(self.cam.stopped)

    def test_mjpeg_streams_are_capped(self):
        client = self._client()
        open_streams = [client.get("/api/camera/Printer-1/stream", buffered=False) for _ in range(4)]
        self.assertTrue(all(r.status_code == 200 for r in open_streams))
        for r in open_streams:
            next(r.response)  # start each generator so it holds its slot
        self.assertEqual(client.get("/api/camera/Printer-1/stream", buffered=False).status_code, 503)
        for r in open_streams:
            r.close()
        freed = client.get("/api/camera/Printer-1/stream", buffered=False)
        self.assertEqual(freed.status_code, 200)
        freed.close()


# ── Build plate tracking ───────────────────────────────

class PlateTrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.queue = JobQueue(
            db_path=os.path.join(self.temp_dir.name, "farm.db"),
            upload_dir=self.temp_dir.name,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run(self, farm, name, *statuses):
        for status in statuses:
            farm._printers[name].state.status = status
            farm.update_plate_states()

    def test_finished_print_requires_confirmation_and_blocks_dispatch(self):
        farm = make_farm(["P1"])
        farm.load_plate_states(self.queue)
        self._run(farm, "P1", PrintStatus.IDLE, PrintStatus.RUNNING, PrintStatus.FINISH)
        self.assertTrue(farm.plate_needs_clearing("P1"))
        self.assertNotIn("P1", farm.get_idle_printers())
        self.assertTrue(farm.confirm_plate_clear("P1", "alice"))
        self.assertFalse(farm.plate_needs_clearing("P1"))
        self.assertIn("P1", farm.get_idle_printers())

    def test_failed_print_also_requires_confirmation(self):
        farm = make_farm(["P1"])
        self._run(farm, "P1", PrintStatus.IDLE, PrintStatus.RUNNING, PrintStatus.FAILED)
        state = farm.get_plate_state("P1")
        self.assertTrue(state["plate_needs_clear"])
        self.assertEqual(state["plate_finished_status"], "FAILED")

    def test_state_survives_restart(self):
        farm = make_farm(["P1"])
        farm.load_plate_states(self.queue)
        self._run(farm, "P1", PrintStatus.RUNNING, PrintStatus.FINISH)
        restarted = make_farm(["P1"])
        restarted.load_plate_states(self.queue)
        self.assertTrue(restarted.plate_needs_clearing("P1"))

    def test_print_started_at_printer_counts_as_cleared(self):
        farm = make_farm(["P1"])
        self._run(farm, "P1", PrintStatus.RUNNING, PrintStatus.FINISH, PrintStatus.RUNNING)
        self.assertFalse(farm.plate_needs_clearing("P1"))

    def test_first_sighting_of_finished_printer_is_flagged(self):
        farm = make_farm(["P1"])
        self._run(farm, "P1", PrintStatus.FINISH)
        self.assertTrue(farm.plate_needs_clearing("P1"))

    def test_first_sighting_after_unknown_report_is_flagged(self):
        farm = make_farm(["P1"])
        self._run(farm, "P1", PrintStatus.UNKNOWN, PrintStatus.FINISH)
        self.assertTrue(farm.plate_needs_clearing("P1"))

    def test_cleared_printer_still_finished_is_not_reflagged(self):
        farm = make_farm(["P1"])
        self._run(farm, "P1", PrintStatus.FINISH)
        farm.confirm_plate_clear("P1", "alice")
        self._run(farm, "P1", PrintStatus.FINISH, PrintStatus.FINISH)
        self.assertFalse(farm.plate_needs_clearing("P1"))

    def test_idle_printer_is_not_flagged(self):
        farm = make_farm(["P1"])
        self._run(farm, "P1", PrintStatus.IDLE, PrintStatus.IDLE)
        self.assertFalse(farm.plate_needs_clearing("P1"))

    def test_check_can_be_disabled_per_printer(self):
        farm = make_farm(["P1"], required=False)
        self._run(farm, "P1", PrintStatus.RUNNING, PrintStatus.FINISH)
        self.assertFalse(farm.plate_needs_clearing("P1"))
        self.assertIn("P1", farm.get_idle_printers())


# ── Plate endpoints ────────────────────────────────────

class PlateFarm(FakeFarm):
    def __init__(self):
        super().__init__()
        self.needs_clear = True
        self.cleared_by = None
        self.required = True

    def get_effective_status(self, name):
        return PrintStatus.IDLE

    def plate_needs_clearing(self, name):
        return self.needs_clear

    def confirm_plate_clear(self, name, who):
        if name != "Printer-1":
            return False
        self.needs_clear = False
        self.cleared_by = who
        return True

    def set_plate_clear_required(self, name, required):
        self.required = required

    def get_plate_state(self, name):
        return {"plate_needs_clear": self.needs_clear, "plate_clear_required": self.required}


class PlateEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        job_file = os.path.join(self.temp_dir.name, "job.gcode")
        with open(job_file, "w") as handle:
            handle.write("G28\n")
        self.queue = FakeQueue(self.temp_dir.name)
        self.queue.jobs = [{
            "id": 1, "status": "queued", "submitted_by": "alice", "printer_name": "",
            "file_path": job_file, "filename": "job.gcode", "original_name": "job.gcode",
        }]
        self.queue.assign_job = lambda job_id, printer: True
        self.farm = PlateFarm()
        self.config_path = os.path.join(self.temp_dir.name, "config.yaml")
        os.environ["FARM_CONFIG"] = self.config_path
        config = {
            "web": {"secret_key": "test-secret"},
            "printers": [{"name": "Printer-1", "type": "klipper"}],
            "student_access": {"allowlist": ["alice"], "banlist": []},
        }
        self.app = create_app(self.farm, self.queue, config=config)

    def tearDown(self):
        os.environ.pop("FARM_CONFIG", None)
        self.temp_dir.cleanup()

    def _client(self, username, role="student"):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session.update(role=role, username=username, display_name=username)
        return client

    def test_sending_to_uncleared_printer_is_refused(self):
        response = self._client("alice").post("/api/jobs/1/assign", json={"printer": "Printer-1"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("build plate", response.get_json()["error"])

    def test_approved_student_can_confirm_plate_cleared(self):
        response = self._client("alice").post("/api/printer/Printer-1/plate_cleared")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.farm.cleared_by, "alice")
        self.assertFalse(self.farm.needs_clear)

    def test_unapproved_student_cannot_confirm_plate_cleared(self):
        response = self._client("carol").post("/api/printer/Printer-1/plate_cleared")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(self.farm.needs_clear)

    def test_only_staff_can_turn_the_plate_check_off(self):
        self.assertEqual(
            self._client("alice").post("/api/printer/Printer-1/plate_clear_required",
                                       json={"required": False}).status_code,
            403,
        )
        staff = self._client("teacher", role="staff")
        response = staff.post("/api/printer/Printer-1/plate_clear_required", json={"required": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.farm.required)


if __name__ == "__main__":
    unittest.main()


# ── Printer card job thumbnail ─────────────────────────

class ThumbFarm(FakeFarm):
    def get_printer(self, name):
        return self.printer if name in ("Printer-1", "Staff-Printer") else None


class ThumbQueue(FakeQueue):
    def get_active_jobs(self):
        return [
            {"id": 7, "status": "printing", "printer_name": "Printer-1", "file_path": "/uploads/job.3mf"},
            {"id": 8, "status": "printing", "printer_name": "Staff-Printer", "file_path": "/uploads/job.3mf"},
        ]


class FakeLibrary:
    def __init__(self, thumb_path):
        self.thumb_path = thumb_path

    def find_by_path(self, file_path):
        return {"thumbnail_path": self.thumb_path} if file_path == "/uploads/job.3mf" else None


class PrinterThumbnailTests(unittest.TestCase):
    PNG = b"\x89PNG\r\n\x1a\nthumb"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        thumb = os.path.join(self.temp_dir.name, "job.thumb.png")
        with open(thumb, "wb") as fh:
            fh.write(self.PNG)
        config = {
            "web": {"secret_key": "test-secret"},
            "printers": [
                {"name": "Printer-1", "type": "klipper"},
                {"name": "Staff-Printer", "type": "klipper", "staff_only": True},
                {"name": "Idle-Printer", "type": "klipper"},
            ],
            "student_access": {"allowlist": [], "banlist": []},
        }
        self.app = create_app(ThumbFarm(), ThumbQueue(self.temp_dir.name),
                              config=config, file_library=FakeLibrary(thumb))

    def tearDown(self):
        self.temp_dir.cleanup()

    def _client(self, role="student"):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session.update(role=role, username="carol", display_name="carol")
        return client

    def test_returns_thumbnail_of_running_job(self):
        res = self._client().get("/api/printer/Printer-1/thumbnail")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, self.PNG)
        self.assertEqual(res.mimetype, "image/png")
        res.close()

    def test_missing_job_or_printer_is_404(self):
        self.assertEqual(self._client().get("/api/printer/Idle-Printer/thumbnail").status_code, 404)
        self.assertEqual(self._client().get("/api/printer/Nope/thumbnail").status_code, 404)

    def test_requires_login_and_respects_staff_only(self):
        self.assertNotEqual(self.app.test_client().get("/api/printer/Printer-1/thumbnail").status_code, 200)
        self.assertEqual(self._client().get("/api/printer/Staff-Printer/thumbnail").status_code, 403)
        staff_res = self._client(role="staff").get("/api/printer/Staff-Printer/thumbnail")
        self.assertEqual(staff_res.status_code, 200)
        staff_res.close()
