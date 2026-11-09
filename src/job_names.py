"""Readable job names.

Stored uploads carry a random prefix (a 32-character UUID, or a 64-character
content hash for OrcaSlicer virtual-printer uploads), and OrcaSlicer's default
file name template appends the filament and print time, e.g.
``f627a845…_cade_pip2_PLA_9m8s.gcode``. Printers show whatever file name they
are sent, so both the dashboard and the printer screen got long, noisy names.
"""
import re

from werkzeug.utils import secure_filename

_PREFIX = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{64})_", re.IGNORECASE)
_EXTENSION = re.compile(r"\.(?:gcode\.3mf|3mf|gcode|bgcode)$", re.IGNORECASE)
# OrcaSlicer's "{input_filename_base}_{filament_type[0]}_{print_time}" suffix:
# a filament token such as PLA, PETG-CF or PLA+ followed by 1d2h3m4s-style time.
_ORCA_SUFFIX = re.compile(
    r"_[A-Za-z][A-Za-z0-9+\-]{1,15}_(?=\d)(?:\d+d)?(?:\d+h)?(?:\d+m)?(?:\d+s)?$"
)


def file_extension(name: str) -> str:
    match = _EXTENSION.search(name or "")
    return match.group(0) if match else ""


def clean_stem(name: str) -> str:
    """Name without random prefix, extension or OrcaSlicer filament/time suffix."""
    stem = _PREFIX.sub("", str(name or "").strip())
    stem = _EXTENSION.sub("", stem)
    stem = _ORCA_SUFFIX.sub("", stem)
    return stem.strip(" _-.") or _EXTENSION.sub("", _PREFIX.sub("", str(name or ""))) or str(name or "")


def display_name(name: str) -> str:
    """Human-friendly job name, e.g. 'cade pip2'."""
    return re.sub(r"\s+", " ", clean_stem(name).replace("_", " ")).strip()


def printer_file_name(original_name: str, fallback: str) -> str:
    """Short, filesystem-safe name to upload to a printer, keeping the extension."""
    ext = file_extension(original_name) or file_extension(fallback)
    stem = secure_filename(clean_stem(original_name))[:80].strip("._")
    return f"{stem}{ext}" if stem and ext else fallback
