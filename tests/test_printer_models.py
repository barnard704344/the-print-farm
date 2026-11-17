"""Printer pictures follow model identity and stay independent of credentials."""
import tempfile
import unittest
from pathlib import Path

from src.printer_models import MODELS, printer_model_details
from src.web import create_app
from test_security import FakeFarm, FakeQueue


class PrinterModelTests(unittest.TestCase):
    def test_recognises_model_names_without_confusing_variants(self):
        examples = [
            ('P1S-ICT-AMS', 'bambulab', 'p1s'), ('Workshop P1P', 'bambulab', 'p1p'),
            ('X1 Carbon', 'bambulab', 'x1c'), ('X1E-01', 'bambulab', 'x1e'),
            ('A1 mini 02', 'bambulab', 'a1-mini'), ('A1-01', 'bambulab', 'a1'),
            ('Voron', 'klipper', 'voron'), ('Voron 2.4', 'klipper', 'voron-2.4'),
            ('Voron Trident', 'klipper', 'voron-trident'), ('Voron V0.2', 'klipper', 'voron-0'),
            ('Switchwire', 'klipper', 'voron-switchwire'),
        ]
        for name, printer_type, expected in examples:
            with self.subTest(name=name):
                self.assertEqual(printer_model_details(name, {'type': printer_type})['printer_model'], expected)

    def test_configured_model_overrides_name(self):
        detail = printer_model_details('Workshop printer', {'type': 'klipper', 'model': 'voron-trident'})
        self.assertEqual(detail['printer_model_name'], 'Voron Trident')
        self.assertEqual(detail['printer_image'], 'printers/voron-trident.png')
        self.assertEqual(printer_model_details('P1S-01', {'model': 'x1c'})['printer_model'], 'x1c')

    def test_unknown_and_unrelated_printers_keep_generic_icon(self):
        for name, cfg in [('Workshop 0', {'type': 'klipper'}), ('P1S', {'type': 'klipper'}),
                          ('Workshop', {}), ('P1S', {'model': 'custom corexy'}),
                          ('P1S', {'model': '../../private.png'})]:
            self.assertEqual(printer_model_details(name, cfg)['printer_image'], '')

    def test_all_mapped_assets_are_bundled(self):
        root = Path(__file__).resolve().parents[1] / 'static' / 'printers'
        for label, filename in MODELS.values():
            self.assertTrue((root / filename).is_file(), label)

    def test_farm_status_returns_picture_without_exposing_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(FakeFarm(), FakeQueue(tmp), config={
                'web': {'secret_key': 'test'},
                'printers': [{'name': 'Printer-1', 'type': 'klipper', 'model': 'voron-2.4',
                              'serial': 'private-serial', 'access_code': 'private-code'}],
            })
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(role='student', username='alice')
            response = client.get('/api/farm/status')
            self.assertEqual(response.status_code, 200)
            state = response.get_json()['printers']['Printer-1']
            self.assertEqual(state['printer_image'], 'printers/voron-2.4.png')
            self.assertEqual(state['printer_model_name'], 'Voron 2.4')
            self.assertEqual(state['access_code'], '')
            self.assertEqual(state['serial'], '')
            with app.test_client().get('/the-print-farm/static/'+state['printer_image']) as picture:
                self.assertEqual(picture.status_code, 200)
