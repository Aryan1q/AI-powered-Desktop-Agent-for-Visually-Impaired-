"""Keyboard primitives: unicode typing, hotkeys, clipboard, key hold/tap.

Typing goes through `SendInput` with the Unicode flag rather than
`pywinauto.send_keys`, so it works for characters no keyboard has, is immune
to the user's keyboard-layout and IME state, and cannot be misparsed as a
pywinauto control sequence.
"""
from __future__ import annotations

import ctypes
import time

from ..errors import ClipboardError, ActionError
from ..perception.uia import Control
from ..utils import win32
from ..utils import text as textutil

_MODIFIERS = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
              "win": 0x5B, "cmd": 0x5B, "meta": 0x5B}
_MOD_ORDER = ("ctrl", "alt", "shift", "win")


def _require_desktop() -> None:
    if not win32.is_windows():
        raise ActionError("Keyboard control requires a Windows desktop session.")


# --------------------------------------------------------------------------
# typing
# --------------------------------------------------------------------------
def type_text(text: str, interval: float = 0.008,
              ctrl: Control | None = None) -> str:
    """Type arbitrary text, including newlines, tabs and non-Latin scripts."""
    _require_desktop()
    text = str(text)
    if ctrl is not None:
        try:
            ctrl.wrapper.click_input()
            time.sleep(0.08)
        except Exception:
            pass
    for ch in text:
        if ch == "\n" or ch == "\r":
            key_event(0x0D)          # Enter, as a real keypress
        elif ch == "\t":
            key_event(0x09)
        else:
            win32.type_unicode(ch)
        if interval:
            time.sleep(interval)
    return f"typed {len(text)} chars"


def type_slow(text: str, interval: float = 0.06) -> str:
    """Type deliberately slowly — for apps that drop synthesized keystrokes."""
    return type_text(text, interval=interval)


def key_tap(name: str, times: int = 1, interval: float = 0.05) -> str:
    """Press and release a named key (`enter`, `f5`, `page_down`, `a`, `3`)."""
    _require_desktop()
    token = textutil.normalize_key(name)
    vk = win32.VK_MAP.get(token)
    if vk is None:
        raise ActionError(f"Unknown key: {name}")
    for _ in range(max(1, int(times))):
        win32.key_event(vk)
        time.sleep(0.02)
        win32.key_event(vk, up=True)
        if interval:
            time.sleep(interval)
    return f"tapped {name}" + (f" x{times}" if times > 1 else "")


def hotkey(combo: str, presses: int = 1) -> str:
    """'ctrl+shift+s', 'alt+f4', 'win+d' — modifiers first, then the key."""
    _require_desktop()
    tokens = [textutil.normalize_key(t) for t in textutil.parse_hotkey(combo)]
    mods = [t for t in tokens[:-1] if t in _MODIFIERS]
    key = tokens[-1]
    vk = win32.VK_MAP.get(key)
    if vk is None:
        raise ActionError(f"Unknown key {key!r} in hotkey {combo!r}.")
    for name in sorted({m for m in mods}, key=_MOD_ORDER.index):
        win32.key_event(win32.VK_MAP[_MODIFIERS[name]])
    try:
        for _ in range(max(1, int(presses))):
            win32.key_event(vk)
            time.sleep(0.02)
            win32.key_event(vk, up=True)
    finally:
        for name in reversed(sorted({m for m in mods}, key=_MOD_ORDER.index)):
            win32.key_event(win32.VK_MAP[_MODIFIERS[name]], up=True)
    time.sleep(0.06)
    return f"pressed {textutil.describe_hotkey(combo)}"


def key_down(name: str) -> str:
    vk = win32.VK_MAP.get(textutil.normalize_key(name))
    if vk is None:
        raise ActionError(f"Unknown key: {name}")
    win32.key_event(vk)
    return f"held {name}"


def key_up(name: str) -> str:
    vk = win32.VK_MAP.get(textutil.normalize_key(name))
    if vk is None:
        raise ActionError(f"Unknown key: {name}")
    win32.key_event(vk, up=True)
    return f"released {name}"


def press_and_hold(name: str, seconds: float) -> str:
    key_down(name)
    try:
        time.sleep(max(0.05, float(seconds)))
    finally:
        key_up(name)
    return f"held {name} for {seconds}s"


# --------------------------------------------------------------------------
# clipboard
# --------------------------------------------------------------------------
def get_clipboard() -> str:
    _require_desktop()
    try:
        import win32clipboard  # type: ignore

        for _ in range(3):
            try:
                win32clipboard.OpenClipboard()
                break
            except Exception:
                time.sleep(0.05)
        else:
            raise ClipboardError("Another app is holding the clipboard open.")
        try:
            if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                return ""
            return str(win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT) or "")
        finally:
            win32clipboard.CloseClipboard()
    except ClipboardError:
        raise
    except Exception as e:
        raise ClipboardError(f"Cannot read the clipboard: {e}") from e


def set_clipboard(text: str, retries: int = 5) -> str:
    _require_desktop()
    try:
        import win32clipboard  # type: ignore

        for attempt in range(retries):
            try:
                win32clipboard.OpenClipboard()
                break
            except Exception:
                time.sleep(0.08 * (attempt + 1))
        else:
            raise ClipboardError(
                "Another app kept the clipboard open. Close it (or stop it "
                "clipboard monitoring) and try again."
            )
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, str(text))
        finally:
            win32clipboard.CloseClipboard()
        return f"copied {len(str(text))} chars to the clipboard"
    except ClipboardError:
        raise
    except Exception as e:
        raise ClipboardError(f"Cannot write the clipboard: {e}") from e


def copy() -> str:
    return hotkey("ctrl+c")


def cut() -> str:
    return hotkey("ctrl+x")


def paste(ctrl: Control | None = None) -> str:
    return hotkey("ctrl+v")


def paste_into(ctrl: Control | None = None) -> str:
    """Put the clipboard into a control (or the focused one)."""
    if ctrl is not None:
        try:
            ctrl.wrapper.click_input()
            time.sleep(0.08)
        except Exception:
            pass
    return hotkey("ctrl+v")


def clipboard_to_control(ctrl: Control, text: str = "") -> str:
    """Clipboard round-trip: fastest, most reliable way to enter long text.

    It never leaks into the model's context and never gets typed keystroke by
    keystroke, so it also survives apps with aggressive input filters.
    """
    if text:
        set_clipboard(text)
    if ctrl is not None:
        try:
            ctrl.wrapper.click_input()
            time.sleep(0.08)
        except Exception:
            pass
    return hotkey("ctrl+v")


def select_all() -> str:
    return hotkey("ctrl+a")


def clear_field(ctrl: Control | None = None) -> str:
    if ctrl is not None:
        try:
            ctrl.wrapper.click_input()
            time.sleep(0.06)
        except Exception:
            pass
    hotkey("ctrl+a")
    return hotkey("{DELETE}" if False else "delete")


# --------------------------------------------------------------------------
# shell-friendly shortcuts
# --------------------------------------------------------------------------
WIN_SHORTCUTS = {
    "win": "win",
    "start": "win",
    "search": "win+s",
    "run": "win+r",
    "explorer": "win+e",
    "lock": "win+l",
    "shutdown_dialog": "win+x",
    "settings": "win+i",
    "task_manager": "ctrl+shift+escape",
    "desktop": "win+d",
    "alt_tab": "alt+tab",
    "close": "alt+f4",
    "save": "ctrl+s",
    "save_as": "ctrl+shift+s",
    "print": "ctrl+p",
    "undo": "ctrl+z",
    "redo": "ctrl+y",
    "copy": "ctrl+c",
    "cut": "ctrl+x",
    "paste": "ctrl+v",
    "select_all": "ctrl+a",
    "find": "ctrl+f",
    "new_tab": "ctrl+t",
    "next_tab": "ctrl+tab",
    "close_tab": "ctrl+w",
    "refresh": "f5",
}


def do(name: str) -> str:
    """Trigger a named Windows shortcut, e.g. 'task_manager' or 'win+s'."""
    combo = WIN_SHORTCUTS.get(str(name).strip().lower())
    if combo is None:
        combo = str(name)
    if combo == "win":
        return key_tap("win")
    return hotkey(combo)
