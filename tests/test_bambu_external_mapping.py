import json
import zipfile

from src.bambu_client import (
    BambuClient,
    build_3mf_ams_mapping,
    read_3mf_first_extruder,
)
from src.gcode_to_3mf import wrap_gcode_as_3mf


def _capture_start_command(ams_mapping, use_ams=True):
    client = BambuClient.__new__(BambuClient)
    client.name = "P1S-Test"
    client._state = type("State", (), {"has_ams": True, "ams_trays": [1, 2, 3, 4]})()
    captured = {}

    def capture(payload, **kwargs):
        captured.update(payload)
        return True

    client._send_command_and_wait = capture
    assert client.start_print(
        "external.3mf", use_ams=use_ams, ams_mapping=ams_mapping
    )
    return captured["print"]


def test_external_only_slicer_slot_is_normalized_for_p1_firmware():
    command = _capture_start_command([-1, -1, -1, -1, 254])

    assert command["use_ams"] is False
    assert command["ams_mapping"] == [-1]
    assert command["ams_mapping2"] == [{"ams_id": 255, "slot_id": 0}]


def test_regular_ams_mapping_is_not_collapsed():
    command = _capture_start_command([0, 2])

    assert command["use_ams"] is True
    assert command["ams_mapping"] == [0, 2]
    assert command["ams_mapping2"] == [
        {"ams_id": 0, "slot_id": 0},
        {"ams_id": 0, "slot_id": 2},
    ]


def test_wrapped_external_gcode_preserves_dispatch_metadata(tmp_path):
    gcode = tmp_path / "external.gcode"
    output = tmp_path / "external.3mf"
    gcode.write_text(
        "; filament_type = PETG;PLA;PLA;PLA;TPU\n"
        "M620 S4A\n"
        "G1 X1 Y1\n"
        "M621 S4A\n",
        encoding="utf-8",
    )

    wrap_gcode_as_3mf(str(gcode), str(output))

    with zipfile.ZipFile(output) as archive:
        plate = json.loads(archive.read("Metadata/plate_1.json"))
    assert plate == {"first_extruder": 4, "filament_ids": [4]}
    assert read_3mf_first_extruder(str(output)) == 4
    assert build_3mf_ams_mapping(str(output), 4) == [-1, -1, -1, -1, 254]
