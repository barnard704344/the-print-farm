"""Validation shared by every print-file ingestion path."""

import os
import zipfile

MAX_ARCHIVE_MEMBERS = 2000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 200


class InvalidPrintFile(ValueError):
    """Raised when an uploaded print file is unsafe or malformed."""


def validate_print_file(path):
    """Validate file shape and archive expansion without extracting anything."""
    size = os.path.getsize(path)
    if size <= 0:
        raise InvalidPrintFile("Uploaded file is empty")

    lower_path = path.lower()
    if lower_path.endswith(".gcode"):
        return
    if not lower_path.endswith(".3mf"):
        raise InvalidPrintFile("Only .gcode and .3mf files are accepted")

    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if not members or len(members) > MAX_ARCHIVE_MEMBERS:
                raise InvalidPrintFile("3MF archive has an invalid number of entries")

            total_size = 0
            has_gcode = False
            for member in members:
                total_size += member.file_size
                if total_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise InvalidPrintFile("3MF archive expands beyond the 2 GiB limit")
                if member.flag_bits & 0x1:
                    raise InvalidPrintFile("Encrypted 3MF entries are not supported")
                if member.file_size and member.compress_size == 0:
                    raise InvalidPrintFile("3MF archive contains an invalid compressed entry")
                if member.compress_size:
                    ratio = member.file_size / member.compress_size
                    if ratio > MAX_ARCHIVE_COMPRESSION_RATIO:
                        raise InvalidPrintFile("3MF archive compression ratio is unsafe")
                if member.filename.lower().endswith(".gcode"):
                    has_gcode = True

            if not has_gcode:
                raise InvalidPrintFile("3MF archive does not contain printable G-code")
            bad_entry = archive.testzip()
            if bad_entry:
                raise InvalidPrintFile(f"3MF archive is corrupt at {bad_entry}")
    except zipfile.BadZipFile as exc:
        raise InvalidPrintFile("Uploaded 3MF is not a valid archive") from exc
