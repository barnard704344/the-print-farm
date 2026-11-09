"""File Library: bulk move/delete, print to a chosen printer, folder names."""
import os
import tempfile
import unittest
from unittest import mock

from src.file_library import FileLibrary
from src.web import create_app
from test_security import FakeFarm, FakeQueue


class LibQueue(FakeQueue):
    def __init__(self, upload_dir):
        super().__init__(upload_dir)
        self.assigned = []

    def assign_job(self, job_id, printer):
        self.assigned.append((job_id, printer))
        return True

    def get_active_jobs(self):
        return []


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = FileLibrary(os.path.join(self.tmp.name, "lib.db"), os.path.join(self.tmp.name, "store"))
        self.ids = []
        for i in range(3):
            path = os.path.join(self.tmp.name, f"part{i}.gcode")
            with open(path, "w") as fh:
                fh.write("G1 X1\n")
            self.ids.append(self.lib.add_file(f"part{i}.gcode", f"s{i}.gcode", path, 6))
        self.folder = self.lib.create_folder("Brackets")["id"]
        self.queue = LibQueue(self.tmp.name)
        config = {"web": {"secret_key": "t"},
                  "printers": [{"name": "Printer-1", "type": "klipper"}],
                  "student_access": {"allowlist": [], "banlist": []}}
        self.app = create_app(FakeFarm(), self.queue, config=config, file_library=self.lib)

    def tearDown(self):
        self.tmp.cleanup()

    def _client(self, role="staff"):
        client = self.app.test_client()
        with client.session_transaction() as s:
            s.update(role=role, username="teacher" if role == "staff" else "carol", admin=role == "staff")
        return client

    def test_bulk_move_files_into_folder_and_back(self):
        client = self._client()
        res = client.post("/api/library/files/bulk-move", json={"file_ids": self.ids[:2], "folder_id": self.folder})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["moved"], 2)
        self.assertEqual({f["id"] for f in self.lib.get_files(self.folder)}, set(self.ids[:2]))
        client.post("/api/library/files/bulk-move", json={"file_ids": self.ids[:2], "folder_id": None})
        self.assertEqual(self.lib.get_files(self.folder), [])

    def test_bulk_move_validates_input(self):
        client = self._client()
        self.assertEqual(client.post("/api/library/files/bulk-move", json={"file_ids": [], "folder_id": None}).status_code, 400)
        self.assertEqual(client.post("/api/library/files/bulk-move", json={"file_ids": self.ids, "folder_id": 999}).status_code, 400)

    def test_bulk_actions_are_staff_only(self):
        student = self._client(role="student")
        self.assertEqual(student.post("/api/library/files/bulk-move", json={"file_ids": self.ids, "folder_id": None}).status_code, 403)
        self.assertEqual(student.post("/api/library/files/bulk-delete", json={"file_ids": self.ids}).status_code, 403)

    def test_bulk_delete(self):
        res = self._client().post("/api/library/files/bulk-delete", json={"file_ids": self.ids[:2]})
        self.assertEqual(res.get_json()["deleted"], 2)
        self.assertEqual([f["id"] for f in self.lib.get_files(None)], [self.ids[2]])

    def test_print_to_chosen_printer_assigns_the_job(self):
        with mock.patch("threading.Thread") as thread:
            res = self._client().post(f"/api/library/files/{self.ids[0]}/print",
                                      json={"printer_name": "Printer-1", "copies": 2})
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        self.assertEqual(body["printer_name"], "Printer-1")
        self.assertEqual(self.queue.assigned, [(body["job_id"], "Printer-1")])
        thread.return_value.start.assert_called_once()

    def test_print_without_printer_only_queues(self):
        res = self._client().post(f"/api/library/files/{self.ids[0]}/print", json={})
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.get_json()["printer_name"])
        self.assertEqual(self.queue.assigned, [])

    def test_print_rejects_bad_copies(self):
        res = self._client().post(f"/api/library/files/{self.ids[0]}/print", json={"copies": 0})
        self.assertEqual(res.status_code, 400)

    def test_folder_names_are_trimmed_and_limited(self):
        self.assertFalse(self.lib.create_folder("x" * 101)["ok"])
        made = self.lib.create_folder('  Tom\'s   "best"  parts ')
        self.assertTrue(made["ok"])
        names = {f["name"] for f in self.lib.get_all_folders()}
        self.assertIn('Tom\'s "best" parts', names)
