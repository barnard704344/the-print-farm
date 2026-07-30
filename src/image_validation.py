"""Bounded image decoding and normalized thumbnail persistence."""

import os
import tempfile
from io import BytesIO

from PIL import Image, UnidentifiedImageError

MAX_IMAGE_PIXELS = 16 * 1024 * 1024


def save_normalized_image(payload, destination, *, max_bytes, max_dimension=2048):
    """Decode a supported bitmap and atomically save a normalized PNG."""
    if not payload or len(payload) > max_bytes:
        raise ValueError(f"Image must be non-empty and no larger than {max_bytes} bytes")

    try:
        with Image.open(BytesIO(payload)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("Image must be PNG, JPEG, or WebP")
            width, height = image.size
            if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("Image dimensions are too large")
            image.load()
            image.thumbnail((max_dimension, max_dimension))
            normalized = image.convert("RGBA" if image.mode in ("RGBA", "LA") else "RGB")
    except (Image.DecompressionBombError, UnidentifiedImageError) as exc:
        raise ValueError("Image data is invalid or unsafe") from exc

    directory = os.path.dirname(os.path.abspath(destination))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".thumbnail-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            normalized.save(handle, format="PNG", optimize=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
