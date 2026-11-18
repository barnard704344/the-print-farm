import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import websocket

from src.bambu_client import BambuClient, PrintState, PrintStatus
from src.bambu_obico import (BambuObicoBridge, BambuObicoManager, PrintLedger,
                             linked_printer, websocket_url)
from src.obico_settings import prepare_obico_config, validate_obico_assignment


def config(**changes):
    return dict(server="http://obico.test", printer_id=4, username="operator",
                password="account-secret", auth_token="printer-secret", **changes)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.printer = BambuClient("P1S-1", "192.0.2.1", "lan-secret", "serial")
        self.printer._state = PrintState(status=PrintStatus.RUNNING, subtask_name="part.3mf",
                                         job_id="job-1", mc_percent=25)
        self.printer._last_report_at = time.monotonic()
        self.printer._connected.set()
        self.farm = MagicMock()
        self.farm.get_printer.return_value = self.printer
        self.cameras = MagicMock()
        self.cameras.get_frame_with_time.return_value = (b"jpeg", time.monotonic())
        self.bridge = BambuObicoBridge("P1S-1", config(), self.farm, self.cameras, self.tmp.name)
        self.bridge.ws = MagicMock()
        self.bridge.session = MagicMock()
        self.bridge.session.post.return_value.status_code = 200

    def active_ledger(self):
        msgs, data = self.bridge.ledger.messages(self.printer.state, 1000)
        self.bridge.ledger.save(data)
        return msgs

    def test_print_lifecycle_and_identity_survive_restart(self):
        first = self.active_ledger()[0]
        self.assertEqual(first["event"]["event_type"], "PrintStarted")
        restored = PrintLedger(self.bridge.ledger.path)
        msgs, data = restored.messages(self.printer.state, 2000)
        self.assertEqual(msgs[0]["current_print_ts"], 1000)
        self.assertNotIn("event", msgs[0])
        self.assertEqual(os.stat(restored.path).st_mode & 0o777, 0o600)
        self.printer.state.status = PrintStatus.PAUSED
        msgs, data = restored.messages(self.printer.state, 2001)
        self.assertEqual(msgs[0]["event"]["event_type"], "PrintPaused")
        restored.save(data)
        self.printer.state.status = PrintStatus.RUNNING
        msgs, data = restored.messages(self.printer.state, 2002)
        self.assertEqual(msgs[0]["event"]["event_type"], "PrintResumed")
        restored.save(data)
        self.printer.state.status = PrintStatus.FINISH
        msgs, data = restored.messages(self.printer.state, 3000)
        self.assertEqual([m["current_print_ts"] for m in msgs], [1000, -1])
        self.assertEqual(msgs[0]["event"]["event_type"], "PrintDone")
        restored.save(data)
        self.assertEqual(restored.data, {})

    def test_same_filename_new_job_and_failed_print(self):
        self.active_ledger()
        self.printer.state.job_id = "job-2"
        msgs, data = self.bridge.ledger.messages(self.printer.state, 2000)
        self.assertEqual(msgs[0]["current_print_ts"], 2000)
        self.bridge.ledger.save(data)
        self.printer.state.status = PrintStatus.FAILED
        msgs, _ = self.bridge.ledger.messages(self.printer.state, 2001)
        self.assertEqual(msgs[0]["event"]["event_type"], "PrintFailed")

    def test_snapshots_share_camera_and_reject_old_or_duplicate_frames(self):
        self.bridge.post_snapshot(self.printer)
        self.cameras.start_camera.assert_called_once_with("P1S-1", "192.0.2.1", "lan-secret", 6000, "")
        call = self.bridge.session.post.call_args
        self.assertEqual(call.args[0], "http://obico.test/api/v1/octo/pic/")
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Token printer-secret")
        self.assertEqual(call.kwargs["data"]["viewing_boost"], "false")
        self.assertFalse(call.kwargs["allow_redirects"])
        self.bridge.post_snapshot(self.printer)
        self.assertEqual(self.bridge.session.post.call_count, 1)
        self.cameras.get_frame_with_time.return_value = (b"old", time.monotonic() - 30)
        self.bridge.post_snapshot(self.printer)
        self.assertEqual(self.bridge.session.post.call_count, 1)
        self.printer.state.status = PrintStatus.IDLE
        self.bridge.post_snapshot(self.printer)
        self.assertEqual(self.bridge.session.post.call_count, 1)

    def test_offline_report_cannot_upload_or_control(self):
        self.active_ledger()
        self.bridge.config["allow_remote_control"] = True
        self.printer._last_report_at = time.monotonic() - 100
        self.printer.pause_print = MagicMock()
        self.bridge.post_snapshot(self.printer)
        self.assertIn("offline", self.bridge.command("pause"))
        self.bridge.session.post.assert_not_called()
        self.printer.pause_print.assert_not_called()

    def test_control_is_opt_in_and_checks_job_and_state(self):
        self.active_ledger()
        self.printer.pause_print = MagicMock(return_value=True)
        self.assertIn("disabled", self.bridge.command("pause"))
        self.bridge.config["allow_remote_control"] = True
        self.printer.state.job_id = "different"
        self.assertIn("changed", self.bridge.command("pause"))
        self.printer.state.job_id = "job-1"
        self.assertIsNone(self.bridge.command("pause"))
        self.assertIn("Duplicate", self.bridge.command("pause"))
        self.printer.pause_print.assert_called_once()
        self.assertIn("state", self.bridge.command("resume"))
        self.assertIn("Unsupported", self.bridge.command("start_print"))

    def test_passthru_deduplicates_and_rejects_arbitrary_calls(self):
        self.active_ledger()
        self.bridge.config["allow_remote_control"] = True
        self.printer.pause_print = MagicMock(return_value=True)
        msg = {"passthru": {"target": "printer", "func": "pause", "ref": "one"}}
        self.bridge.receive(msg)
        self.bridge.receive(msg)
        self.printer.pause_print.assert_called_once()
        self.bridge.receive({"passthru": {"target": "printer", "func": "connect", "ref": "two"}})
        self.assertIn("error", json.loads(self.bridge.ws.send.call_args.args[0])["passthru"])
        self.bridge.receive({"passthru": {"target": "printer", "func": "pause", "args": ["injected"], "ref": "three"}})
        self.printer.pause_print.assert_called_once()

    def test_worker_exchanges_status_snapshot_and_command(self):
        self.bridge.config["allow_remote_control"] = True
        ws = MagicMock()
        ws.recv_data.return_value = (websocket.ABNF.OPCODE_TEXT, b'{"commands":[{"cmd":"pause"}]}')
        def pause():
            self.bridge.stop_event.set()
            return True
        self.printer.pause_print = MagicMock(side_effect=pause)
        self.bridge.reader.fetch_status = MagicMock(return_value={"connected": True, "normalized_p": 0.1})
        with patch("src.bambu_obico.linked_printer"), patch("src.bambu_obico.websocket.create_connection", return_value=ws) as connect:
            self.bridge.run()
        messages = [json.loads(x.args[0]) for x in ws.send.call_args_list]
        self.assertEqual(connect.call_args.args[0], "ws://obico.test/ws/dev/")
        self.assertEqual(connect.call_args.kwargs["header"]["authorization"], "bearer printer-secret")
        self.assertTrue(any(m.get("event", {}).get("event_type") == "PrintStarted" for m in messages))
        self.bridge.session.post.assert_called_once()
        self.printer.pause_print.assert_called_once()
        self.assertFalse(self.printer.state.obico["connected"])

    def test_duplicate_token_close_does_not_reconnect(self):
        ws = MagicMock()
        ws.recv_data.return_value = (websocket.ABNF.OPCODE_CLOSE, (4321).to_bytes(2, "big"))
        self.bridge.reader.fetch_status = MagicMock(return_value={"connected": True})
        with patch("src.bambu_obico.linked_printer"), patch("src.bambu_obico.websocket.create_connection", return_value=ws) as connect:
            self.bridge.run()
        connect.assert_called_once()
        self.assertIn("already in use", self.printer.state.obico["error"])

    def test_multiple_printers_have_independent_workers_and_credentials(self):
        manager = BambuObicoManager(self.farm, self.cameras, self.tmp.name)
        with patch.object(BambuObicoBridge, "start"), patch.object(BambuObicoBridge, "stop", return_value=True):
            manager.configure("P1S-1", config())
            other = config()
            other.update(auth_token="second-secret", printer_id=5)
            manager.configure("P1S-2", other)
            self.assertNotEqual(manager.bridges["P1S-1"].ledger.path, manager.bridges["P1S-2"].ledger.path)
            self.assertEqual(manager.bridges["P1S-2"].config["auth_token"], "second-secret")
            manager.configure("P1S-1", None)
            self.assertEqual(list(manager.bridges), ["P1S-2"])
            manager.stop_all()

    def test_partial_mqtt_reports_preserve_job_identity(self):
        self.printer._update_state({"task_id": "task", "subtask_id": "subtask", "gcode_state": "RUNNING"})
        self.printer._update_state({"mc_percent": 30})
        self.assertEqual(self.printer.state.job_id, "task:subtask")
        self.assertLess(self.printer.last_report_age, 1)

    def test_reconnect_waits_for_actual_print_telemetry(self):
        self.printer._reconnect_backoff = 1
        self.printer._handle_disconnect(None, None)
        self.printer._connected.set()
        self.printer._update_state({"bed_temper": 30})
        self.assertIsNone(self.printer.last_report_age)
        self.printer._update_state({"gcode_state": "RUNNING"})
        self.assertLess(self.printer.last_report_age, 1)

    def test_camera_upload_failure_does_not_drop_the_agent(self):
        self.bridge.session.post.side_effect = OSError("network problem")
        self.bridge.post_snapshot(self.printer)
        self.assertIn("Camera upload failed", self.printer.state.obico["camera_error"])

    def test_partial_task_identity_does_not_start_another_obico_print(self):
        self.printer.state.job_id = "task:"
        self.active_ledger()
        self.printer.state.job_id = "task:subtask"
        self.printer.state.subtask_name = "reported-name.3mf"
        messages, _ = self.bridge.ledger.messages(self.printer.state, 2000)
        self.assertEqual(messages[0]["current_print_ts"], 1000)
        self.assertNotIn("event", messages[0])

    def test_repeated_socket_failures_back_off_without_new_print_identity(self):
        ws = MagicMock()
        ws.recv_data.return_value = (websocket.ABNF.OPCODE_CLOSE, (1001).to_bytes(2, "big"))
        self.bridge.reader.fetch_status = MagicMock(return_value={"connected": True})
        with (patch("src.bambu_obico.linked_printer"),
              patch("src.bambu_obico.websocket.create_connection", return_value=ws),
              patch.object(self.bridge.stop_event, "wait", side_effect=[False, False, True]) as wait):
            self.bridge.run()
        self.assertEqual([c.args[0] for c in wait.call_args_list], [2, 4, 8])
        sent = [json.loads(c.args[0]) for c in ws.send.call_args_list]
        self.assertEqual(sum(m.get("event", {}).get("event_type") == "PrintStarted" for m in sent), 1)


class SettingsTests(unittest.TestCase):
    def test_saved_credentials_are_masked_and_bound_to_target(self):
        saved = config()
        data = {**saved, "password": "********", "auth_token": "********"}
        self.assertEqual(prepare_obico_config(data, saved, "bambulab")["auth_token"], "printer-secret")
        with self.assertRaisesRegex(ValueError, "Re-enter the agent"):
            prepare_obico_config({**data, "printer_id": 9}, saved, "bambulab")
        with self.assertRaisesRegex(ValueError, "Re-enter the Obico password"):
            prepare_obico_config({**data, "server": "https://other.test"}, saved, "bambulab")

    def test_unique_printer_assignment_and_token(self):
        printers = [{"name": "other", "obico": config()}]
        with self.assertRaises(ValueError):
            validate_obico_assignment("new", config(), printers)
        other = config()
        other["printer_id"] = 9
        with self.assertRaisesRegex(ValueError, "own Obico agent token"):
            validate_obico_assignment("new", other, printers)

    def test_agent_test_verifies_token_printer_and_disallows_redirects(self):
        session = MagicMock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {"printer": {"id": 4, "name": "P1S"}}
        self.assertEqual(linked_printer(config(), session)["id"], 4)
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        session.get.return_value.json.return_value = {"printer": {"id": 8}}
        with self.assertRaisesRegex(ValueError, "different"):
            linked_printer(config(), session)

    def test_base_path_and_secure_websocket_and_invalid_url(self):
        self.assertEqual(websocket_url("https://example.test/obico/"), "wss://example.test/obico/ws/dev/")
        for url in ["ftp://example.test", "https://user:password@example.test", "https://example.test?token=secret"]:
            with self.assertRaises(ValueError):
                websocket_url(url)


if __name__ == "__main__":
    unittest.main()
