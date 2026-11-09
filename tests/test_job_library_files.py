"""Deleting jobs and library files never pulls a file out from under a live job."""
import os
import tempfile
import unittest

from src.file_library import FileLibrary
from src.job_queue import JobQueue
from src.virtual_printer import VirtualFTPServer
from src.web import create_app
from test_security import FakeFarm


class SharedFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = os.path.join(self.tmp.name, "farm.db")
        self.queue = JobQueue(db, self.tmp.name)
        self.lib = FileLibrary(db, self.tmp.name)
        self.path = os.path.join(self.tmp.name, "abc_model.gcode")
        with open(self.path, "w") as fh:
            fh.write("G1 X1\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _job(self):
        return self.queue.add_job(filename="abc_model.gcode", original_name="model.gcode", file_path=self.path)

    def test_deleting_one_job_keeps_file_for_another_queued_job(self):
        first, second = self._job(), self._job()
        self.assertTrue(self.queue.delete_job(first))
        self.assertTrue(os.path.exists(self.path))
        self.assertTrue(self.queue.delete_job(second))
        self.assertFalse(os.path.exists(self.path))

    def test_finished_jobs_do_not_keep_the_file(self):
        first, second = self._job(), self._job()
        self.queue.cancel_job(second)
        self.queue.delete_job(first)
        self.assertFalse(os.path.exists(self.path))

    def test_library_file_survives_job_deletion(self):
        self.lib.add_file("model.gcode", "abc_model.gcode", self.path, 6)
        self.queue.delete_job(self._job())
        self.assertTrue(os.path.exists(self.path))

    def test_library_refuses_delete_while_job_queued(self):
        file_id = self.lib.add_file("model.gcode", "abc_model.gcode", self.path, 6)
        self._job()
        self.assertFalse(self.lib.delete_file(file_id)["ok"])
        self.assertTrue(os.path.exists(self.path))


class JobDeleteRouteTests(SharedFileTests):
    def _app(self):
        config = {"web": {"secret_key": "t"}, "printers": [], "student_access": {"allowlist": [], "banlist": []}}
        app = create_app(FakeFarm(), self.queue, config=config, file_library=self.lib, admin_api_key="k" * 24)
        client = app.test_client()
        with client.session_transaction() as s:
            s.update(role="staff", admin=True, username="teacher")
        return client

    def test_job_delete_reports_library_file_kept_for_other_job(self):
        self.lib.add_file("model.gcode", "abc_model.gcode", self.path, 6)
        first, _second = self._job(), self._job()
        res = self._app().delete(f"/api/jobs/{first}/delete?delete_library=true")
        body = res.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["library_kept"]), 1)
        self.assertTrue(os.path.exists(self.path))

    def test_bulk_job_delete_removes_library_copy_when_unused(self):
        self.lib.add_file("model.gcode", "abc_model.gcode", self.path, 6)
        job = self._job()
        res = self._app().post("/api/jobs/bulk_delete", json={"ids": [job], "delete_library": True})
        self.assertEqual(res.get_json()["library_kept"], [])
        self.assertIsNone(self.lib.find_by_path(self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_api_v1_library_delete_returns_conflict_when_in_use(self):
        file_id = self.lib.add_file("model.gcode", "abc_model.gcode", self.path, 6)
        self._job()
        client = self._app()
        res = client.delete(f"/api/v1/library/files/{file_id}", headers={"X-Api-Key": "k" * 24})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(client.delete("/api/v1/library/files/9999", headers={"X-Api-Key": "k" * 24}).status_code, 404)


class VirtualPrinterLibraryTests(unittest.TestCase):
    def test_orca_upload_is_added_to_library_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = FileLibrary(os.path.join(tmp, "farm.db"), tmp)
            path = os.path.join(tmp, "hash_part.gcode")
            with open(path, "w") as fh:
                fh.write("G1 X1\n")
            ftp = VirtualFTPServer.__new__(VirtualFTPServer)
            ftp.name = ftp.printer_name = "P1S-01"
            ftp.file_library = lib
            ftp._add_to_library(path, "part.gcode", {})
            ftp._add_to_library(path, "part.gcode", {})
            files = lib.get_files(-1)
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0]["original_name"], "part.gcode")
            self.assertEqual(files[0]["uploaded_by"], "orca_virtual/P1S-01")


class JobFilamentsAuthTests(unittest.TestCase):
    def test_job_filaments_requires_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = JobQueue(os.path.join(tmp, "farm.db"), tmp)
            path = os.path.join(tmp, "a.gcode")
            with open(path, "w") as fh:
                fh.write("; filament_type = PLA\nG1 X1\n")
            job_id = queue.add_job(filename="a.gcode", original_name="a.gcode", file_path=path)
            config = {"web": {"secret_key": "t"}, "printers": [], "student_access": {"allowlist": [], "banlist": []}}
            app = create_app(FakeFarm(), queue, config=config)
            self.assertEqual(app.test_client().get(f"/api/jobs/{job_id}/filaments").status_code, 401)
            staff = app.test_client()
            with staff.session_transaction() as s:
                s.update(role="staff", admin=True, username="teacher")
            self.assertEqual(staff.get(f"/api/jobs/{job_id}/filaments").status_code, 200)
