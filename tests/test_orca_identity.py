"""Personal Orca keys attribute uploads without relying on browser sessions."""
import io
import os
import tempfile
import unittest

from src.file_library import FileLibrary
from src.job_queue import JobQueue
from src.web import create_app
from test_security import FakeFarm


class OrcaIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = os.path.join(self.tmp.name, 'farm.db')
        self.queue = JobQueue(db, self.tmp.name)
        self.library = FileLibrary(db, self.tmp.name)
        self.config = {
            'web': {'secret_key': 'session-secret', 'orca_api_key': 'legacy-key'},
            'student_access': {'allowlist': ['alice', 'bob'], 'banlist': []},
        }
        self.app = self._app()

    def tearDown(self):
        self.tmp.cleanup()

    def _app(self):
        return create_app(FakeFarm(), self.queue, config=self.config, file_library=self.library)

    def _key(self, name='alice', role='student'):
        client = self.app.test_client()
        with client.session_transaction() as s:
            s.update(username=name, role=role, display_name=name.title())
        response = client.get('/api/orca/api-key')
        self.assertEqual(response.status_code, 200)
        return response.get_json()['api_key']

    def _upload(self, key, client=None, route='/api/files/local'):
        return (client or self.app.test_client()).post(
            route, headers={'X-Api-Key': key},
            data={'file': (io.BytesIO(b'G28\nG1 X10\n'), 'part.gcode'), 'submitted_by': 'spoofed'},
            content_type='multipart/form-data',
        )

    def test_each_person_has_a_stable_distinct_key(self):
        alice = self._key()
        self.assertEqual(alice, self._key())
        self.assertNotEqual(alice, self._key('bob'))
        self.assertNotEqual(alice, 'legacy-key')
        restarted = self._app().test_client()
        self.assertEqual(restarted.get('/api/version', headers={'X-Api-Key': alice}).status_code, 200)

    def test_generic_and_printer_uploads_attribute_jobs_and_library(self):
        for name, route in [('alice', '/api/files/local'), ('bob', '/Printer-1/api/files/local'),
                            ('teacher', '/the-print-farm/api/files/local')]:
            key = self._key(name, 'staff' if name == 'teacher' else 'student')
            self.assertEqual(self._upload(key, route=route).status_code, 201)
            job = self.queue.get_all_jobs()[0]
            self.assertEqual(job['submitted_by'], name)
            entry = self.library.find_by_path(job['file_path'])
            self.assertEqual(entry['uploaded_by'], name)
            if name == 'bob':
                self.assertEqual(job['printer_name'], 'Printer-1')

    def test_identity_comes_from_key_even_when_browser_has_other_user(self):
        client = self.app.test_client()
        with client.session_transaction() as s:
            s.update(username='bob', role='student')
        self.assertEqual(self._upload(self._key(), client=client).status_code, 201)
        self.assertEqual(self.queue.get_all_jobs()[0]['submitted_by'], 'alice')
        with client.session_transaction() as s:
            self.assertEqual(s['username'], 'bob')

    def test_legacy_shared_key_still_uploads_without_attribution(self):
        self.assertEqual(self._upload('legacy-key').status_code, 201)
        self.assertEqual(self.queue.get_all_jobs()[0]['submitted_by'], '')

    def test_personal_key_cannot_access_other_apis(self):
        client = self.app.test_client()
        headers = {'X-Api-Key': self._key()}
        for route in ['/api/jobs', '/api/v1/printers', '/api/orca/api-key']:
            self.assertIn(client.get(route, headers=headers).status_code, (401, 403))

    def test_changed_student_access_blocks_existing_key(self):
        key = self._key()
        self.config['student_access']['banlist'].append('Alice')
        self.assertEqual(self._upload(key).status_code, 403)
        self.config['student_access']['banlist'].clear()
        self.config['student_access']['allowlist'].remove('alice')
        self.assertEqual(self._upload(key).status_code, 403)
        self.assertEqual(self.queue.get_all_jobs(), [])

    def test_tampered_key_and_key_from_other_farm_are_rejected(self):
        key = self._key()
        self.assertEqual(self._upload(key + 'bad').status_code, 403)
        for setting in ['secret_key', 'orca_api_key']:
            previous = self.config['web'][setting]
            self.config['web'][setting] = 'rotated'
            other = self._app().test_client()
            self.assertEqual(other.get('/api/version', headers={'X-Api-Key': key}).status_code, 403)
            self.config['web'][setting] = previous
