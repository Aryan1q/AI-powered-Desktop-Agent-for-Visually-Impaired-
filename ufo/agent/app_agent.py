"""AppAgent — the window-scoped loop.

Same machine, narrower field of view: it observes and acts inside exactly one
application. It is what you want for dense, control-heavy work (a spreadsheet,
a settings dialog, a code editor) where the accessibility tree is richer than
the screen and a whole-desktop observation would only add noise.

`ufo.agent.ComputerAgent` is the whole-laptop equivalent. Both share the loop
in `ufo.agent.base`, so safety, budgets and logging behave identically.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..actions import gui
from ..config import Config
from ..errors import ActionError, WindowClosedError, WindowNotFoundError
from ..logging_setup import get_logger
from ..perception import uia, vision
from ..perception.vision import Observation
from ..safety.policy import RISK_HIGH
from ..utils import text as textutil
from .base import BASE_ACTIONS, AgentLoop, Decision

log = get_logger(__name__)

APP_ACTIONS = BASE_ACTIONS + (
    "hover", "scroll", "select", "toggle", "expand", "collapse", "invoke",
    "append_text", "get_text", "drag", "key", "paste",
)
VALID_ACTIONS = frozenset(APP_ACTIONS)

SYSTEM_PROMPT = f"""You are an automation agent controlling ONE Windows application.

Every turn you get: the task, a numbered list of interactive controls, a
screenshot with the same numbers drawn on it, screen text, and everything you
have already done.

Reply with EXACTLY one JSON object:
{{
  "thought": "why this is the next action",
  "action": "<action name>",
  "target": "<control id, or a control name>",
  "args": "<text to type, key string, seconds, or empty>",
  "status": "CONTINUE" | "FINISH" | "FAIL"
}}

Actions: {", ".join(sorted(VALID_ACTIONS))}

Rules:
- "target" is a control id from THIS turn's list, or an exact control name.
  Ids are renumbered every turn and are never valid across turns.
- "type"/"set_text" need both a control and the text in "args".
  "click"/"double_click"/"right_click"/"select"/"toggle" need a control.
  "hotkey"/"key" do not need a control; put the keys in "args".
  "wait" takes a number of seconds in "args".
- Clicking is not completing. To enter text you must actually type it.
- Prefer "set_text" for filling a field in one shot; use "type" for
  multi-line content or when the app rejects programmatic text.
- Never set status FINISH on your first turn, and never without seeing
  evidence in the controls or the screen text that the task is done.
- Do not retype the same failing action. After two failures, try a different
  control or a different approach (menu, keyboard shortcut).
"""


@dataclass
class AppAgent(AgentLoop):
    """Observe -> reason -> act inside one window."""

    window: Any = None
    valid_actions: tuple[str, ...] = APP_ACTIONS
    system_prompt: str = SYSTEM_PROMPT
    close_on_finish: bool = False
    require_state_change: bool = True
    _last_observation: Observation | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.window is None:
            raise WindowNotFoundError("AppAgent needs a window to control.")
        super().__post_init__()
        from ..utils import win32

        win32.dpi_awareness_set()
        vision.reset_frame_tracking()

    # ---- target resolution ----
    @property
    def window_title(self) -> str:
        try:
            return self.window.window_text() or ""
        except Exception:
            return ""

    def reattach(self) -> bool:
        """Re-resolve the window after it was recreated (app updates, file
        dialogs, 'save changes' prompts replacing the client area)."""
        try:
            title = self.window_title
            handle = int(getattr(self.window, "handle", 0) or 0)
        except Exception:
            return False
        try:
            if handle:
                self.window = uia.get_window(handle)
                return True
            for info in uia.list_all_windows():
                if title and info.title == title:
                    self.window = info.wrapper
                    return True
        except Exception:
            return False
        return False

    def subject(self, observation) -> str:
        return self.window_title

    # ---- observe ----
    def observe(self, step: int) -> Observation:
        if not uia.is_alive(self.window) and not self.reattach():
            raise WindowClosedError(
                f"The window {self.window_title!r} is no longer open."
            )
        if self.cfg.automation.auto_focus_window:
            uia.focus_window(self.window)
        obs = vision.observe_window(self.window, self.cfg)
        self._last_observation = obs
        return obs

    def describe_context(self) -> str:
        obs = self._last_observation
        if obs is None:
            return "(no observation)"
        blocks = [f"WINDOW: {self.window_title!r}", obs.describe(verbose=True)]
        try:
            text = uia.window_text(self.window, limit=1500)
            if text:
                blocks.append(f"WINDOW TEXT:\n{textutil.truncate(text, 1200)}")
        except Exception:
            pass
        return "\n\n".join(blocks)

    def signature(self, observation) -> Any:
        return tuple((c.type, c.name, c.value) for c in (observation.controls or []))

    def image_for_model(self, observation) -> str | None:
        return observation.b64

    # ---- act ----
    def perform(self, decision: Decision, observation) -> str:
        action = decision.action
        if action in ("finish", "fail"):
            return f"{action}: {decision.thought}"
        if action == "wait":
            seconds = float(decision.args or 1)
            time.sleep(max(0.0, min(30.0, seconds)))
            return f"waited {seconds}s"
        if action in ("screenshot",):
            from ..perception import screen
            from ..utils import image as imageutil

            cap = screen.capture_window(self.window)
            path = (self.run_dir or ".") / f"manual_{int(time.time())}.png"
            imageutil.save(cap.image, path)
            return f"saved a screenshot to {path}"
        if action in ("read_text",):
            return uia.window_text(self.window) or "(no text found)"
        if action == "key":
            from ..actions import keyboard

            return keyboard.key_tap(decision.args or decision.target)
        if action == "hotkey":
            return gui.hotkey(None, decision.args)
        if action == "paste":
            from ..actions import keyboard

            return keyboard.paste_into(self._control(decision, observation))

        ctrl = self._control(decision, observation)
        return gui.execute(action, ctrl, decision.args)

    def _control(self, decision: Decision, observation) -> Any:
        target = decision.target.strip()
        obs = observation or self._last_observation
        if not target:
            return None
        if target.lstrip("-").isdigit():
            ctrl = obs.find_control(int(target)) if obs else None
            if ctrl is None:
                raise ActionError(
                    f"Control id {target} is not in the current list. "
                    "Ids are renumbered every step."
                )
            return ctrl
        ctrl = obs.find_by_name(target) if obs else None
        if ctrl is None:
            ctrl = uia.find_control(self.window, name=target)
        if ctrl is None:
            raise ActionError(
                f"No visible control in {self.window_title!r} matches {target!r}."
            )
        return ctrl

    def _run(self) -> str:
        result = super()._run()
        if result == "FINISH" and self.close_on_finish:
            try:
                from ..actions import window as win

                log.info("closing %r after completion", self.window_title)
                win.close(self.window)
            except Exception as e:
                log.warning("could not close the window: %s", e)
        return result


def attach(task: str, title: str = "", process: str = "", cfg: Config | None = None,
           timeout: float = 30.0, **kwargs) -> AppAgent:
    """Build an AppAgent against an already-open, allowlisted window."""
    cfg = cfg or Config.load()
    info = uia.wait_for(title or ".*", timeout=timeout,
                        allowed=cfg.safety.allowlist or None) if title else \
        uia.foreground_window()
    if info is None:
        raise WindowNotFoundError(
            "No allowlisted window is open. Start the app first, or pass --allow "
            "for it, or use ComputerAgent to launch it."
        )
    return AppAgent(info.wrapper, task, cfg=cfg, **kwargs)
