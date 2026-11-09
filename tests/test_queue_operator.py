"""Approved (allowlisted) students can work on the shared print queue."""
import tempfile
import unittest

from src.web import create_app
from test_security import FakeFarm, FakeQueue


class OperatorQueue(FakeQueue):
    def __init__(self, upload_dir):
        super().__init__(upload_dir)
        self.jobs += [
            {"id": 3, "status": "queued", "submitted_by": "", "printer_name": "",
             "file_path": "/secret/orca.gcode", "filename": "orca.gcode",
             "original_name": "orca.gcode"},
            {"id": 4, "status": "printing", "submitted_by": "bob", "printer_name": "Printer-1",
             "file_path": "/secret/busy.gcode", "filename": "busy.gcode",
             "original_name": "busy.gcode"},
            {"id": 5, "status": "queued", "submitted_by": "", "printer_name": "Staff-Printer",
             "file_path": "/secret/staff.gcode", "filename": "staff.gcode",
             "original_name": "staff.gcode"},
        ]
        self.cancelled = []

    def get_queued_jobs(self):
        return [j for j in self.jobs if j["status"] == "queued"]

    def cancel_job(self, job_id):
        self.cancelled.append(job_id)
        return True


class ControlPrinter:
    def __init__(self):
        self.state = type("S", (), {"chamber_light": False})()
        self.bed = None

    def is_connected(self):
        return True

    def set_chamber_light(self, on):
        self.state.chamber_light = on
        return True

    def set_bed_temperature(self, temp):
        self.bed = temp
        return True


class ControlFarm(FakeFarm):
    def __init__(self):
        super().__init__()
        self.printers = {"Printer-1": ControlPrinter(), "Staff-Printer": ControlPrinter()}

    def get_printer(self, name):
        return self.printers.get(name)


class QueueOperatorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.queue = OperatorQueue(self.temp_dir.name)
        config = {
            "web": {"secret_key": "test-secret"},
            "printers": [
                {"name": "Printer-1", "type": "klipper"},
                {"name": "Staff-Printer", "type": "klipper", "staff_only": True},
            ],
            "student_access": {"allowlist": ["Alice.Smith"], "banlist": []},
        }
        self.farm = ControlFarm()
        self.app = create_app(self.farm, self.queue, config=config)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _client(self, username):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session.update(role="student", username=username, display_name=username)
        return client

    def test_auth_status_flags_queue_operator(self):
        self.assertTrue(self._client("alice.smith").get("/api/auth/status").get_json()["queue_operator"])
        self.assertFalse(self._client("carol").get("/api/auth/status").get_json()["queue_operator"])

    def test_operator_sees_shared_queue_without_staff_only_jobs(self):
        ids = [j["id"] for j in self._client("alice.smith").get("/api/jobs/queued").get_json()]
        self.assertEqual(sorted(ids), [1, 2, 3])

    def test_unapproved_student_sees_only_own_queued_jobs(self):
        ids = [j["id"] for j in self._client("bob").get("/api/jobs/queued").get_json()]
        self.assertEqual(ids, [2])

    def test_operator_can_cancel_unowned_queued_job(self):
        response = self._client("alice.smith").post("/api/jobs/3/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.queue.cancelled, [3])

    def test_operator_cannot_cancel_someone_elses_running_print(self):
        response = self._client("alice.smith").post("/api/jobs/4/cancel")
        self.assertEqual(response.status_code, 403)

    def test_operator_cannot_touch_staff_only_printer_job(self):
        response = self._client("alice.smith").post("/api/jobs/5/cancel")
        self.assertEqual(response.status_code, 403)

    def test_unapproved_student_cannot_cancel_others_queued_job(self):
        response = self._client("carol").post("/api/jobs/3/cancel")
        self.assertEqual(response.status_code, 403)

    def test_operator_cannot_delete_jobs(self):
        response = self._client("alice.smith").post("/api/jobs/3/delete")
        self.assertEqual(response.status_code, 403)

    def test_approved_student_can_use_printer_card_controls(self):
        client = self._client("alice.smith")
        self.assertEqual(client.post("/api/printer/Printer-1/light").status_code, 200)
        self.assertTrue(self.farm.printers["Printer-1"].state.chamber_light)
        response = client.post("/api/printer/Printer-1/bed_temp", json={"temp": 60})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.farm.printers["Printer-1"].bed, 60)

    def test_unapproved_student_printer_controls_are_read_only(self):
        client = self._client("carol")
        self.assertEqual(client.post("/api/printer/Printer-1/light").status_code, 403)
        self.assertEqual(client.post("/api/printer/Printer-1/bed_temp", json={"temp": 60}).status_code, 403)
        self.assertFalse(self.farm.printers["Printer-1"].state.chamber_light)
        # Cards and cameras remain viewable for every logged-in student.
        self.assertEqual(client.get("/api/farm/status").status_code, 200)

    def test_approved_student_cannot_control_staff_only_printer(self):
        client = self._client("alice.smith")
        self.assertEqual(client.post("/api/printer/Staff-Printer/light").status_code, 403)

    def test_approved_student_cannot_change_staff_only_flag(self):
        client = self._client("alice.smith")
        response = client.post("/api/printer/Printer-1/staff_only", json={"staff_only": False})
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
