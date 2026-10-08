"""Window management: enumerate, focus, move, resize, snap, close, switch.

This is how the agent manipulates whole applications rather than individual
controls — arranging windows for a side-by-side task, restoring a minimized
app, snapping to a half-screen, or cycling through what is open.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from ..errors import ActionError, WindowNotFoundError
from ..perception import uia
from ..utils import win32


@dataclass
class Rect:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)

    def clamp(self, screen: Rect | None = None) -> "Rect":
        if screen is None:
            vx, vy, vrx, vry = win32.virtual_screen()
            screen = Rect(vx, vy, vrx - vx, vry - vy)
        w = min(max(160, self.width), screen.width)
        h = min(max(120, self.height), screen.height)
        return Rect(
            min(max(self.left, screen.left), screen.right - w),
            min(max(self.top, screen.top), screen.bottom - h),
            w, h,
        )


HALVES = {
    "left": (0.0, 0.0, 0.5, 1.0),
    "right": (0.5, 0.0, 1.0, 1.0),
    "top": (0.0, 0.0, 1.0, 0.5),
    "bottom": (0.0, 0.5, 1.0, 1.0),
    "top_left": (0.0, 0.0, 0.5, 0.5),
    "top_right": (0.5, 0.0, 1.0, 0.5),
    "bottom_left": (0.0, 0.5, 0.5, 1.0),
    "bottom_right": (0.5, 0.5, 1.0, 1.0),
}

QUARTERS = {
    "tl": "top_left", "tr": "top_right", "bl": "bottom_left", "br": "bottom_right",
}

MAXIMIZED, MINIMIZED, NORMAL = 3, 2, 1


def list_windows(allowed: list[str] | None = None) -> list[uia.WindowInfo]:
    return [w for w in uia.list_all_windows() if uia.is_allowed(w, allowed)]


def find(title: str = "", process: str = "", allowed: list[str] | None = None) -> uia.WindowInfo:
    """Deterministically find a window by title substring or process name."""
    if allowed is not None and not allowed:
        raise WindowNotFoundError("No window is allowlisted; refusing to search.")
    import re

    for w in uia.list_all_windows():
        if allowed is not None and not uia.is_allowed(w, allowed):
            continue
        if title and title.lower() not in w.title.lower():
            continue
        if process and process.lower() not in (w.process_name or "").lower():
            continue
        return w
    raise WindowNotFoundError(
        f"No window matches title={title!r} process={process!r}."
    )


def focus(window_or_handle) -> str:
    w = _wrap(window_or_handle)
    h = int(getattr(w, "handle", window_or_handle) or 0)
    if not h:
        raise WindowNotFoundError("That window is gone.")
    try:
        if hasattr(w, "is_minimized") and w.is_minimized():
            w.restore()
            time.sleep(0.25)
        w.set_focus()
    except Exception:
        try:
            import win32gui  # type: ignore
            import win32con  # type: ignore

            win32gui.ShowWindow(h, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(h)
        except Exception as e:
            raise ActionError(
                f"Cannot focus window {h}: {e}. Windows blocks foreground "
                "changes from background processes; the agent usually has to "
                "click the taskbar entry first."
            ) from e
    time.sleep(0.15)
    return f"focused \"{win32.window_title(h)}\""


def restore(window_or_handle) -> str:
    w = _wrap(window_or_handle)
    try:
        w.restore()
    except Exception:
        import win32gui  # type: ignore
        import win32con  # type: ignore

        win32gui.ShowWindow(int(w.handle), win32con.SW_RESTORE)
    return f"restored \"{win32.window_title(int(w.handle))}\""


def minimize(window_or_handle) -> str:
    w = _wrap(window_or_handle)
    try:
        w.minimize()
    except Exception:
        import win32gui  # type: ignore
        import win32con  # type: ignore

        win32gui.ShowWindow(int(w.handle), win32con.SW_MINIMIZE)
    return f"minimized \"{win32.window_title(int(w.handle))}\""


def maximize(window_or_handle) -> str:
    w = _wrap(window_or_handle)
    try:
        w.maximize()
    except Exception:
        import win32gui  # type: ignore
        import win32con  # type: ignore

        win32gui.ShowWindow(int(w.handle), win32con.SW_MAXIMIZE)
    return f"maximized \"{win32.window_title(int(w.handle))}\""


def close(window_or_handle, force: bool = False) -> str:
    """Close a window politely, escalating to a kill only if asked."""
    w = _wrap(window_or_handle)
    h = int(w.handle)
    title = win32.window_title(h)
    try:
        w.close()
    except Exception:
        import win32gui  # type: ignore
        import win32con  # type: ignore

        win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
    if force:
        time.sleep(1.5)
        if win32.window_title(h):
            from . import process as process_mod

            process_mod.terminate(process_id_of(h), force=True)
            return f"force-closed \"{title}\""
    return f"closed \"{title}\""


def move_to(window_or_handle, x: int, y: int) -> str:
    w = _wrap(window_or_handle)
    r = w.rectangle()
    try:
        w.move_window(x=int(x), y=int(y), width=r.width(), height=r.height(), repaint=True)
    except Exception as e:
        raise ActionError(f"Cannot move window: {e}") from e
    return f"moved \"{win32.window_title(int(w.handle))}\" to ({x},{y})"


def resize(window_or_handle, width: int, height: int) -> str:
    w = _wrap(window_or_handle)
    r = w.rectangle()
    try:
        w.resize(width=int(width), height=int(height))
    except Exception:
        w.move_window(x=r.left(), y=r.top(), width=int(width), height=int(height))
    return f"resized \"{win32.window_title(int(w.handle))}\" to {width}x{height}"


def set_rect(window_or_handle, left: int, top: int, width: int, height: int) -> str:
    w = _wrap(window_or_handle)
    try:
        w.move_window(x=int(left), y=int(top), width=int(width), height=int(height),
                      repaint=True)
    except Exception:
        resize(w, width, height)
        move_to(w, left, top)
    return (f"placed \"{win32.window_title(int(w.handle))}\" at "
            f"({left},{top}) {width}x{height}")


def snap(window_or_handle, position: str = "left", monitor: int = 0) -> str:
    """Snap to a screen region: left/right/top/bottom/quarters/maximize."""
    w = _wrap(window_or_handle)
    position = position.strip().lower()
    if position in QUARTERS:
        position = QUARTERS[position]
    if position in ("max", "maximize", "full"):
        return maximize(w)
    if position in ("min", "minimize"):
        return minimize(w)
    if position == "center":
        mon = win32.monitors()[monitor] if monitor < len(win32.monitors()) else win32.primary_monitor()
        r = Rect(mon.left, mon.top, mon.width, mon.height)
        cur = w.rectangle()
        target = Rect(r.left + (r.width - cur.width()) // 2,
                      r.top + (r.height - cur.height()) // 2,
                      cur.width(), cur.height()).clamp(r)
        return set_rect(w, target.left, target.top, target.width, target.height)
    if position not in HALVES:
        raise ActionError(
            f"Unknown snap position {position!r}. Try: {sorted(HALFES)} or 'center'."
        )
    mon = win32.monitors()[monitor] if monitor < len(win32.monitors()) else win32.primary_monitor()
    fx, fy, fw, fh = HALVES[position]
    return set_rect(
        w,
        mon.left + int(mon.width * fx),
        mon.top + int(mon.height * fy),
        int(mon.width * fw),
        int(mon.height * fh),
    )


def tile(allowed: list[str] | None = None, monitor: int = 0) -> str:
    """Lay every allowlisted window out side by side on one monitor."""
    windows = [w for w in list_windows(allowed) if not w.minimized and w.wrapper]
    if not windows:
        return "nothing to tile"
    mon = win32.monitors()[monitor] if monitor < len(win32.monitors()) else win32.primary_monitor()
    count = len(windows)
    width = mon.width // count
    for index, w in enumerate(windows):
        set_rect(w.wrapper, mon.left + index * width, mon.top, width, mon.height)
    return f"tiled {count} windows across the {'primary' if monitor == 0 else 'selected'} monitor"


def cascade(allowed: list[str] | None = None, offset: int = 30) -> str:
    windows = [w for w in list_windows(allowed) if not w.minimized and w.wrapper]
    for index, w in enumerate(windows):
        r = w.rectangle()
        move_to(w.wrapper, r.left() + index * offset, r.top() + index * offset)
    return f"cascaded {len(windows)} windows"


def switch_to(title_or_process: str = "", index: int = -1, timeout: float = 5.0) -> str:
    """Alt-Tab to a window. Matching by name is far more reliable than by index."""
    from . import keyboard

    if title_or_process:
        w = find(title=title_or_process) if len(title_or_process) > 3 else None
        if w is not None:
            return focus(w.wrapper)
        return keyboard.do("alt_tab")
    if index >= 0:
        for _ in range(index + 1):
            keyboard.do("alt_tab")
            time.sleep(0.25)
        return f"alt-tabbed {index + 1} time(s)"
    return keyboard.do("alt_tab")


def activate_next_desktop(direction: str = "next") -> str:
    combo = "win+ctrl+right" if direction == "next" else "win+ctrl+left"
    from . import keyboard

    return keyboard.hotkey(combo)


def close_all(allowed: list[str] | None = None) -> str:
    windows = list_windows(allowed)
    for w in windows:
        try:
            close(w.wrapper)
        except Exception:
            continue
    return f"closed {len(windows)} window(s)"


def wait_for(title_re: str = ".*", timeout: float = 15.0,
             allowed: list[str] | None = None) -> uia.WindowInfo:
    import re

    pattern = re.compile(title_re, re.IGNORECASE)
    end = time.time() + timeout
    while time.time() < end:
        for w in list_windows(allowed):
            if pattern.search(w.title):
                return w
        time.sleep(0.3)
    raise WindowNotFoundError(f"No window matching /{title_re}/ appeared in {timeout:.0f}s.")


def process_id_of(handle: int) -> int:
    return win32.process_id_of(handle)


def _wrap(window_or_handle):
    if hasattr(window_or_handle, "handle"):
        return window_or_handle
    return uia.get_window(int(window_or_handle))
