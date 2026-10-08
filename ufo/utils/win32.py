"""Low-level Win32 / screen plumbing.

Everything DPI-, monitor- and cursor-related lives here so the rest of the
codebase never touches ctypes directly.
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from functools import lru_cache

_user32 = getattr(ctypes, "windll", None) and ctypes.windll.user32

# SM_* virtual-screen metrics
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CXSCREEN = 0
SM_CYSCREEN = 1

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_EXTENDEDKEY = 0x0001

VK_MAP = {
    "backspace": 0x08, "tab": 0x09, "clear": 0x0C, "enter": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12,
    "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, " ": 0x20, "pageup": 0x21, "pagedown": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27,
    "down": 0x28, "select": 0x29, "print": 0x2A, "execute": 0x2B,
    "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E, "help": 0x2F,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "apps": 0x5D,
    "numlock": 0x90, "scrolllock": 0x91,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
    "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
    "add": 0x6B, "subtract": 0x6D, "multiply": 0x6A, "divide": 0x6F,
    "decimal": 0x6E,
}
for _i in range(1, 25):
    VK_MAP[f"f{_i}"] = 0x6F + _i
for _c in "0123456789":
    VK_MAP[_c] = ord(_c)
for _c in "abcdefghijklmnopqrstuvwxyz":
    VK_MAP[_c] = ord(_c.upper())


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


def is_windows() -> bool:
    return sys.platform == "win32" and _user32 is not None


def require_windows() -> None:
    if not is_windows():
        raise RuntimeError(
            "This agent automates Windows through UI Automation and is "
            f"not supported on {sys.platform!r}."
        )


@lru_cache(maxsize=1)
def dpi_awareness_set() -> str:
    """Make coordinates real pixels on scaled/multi-monitor setups.

    Must run before any window or screen call. Idempotent.
    """
    if not is_windows():
        return "unsupported"
    try:  # Win10 1703+ per-monitor v2 — the correct one for mixed DPI
        ctypes.windll.user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(-4)
        )
        return "per-monitor-v2"
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor"
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return "system"
    except Exception:
        return "unaware"


def dpi_scale() -> float:
    """Scale factor of the primary monitor (1.0 at 100% scaling)."""
    if not is_windows():
        return 1.0
    try:
        dc = _user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)  # LOGPIXELSX
        _user32.ReleaseDC(0, dc)
        return max(0.5, (dpi or 96) / 96.0)
    except Exception:
        return 1.0


@dataclass(frozen=True)
class Monitor:
    left: int
    top: int
    width: int
    height: int
    primary: bool = False

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def clamp(self, x: int, y: int) -> tuple[int, int]:
        return (min(max(x, self.left), self.right - 1),
                min(max(y, self.top), self.bottom - 1))


def _metric(index: int, default: int = 0) -> int:
    try:
        return int(_user32.GetSystemMetrics(index)) if _user32 else default
    except Exception:
        return default


def virtual_screen() -> tuple[int, int, int, int]:
    """(left, top, right, bottom) covering every monitor; left/top can be < 0."""
    left = _metric(SM_XVIRTUALSCREEN, 0)
    top = _metric(SM_YVIRTUALSCREEN, 0)
    width = _metric(SM_CXVIRTUALSCREEN, _metric(SM_CXSCREEN, 1920))
    height = _metric(SM_CYVIRTUALSCREEN, _metric(SM_CYSCREEN, 1080))
    if width <= 0 or height <= 0:
        width, height = _metric(SM_CXSCREEN, 1920), _metric(SM_CYSCREEN, 1080)
    return left, top, left + width, top + height


def virtual_size() -> tuple[int, int]:
    _, _, right, bottom = virtual_screen()
    return right, bottom


def primary_monitor() -> Monitor:
    return Monitor(0, 0, _metric(SM_CXSCREEN, 1920), _metric(SM_CYSCREEN, 1080), True)


def monitors() -> list[Monitor]:
    """Every display, using the Win32 EnumDisplayMonitors API."""
    if not is_windows():
        return [Monitor(0, 0, 1920, 1080, True)]

    found: list[Monitor] = []

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM,
    )

    def _cb(hmon, _hdc, _lprc, _lp):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        try:
            if _user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                r = info.rcMonitor
                found.append(Monitor(r.left, r.top, r.right - r.left,
                                     r.bottom - r.top, bool(info.dwFlags & 1)))
        except Exception:
            pass
        return True

    try:
        _user32.EnumDisplayMonitors(None, None, WNDENUMPROC(_cb), 0)
    except Exception:
        pass

    if not found:
        left, top, right, bottom = virtual_screen()
        return [Monitor(left, top, right - left, bottom - top, True)]
    return found


def monitor_at(x: int, y: int) -> Monitor:
    for m in monitors():
        if m.contains(x, y):
            return m
    return primary_monitor()


def clamp_to_screen(x: int, y: int) -> tuple[int, int]:
    left, top, right, bottom = virtual_screen()
    return (min(max(int(x), left), right - 1), min(max(int(y), top), bottom - 1))


def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    try:
        _user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y
    except Exception:
        return (0, 0)


def _absolute(x: int, y: int) -> tuple[int, int]:
    left, top, right, bottom = virtual_screen()
    span_x, span_y = max(1, right - left - 1), max(1, bottom - top - 1)
    ax = int(65535 * (x - left) / span_x)
    ay = int(65535 * (y - top) / span_y)
    return min(65535, max(0, ax)), min(65535, max(0, ay))


def move_cursor(x: int, y: int) -> tuple[int, int]:
    x, y = clamp_to_screen(x, y)
    ax, ay = _absolute(x, y)
    inp = INPUT(type=INPUT_MOUSE)
    inp.union.mi = MOUSEINPUT(ax, ay, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0, None)
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    return x, y


def mouse_event(flags: int, data: int = 0) -> None:
    inp = INPUT(type=INPUT_MOUSE)
    inp.union.mi = MOUSEINPUT(0, 0, data, flags, 0, None)
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def key_event(vk: int, up: bool = False) -> None:
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.union.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, None)
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def type_unicode(char: str) -> None:
    """Send one arbitrary character, including ones no keyboard has."""
    for unit in (ord(char) & 0xFFFF,):
        down = INPUT(type=INPUT_KEYBOARD)
        down.union.ki = KEYBDINPUT(0, unit, KEYEVENTF_UNICODE, 0, None)
        up = INPUT(type=INPUT_KEYBOARD)
        up.union.ki = KEYBDINPUT(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None)
        _user32.SendInput(2, ctypes.byref(down), ctypes.sizeof(INPUT))
        del unit


def lock_screen() -> None:
    try:
        ctypes.windll.user32.LockWorkStation()
    except Exception:
        subprocess_run(["rundll32.exe", "user32.dll,LockWorkStation"])


def subprocess_run(cmd: list[str]) -> int:
    import subprocess
    try:
        return subprocess.run(cmd, check=False, timeout=30).returncode
    except Exception:
        return -1


def foreground_window_handle() -> int:
    try:
        h = _user32.GetForegroundWindow()
        return int(h) if h else 0
    except Exception:
        return 0


def window_title(handle: int) -> str:
    if not handle or not is_windows():
        return ""
    try:
        length = _user32.GetWindowTextLengthW(handle)
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(handle, buf, length + 1)
        return buf.value
    except Exception:
        return ""


def process_id_of(handle: int) -> int:
    pid = wintypes.DWORD()
    try:
        _user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def is_foreground(handle: int) -> bool:
    return bool(handle) and foreground_window_handle() == handle


def wait_for_foreground(handle: int, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if is_foreground(handle):
            return True
        time.sleep(0.05)
    return False
