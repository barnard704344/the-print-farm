"""
Wrap a raw .gcode file into a minimal .3mf (ZIP) archive
that the BambuLab P1S accepts via the project_file MQTT command.

The P1S firmware validates the slice_info.config metadata against the
gcode headers, so we parse the OrcaSlicer gcode to extract real values
(prediction time, weight, layer count, filament type).
"""

import hashlib
import re
import zipfile


CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="gcode" ContentType="text/x.gcode"/>
</Types>"""

RELS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>"""

MODEL_3D = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter"
       xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources/>
  <build/>
</model>"""

MODEL_SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="plater_id" value="1"/>
    <metadata key="plater_name" value=""/>
    <metadata key="locked" value="false"/>
    <metadata key="gcode_file" value="Metadata/plate_1.gcode"/>
  </plate>
</config>"""

PROJECT_SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<config>
</config>"""


def _parse_gcode_metadata(gcode_text: str) -> dict:
    """Parse OrcaSlicer gcode header for metadata the printer needs."""
    meta = {
        "prediction": 0,
        "weight": "0",
        "filament_type": "PLA",
        "filament_color": "#FFFFFFFF",
        "filament_used_m": "0",
        "filament_used_g": "0",
        "filaments": [],       # list of {slot, type, color} for each filament
        "used_slots": [],      # which AMS slots (M620 Sx) the gcode actually references
    }

    # Parse total estimated time from header: "total estimated time: 13m 29s"
    m = re.search(r";\s*total estimated time:\s*(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?", gcode_text[:4096])
    if m:
        hours = int(m.group(1) or 0)
        minutes = int(m.group(2) or 0)
        seconds = int(m.group(3) or 0)
        meta["prediction"] = hours * 3600 + minutes * 60 + seconds

    # Filament used weight from footer: "; filament used [g] = 1.95"
    m = re.search(r";\s*filament used \[g\]\s*=\s*([\d.]+)", gcode_text[-2048:])
    if m:
        meta["weight"] = m.group(1)
        meta["filament_used_g"] = m.group(1)

    # Filament used length: "; filament used [mm] = 644.80"
    m = re.search(r";\s*filament used \[mm\]\s*=\s*([\d.]+)", gcode_text[-2048:])
    if m:
        length_mm = float(m.group(1))
        meta["filament_used_m"] = f"{length_mm / 1000:.2f}"

    # Multi-filament parsing: "; filament_type = PLA;PLA;PETG"
    # Search up to 32KB - OrcaSlicer embeds long change_filament_gcode blocks
    # that can push filament metadata well beyond 8KB
    header = gcode_text[:32768]
    types = []
    m = re.search(r";\s*filament_type\s*=\s*(.+)", header)
    if m:
        types = [t.strip() for t in m.group(1).split(";")]
        meta["filament_type"] = types[0] if types else "PLA"

    # Multi-filament colours: "; filament_colour = #FF9016;#00AE42;#FFFFFF"
    colors = []
    m = re.search(r";\s*filament_colour\s*=\s*(.+)", header)
    if m:
        colors = [c.strip() for c in m.group(1).split(";")]
        if colors:
            color = colors[0]
            if len(color) == 7:
                color = color + "FF"
            meta["filament_color"] = color

    # Build filament list (one per slot defined in the slicer)
    num_filaments = max(len(types), len(colors))
    for i in range(num_filaments):
        color_hex = colors[i] if i < len(colors) else "#FFFFFF"
        if len(color_hex) == 7:
            color_hex += "FF"
        meta["filaments"].append({
            "slot": i,
            "type": types[i] if i < len(types) else "PLA",
            "color": color_hex,
        })

    # Find which AMS slots the gcode actually uses (M620 Sx commands)
    used = set()
    for m in re.finditer(r"^M620 S(\d+)A", gcode_text, re.MULTILINE):
        used.add(int(m.group(1)))
    meta["used_slots"] = sorted(used)

    return meta


def parse_gcode_model_name(gcode_path: str) -> str | None:
    """Extract the model/object name from gcode metadata.

    OrcaSlicer embeds lines like:
        ; printing object Voron_Design_Cube_v7(R2).stl id:0 copy 0
    Returns a cleaned-up name (without .stl extension), or None if not found.
    """
    with open(gcode_path, "r", errors="replace") as f:
        # Read enough to find the first printing object line (usually within first 100KB)
        text = f.read(102400)

    names = []
    for m in re.finditer(r"^; printing object (.+?)(?:\s+id:\d+\s+copy\s+\d+)?\s*$", text, re.MULTILINE):
        name = m.group(1).strip()
        # Strip common CAD file extensions
        name = re.sub(r"\.(stl|obj|step|stp|3mf)$", "", name, flags=re.IGNORECASE)
        if name and name not in names:
            names.append(name)

    if not names:
        return None

    # Join multiple object names (multi-part prints)
    return ", ".join(names)


def parse_gcode_filaments(gcode_path: str) -> dict:
    """Parse a gcode file and return its filament requirements.

    Returns dict with:
        filaments: list of {slot, type, color} for each defined filament
        used_slots: list of slot indices actually referenced by M620 commands
        used_filaments: list of {slot, type, color} for only the used slots
    """
    with open(gcode_path, "r", errors="replace") as f:
        text = f.read()
    meta = _parse_gcode_metadata(text)
    used_filaments = [
        fil for fil in meta["filaments"]
        if fil["slot"] in meta["used_slots"]
    ]
    # For single-filament / non-AMS prints (no M620 commands),
    # treat all defined filaments as used.
    if not used_filaments and meta["filaments"]:
        used_filaments = meta["filaments"]
    return {
        "filaments": meta["filaments"],
        "used_slots": meta["used_slots"],
        "used_filaments": used_filaments,
        "filament_used_g": meta.get("filament_used_g", "0"),
    }


def _build_slice_info(meta: dict) -> str:
    """Build slice_info.config XML with real metadata from gcode."""
    filament_lines = []
    if meta.get("filaments"):
        for i, fil in enumerate(meta["filaments"]):
            color = fil["color"]
            filament_lines.append(
                f'    <filament id="{i + 1}" type="{fil["type"]}" '
                f'color="{color}" '
                f'used_m="{meta["filament_used_m"]}" '
                f'used_g="{meta["filament_used_g"]}"/>'
            )
    else:
        filament_lines.append(
            f'    <filament id="1" type="{meta["filament_type"]}" '
            f'color="{meta["filament_color"]}" '
            f'used_m="{meta["filament_used_m"]}" '
            f'used_g="{meta["filament_used_g"]}"/>'
        )

    filament_xml = "\n".join(filament_lines)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header>
    <header_item key="X-BBL-Client-Type" value="slicer"/>
    <header_item key="X-BBL-Client-Version" value="02.03.02.00"/>
  </header>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="prediction" value="{meta['prediction']}"/>
    <metadata key="weight" value="{meta['weight']}"/>
    <metadata key="outside" value="0"/>
{filament_xml}
  </plate>
</config>"""


def wrap_gcode_as_3mf(gcode_path: str, output_path: str) -> str:
    """Wrap a .gcode file into a .3mf archive.

    Returns the md5 hex digest of the gcode content.
    """
    with open(gcode_path, "rb") as f:
        gcode_data = f.read()

    gcode_text = gcode_data.decode("utf-8", errors="replace")
    gcode_md5 = hashlib.md5(gcode_data).hexdigest().upper()
    meta = _parse_gcode_metadata(gcode_text)

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", RELS_XML)
        zf.writestr("3D/3dmodel.model", MODEL_3D)
        zf.writestr("Metadata/slice_info.config", _build_slice_info(meta))
        zf.writestr("Metadata/model_settings.config", MODEL_SETTINGS)
        zf.writestr("Metadata/project_settings.config", PROJECT_SETTINGS)
        zf.writestr("Metadata/plate_1.gcode", gcode_data)
        zf.writestr("Metadata/plate_1.gcode.md5", gcode_md5)

    return gcode_md5
