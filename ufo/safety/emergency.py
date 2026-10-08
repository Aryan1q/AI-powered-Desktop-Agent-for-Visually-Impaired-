"""Global emergency abort.

A background thread holds a low-level keyboard hook for the panic hotkey
(default Ctrl+Alt+Q). Once tripped it stays tripped: every action in the
process consults `emergency.tripped` before it runs, so the abort is honoured
even if the model is mid-thought or a confirmation prompt is open.

The hook falls back to polling `GetAsyncKeyState` if `SetWindowsHookEx` is
refused (some hardened setups deny it), so the kill switch still works.
"""
from __future__ import annotations

import ctypes
import threading
from typing import Callable

from ..utils import text as textutil
from ..utils import win32

user32 = getattr(ctypes, "windll", None) and ctypes.windll.user32

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
WM_QUIT = 0x0012

VK_CONTROL, VK_MENU, VK_Q = 0x11, 0x12, 0x51

LLKHF_INJECTED = 0x00000010


class EmergencySwitch:
    """One-way latch plus a user interrupt channel."""

    def __init__(self, reason: str = ""):
        self._event = threading.Event()
        self.reason = reason
        self._listeners: list[Callable[[str], None]] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # -- state --
    @property
    def tripped(self) -> bool:
        return self._event.is_set()

    def trip(self, reason: str = "emergency stop requested") -> None:
        with self._lock:
            if self._event.is_set():
                return
            self.reason = reason
            self._event.set()
        for fn in list(self._listeners):
            try:
                fn(reason)
            except Exception:
                pass

    def reset(self) -> None:
        with self._lock:
            self.reason = ""
            self._event.clear()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def check(self, what: str = "action") -> None:
        """Raise if the kill switch is engaged. Called before every action."""
        from ..errors import EmergencyAbort

        if self.tripped:
            raise EmergencyAbort(f"Emergency stop engaged before {what}: {self.reason}")

    def on_trip(self, callback: Callable[[str], None]) -> None:
        self._listeners.append(callback)

    # -- hotkey watch --
    def start_hotkey_watch(self, hotkey: str = "ctrl+alt+q") -> bool:
        """Start watching the panic hotkey. Returns True if a hook is active."""
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        keys = [win32.VK_MAP.get(textutil.normalize_key(k)) for k in textutil.parse_hotkey(hotkey)]
        if any(k is None for k in keys):
            return False
        self._thread = threading.Thread(
            target=self._watch, args=(tuple(keys), hotkey),
            name="ufo-emergency", daemon=True,
        )
        self._thread.start()
        return True

    def stop_hotkey_watch(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive() and user32:
            try:
                user32.PostThreadMessageW(threading.get_ident(), WM_QUIT, 0, 0)
            except Exception:
                pass
        self._thread = None

    # -- internals --
    def _declare_prototypes(self) -> None:
        """Give the user32 calls explicit 64-bit-safe signatures.

        Without argtypes, ctypes marshals every integer as a 32-bit ``c_int``.
        LPARAM and LRESULT are pointer-sized, so a hook's ``lparam`` routinely
        exceeds 2**31 and the call raised

            ctypes.ArgumentError: argument 4: OverflowError: int too long

        from inside the hook callback. ctypes swallows an exception raised in a
        callback, so the hook kept running and the emergency stop never fired —
        the failure was silent, which is the worst possible failure for the one
        control that is supposed to always work.
        """
        if not user32:
            return
        ptr = ctypes.c_void_p
        longptr = ctypes.c_longlong          # LPARAM / LRESULT / LPARAM-ish
        ulongptr = ctypes.c_ulonglong        # WPARAM
        for name, argtypes, restype in (
            ("CallNextHookEx", [ptr, ctypes.c_int, ulongptr, longptr], longptr),
            ("GetAsyncKeyState", [ctypes.c_int], ctypes.c_short),
            ("SetWindowsHookExW", [ctypes.c_int, ptr, ctypes.c_void_p,
                                   ctypes.c_uint], ptr),
            ("UnhookWindowsHookEx", [ptr], ctypes.c_int),
            ("PostThreadMessageW", [ctypes.c_ulong, ctypes.c_uint,
                                    ctypes.c_ulong, ctypes.c_longlong], ctypes.c_int),
        ):
            func = getattr(user32, name, None)
            if func is None:
                continue
            try:
                func.argtypes = argtypes
                func.restype = restype
            except (AttributeError, TypeError):
                pass

    def _watch(self, keys: tuple[int, ...], hotkey: str) -> None:  # pragma: no cover - needs a real message loop
        if not user32:
            return
        self._declare_prototypes()
        if not self._hook_loop(keys, hotkey):
            self._poll_loop(keys, hotkey)

    def _hook_loop(self, keys: tuple[int, ...], hotkey: str) -> bool:  # pragma: no cover
        from ..logging_setup import get_logger

        log = get_logger(__name__)
        KBDSTRUCT = ctypes.Structure
        state = {"armed": False, "ok": True}

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", ctypes.c_uint), ("scanCode", ctypes.c_uint),
                        ("flags", ctypes.c_uint), ("time", ctypes.c_uint),
                        ("dwExtraInfo", ctypes.c_void_p)]

        def _proc(code, wparam, lparam):
            if code >= 0:
                try:
                    data = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    if data.flags & LLKHF_INJECTED:
                        return user32.CallNextHookEx(None, code, wparam, lparam)
                    vk = int(data.vkCode)
                    if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        if vk == keys[-1] and all(
                            user32.GetAsyncKeyState(k) & 0x8000 for k in keys[:-1]
                        ):
                            state["armed"] = False
                            log.warning("Emergency stop hotkey %s pressed.", hotkey)
                            self.trip(f"panic hotkey {hotkey} pressed")
                            return 1
                        state["armed"] = True
                    elif wparam in (WM_KEYUP, 0x0105):
                        state["armed"] = False
                except Exception:
                    pass
            return user32.CallNextHookEx(None, code, wparam, lparam)

        try:
            proc_t = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int, ctypes.c_ulonglong, ctypes.c_longlong)
            hook = proc_t(_proc)
            handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, hook, None, 0)
        except Exception as e:
            log.debug("SetWindowsHookEx failed (%s); falling back to polling.", e)
            return False
        if not handle:
            return False

        msg = ctypes.wintypes.MSG()
        while not self._stop.is_set() and not self.tripped:
            if user32.GetMessageW(ctypes.byref(msg), None, 0, 0) == 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        try:
            user32.UnhookWindowsHookEx(handle)
        except Exception:
            pass
        del KBDSTRUCT, state
        return True

    def _poll_loop(self, keys: tuple[int, ...], hotkey: str) -> None:
        """Fallback: sample modifier state once per 30 ms."""
        from ..logging_setup import get_logger

        log = get_logger(__name__)
        was_down = False
        while not self._stop.is_set() and not self.tripped:
            try:
                down = all(user32.GetAsyncKeyState(k) & 0x8000 for k in keys)
                if down and not was_down:
                    log.warning("Emergency stop hotkey %s pressed (polled).", hotkey)
                    self.trip(f"panic hotkey {hotkey} pressed")
                    break
                was_down = down
            except Exception:
                break
            self._stop.wait(0.03)


# Process-wide latch shared by every agent run.
emergency = EmergencySwitch()


def active_reason() -> str:
    return emergency.reason or "emergency stop"
