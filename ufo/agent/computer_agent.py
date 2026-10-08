"""ComputerAgent — automates the whole laptop, not one application.

Observe = every monitor + every allowed window + the annotated screen.
Act = anything in `ufo.actions`: control clicks, coordinate clicks, keyboard,
window management, shell, filesystem, processes, system settings, network.

Two safety properties this adds on top of the shared loop:
  * the allowlist is re-checked on the *target window* before every action, and
  * a coordinate click is resolved to whatever window is under the cursor, so a
    pixel click cannot silently reach an app the user never allowed.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..errors import ActionError, WindowNotFoundError
from ..logging_setup import get_logger, record
from ..perception import uia, vision
from ..perception.vision import Observation
from ..safety.policy import RISK_HIGH
from ..utils import text as textutil
from ..utils import win32
from .base import SYSTEM_ACTIONS, AgentLoop, Decision

log = get_logger(__name__)

SYSTEM_PROMPT = """You are an automation agent with control of this entire Windows
computer. You can see the screen, read window titles and controls, and use the
mouse, the keyboard, and system tools.

Reply with EXACTLY one JSON object:
{
  "thought": "why this is the next action",
  "action": "<action name>",
  "target": "<control id, window title, app name, or x,y screen coordinates>",
  "args": "<text, key string, command, seconds, or empty>",
  "status": "CONTINUE" | "FINISH" | "FAIL"
}

Actions:
  GUI on a control id:   click, double_click, right_click, type, set_text
  Screen coordinates:    click_at, double_click_at, right_click_at, move,
                         scroll_at, drag        (target is "x,y" in SCREEN pixels)
  Keyboard:              hotkey, key              (args: "ctrl+s", "enter", "f5")
  Windows:               list_windows, focus_window, app
                         (args for app: "open notepad", "close notepad",
                          "list", or a full path to launch)
  System settings:       volume (args 0-100), audio (args mute/unmute),
                         power (args lock/sleep/shutdown_dialog), browser (args url)
  Files and shell:       run_command, read_file, write_file, list_dir, search_files
  Utility:               screenshot, read_text, wait (args seconds)

Rules:
- "action" must be one of the names listed above, spelled exactly. There is no
  close, quit, minimise, maximise or switch action. Never invent a name; if
  nothing fits, use "fail" and explain in "thought".
- Coordinates in "target" are SCREEN pixels on the current virtual desktop.
  The observation tells you the screen size and origin; use them.
- Prefer a numbered control over raw coordinates whenever a control exists.
- If the task is a question and CURRENT STATE already shows the answer, reply
  with status FINISH and put the answer in "args". Never click or type to
  answer a question. If the answer is not there yet, look for it with
  list_windows or read_text first.
- run_command args are PowerShell. Only use it when there is genuinely no UI
  way to do the task, and prefer read-only commands.
- Never enter passwords, PINs, card numbers or one-time codes. Ask the user to
  type those themselves, and say so in "thought".
- Do not close an application that has unsaved work, and do not run destructive
  commands, unless the user asked for exactly that.
- Set status FINISH only when the screen clearly shows the task is done.
"""


@dataclass
class WindowUnderCursor:
    handle: int
    title: str
    allowed: bool
    rect: tuple

    def to_prompt(self) -> str:
        state = "allowed" if self.allowed else "NOT ALLOWED"
        return f'"{self.title}" ({state})'


@dataclass
class ComputerAgent(AgentLoop):
    """The whole-laptop loop."""

    monitor: int = -1
    valid_actions: tuple[str, ...] = SYSTEM_ACTIONS
    system_prompt: str = SYSTEM_PROMPT
    allow_system: bool = True
    _last_observation: Observation | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        win32.dpi_awareness_set()
        vision.reset_frame_tracking()
        if not self.allow_system:
            self.valid_actions = tuple(
                a for a in self.valid_actions
                if a not in ("run_command", "write_file", "power")
            )

    # ---- observe ----
    def observe(self, step: int) -> Observation:
        obs = vision.observe_screen(self.cfg, monitor=self.monitor)
        self._last_observation = obs
        if obs.text:
            self.history.append(f"note: {obs.text}")
        return obs

    def describe_context(self) -> str:
        obs = self._last_observation
        if obs is None:
            return "(no observation)"
        blocks = [obs.describe(verbose=True)]
        under = self.window_under_cursor(obs)
        if under:
            blocks.append(f"WINDOW UNDER THE CURSOR: {under.to_prompt()}")
        blocked = [w.title for w in obs.windows
                   if not uia.is_allowed(w, self.cfg.safety.allowlist)]
        if blocked:
            blocks.append(
                "NOT ALLOWED (do not interact with these): "
                + ", ".join(repr(t) for t in blocked[:10])
            )
        return "\n\n".join(blocks)

    def signature(self, observation) -> Any:
        return (observation.foreground().title if observation.foreground() else None,
                tuple((c.type, c.name) for c in observation.controls[:40]))

    def subject(self, observation, decision=None) -> str:
        """The allowlist gate: which window the action is really happening in.

        The window that receives a keystroke or a click is the focused one, so
        that is the default subject. An action that *names* its window is
        judged on the window it named, resolved the same way the action itself
        resolves it — otherwise the allowlist becomes unusable: focusing a
        permitted window was blocked because whatever happened to be in front
        was not permitted, which left no way to ever reach a permitted window.
        An unresolvable name falls back to the foreground, which blocks.
        """
        if decision is not None:
            if decision.action in WINDOW_TARGET_ACTIONS:
                named = self._resolve_named_window(decision)
                if named is not None:
                    return named
            if decision.action in GLOBAL_ACTIONS:
                return ""
        if not observation:
            return ""
        foreground = observation.foreground()
        return foreground.title if foreground else ""

    def _resolve_named_window(self, decision) -> str | None:
        """The window this action names, resolved against the real ones.

        Resolution goes through the same allowlist-filtered search the action
        itself uses, so the returned title is a window that really exists. When
        the name does not resolve, the requested title is returned unchanged so
        the policy still judges it: an empty subject is treated by the policy
        as "no window check at all", which would let a request name its way
        past the gate. The action itself will then fail to find the window.
        """
        from ..actions import window as win

        wanted = str(decision.target or decision.args or "").strip()
        if not wanted:
            return None
        try:
            info = win.find(title=wanted, allowed=self.cfg.safety.allowlist or None)
        except Exception:
            return wanted
        return getattr(info, "title", "") or wanted

    _current_action_target_is_global: bool = False

    def window_under_cursor(self, observation=None) -> WindowUnderCursor | None:
        """Which window a coordinate click would actually land in."""
        obs = observation or self._last_observation
        x, y = win32.cursor_pos()
        # cursor_pos and WindowInfo.rect are both screen coordinates, so they
        # are compared directly. The observation only says whether the cursor
        # was inside the captured region at all; if it was not, the capture is
        # stale and the answer is worth less, but the coords still stand.
        if obs is not None and obs.image is not None and not obs.contains(x, y):
            log.debug("cursor at %s is outside the captured region %s",
                      (x, y), obs.bbox)
        best = None
        for info in uia.list_all_windows():
            left, top, right, bottom = info.rect
            if left <= x < right and top <= y < bottom:
                if best is None or info.is_foreground:
                    best = info
        if best is None:
            return None
        return WindowUnderCursor(best.handle, best.title,
                                 uia.is_allowed(best, self.cfg.safety.allowlist),
                                 best.rect)

    # ---- act ----
    def perform(self, decision: Decision, observation) -> str:
        handler = getattr(self, f"_do_{decision.action}", None)
        if handler is None:
            raise ActionError(
                f"No handler for action {decision.action!r}. "
                f"Available: {', '.join(sorted(a for a in self.valid_actions))}"
            )
        self._current_action_target_is_global = decision.action in GLOBAL_ACTIONS
        self._focus_named_window(decision, observation)
        return handler(decision, observation)

    def _focus_named_window(self, decision: Decision, observation) -> str | None:
        """Raise a named allowlisted window before typing into it.

        A keystroke goes to whatever is focused, so `type` aimed at a window
        that is behind another one would land in the wrong place. The guard
        catches that by refusing the action, which is right but leaves the
        agent stuck retrying: it asked for a specific window by name, that
        window is on the allowlist, and the user plainly granted permission to
        type into it. Focusing it first is what a person would do.

        The focus is then *confirmed* before the keystrokes go out. The guard
        approved this action against the window we intended, and a window that
        vanished or refused focus between that check and the typing would send
        the text somewhere the policy never approved — into a browser, a
        terminal, another app. If the intended window is not actually in
        front, nothing is typed at all.
        """
        if decision.action not in KEYBOARD_ACTIONS:
            return None
        named = self._resolve_named_window(decision)
        if not named:
            return None
        foreground = observation.foreground() if observation else None
        if foreground is not None and foreground.title == named:
            self._require_front_window(named, decision)
            return None

        from ..actions import window as win

        try:
            info = win.find(title=named, allowed=self.cfg.safety.allowlist or None)
        except Exception:
            return None                      # not a window name: leave it alone
        try:
            win.focus(info.wrapper)
        except Exception as e:
            raise ActionError(
                f"Could not bring {named!r} to the front before "
                f"{decision.action}; refusing to type, because the keystrokes "
                f"would land in whichever window is focused instead."
            ) from e
        log.info("focused %r before %s", named, decision.action)
        self._require_front_window(named, decision)
        return named

    def _require_front_window(self, expected: str, decision: Decision) -> None:
        """Abort unless `expected` is the window that has focus right now."""
        from ..perception import uia

        front = uia.foreground_window()
        actual = getattr(front, "title", None) if front is not None else None
        if actual == expected:
            return
        allowlist = self.cfg.safety.allowlist
        if actual and allowlist:
            from ..safety.policy import ActionPolicy

            if not ActionPolicy.from_config(self.cfg.safety).window_allowed(actual):
                raise ActionError(
                    f"Refusing to {decision.action}: {expected!r} did not come "
                    f"to the front, and the window that has focus ({actual!r}) "
                    f"is not in the allowlist."
                )
        raise ActionError(
            f"Refusing to {decision.action}: expected {expected!r} to be in "
            f"front, but {actual!r} is. Refocus it and try again."
        )

    # -- pointer --
    def _coords(self, decision: Decision, observation) -> tuple[int, int]:
        raw = decision.target or decision.args
        parts = str(raw).replace(";", ",").split(",")
        if len(parts) < 2:
            raise ActionError(
                f"{decision.action} needs 'x,y' screen coordinates; got {raw!r}."
            )
        try:
            x, y = int(float(parts[0])), int(float(parts[1]))
        except ValueError as e:
            raise ActionError(f"Bad coordinates {raw!r}: {e}") from e
        if observation is not None and observation.image is not None:
            # The model reads local pixels; convert back to screen space.
            return observation.to_screen(x, y)
        return win32.clamp_to_screen(x, y)

    def _check_point_allowed(self, x: int, y: int) -> None:
        under = self.window_under_cursor(self._last_observation)
        if under is not None and not under.allowed and self.cfg.safety.allowlist:
            raise ActionError(
                f"Refusing to click at ({x},{y}): that point is inside "
                f"{under.title!r}, which is not in the allowlist."
            )

    def _do_click_at(self, decision, observation) -> str:
        from ..actions import mouse

        x, y = self._coords(decision, observation)
        self._check_point_allowed(x, y)
        return mouse.click_at(x, y)

    def _do_double_click_at(self, decision, observation) -> str:
        from ..actions import mouse

        x, y = self._coords(decision, observation)
        self._check_point_allowed(x, y)
        return mouse.double_click_at(x, y)

    def _do_right_click_at(self, decision, observation) -> str:
        from ..actions import mouse

        x, y = self._coords(decision, observation)
        self._check_point_allowed(x, y)
        return mouse.right_click_at(x, y)

    def _do_move(self, decision, observation) -> str:
        from ..actions import mouse

        x, y = self._coords(decision, observation)
        return f"moved the pointer to ({x},{y})"

    def _do_scroll_at(self, decision, observation) -> str:
        from ..actions import mouse

        amount = int(float(decision.args or -3))
        if decision.target:
            x, y = self._coords(decision, observation)
            return mouse.scroll_at(x, y, amount)
        return mouse.scroll_at(amount=amount)

    def _do_drag(self, decision, observation) -> str:
        from ..actions import mouse

        start = self._coords(decision, observation)
        end_parts = str(decision.args or "0,0").split(",")
        try:
            dx = int(float(end_parts[0] or 0))
            dy = int(float(end_parts[1] or 0)) if len(end_parts) > 1 else 0
        except ValueError as e:
            raise ActionError(f"drag args must be 'dx,dy'; got {decision.args!r}") from e
        return mouse.drag_from(start[0], start[1], dx, dy)

    # -- control-targeted --
    def _resolve_control(self, decision: Decision, observation) -> Any:
        if decision.target.strip().isdigit():
            ctrl = observation.find_control(int(decision.target))
            if ctrl is None:
                raise ActionError(
                    f"Control id {decision.target} is not in the current list. "
                    "Ids change every step — re-read the observation."
                )
            return ctrl
        ctrl = observation.find_by_name(decision.target)
        if ctrl is None and self._last_observation is not None:
            for info in uia.list_all_windows():
                if not uia.is_allowed(info, self.cfg.safety.allowlist) or not info.wrapper:
                    continue
                try:
                    ctrl = uia.find_control(info.wrapper, name=decision.target)
                except Exception:
                    ctrl = None
                if ctrl is not None:
                    break
        if ctrl is None:
            raise ActionError(
                f"No control matching {decision.target!r} is visible right now."
            )
        return ctrl

    def _do_click(self, decision, observation) -> str:
        from ..actions import gui

        return gui.click(self._resolve_control(decision, observation))

    def _do_double_click(self, decision, observation) -> str:
        from ..actions import gui

        return gui.double_click(self._resolve_control(decision, observation))

    def _do_right_click(self, decision, observation) -> str:
        from ..actions import gui

        return gui.right_click(self._resolve_control(decision, observation))

    def _do_type(self, decision, observation) -> str:
        from ..actions import keyboard

        ctrl = self._resolve_control(decision, observation) if decision.target else None
        return keyboard.type_text(decision.args, ctrl=ctrl)

    def _do_set_text(self, decision, observation) -> str:
        from ..actions import gui

        return gui.set_text(self._resolve_control(decision, observation), decision.args)

    # -- keyboard --
    def _do_hotkey(self, decision, observation) -> str:
        from ..actions import keyboard

        return keyboard.hotkey(decision.args)

    def _do_key(self, decision, observation) -> str:
        from ..actions import keyboard

        return keyboard.key_tap(decision.args or decision.target)

    # -- windows --
    def _do_list_windows(self, decision, observation) -> str:
        windows = uia.list_all_windows()
        allowed = [w for w in windows if uia.is_allowed(w, self.cfg.safety.allowlist)]
        lines = []
        for w in windows[:25]:
            mark = "OK " if w in allowed else "-- "
            lines.append(f"  {mark}[{w.handle}] {w.title!r} ({w.process_name})"
                         + (" [front]" if w.is_foreground else ""))
        blocked = len(windows) - len(allowed)
        return f"{len(windows)} open window(s), {len(allowed)} allowed:\n" + "\n".join(lines) + (
            f"\n({blocked} not in the allowlist and must not be touched)" if blocked else "")

    def _do_focus_window(self, decision, observation) -> str:
        from ..actions import window as win

        target = decision.target or decision.args
        info = win.find(title=target, allowed=self.cfg.safety.allowlist or None)
        return win.focus(info.wrapper)

    def _do_app(self, decision, observation) -> str:
        from ..actions import shell
        from ..actions import window as win

        verb = textutil.collapse_ws(decision.args or decision.target).lower()
        if verb in ("list", ""):
            return self._do_list_windows(decision, observation)
        if verb.startswith("open ") or verb.startswith("launch "):
            name = verb.split(" ", 1)[1].strip()
            return self.launch(name)
        if verb.startswith("close "):
            name = verb.split(" ", 1)[1].strip()
            info = win.find(title=name, allowed=self.cfg.safety.allowlist or None)
            return win.close(info.wrapper)
        if verb.startswith("focus "):
            name = verb.split(" ", 1)[1].strip()
            info = win.find(title=name, allowed=self.cfg.safety.allowlist or None)
            return win.focus(info.wrapper)
        return self.launch(verb)

    def launch(self, name: str, wait: float = 2.0) -> str:
        """Start a program and report the window it produced."""
        from ..actions import shell

        if os.path_is_path(name):
            if os.path.isfile(name):
                return shell.open_path(name)
            raise ActionError(f"No such file: {name}")
        before = uia.snapshot_handles()
        try:
            pid = shell.launch(name)
        except Exception as e:
            resolved = _resolve_app(name)
            if resolved is None:
                raise ActionError(
                    f"Could not start {name!r}: {e}. Pass a full path "
                    "(e.g. C:\\\\Windows\\\\System32\\\\notepad.exe) if it is not on PATH."
                ) from e
            pid = shell.launch(resolved)
            name = resolved
        time.sleep(wait)
        new = uia.list_all_windows()
        opened = [w for w in new if w.handle not in before]
        record("app_launch", app=name, pid=pid, new_windows=len(opened))
        if opened:
            return (f"launched {name} (pid {pid}); new window(s): "
                    + ", ".join(repr(w.title) for w in opened[:3]))
        return f"launched {name} (pid {pid}); no new window yet"

    # -- system --
    def _do_volume(self, decision, observation) -> str:
        from ..actions import system

        try:
            return system.set_volume(int(float(decision.args or 50)))
        except ValueError:
            return system.set_volume(50)

    def _do_audio(self, decision, observation) -> str:
        from ..actions import system

        verb = textutil.collapse_ws(decision.args or decision.target).lower()
        if verb.startswith("unmute"):
            return system.mute(False)
        if verb.startswith("mute"):
            return system.mute(True)
        if verb == "next":
            return system.media_next()
        if verb == "previous":
            return system.media_previous()
        if verb in ("play", "pause", "play/pause"):
            return system.media_play_pause()
        return system.mute(not _is_muted())

    def _do_power(self, decision, observation) -> str:
        from ..actions import system

        verb = textutil.collapse_ws(decision.args or decision.target).lower()
        if verb == "lock":
            return system.lock()
        if verb == "sleep":
            return system.sleep()
        if verb in ("shutdown", "restart", "logoff", "hibernate"):
            raise ActionError(
                f"{verb!r} would end the user's session and is never executed "
                "automatically. Open the Start menu and let the user choose, "
                "or run it yourself if that is really what you want."
            )
        if verb == "shutdown_dialog":
            return system.start_menu()
        raise ActionError(
            f"Unknown power action {verb!r}. Use lock, sleep, or shutdown_dialog."
        )

    def _do_browser(self, decision, observation) -> str:
        from ..actions import network

        return network.open_browser(decision.args or decision.target)

    # -- shell / files --
    def _do_run_command(self, decision, observation) -> str:
        from ..actions import shell

        return str(shell.run_powershell(decision.args, timeout=self.auto.action_timeout))

    def _do_read_file(self, decision, observation) -> str:
        from ..actions import filesystem

        return filesystem.read_text(decision.target or decision.args, max_bytes=200_000)

    def _do_write_file(self, decision, observation) -> str:
        from ..actions import filesystem

        if not decision.args:
            raise ActionError("write_file needs the content in 'args'.")
        return filesystem.write_text(decision.target, decision.args)

    def _do_list_dir(self, decision, observation) -> str:
        from ..actions import filesystem

        entries = filesystem.list_dir(decision.target or decision.args or ".")
        return "\n".join(
            f"  {'[dir] ' if e.is_dir else '      '}{e.path.name}"
            f"{'' if e.is_dir else f'  ({e.size} bytes)'}"
            for e in entries[:80]
        ) or "(empty)"

    def _do_search_files(self, decision, observation) -> str:
        from ..actions import filesystem

        root = decision.target or "."
        pattern = decision.args or "*"
        found = filesystem.search(root, pattern, limit=50)
        return "\n".join(str(p) for p in found) or f"no files matching {pattern!r} under {root}"

    # -- utility --
    def _do_screenshot(self, decision, observation) -> str:
        from ..perception import screen

        cap = screen.capture_monitor(self.monitor) if self.monitor >= 0 else screen.capture_full()
        from ..utils import image as imageutil

        path = (self.run_dir or ".") / f"manual_{int(time.time())}.png"
        imageutil.save(cap.image, path)
        return f"saved a screenshot to {path}"

    def _do_read_text(self, decision, observation) -> str:
        from ..perception import uia as uia_mod

        if decision.target:
            info = uia_mod.list_all_windows()
            for w in info:
                if decision.target.lower() in w.title.lower():
                    return uia_mod.window_text(w.wrapper) or "(no text found)"
            raise ActionError(f"No window matching {decision.target!r}.")
        fg = uia_mod.foreground_window()
        if fg is None:
            return "no focused window"
        return uia_mod.window_text(fg.wrapper) or "(no text found in the focused window)"

    def _do_wait(self, decision, observation) -> str:
        seconds = float(decision.args or 1)
        time.sleep(max(0.0, min(MAX_WAIT, seconds)))
        return f"waited {seconds}s"

    def _do_finish(self, decision, observation) -> str:
        return "finished"

    def _do_fail(self, decision, observation) -> str:
        return f"failed: {decision.thought or 'no reason given'}"


GLOBAL_ACTIONS = {
    "run_command", "read_file", "write_file", "list_dir", "search_files",
    "list_windows", "volume", "audio", "power", "browser", "wait",
    "screenshot", "read_text", "focus_window", "app", "finish", "fail",
}

# Actions whose target is a window name rather than a control. They are gated
# on the window they name, not on whatever happens to be in front.
WINDOW_TARGET_ACTIONS = ("focus_window",)

# Actions whose keystrokes land in the focused window. Naming an allowlisted
# window raises it first, rather than leaving the agent stuck retrying.
KEYBOARD_ACTIONS = ("type", "set_text", "hotkey", "key")

MAX_WAIT = 30.0

_COMMON_APPS = {
    "notepad": "notepad.exe", "calculator": "calc.exe", "calc": "calc.exe",
    "paint": "mspaint.exe", "explorer": "explorer.exe", "files": "explorer.exe",
    "terminal": "wt.exe", "powershell": "powershell.exe",
    "command prompt": "cmd.exe", "cmd": "cmd.exe",
    "edge": "msedge.exe", "chrome": "chrome.exe", "firefox": "firefox.exe",
    "outlook": "outlook.exe", "settings": "ms-settings:",
    "task manager": "taskmgr.exe", "registry editor": "regedit.exe",
    "vscode": "code.exe", "code": "code.exe", "spotify": "spotify.exe",
    "word": "winword.exe", "excel": "excel.exe", "powerpoint": "powerpnt.exe",
}


def _resolve_app(name: str) -> str | None:
    from ..actions import shell

    key = name.strip().lower()
    if key in _COMMON_APPS:
        return _COMMON_APPS[key]
    import shutil

    for candidate in (name, f"{name}.exe", _COMMON_APPS.get(key, "")):
        if candidate and shutil.which(candidate):
            return candidate
    for folder in (r"C:\Program Files", r"C:\Program Files (x86)",
                   r"C:\Program Files (x86)\WindowsApps", os.environ.get("LOCALAPPDATA", "")):
        if not folder or not os.path.isdir(folder):
            continue
        for match in glob_top(folder, f"{name}*.exe", depth=3):
            return match
    del shell
    return None


def glob_top(root: str, pattern: str, depth: int = 2) -> list[str]:
    """Cheap bounded glob — WindowsApps is enormous, so do not walk it fully."""
    import fnmatch
    import os

    out: list[str] = []
    base_depth = root.rstrip("\\/").count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.count(os.sep) - base_depth >= depth:
            dirnames[:] = []
        if any(part in dirnames for part in ("node_modules", ".git")):
            dirnames[:] = []
        for name in filenames:
            if fnmatch.fnmatch(name, pattern):
                out.append(os.path.join(dirpath, name))
                if len(out) >= 5:
                    return out
    return out


def os_path_is_path(value: str) -> bool:
    import os

    return os.path.isabs(value) or ":" in value or value.startswith("\\\\")


def _is_muted() -> bool:
    from ..actions import system

    try:
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume  # type: ignore
        from ctypes import cast, POINTER

        device = AudioUtilities.GetSpeakers()
        interface = device.Activate(IAudioEndpointVolume._iid_, 0, None)
        return bool(cast(interface, POINTER(IAudioEndpointVolume)).GetMute())
    except Exception:
        return False
