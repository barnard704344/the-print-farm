"""Toolpath parsing for the solid 3D viewer."""
import json
import math
import os
import struct
import tempfile
import threading
import unittest
import zipfile
from array import array

from src.file_library import FileLibrary


def make_library():
    lib = FileLibrary.__new__(FileLibrary)
    lib._lock = threading.Lock()
    return lib


class ToolpathParserTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lib = make_library()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _gcode(self, text, name="t.gcode"):
        path = os.path.join(self.temp_dir.name, name)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def test_arc_segments_stay_within_chord_tolerance(self):
        segs = FileLibrary._tessellate_arc(10, 0, 0.2, -10, 0, 0.2, -10, 0, clockwise=False)
        # Half circle, r=10, 0.01 mm tolerance -> a few dozen segments, not thousands.
        self.assertLess(len(segs), 64)
        self.assertGreater(len(segs), 8)
        for x1, y1, _, x2, y2, _ in segs:
            mid = math.hypot((x1 + x2) / 2, (y1 + y2) / 2)
            self.assertLess(10 - mid, 0.0105)
        self.assertEqual(segs[-1][3:5], (-10, 0))

    def test_tiny_arc_is_cheap(self):
        segs = FileLibrary._tessellate_arc(0.1, 0, 0.2, 0, 0.1, 0.2, -0.1, 0, clockwise=False)
        self.assertLessEqual(len(segs), 4)

    def test_widths_heights_features_and_extrusion_modes(self):
        path = self._gcode(
            "G90\nM83\n"
            "G1 X0 Y0 Z0.2 F3000\n"               # travel, no feature
            "; FEATURE: Outer wall\n; LINE_WIDTH: 0.45\n; LAYER_HEIGHT: 0.2\n"
            "G1 X10 Y0 E0.5\n"
            "G1 X10 Y10 E-0.8\n"                   # retract while moving: not extrusion
            "; FEATURE: Inner wall\n; LINE_WIDTH: 0.5\n"
            "G1 X0 Y10 E0.4\n"
            "G0 X5 Y5\n"                           # travel
            "G91\nG1 X1 Y0 E0.1\nG90\n"            # relative XYZ
            "M82\nG92 E0\nG1 X0 Y0 E1\nG1 X1 Y0 E0.5\n"  # absolute E going backwards
        )
        data = self.lib._parse_toolpath(path)
        self.assertEqual(data["count"], 4)
        names = data["feature_names"]
        feats = [names[i] for i in data["features"]]
        self.assertEqual(feats, ["Outer wall", "Inner wall", "Inner wall", "Inner wall"])
        self.assertAlmostEqual(data["widths"][0], 0.45, places=5)
        self.assertAlmostEqual(data["widths"][1], 0.5, places=5)
        self.assertAlmostEqual(data["heights"][0], 0.2, places=5)
        p = data["positions"]
        for got, want in zip(p[12:18], [5.0, 5.0, 0.2, 6.0, 5.0, 0.2]):  # relative move
            self.assertAlmostEqual(got, want, places=5)
        self.assertFalse(data["truncated"])

    def test_truncation_is_reported(self):
        lines = ["G90", "M83", "; FEATURE: Outer wall"]
        lines += [f"G1 X{i} Y0 Z0.2 E0.1" for i in range(1, 50)]
        data = self.lib._parse_toolpath(self._gcode("\n".join(lines)), max_moves=10)
        self.assertTrue(data["truncated"])
        self.assertEqual(data["count"], 10)
        self.assertEqual(len(data["positions"]), 60)

    def test_reads_plate_gcode_from_3mf(self):
        path = os.path.join(self.temp_dir.name, "t.3mf")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Metadata/plate_1.gcode",
                             "M83\n; FEATURE: Top surface\nG1 X0 Y0 Z0.4\nG1 X5 Y0 E0.2\n")
        data = self.lib._parse_toolpath(path)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["feature_names"][0], "Top surface")


class ToolpathBinaryTests(unittest.TestCase):
    def test_binary_layout(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = os.path.join(temp_dir.name, "b.gcode")
        with open(path, "w") as handle:
            handle.write("M83\n; FEATURE: Outer wall\n; LINE_WIDTH: 0.42\nG1 X0 Y0 Z0.2\n"
                         "G1 X10 Y0 E0.3\nG1 X10 Y10 E0.3\n")
        lib = make_library()
        lib._toolpath_source = lambda file_id: path
        payload = lib.get_toolpath_binary(1)
        self.assertEqual(payload[:4], b"TPF2")
        count, header_len = struct.unpack_from("<II", payload, 4)
        self.assertEqual(count, 2)
        self.assertEqual(header_len % 4, 0)
        header = json.loads(payload[12:12 + header_len])
        self.assertEqual(header["feature_names"], {"0": "Outer wall"})
        self.assertEqual(header["bounds"]["max"], [10.0, 10.0, 0.2])
        offset = 12 + header_len
        positions = array("f"); positions.frombytes(payload[offset:offset + count * 24])
        for got, want in zip(positions[:6], [0.0, 0.0, 0.2, 10.0, 0.0, 0.2]):
            self.assertAlmostEqual(got, want, places=5)
        offset += count * 24
        widths = array("f"); widths.frombytes(payload[offset:offset + count * 4])
        self.assertAlmostEqual(widths[0], 0.42, places=5)
        offset += count * 8  # widths + heights
        self.assertEqual(payload[offset:offset + count], bytes([0, 0]))
        self.assertEqual(len(payload), offset + count)
        # Cached on second call
        self.assertIs(lib.get_toolpath_binary(1), payload)


if __name__ == "__main__":
    unittest.main()
