"""Backwards-compatible screenshot helpers.

The real implementation is split across two modules:
  * `ufo.perception.screen`  — capture, with the screen-space bbox
  * `ufo.perception.vision`  — Set-of-Mark annotation and change detection

This module keeps the original function names working.
"""
from __future__ import annotations

from typing import Optional

from PIL import Image

from .screen import (  # noqa: F401
    Capture,
    capture_full,
    capture_monitor,
    capture_primary,
    capture_region,
    capture_window,
    capture_with_cursor,
    to_base64,
    virtual_bbox,
)


def annotate(img: Image.Image, origin: tuple, controls) -> Image.Image:
    """Draw numbered boxes over controls (Set-of-Mark).

    `origin` is the screen-space (left, top) the image was cropped from, and
    `controls` need a `.rect` and an `.id`. This is the original signature and
    it is preserved exactly.
    """
    from .vision import annotator

    return annotator().draw(img, controls, tuple(origin))


def draw_ocr(img: Image.Image, result, origin: tuple, needle: str = "") -> Image.Image:
    """Outline OCR hits, marking the one matching `needle`."""
    from .vision import annotator

    return annotator().draw_ocr_words(img, result, tuple(origin), needle)


def difference(before: Image.Image, after: Image.Image, threshold: int = 12) -> float:
    from ..utils import image as imageutil

    return imageutil.diff_ratio(before, after, threshold)


def changed_regions(before: Image.Image, after: Image.Image,
                    threshold: int = 12) -> list[tuple]:
    from ..utils import image as imageutil

    return imageutil.changed_regions(before, after, threshold)


def save(img: Image.Image, path, quality: int = 70) -> str:
    from ..utils import image as imageutil

    return str(imageutil.save(img, path))


def encode(img: Image.Image, fmt: str = "png", quality: int = 70) -> str:
    from ..utils import image as imageutil

    return imageutil.encode(img, fmt, quality)


__all__ = [
    "Capture", "annotate", "capture_full", "capture_monitor",
    "capture_primary", "capture_region", "capture_window",
    "capture_with_cursor", "changed_regions", "difference", "draw_ocr",
    "encode", "save", "to_base64", "virtual_bbox",
]
