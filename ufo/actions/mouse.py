"""Mouse primitives: absolute clicks, drags, scroll, cursor placement.

These are the fallback for anything the accessibility tree does not expose —
canvas-drawn UIs, games, remote desktops, custom-rendered editors. All
coordinates are in virtual-screen space and are clamped to the desktop, so a
bad coordinate from a vision model can never click outside the machine.
"""
from __future__ import annotations

import time
from typing import Literal

from ..errors import ActionError
from ..utils import win32

Button = Literal["left", "right", "middle"]

_DOWN = {
    "left": (win32.MOUSEEVENTF_LEFTDOWN, win32.MOUSEEVENTF_LEFTUP),
    "right": (win32.MOUSEEVENTF_RIGHTDOWN, win32.MOUSEEVENTF_RIGHTUP),
    "middle": (win32.MOUSEEVENTF_MIDDLEDOWN, win32.MOUSEEVENTF_MIDDLEUP),
}
WHEEL_DELTA = 120


def _require_desktop() -> None:
    if not win32.is_windows():
        raise ActionError("Mouse control requires a Windows desktop session.")


def _smooth_move(x: int, y: int, steps: int = 12, delay: float = 0.006) -> None:
    """Move along a straight line so apps that track motion behave normally."""
    sx, sy = win32.cursor_pos()
    steps = max(1, min(int(steps), 200))
    for i in range(1, steps + 1):
        t = i / steps
        eased = t * t * (3 - 2 * t)  # smoothstep
        win32.move_cursor(int(sx + (x - sx) * eased), int(sy + (y - sy) * eased))
        if delay:
            time.sleep(delay)


def move_to(x: int, y: int, smooth: bool = True, steps: int = 12) -> tuple[int, int]:
    _require_desktop()
    if smooth:
        _smooth_move(x, y, steps)
    else:
        win32.move_cursor(x, y)
    time.sleep(0.03)
    return win32.cursor_pos()


def move_relative(dx: int, dy: int, smooth: bool = True) -> tuple[int, int]:
    x, y = win32.cursor_pos()
    return move_to(x + dx, y + dy, smooth)


def click_at(x: int, y: int, double: bool = False, right: bool = False,
             button: str = "", steps: int = 12, hold: float = 0.05) -> str:
    _require_desktop()
    which = (button or ("right" if right else "left")).lower()
    if which not in _DOWN:
        raise ActionError(f"Unknown mouse button: {which}")
    down, up = _DOWN[which]
    _smooth_move(x, y, steps)
    time.sleep(0.05)
    win32.mouse_event(down)
    time.sleep(hold)
    win32.mouse_event(up)
    if double:
        time.sleep(0.06)
        win32.mouse_event(down)
        time.sleep(hold)
        win32.mouse_event(up)
    label = f"double-click" if double else "click"
    if which == "right":
        label = f"right-{label}"
    elif which == "middle":
        label = f"middle-{label}"
    return f"{label} at ({x},{y})"


def double_click_at(x: int, y: int) -> str:
    return click_at(x, y, double=True)


def right_click_at(x: int, y: int) -> str:
    return click_at(x, y, right=True)


def press(button: str = "left") -> str:
    down, _up = _DOWN[button.lower()]
    win32.mouse_event(down)
    return f"pressed {button}"


def release(button: str = "left") -> str:
    _down, up = _DOWN[button.lower()]
    win32.mouse_event(up)
    return f"released {button}"


def scroll_at(x: int | None = None, y: int | None = None, amount: int = -3,
              horizontal: bool = False, control: str = "") -> str:
    """Scroll `amount` notches at a point (or wherever the pointer already is)."""
    _require_desktop()
    if x is not None and y is not None:
        _smooth_move(x, y, 6)
    flag = win32.MOUSEEVENTF_WHEEL if not horizontal else win32.MOUSEEVENTF_HWHEEL
    notches = max(-50, min(50, int(amount)))
    win32.mouse_event(flag, notches * WHEEL_DELTA)
    time.sleep(0.12)
    where = f" at ({x},{y})" if x is not None else ""
    target = f" on {control}" if control else where
    return f"scrolled {notches} notch(es){target or ' at current position'}"


def drag_from(x: int, y: int, dx: int = 0, dy: int = 0, steps: int = 20,
              button: str = "left", source: str = "", duration: float = 0.6) -> str:
    """Press at (x,y), move by (dx,dy), release. The workhorse for dragging
    sliders, window edges, file icons and scrollbars."""
    _require_desktop()
    down, up = _DOWN[button.lower()]
    ex, ey = win32.clamp_to_screen(x + dx, y + dy)
    _smooth_move(x, y, 8)
    time.sleep(0.08)
    win32.mouse_event(down)
    time.sleep(0.12)
    sx, sy = x, y
    steps = max(2, min(int(steps), 300))
    per = max(0.0, duration) / steps
    for i in range(1, steps + 1):
        t = i / steps
        eased = t * t * (3 - 2 * t)
        win32.move_cursor(int(sx + (ex - sx) * eased), int(sy + (ey - sy) * eased))
        if per:
            time.sleep(per)
    time.sleep(0.08)
    win32.mouse_event(up)
    origin = f" from {source}" if source else ""
    return f"dragged ({x},{y}) to ({ex},{ey}){origin}"


def drag_path(points: list[tuple[int, int]], button: str = "left") -> str:
    if len(points) < 2:
        raise ActionError("drag_path needs at least two points.")
    down, up = _DOWN[button.lower()]
    win32.move_cursor(*points[0])
    time.sleep(0.08)
    win32.mouse_event(down)
    for point in points[1:]:
        _smooth_move(point[0], point[1], 6)
        time.sleep(0.04)
    time.sleep(0.08)
    win32.mouse_event(up)
    return f"dragged through {len(points)} points"


def drag_to(x: int, y: int, steps: int = 20) -> str:
    sx, sy = win32.cursor_pos()
    return drag_from(sx, sy, x - sx, y - sy, steps=steps)


def position() -> tuple[int, int]:
    return win32.cursor_pos()


def on_screen(x: int, y: int) -> bool:
    left, top, right, bottom = win32.virtual_screen()
    return left <= x < right and top <= y < bottom
