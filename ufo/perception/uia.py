"""UI Automation perception: the desktop, its windows, and their controls.

Two levels of granularity, both fail-soft:

  * `list_all_windows()` — title + owning process for every visible top-level
    window. Cheap. Used to *choose* a target. Never reads window content.
  * `list_controls(window)` — the interactive elements inside one window.
    Expensive. Only call this after the allowlist has approved the window.

`is_allowed()` is deliberately the only gate, so there is one auditable place
where "look at this window's contents" becomes possible.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from ..config import VisionConfig
from ..errors import WindowClosedError, WindowNotFoundError
from ..utils import text as textutil
from ..utils import win32

try:  # pywinauto is a hard dependency, but keep import failures readable
    from pywinauto import Desktop
    from pywinauto.controls.uiawrapper import UIAWrapper  # type: ignore
except Exception as _pywinauto_error:  # pragma: no cover
    Desktop = None  # type: ignore
    UIAWrapper = object  # type: ignore
    _IMPORT_ERROR = _pywinauto_error
else:
    _IMPORT_ERROR = None

MAX_NAME_LEN = 120
MAX_VALUE_LEN = 200

# Extra types that matter for automation but are easy to forget.
_EXTRA_TYPES = {
    "Pane", "Group", "Header", "HeaderItem", "TitleBar", "ToolBar",
    "StatusBar", "Thumb", "Calendar", "Date", "Time", "SearchBox",
    "Table", "Custom", "ProgressBar", "ScrollBar", "Separator",
    "SemanticZoom", "Animation", "Document", "Fragment",
}


@dataclass
class Control:
    """One interactable element. `id` is rebuilt every observation."""

    id: int
    type: str
    name: str
    rect: tuple
    wrapper: object = field(repr=False, default=None)
    value: str = ""
    automation_id: str = ""
    enabled: bool = True
    offscreen: bool = False
    depth: int = 0
    handle: int = 0
    process_id: int = 0

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        return ((left + right) // 2, (top + bottom) // 2)

    @property
    def size(self) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        return (right - left, bottom - top)

    @property
    def label(self) -> str:
        return f"{self.type} \"{self.name}\""

    def to_prompt(self, verbose: bool = False) -> str:
        base = f'[{self.id}] {self.type}: "{self.name}"'
        if verbose:
            extra = []
            if self.automation_id:
                extra.append(f"aid={self.automation_id}")
            if self.value:
                extra.append(f"value={textutil.truncate(self.value, 40)!r}")
            extra.append(f"at={self.rect[0]},{self.rect[1]}")
            extra.append("disabled" if not self.enabled else "enabled")
            if extra:
                base += " (" + ", ".join(extra) + ")"
        return base

    def to_dict(self) -> dict:
        return {
            "id": self.id, "type": self.type, "name": self.name,
            "rect": list(self.rect), "value": self.value,
            "automation_id": self.automation_id, "enabled": self.enabled,
            "offscreen": self.offscreen, "handle": self.handle,
        }


@dataclass
class WindowInfo:
    """A top-level window, described without touching its contents."""

    handle: int
    title: str
    process_name: str = ""
    process_id: int = 0
    rect: tuple = (0, 0, 0, 0)
    is_foreground: bool = False
    minimized: bool = False
    maximized: bool = False
    wrapper: object = field(repr=False, default=None)

    def to_prompt(self) -> str:
        return f'[{self.handle}] "{self.title}" ({self.process_name or "?"})'

    def to_dict(self) -> dict:
        return {
            "handle": self.handle, "title": self.title,
            "process_name": self.process_name, "process_id": self.process_id,
            "rect": list(self.rect), "is_foreground": self.is_foreground,
            "minimized": self.minimized, "maximized": self.maximized,
        }


# --------------------------------------------------------------------------
# availability
# --------------------------------------------------------------------------
def available() -> bool:
    return _IMPORT_ERROR is None and win32.is_windows()


def _require() -> None:
    if _IMPORT_ERROR is not None:
        raise RuntimeError(
            "pywinauto could not be imported; install requirements.txt "
            f"({_IMPORT_ERROR})."
        ) from _IMPORT_ERROR
    if not win32.is_windows():
        raise RuntimeError("UI Automation is only available on Windows.")


# --------------------------------------------------------------------------
# window enumeration
# --------------------------------------------------------------------------
def desktop_root():
    """The whole desktop as a UIA element — the root of a full-screen agent."""
    _require()
    return Desktop(backend="uia")


def _describe(w) -> WindowInfo | None:
    try:
        title = w.window_text() or ""
        if not title.strip():
            return None
        r = w.rectangle()
        return WindowInfo(
            handle=int(w.handle),
            title=title,
            process_name=_process_name(w),
            process_id=win32.process_id_of(int(w.handle)),
            rect=(r.left, r.top, r.right, r.bottom),
            is_foreground=win32.is_foreground(int(w.handle)),
            minimized=bool(w.is_minimized()) if hasattr(w, "is_minimized") else False,
            maximized=bool(w.is_maximized()) if hasattr(w, "is_maximized") else False,
            wrapper=w,
        )
    except Exception:
        return None


def _process_name(w) -> str:
    try:
        pid = int(w.handle)
        import psutil  # type: ignore
        return psutil.Process(win32.process_id_of(pid)).name()
    except Exception:
        pass
    try:
        from win32process import GetModuleFileNameEx, OpenProcess  # type: ignore
        import win32api  # type: ignore
        import win32con  # type: ignore
        import psutil  # type: ignore
    except Exception:
        pass
    try:
        import win32process  # type: ignore
        import win32api  # type: ignore
        import os

        handle = win32api.OpenProcess(0x1000, False, win32.process_id_of(int(w.handle)))
        path = win32process.GetModuleFileNameEx(handle, 0)
        win32api.CloseHandle(handle)
        return os.path.basename(path)
    except Exception:
        try:
            cls = w.element_info.class_name or ""
            return cls if cls and cls != "ConsoleWindowClass" else ""
        except Exception:
            return ""


def list_all_windows(include_untitled: bool = False,
                     include_invisible: bool = False) -> list[WindowInfo]:
    """Every top-level window: title, process, geometry, state. No contents."""
    if not available():
        return []
    out: list[WindowInfo] = []
    try:
        candidates = desktop_root().windows()
    except Exception:
        return []
    for w in candidates:
        try:
            if not include_invisible and not w.is_visible():
                continue
        except Exception:
            continue
        info = _describe(w)
        if info is None and not include_untitled:
            continue
        if info is not None:
            out.append(info)
    return out


def foreground_window() -> WindowInfo | None:
    h = win32.foreground_window_handle()
    if not h:
        return None
    return next((w for w in list_all_windows(include_untitled=True) if w.handle == h), None)


def get_window(handle: int):
    """Re-resolve a live pywinauto window object from a handle."""
    _require()
    if not handle:
        raise WindowNotFoundError("No window handle supplied.")
    try:
        for w in Desktop(backend="uia").windows():
            if int(w.handle) == int(handle):
                return w
    except Exception as e:
        raise WindowClosedError(f"Could not resolve window {handle}: {e}") from e
    raise WindowNotFoundError(f"Window {handle} is no longer open.")


def find_window(title_re: str = ".*", timeout: float = 15.0,
                process: str = "", poll: float = 0.4) -> object:
    """Poll for a visible top-level window matching a title regex."""
    _require()
    pattern = re.compile(title_re, re.IGNORECASE)
    proc_re = re.compile(process, re.IGNORECASE) if process else None
    end = time.time() + timeout
    while time.time() < end:
        for info in list_all_windows():
            if not pattern.search(info.title):
                continue
            if proc_re and not proc_re.search(info.process_name or ""):
                continue
            return info.wrapper
        time.sleep(poll)
    raise WindowNotFoundError(
        f"No window matching /{title_re}/"
        + (f" in process /{process}/" if process else "")
        + f" within {timeout:.0f}s."
    )


def snapshot_handles() -> set[int]:
    """Handles of all current top-level windows. Call BEFORE launching an app."""
    if not available():
        return set()
    handles = set()
    try:
        for w in Desktop(backend="uia").windows():
            try:
                handles.add(int(w.handle))
            except Exception:
                continue
    except Exception:
        pass
    return handles


def find_new_window(title_re: str = ".*", before: set[int] | None = None,
                    timeout: float = 20.0, poll: float = 0.4) -> object:
    """Like find_window but ignores windows that already existed, so the agent
    never attaches to something the user had open before the task started."""
    _require()
    pattern = re.compile(title_re, re.IGNORECASE)
    before = set(before or ())
    end = time.time() + timeout
    while time.time() < end:
        for info in list_all_windows():
            if info.handle in before:
                continue
            if pattern.search(info.title):
                return info.wrapper
        time.sleep(poll)
    raise WindowNotFoundError(
        f"No NEW window matching /{title_re}/ appeared in {timeout:.0f}s. "
        "If the app reuses an existing window (Windows 11 Notepad opens a tab), "
        "close its windows first and retry."
    )


# --------------------------------------------------------------------------
# allowlist
# --------------------------------------------------------------------------
def is_allowed(window_or_info, allowlist: Iterable[str] | None) -> bool:
    """Is this window opted in? An empty allowlist allows nothing (fail closed)."""
    if not allowlist:
        return False
    patterns = [p for p in allowlist if p]
    if not patterns:
        return False
    title = (window_or_info.title if isinstance(window_or_info, WindowInfo)
             else (window_or_info.window_text() or ""))
    title = title or ""
    for pattern in patterns:
        try:
            if re.search(pattern, title, re.IGNORECASE):
                return True
        except re.error:
            if pattern.lower() in title.lower():
                return True
    return False


# --------------------------------------------------------------------------
# controls
# --------------------------------------------------------------------------
def _value_of(el) -> str:
    for getter in ("get_value", "legacy_properties"):
        try:
            if getter == "legacy_properties":
                v = el.legacy_properties().get("Value")
            else:
                v = getattr(el, getter)()
            if v:
                return textutil.truncate(str(v), MAX_VALUE_LEN)
        except Exception:
            continue
    try:
        pattern = el.iface_value.CurrentValue
        if pattern:
            return textutil.truncate(str(pattern), MAX_VALUE_LEN)
    except Exception:
        pass
    return ""


def list_controls(window, cfg: VisionConfig | None = None,
                  max_controls: int | None = None,
                  include_rect: bool = True) -> list[Control]:
    """Visible, enabled, on-screen, non-empty controls inside one window.

    Ordered by (depth, y, x) so the numbered list the model sees matches what
    the screenshot shows and reads top-to-bottom like a human would scan.
    """
    cfg = cfg or VisionConfig()
    limit = max_controls or cfg.max_controls
    allowed_types = set(cfg.include_control_types) | _EXTRA_TYPES

    try:
        descendants = list(window.descendants())
    except Exception:
        return []

    controls: list[Control] = []
    for el in descendants:
        try:
            ctype = el.element_info.control_type
            if ctype not in allowed_types:
                continue
            if not el.is_visible():
                continue
            enabled = bool(el.is_enabled())
            r = el.rectangle()
            if r.width() <= 1 or r.height() <= 1:
                continue
            try:
                name = textutil.collapse_ws(el.window_text() or el.element_info.name or "")
            except Exception:
                name = ""
            if ctype == "Text" and not name:
                continue
            if not name and ctype not in ("Edit", "Document", "Custom", "Pane", "Image"):
                continue

            controls.append(Control(
                id=0,
                type=ctype,
                name=name[:MAX_NAME_LEN],
                rect=(r.left, r.top, r.right, r.bottom) if include_rect else (0, 0, 0, 0),
                wrapper=el,
                value=_value_of(el),
                automation_id=str(getattr(el.element_info, "automation_id", "") or ""),
                enabled=enabled,
                offscreen=bool(getattr(el.element_info, "offscreen", False)),
                handle=int(getattr(el.element_info, "handle", 0) or 0),
                process_id=int(getattr(el.element_info, "process_id", 0) or 0),
            ))
        except Exception:
            continue  # elements vanish mid-enumeration all the time

    if not controls:
        return []

    controls.sort(key=lambda c: (c.rect[1], c.rect[0]))
    trimmed = controls[:limit]
    for index, c in enumerate(trimmed, 1):
        c.id = index
    return trimmed


def find_control(window, name: str = "", control_type: str = "",
                 automation_id: str = "", exact: bool = False,
                 cfg: VisionConfig | None = None) -> Control | None:
    """Locate a control by human name, without going through the model.

    This is the deterministic path: when the user says "click the Save button",
    the agent can match it itself instead of asking a vision model to guess.
    """
    needle = textutil.collapse_ws(name).lower()
    for c in list_controls(window, cfg=cfg):
        if control_type and c.type != control_type:
            continue
        if automation_id and c.automation_id != automation_id:
            continue
        if not needle:
            continue
        hay = f"{c.name} {c.automation_id}".lower()
        if (hay == needle) if exact else (needle in hay or hay in needle):
            return c
    return None


def window_text(window, limit: int = 4000) -> str:
    """All readable text in a window — the cheapest 'what does this say?' call."""
    chunks: list[str] = []
    try:
        for el in window.descendants():
            try:
                ctype = el.element_info.control_type
                if ctype not in ("Text", "Edit", "Document", "Hyperlink", "TitleBar"):
                    continue
                value = _value_of(el) or textutil.collapse_ws(el.window_text() or "")
                if value:
                    chunks.append(value)
            except Exception:
                continue
    except Exception:
        pass
    return "\n".join(chunks)[:limit]


def control_value(control: Control) -> str:
    return _value_of(control.wrapper) if control and control.wrapper else control.value


def focus_window(window) -> bool:
    try:
        if hasattr(window, "is_minimized") and window.is_minimized():
            window.restore()
        window.set_focus()
        return True
    except Exception:
        try:
            import win32gui  # type: ignore

            win32gui.SetForegroundWindow(int(window.handle))
            return True
        except Exception:
            return False


def is_alive(window) -> bool:
    try:
        return bool(window.rectangle().width() > 0)
    except Exception:
        return False


def wait_until(predicate: Callable[[], bool], timeout: float = 10.0,
               poll: float = 0.3) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(poll)
    return False
