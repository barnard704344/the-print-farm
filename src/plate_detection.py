"""
Build plate empty detection.

Compares the current camera snapshot against one or more calibrated empty-plate
reference images inside a configurable region of interest.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Iterable

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat


@dataclass
class PlateDetectionResult:
    ok: bool
    occupied: bool
    score: float
    threshold: float
    message: str


def analyse_plate(
    current_image: bytes,
    reference_images: Iterable[bytes],
    roi: dict | None = None,
    threshold: float = 12.0,
) -> PlateDetectionResult:
    """Return whether the current snapshot differs from calibrated references."""
    refs = list(reference_images)
    if not refs:
        return PlateDetectionResult(False, False, 0.0, threshold, "No reference images configured")

    try:
        current = _prepare_image(current_image, roi)
        scores = []
        for ref_bytes in refs:
            ref = _prepare_image(ref_bytes, roi, size=current.size)
            diff = ImageChops.difference(current, ref)
            stat = ImageStat.Stat(diff)
            scores.append(sum(stat.mean) / len(stat.mean))
    except Exception as exc:
        return PlateDetectionResult(False, False, 0.0, threshold, f"Plate detection failed: {exc}")

    score = min(scores) if scores else 0.0
    occupied = score > threshold
    message = "Build plate appears occupied" if occupied else "Build plate appears empty"
    return PlateDetectionResult(True, occupied, score, threshold, message)


def _prepare_image(image_bytes: bytes, roi: dict | None, size: tuple[int, int] | None = None) -> Image.Image:
    img = Image.open(io.BytesIO(image_bytes)).convert("L")
    img = _crop_roi(img, roi)
    if size:
        img = img.resize(size)
    else:
        img.thumbnail((480, 360))
    img = ImageOps.autocontrast(img)
    img = img.filter(ImageFilter.GaussianBlur(radius=1))
    return img


def _crop_roi(img: Image.Image, roi: dict | None) -> Image.Image:
    if not roi:
        return img

    width, height = img.size
    x = _clamp(float(roi.get("x", 0)), 0, 100)
    y = _clamp(float(roi.get("y", 0)), 0, 100)
    w = _clamp(float(roi.get("w", 100)), 1, 100)
    h = _clamp(float(roi.get("h", 100)), 1, 100)

    left = int(width * x / 100)
    top = int(height * y / 100)
    right = int(width * min(100, x + w) / 100)
    bottom = int(height * min(100, y + h) / 100)
    if right <= left or bottom <= top:
        return img
    return img.crop((left, top, right, bottom))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
