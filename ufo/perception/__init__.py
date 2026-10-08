"""Perception: UIA tree, screen pixels, OCR, and the combined observation."""
from __future__ import annotations

from .ocr import OCRResult, read_text, available as ocr_available
from .screen import capture_full, capture_region, capture_window, virtual_bbox
from .uia import (
    Control,
    WindowInfo,
    desktop_root,
    find_new_window,
    find_window,
    get_window,
    is_allowed,
    list_all_windows,
    list_controls,
    snapshot_handles,
)
from .vision import Observation, annotator, diff_since_last

__all__ = [
    "Control",
    "OCRResult",
    "Observation",
    "WindowInfo",
    "annotator",
    "available",
    "capture_full",
    "capture_region",
    "capture_window",
    "desktop_root",
    "diff_since_last",
    "find_new_window",
    "find_window",
    "get_window",
    "is_allowed",
    "list_all_windows",
    "list_controls",
    "ocr_available",
    "read_text",
    "snapshot_handles",
    "virtual_bbox",
]
