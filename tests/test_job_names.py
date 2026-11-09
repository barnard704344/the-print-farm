import unittest

from src.job_names import display_name, printer_file_name


class JobNameTests(unittest.TestCase):
    def test_strips_prefix_extension_and_orca_suffix(self):
        self.assertEqual(display_name("f627a845044e425987896c65c971c2a5_cade_pip2_PLA_9m8s.gcode"), "cade pip2")
        self.assertEqual(display_name("a" * 64 + "_Baby_Turtle_1_PLA_1h24m.3mf"), "Baby Turtle 1")
        self.assertEqual(display_name("bracket_PETG-CF_2d3h4m5s.gcode.3mf"), "bracket")

    def test_leaves_ordinary_names_alone(self):
        self.assertEqual(display_name("All_Pips_MAX.3mf"), "All Pips MAX")
        self.assertEqual(display_name("Henley.pip2.0.3mf"), "Henley.pip2.0")
        self.assertEqual(display_name("gear_20T.gcode"), "gear 20T")

    def test_never_returns_empty_for_a_real_name(self):
        self.assertEqual(display_name("PLA_5m.gcode"), "PLA 5m")

    def test_printer_file_name_is_short_and_safe(self):
        self.assertEqual(printer_file_name("cade_pip2_PLA_9m8s.gcode", "uuid_cade.gcode"), "cade_pip2.gcode")
        self.assertEqual(printer_file_name("My part (v2)_PLA_1h.3mf", "x.3mf"), "My_part_v2.3mf")
        self.assertEqual(printer_file_name("", "f627_stored.3mf"), "f627_stored.3mf")
        self.assertLessEqual(len(printer_file_name("x" * 300 + ".3mf", "f.3mf")), 84)
