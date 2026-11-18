"""Jobs detached from removed printers must not block deployment forever."""
import os
import tempfile
import unittest
from unittest.mock import patch

from src.farm_manager import FarmManager
from src.job_queue import JobQueue
from src.main import cmd_run
from src.web import create_app
from test_security import FakeFarm


class OrphanedJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.queue = JobQueue(os.path.join(self.temp.name, "farm.db"), self.temp.name)

    def active_job(self, printer, printing=True, notes=""):
        job_id = self.queue.add_job("part.3mf", "part.3mf", "/part.3mf", notes=notes)
        self.queue.assign_job(job_id, printer)
        if printing:
            self.queue.mark_printing(job_id)
        return job_id

    def test_removed_printer_jobs_fail_without_claiming_completion(self):
        for printing in (False, True):
            with self.subTest(printing=printing):
                job_id = self.active_job("Removed", printing, notes="Keep this note")
                self.assertEqual(self.queue.fail_orphaned_jobs(["Printer-1"]), [job_id])
                job = self.queue.get_job(job_id)
                self.assertEqual(job["status"], "failed")
                self.assertEqual(job["copies_done"], 0)
                self.assertIsNotNone(job["completed_at"])
                self.assertIn("Keep this note\nTracking ended:", job["notes"])
                self.assertIn("no longer configured", job["notes"])
                self.assertEqual(self.queue.fail_orphaned_jobs(["Printer-1"]), [])

    def test_configured_jobs_remain_active_even_without_connectivity(self):
        job_id = self.active_job("Offline")
        self.assertEqual(self.queue.fail_orphaned_jobs(["Offline"]), [])
        self.assertEqual(self.queue.get_job(job_id)["status"], "printing")

    def test_queued_and_historical_jobs_are_preserved(self):
        queued = self.queue.add_job("part.3mf", "part.3mf", "/part.3mf", printer_name="Removed")
        completed = self.active_job("Removed")
        self.queue.mark_completed(completed)
        history = self.queue.get_job(completed)
        self.assertEqual(self.queue.fail_orphaned_jobs([]), [])
        self.assertEqual(self.queue.get_job(queued)["status"], "queued")
        self.assertEqual(self.queue.get_job(completed), history)

    def test_removing_last_printer_clears_active_records(self):
        job_id = self.active_job("Removed")
        self.assertEqual(self.queue.fail_orphaned_jobs([]), [job_id])
        self.assertEqual(self.queue.get_active_jobs(), [])

    def test_remove_route_reconciles_jobs_after_saving_configuration(self):
        job_id = self.active_job("Removed")
        config = {"web": {"secret_key": "test"}, "printers": [{"name": "Removed"}]}
        client = create_app(FarmManager([]), self.queue, config=config).test_client()
        with client.session_transaction() as session:
            session.update(role="staff", username="teacher")
        with (patch("src.web.load_yaml_config", return_value=config),
              patch("src.web.save_yaml_config") as save,
              patch("src.web.os.path.isfile", return_value=True),
              patch("src.web.os.access", return_value=True),
              patch("src.web.os.geteuid", return_value=0),
              patch("src.web.subprocess.run") as run):
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            run.return_value.stderr = ""
            response = client.post("/api/discover/remove", json={"name": "Removed"})
        self.assertTrue(response.get_json()["ok"])
        save.assert_called_once()
        self.assertEqual(run.call_args.args[0][-2:], ["orca-remove", "Removed"])
        self.assertEqual(self.queue.get_job(job_id)["status"], "failed")

    def test_failed_configuration_save_does_not_close_jobs(self):
        job_id = self.active_job("Removed")
        config = {"web": {"secret_key": "test"}, "printers": [{"name": "Removed"}]}
        client = create_app(FarmManager([]), self.queue, config=config).test_client()
        with client.session_transaction() as session:
            session.update(role="staff", username="teacher")
        with (patch("src.web.load_yaml_config", return_value=config),
              patch("src.web.save_yaml_config", side_effect=OSError("disk full"))):
            self.assertEqual(client.post("/api/discover/remove", json={"name": "Removed"}).status_code, 500)
        self.assertEqual(self.queue.get_job(job_id)["status"], "printing")

    def test_startup_reconciles_before_connecting_or_serving_requests(self):
        orphan = self.active_job("Removed")
        configured = self.active_job("Printer-1")
        config = {
            "printers": [{"name": "Printer-1", "type": "klipper", "host": "localhost"}],
            "queue": {"db_path": self.queue.db_path, "upload_dir": self.temp.name},
        }
        with patch("src.main.FileLibrary", side_effect=RuntimeError("stop before connections")):
            with self.assertRaisesRegex(RuntimeError, "stop before connections"):
                cmd_run(None, config)
        self.assertEqual(self.queue.get_job(orphan)["status"], "failed")
        self.assertEqual(self.queue.get_job(configured)["status"], "printing")

    def test_reconciliation_unblocks_update_but_keeps_printer_safety_checks(self):
        self.active_job("Removed")
        farm = FakeFarm()
        config = {"web": {"secret_key": "test"}, "printers": [{"name": "Printer-1"}]}
        client = create_app(farm, self.queue, config=config).test_client()
        with client.session_transaction() as session:
            session.update(role="staff", username="teacher")
        self.assertEqual(client.post("/api/update/apply").status_code, 409)
        self.queue.fail_orphaned_jobs(["Printer-1"])
        with (patch("src.web.os.path.isfile", return_value=True),
              patch("src.web.os.access", return_value=True),
              patch("src.web.os.geteuid", return_value=0),
              patch("src.web.subprocess.run") as run,
              patch("src.web.threading.Thread")):
            run.return_value.returncode = 0
            run.return_value.stdout = "Already up to date"
            run.return_value.stderr = ""
            self.assertTrue(client.post("/api/update/apply").get_json()["ok"])
            run.reset_mock()
            for connected, status in ((False, "IDLE"), (True, "RUNNING"), (True, "PAUSED")):
                farm.get_all_states = lambda: {
                    "Printer-1": {"connected": connected, "status": status}
                }
                self.assertEqual(client.post("/api/update/apply").status_code, 409)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
