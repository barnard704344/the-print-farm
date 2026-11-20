"""Regression replay for slot-2 runout/purge failure; never connect to hardware."""
import tempfile
import time
import unittest
from unittest.mock import Mock

from src.bambu_client import BambuClient, PrintStatus
from src.bambu_errors import recovery_state
from src.job_queue import JobQueue
import test_filament_load_unload as filament_tests

RUNOUT = {
    'gcode_state': 'PAUSE', 'mc_print_sub_stage': 4,
    'print_error': 0x07008013, 'ams_status': 263,
    'ams': {'tray_now': '254', 'tray_tar': '255', 'tray_exist_bits': 'f'},
    'hms': [{'attr': 0x07002100, 'code': 0x00020005, 'action': 0}],
    'nozzle_target_temper': 42,
}


def client():
    c = BambuClient('Printer-1', 'invalid.test', 'unused', 'unused')
    c._connected.set()  # simulate connection flag, without constructing MQTT
    return c


class TelemetryRegression(unittest.TestCase):
    def test_actual_slot2_error_is_readable_and_requires_filament_recovery(self):
        c = client(); c._update_state(RUNOUT)
        self.assertEqual(c.state.status, PrintStatus.PAUSE_FILAMENT)
        self.assertIn('slot 2', c.state.error_message)
        self.assertIn('toolhead', c.state.error_message)
        self.assertEqual(c.state.ams_recovery['source_tray'], 1)
        self.assertTrue(c.state.ams_recovery['can_retry'])
        self.assertFalse(c.state.ams_recovery['can_confirm_extrusion'])
        self.assertIn('Retry AMS', c.filament_change_blocked())

    def test_running_during_unresolved_recovery_is_not_presented_as_printing(self):
        c = client(); c._update_state(RUNOUT)
        c._update_state({'gcode_state': 'RUNNING', 'print_error': 0})
        self.assertEqual(c.state.gcode_state, 'RUNNING')
        self.assertEqual(c.state.status, PrintStatus.PAUSE_FILAMENT)
        self.assertFalse(c.resume_print())
        self.assertIn('Retry AMS', c.state.command_status['message'])

    def test_partial_temperature_packet_does_not_erase_error_or_ams_phase(self):
        c = client(); c._update_state(RUNOUT); c._update_state({'nozzle_temper': 100})
        self.assertEqual(c.state.raw_data['ams_status'], 263)
        self.assertEqual(c.state.raw_data['ams']['tray_tar'], '255')
        self.assertIn('slot 2', c.state.error_message)
        self.assertEqual(c.state.status, PrintStatus.PAUSE_FILAMENT)

    def test_explicit_clear_returns_to_running_but_keeps_normal_ams_progress(self):
        c = client(); c._update_state(RUNOUT)
        c._update_state({'gcode_state': 'RUNNING', 'print_error': 0, 'hms': [], 'ams_status': 258})
        self.assertEqual(c.state.status, PrintStatus.RUNNING)
        self.assertEqual(c.state.error_message, '')
        self.assertFalse(c.state.ams_recovery['active'])
        self.assertTrue(c.state.ams_recovery['busy'])
        c._update_state({'ams_status': 0})
        self.assertFalse(c.state.ams_recovery['busy'])

    def test_informational_runout_purge_does_not_stop_automatic_filament_change(self):
        c = client(); c._update_state({'gcode_state': 'RUNNING', 'ams_status': 263,
            'hms': [{'attr': 0x07002100, 'code': 0x00030001}]})
        self.assertEqual(c.state.status, PrintStatus.RUNNING)
        self.assertFalse(c.state.ams_recovery['active'])
        self.assertEqual(c.state.ams_recovery['source_label'], 'AMS A slot 2')

    def test_finished_print_is_not_resurrected_by_retained_hms(self):
        c = client(); c._update_state(RUNOUT); c._update_state({'gcode_state': 'FINISH'})
        self.assertEqual(c.state.status, PrintStatus.FINISH)
        self.assertTrue(c.state.ams_recovery['busy'])  # independent AMS lifecycle
        c._update_state({'ams_status': 0, 'hms': [], 'print_error': 0})
        self.assertFalse(c.state.ams_recovery['can_retry'])

    def test_manual_slot4_load_after_finished_job_retains_target_and_previous_source(self):
        c = client(); c._update_state({'gcode_state':'FINISH','ams_status':260,
            'print_error':0x07008003,
            'ams':{'tray_now':'254','tray_tar':'3','tray_pre':'1'},
            'hms':[{'attr':0x07002100,'code':0x00020004}]})
        r = c.state.ams_recovery
        self.assertEqual(c.state.status, PrintStatus.FINISH)
        self.assertTrue(r['busy'])
        self.assertEqual(r['target_label'], 'AMS A slot 4')
        self.assertEqual(r['source_label'], 'AMS A slot 2')
        self.assertEqual(r['phase'], 'Retracting filament')
        c._update_state({'nozzle_temper':250})
        self.assertEqual(c.state.ams_recovery['target_tray'], 3)

    def test_unknown_errors_are_preserved(self):
        r = recovery_state({'print_error': 0x03008099, 'hms': [{'attr': 1, 'code': 2}]})
        self.assertIn('03008099', r['message'])
        self.assertEqual(len(r['errors']), 2)


class CommandRegression(unittest.TestCase):
    def test_acknowledged_but_unchanged_target_is_failure_not_success(self):
        c = client(); c._update_state({'gcode_state': 'PAUSE', 'nozzle_target_temper': 42})
        sent = []
        def publish(payload):
            p = payload['print']; sent.append(p)
            c._command_responses[(p['command'], p['sequence_id'])] = {'result': 'success'}
            return True
        c._send_command = publish
        ok = c._observed_control('gcode_line', {'param': 'M104 S220\n'},
            lambda s: s.nozzle_target_temper == 220, 'Heat target set', timeout=.01)
        self.assertFalse(ok)
        self.assertEqual(len(sent), 1)
        self.assertEqual(c.state.command_status['state'], 'failed')
        self.assertIn('acknowledged', c.state.command_status['message'])

    def test_telemetry_confirms_start_and_later_completion_without_firmware_ack(self):
        c = client(); c._update_state({'gcode_state': 'PAUSE', 'nozzle_target_temper': 42})
        def publish(payload):
            c._update_state({'nozzle_target_temper': 220})
            return True
        c._send_command = publish
        ok = c._observed_control('gcode_line', {'param': 'M104 S220\n'},
            lambda s: s.nozzle_target_temper == 220, 'Heating',
            completion=lambda s: abs(s.nozzle_temper - 220) < 3, timeout=.02)
        self.assertTrue(ok)
        self.assertEqual(c.state.command_status['state'], 'in_progress')
        c._update_state({'nozzle_temper': 220})
        self.assertEqual(c.state.command_status['state'], 'completed')

    def test_explicit_rejection_is_reported_with_reason(self):
        c = client()
        def publish(payload):
            p = payload['print']; c._command_responses[(p['command'], p['sequence_id'])] = {'result': 'fail', 'reason': 'AMS busy'}
            return True
        c._send_command = publish
        self.assertFalse(c._observed_control('ams_control', {'param': 'resume'}, lambda s: False, 'Retry', timeout=.02))
        self.assertIn('AMS busy', c.state.command_status['message'])

    def test_no_automatic_retry_or_extrusion_confirmation(self):
        c = client(); c._send_command = Mock(return_value=True)
        c._update_state(RUNOUT)
        self.assertFalse(c.ams_recover('confirm_extrusion', confirmed=True))
        c._send_command.assert_not_called()
        c._update_state({'print_error': 0x07FF8007})
        self.assertFalse(c.ams_recover('confirm_extrusion', confirmed=False))
        c._send_command.assert_not_called()

    def test_command_completion_timeout_is_visible(self):
        c = client(); c._pending_control = {'complete': lambda s: False, 'deadline': time.monotonic()-1, 'error': 0}
        c.state.command_status = {'action': 'ams_control', 'state': 'in_progress'}
        c._update_state({'nozzle_temper': 100})
        self.assertEqual(c.state.command_status['state'], 'failed')


class RecoveryRoutes(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    _staff = filament_tests.RouteTests._staff

    def test_recovery_route_requires_physical_confirmation_and_current_state(self):
        c = client(); c._update_state(RUNOUT); c._send_command = Mock(return_value=True)
        http = self._staff(c)
        r = http.post('/api/printer/Printer-1/ams_recovery', json={'action':'confirm_extrusion'})
        self.assertEqual(r.status_code, 400)
        r = http.post('/api/printer/Printer-1/ams_recovery', json={'action':'confirm_extrusion', 'confirmed':True})
        self.assertEqual(r.status_code, 502)
        c._send_command.assert_not_called()

    def test_recovery_route_does_not_expose_control_to_anonymous_users(self):
        c = client(); http = self._staff(c)
        with http.session_transaction() as s: s.clear()
        r = http.post('/api/printer/Printer-1/ams_recovery', json={'action':'retry'})
        self.assertIn(r.status_code, (401, 403))

    def test_firmware_ack_without_progress_is_presented_as_error(self):
        c = client(); c._update_state(RUNOUT)
        c.ams_recover = Mock(return_value=False)
        c.state.command_status = {'state':'failed','message':'Printer acknowledged, but recovery did not start'}
        r = self._staff(c).post('/api/printer/Printer-1/ams_recovery', json={'action':'retry'})
        self.assertEqual(r.status_code, 502)
        self.assertIn('did not start', r.get_json()['message'])


class CancelRace(unittest.TestCase):
    def test_explicit_cancel_preserves_intent_after_finish_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = JobQueue(tmp+'/farm.db', tmp+'/uploads')
            job = q.add_job('a.gcode', 'a.gcode', tmp+'/a.gcode')
            q.assign_job(job, 'Printer-1'); q.mark_printing(job); q.mark_completed(job)
            self.assertTrue(q.cancel_stopped_job(job, copies_done=0))
            self.assertEqual(q.get_job(job)['status'], 'cancelled')
            self.assertEqual(q.get_job(job)['copies_done'], 0)

class PauseNotifications(unittest.TestCase):
    def test_runout_escalating_to_purge_failure_alerts_once_for_each_reason(self):
        from src.main import pause_alert_changed
        self.assertTrue(pause_alert_changed(PrintStatus.RUNNING, None, 'Slot 2 ran out'))
        self.assertTrue(pause_alert_changed(PrintStatus.PAUSE_FILAMENT, 'Slot 2 ran out', 'Old filament could not be purged'))
        self.assertFalse(pause_alert_changed(PrintStatus.PAUSE_FILAMENT, 'Old filament could not be purged', 'Old filament could not be purged'))

class HeatingGuards(unittest.TestCase):
    def test_manual_heating_is_not_queued_behind_unresolved_ams_operation(self):
        c = client(); c._update_state(RUNOUT); c._send_command = Mock(return_value=True)
        self.assertFalse(c.set_nozzle_temperature(220))
        self.assertIn('not sent', c.state.command_status['message'])
        self.assertFalse(c.set_bed_temperature(60))
        c._send_command.assert_not_called()

    def test_heater_off_requests_are_not_blocked_by_recovery_guard(self):
        c = client(); c._update_state(RUNOUT); c._observed_control = Mock(return_value=True)
        self.assertTrue(c.set_nozzle_temperature(0))
        self.assertEqual(c._observed_control.call_args.args[1]['param'], 'M104 S0\n')
