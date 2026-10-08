"""Screen capture: whole virtual desktop, a monitor, a region, or one window.

Everything is clamped to the virtual screen so multi-monitor layouts with
negative coordinates work, and every capture reports the screen-space bbox it
came from — that offset is what makes pixel coordinates from the model usable
as real click coordinates.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from PIL import Image, ImageGrab

from ..errors import ScreenCaptureError
from ..utils import win32


@dataclass
class Capture:
    """An image plus the screen coordinates it covers."""

    image: Image.Image
    bbox: tuple[int, int, int, int]   # (left, top, right, bottom) in screen px
    monitor: int = 0

    @property
    def width(self) -> int:
        return self.image.width

    @property
    def height(self) -> int:
        return self.image.height

    @property
    def origin(self) -> tuple[int, int]:
        return self.bbox[0], self.bbox[1]

    def to_local(self, x: int, y: int) -> tuple[int, int]:
        return x - self.bbox[0], y - self.bbox[1]

    def to_screen(self, x: int, y: int) -> tuple[int, int]:
        return x + self.bbox[0], y + self.bbox[1]

    def contains(self, x: int, y: int) -> bool:
        left, top, right, bottom = self.bbox
        return left <= x < right and top <= y < bottom

    def crop_local(self, box) -> Image.Image:
        from ..utils import image as imageutil

        return imageutil.crop(self.image, box)


def _grab(all_screens: bool = True) -> Image.Image:
    try:
        return ImageGrab.grab(all_screens=all_screens).convert("RGB")
    except Exception as e:
        raise ScreenCaptureError(
            f"Screen capture failed: {type(e).__name__}: {e}. "
            "On a locked or secure desktop there is nothing to capture."
        ) from e


def virtual_bbox() -> tuple[int, int, int, int]:
    return win32.virtual_screen()


def capture_full(settle: float = 0.0) -> Capture:
    """Every monitor, in one image, with the correct virtual-screen origin."""
    if settle:
        time.sleep(settle)
    img = _grab(True)
    left, top, _right, _bottom = virtual_bbox()
    if img.width <= 0 or img.height <= 0:
        raise ScreenCaptureError("Screen capture returned an empty image.")
    return Capture(image=img, bbox=(left, top, left + img.width, top + img.height))


def capture_monitor(index: int = 0) -> Capture:
    """One display by index. Out-of-range falls back to the primary monitor."""
    screens = win32.monitors()
    if not screens:
        return capture_full()
    mon = screens[index] if 0 <= index < len(screens) else win32.primary_monitor()
    full = capture_full()
    box = (mon.left, mon.top, mon.right, mon.bottom)
    from ..utils import image as imageutil

    local = (
        box[0] - full.bbox[0], box[1] - full.bbox[1],
        box[2] - full.bbox[0], box[3] - full.bbox[1],
    )
    return Capture(image=imageutil.crop(full.image, local), bbox=box, monitor=index)


def capture_primary() -> Capture:
    return capture_monitor(0)


def capture_region(box: tuple[int, int, int, int]) -> Capture:
    """Capture an arbitrary screen-space rectangle, clamped to the desktop."""
    left, top, right, bottom = (int(v) for v in box)
    vx, vy, vrx, vry = virtual_bbox()
    left, top = max(left, vx), max(top, vy)
    right, bottom = min(right, vrx), min(bottom, vry)
    if right - left < 2 or bottom - top < 2:
        raise ScreenCaptureError(
            f"Region {box} is empty or entirely off-screen "
            f"(virtual screen is {vx},{vy}..{vrx},{vry})."
        )
    full = capture_full()
    local = (left - full.bbox[0], top - full.bbox[1],
             right - full.bbox[0], bottom - full.bbox[1])
    from ..utils import image as imageutil

    return Capture(image=imageutil.crop(full.image, local), bbox=(left, top, right, bottom))


def capture_window(window) -> Capture:
    """The window's own pixels: its rect clamped to the visible desktop.

    A window restored from the taskbar, snapped, or partly off-screen all
    produce a valid (clamped) capture rather than an error.
    """
    try:
        if hasattr(window, "is_minimized") and window.is_minimized():
            window.restore()
            time.sleep(0.3)
        r = window.rectangle()
        box = (r.left, r.top, r.right, r.bottom)
    except Exception as e:
        raise ScreenCaptureError(f"Cannot read window rectangle: {e}") from e
    return capture_region(box)


def capture_with_cursor(capture: Capture) -> Capture:
    """Draw the mouse pointer into the capture.

    Only used for explanation/debug output. Never sent to the model: a drawn
    arrow would read as a real UI element and confuse grounding.
    """
    from ..utils import image as imageutil

    try:
        import ctypes

        user32 = ctypes.windll.user32
        cx, cy = win32.cursor_pos()
        pt = ctypes.wintypes.POINT(cx, cy)
        user32.GetCursorPos(ctypes.byref(pt))
        icon_info = (ctypes.c_byte * 72)()
        ico = user32.GetIconInfo(
            ctypes.c_void_p(user32.LoadCursorW(None, 32512)), ctypes.byref(icon_info)
        )
        del ico, icon_info
    except Exception:
        return capture
    lx, ly = capture.to_local(cx, cy)
    img = capture.image.copy()
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    d.polygon(
        [(lx, ly), (lx, ly + 14), (lx + 4, ly + 10), (lx + 7, ly + 16), (lx + 10, ly + 15),
         (lx + 7, ly + 9), (lx + 13, ly + 9)],
        fill=(255, 0, 0), outline=(0, 0, 0),
    )
    del imageutil
    return Capture(image=img, bbox=capture.bbox, monitor=capture.monitor)


def to_base64(img: Image.Image, fmt: str = "png", quality: int = 70) -> str:
    from ..utils import image as imageutil

    return imageutil.encode(img, fmt, quality)
