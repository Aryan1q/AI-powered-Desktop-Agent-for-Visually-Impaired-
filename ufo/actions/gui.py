"""Control-level GUI actions against a pywinauto UIA wrapper.

These are the "smart" actions: they target a semantic element, so they keep
working when the window moves, the theme changes, or the DPI differs.
`mouse`/`keyboard` are the coordinate-based fallback for UIA-invisible UIs.

Every function returns a short human-readable result string, because that
string goes into the agent's history and gets spoken aloud.
"""
from __future__ import annotations

import time

from ..errors import ControlNotActionableError, ControlNotFoundError
from ..perception.uia import Control
from ..utils import text as textutil

try:
    from pywinauto.keyboard import send_keys as _send_keys
except Exception:  # pragma: no cover - pywinauto missing
    _send_keys = None

TYPE_PAUSE = 0.008
ACTION_PAUSE = 0.12

# Characters that pywinauto's send_keys parser treats as syntax.
_SPECIAL = set("{}+^%~()[]")
_BRACED = {
    "\n": "{ENTER}", "\r": "{ENTER}", "\t": "{TAB}",
    "\v": "{VK_SPACE}", "\b": "{BACKSPACE}",
}

# Patterns that mean "this is not a text field" even if the type says Edit.
_NON_EDIT = ("Button", "CheckBox", "RadioButton", "Hyperlink", "MenuItem",
             "SplitButton", "Slider", "Spinner", "TabItem", "ListItem",
             "TreeItem", "ComboBox")


def _require(ctrl: Control | None) -> Control:
    if ctrl is None:
        raise ControlNotFoundError("Action needs a control but none was resolved.")
    if getattr(ctrl, "wrapper", None) is None:
        raise ControlNotFoundError(
            f"Control [{ctrl.id}] {ctrl.label} is no longer available "
            "(it was rebuilt or the window changed)."
        )
    return ctrl


def _check_enabled(ctrl: Control) -> None:
    if not ctrl.enabled:
        raise ControlNotActionableError(
            f"Control {ctrl.label} is disabled and cannot be interacted with."
        )


def escape(text: str) -> str:
    """Escape text for pywinauto's key parser."""
    out = []
    for ch in str(text):
        if ch in _BRACED:
            out.append(_BRACED[ch])
        elif ch in _SPECIAL:
            out.append("{" + ch + "}")
        else:
            out.append(ch)
    return "".join(out)


def _settle(ctrl: Control) -> None:
    """Let a control redraw before we act on it."""
    if ctrl.type in ("Button", "MenuItem", "TabItem", "ListItem", "TreeItem"):
        time.sleep(ACTION_PAUSE)


# --------------------------------------------------------------------------
# pointer
# --------------------------------------------------------------------------
def click(ctrl: Control, double: bool = False, right: bool = False,
          button: str = "left", offset: tuple[int, int] | None = None) -> str:
    _require(ctrl)
    _check_enabled(ctrl)
    w = ctrl.wrapper
    if offset is not None:
        x = ctrl.rect[0] + offset[0]
        y = ctrl.rect[1] + offset[1]
        from . import mouse

        return mouse.click_at(x, y, double=double, right=right, button=button)
    if ctrl.offscreen:
        try:
            w.scroll_into_view()
            time.sleep(0.2)
        except Exception:
            raise ControlNotActionableError(
                f"{ctrl.label} is scrolled out of view and could not be revealed."
            ) from None
    if right or button == "right":
        w.click_input(button="right")
    elif double:
        w.double_click_input()
    else:
        w.click_input()
    _settle(ctrl)
    verb = "double-clicked" if double else ("right-clicked" if right else "clicked")
    return f"{verb} {ctrl.label}"


def double_click(ctrl: Control) -> str:
    return click(ctrl, double=True)


def right_click(ctrl: Control) -> str:
    return click(ctrl, right=True)


def hover(ctrl: Control) -> str:
    _require(ctrl)
    try:
        ctrl.wrapper.move_mouse_input()
    except Exception as e:
        raise ControlNotActionableError(f"Cannot hover {ctrl.label}: {e}") from e
    time.sleep(0.25)
    return f"hovered {ctrl.label}"


def scroll(ctrl: Control, amount: int = -3) -> str:
    _require(ctrl)
    if ctrl.type in ("ComboBox", "List", "ListItem", "Tree", "TreeItem",
                     "Slider", "Spinner", "ScrollBar"):
        try:
            ctrl.wrapper.scroll("up" if amount > 0 else "down",
                                "wheel", abs(int(amount)))
            return f"scrolled {ctrl.label} by {amount}"
        except Exception:
            pass
    from . import mouse

    x, y = ctrl.center
    return mouse.scroll_at(x, y, amount, control=ctrl.label)


def drag(ctrl: Control, dx: int = 0, dy: int = 0, steps: int = 12) -> str:
    _require(ctrl)
    from . import mouse

    return mouse.drag_from(*ctrl.center, dx, dy, steps=steps, source=ctrl.label)


# --------------------------------------------------------------------------
# text
# --------------------------------------------------------------------------
def type_text(ctrl: Control, text: str) -> str:
    """Click to focus, then type. Newlines become Enter."""
    ctrl = _require(ctrl)
    if not ctrl.enabled:
        raise ControlNotActionableError(f"{ctrl.label} is not editable (disabled).")
    if _send_keys is None:
        raise ControlNotActionableError("pywinauto is unavailable; cannot type.")
    text = str(text)
    try:
        ctrl.wrapper.click_input()
    except Exception:
        pass
    time.sleep(0.1)
    _send_keys(escape(text), with_spaces=True, with_newlines=True, pause=TYPE_PAUSE)
    return f"typed {len(text)} chars into {ctrl.label}"


def set_text(ctrl: Control, text: str) -> str:
    """Replace a field's contents without retyping the whole thing.

    Prefers the control pattern (instant, no keystrokes, no IME interference),
    then ValuePattern, and only falls back to select-all + type.
    """
    ctrl = _require(ctrl)
    text = str(text)
    w = ctrl.wrapper
    for attempt in (lambda: w.set_edit_text(text),
                    lambda: w.set_text(text),
                    lambda: w.set_value(text),
                    lambda: _set_value_pattern(w, text)):
        try:
            attempt()
            return f"set {len(text)} chars in {ctrl.label}"
        except Exception:
            continue
    if _send_keys is None:
        raise ControlNotActionableError("No way to set text on this control.")
    try:
        w.click_input()
    except Exception:
        pass
    _send_keys("^a{BACKSPACE}", pause=TYPE_PAUSE)
    if text:
        _send_keys(escape(text), with_spaces=True, with_newlines=True, pause=TYPE_PAUSE)
    return f"replaced contents of {ctrl.label} with {len(text)} chars"


def _set_value_pattern(wrapper, text: str) -> None:
    wrapper.iface_value.SetValue(text)


def get_text(ctrl: Control) -> str:
    from ..perception import uia

    return uia.control_value(ctrl)


def append_text(ctrl: Control, text: str) -> str:
    ctrl = _require(ctrl)
    existing = get_text(ctrl)
    return set_text(ctrl, f"{existing}{text}")


# --------------------------------------------------------------------------
# value controls
# --------------------------------------------------------------------------
def select_combo_item(ctrl: Control, item: str = "") -> str:
    """Pick an item in a ComboBox: by text, or by index in `item`."""
    _require(ctrl)
    w = ctrl.wrapper
    if item.isdigit() and not item.strip().lstrip("-").isdigit() is False:
        pass
    if item and not item.isdigit():
        try:
            w.select(item)
            return f"selected {item!r} in {ctrl.label}"
        except Exception:
            pass
    if item.isdigit():
        try:
            w.select(int(item))
            return f"selected index {item} in {ctrl.label}"
        except Exception as e:
            raise ControlNotActionableError(
                f"Could not select index {item} in {ctrl.label}: {e}") from e
    try:
        w.select()
        return f"opened {ctrl.label}"
    except Exception as e:
        raise ControlNotActionableError(f"{ctrl.label} is not selectable: {e}") from e


def toggle_checkbox(ctrl: Control, state: bool | None = None) -> str:
    """Set a CheckBox/RadioButton to a specific state (or flip it)."""
    _require(ctrl)
    w = ctrl.wrapper
    try:
        current = bool(w.get_toggle_state())
    except Exception:
        current = False
    target = (not current) if state is None else bool(state)
    if current == target:
        return f"{ctrl.label} already {'checked' if current else 'unchecked'}"
    try:
        w.toggle()
    except Exception:
        w.click_input()
    return f"{'checked' if target else 'unchecked'} {ctrl.label}"


def set_slider(ctrl: Control, percent: float = 50.0) -> str:
    """Drag a Slider/Spinner to a percentage of its range."""
    _require(ctrl)
    percent = max(0.0, min(100.0, float(percent)))
    try:
        ctrl.wrapper.iface_range_value.SetValue(percent)
        return f"set {ctrl.label} to {percent:.0f}%"
    except Exception:
        pass
    from . import mouse

    x0, _t, x1, b = ctrl.rect
    target_x = int(x0 + (x1 - x0) * percent / 100)
    _y = (ctrl.rect[1] + b) // 2
    mouse.drag_from((x0 + x1) // 2, _y, target_x - (x0 + x1) // 2, 0,
                    source=ctrl.label)
    return f"dragged {ctrl.label} to ~{percent:.0f}%"


def expand(ctrl: Control) -> str:
    """Open a collapsed tree item, menu, or expander."""
    _require(ctrl)
    try:
        ctrl.wrapper.expand()
        return f"expanded {ctrl.label}"
    except Exception:
        return click(ctrl)


def collapse(ctrl: Control) -> str:
    _require(ctrl)
    try:
        ctrl.wrapper.collapse()
        return f"collapsed {ctrl.label}"
    except Exception:
        return click(ctrl)


def invoke(ctrl: Control) -> str:
    """Trigger the control's default action (InvokePattern)."""
    _require(ctrl)
    w = ctrl.wrapper
    for fn in (lambda: w.invoke(), lambda: w.select(), lambda: w.click_input()):
        try:
            fn()
            return f"invoked {ctrl.label}"
        except Exception:
            continue
    raise ControlNotActionableError(f"{ctrl.label} has no invokable action.")


# --------------------------------------------------------------------------
# global keyboard
# --------------------------------------------------------------------------
def hotkey(keys: str) -> str:
    """Raw pywinauto key string, e.g. '^s', '{ENTER}', '%{F4}', '^+{TAB}'."""
    if _send_keys is None:
        raise ControlNotActionableError("pywinauto is unavailable; cannot send keys.")
    keys = str(keys or "").strip()
    if not keys:
        raise ControlNotActionableError("Empty hotkey.")
    _send_keys(keys, pause=0.02)
    return f"sent keys {textutil.truncate(keys, 60)}"


def hotkey_on(ctrl: Control, keys: str) -> str:
    """Focus a control first, then send a hotkey — context-sensitive shortcuts."""
    _require(ctrl)
    try:
        ctrl.wrapper.click_input()
        time.sleep(0.08)
    except Exception:
        pass
    return hotkey(keys)


def paste(ctrl: Control | None = None) -> str:
    from . import keyboard

    return keyboard.paste_into(ctrl)


# --------------------------------------------------------------------------
# dispatch table
# --------------------------------------------------------------------------
ACTIONS = {
    "click": lambda ctrl, args: click(ctrl),
    "double_click": double_click,
    "right_click": right_click,
    "hover": hover,
    "scroll": lambda ctrl, args: scroll(ctrl, int(args or -3)),
    "drag": lambda ctrl, args: drag(ctrl, *_drag_args(args)),
    "type": type_text,
    "set_text": set_text,
    "append_text": append_text,
    "get_text": get_text,
    "select": select_combo_item,
    "toggle": toggle_checkbox,
    "set_slider": set_slider,
    "expand": expand,
    "collapse": collapse,
    "invoke": invoke,
    "hotkey": lambda ctrl, args: hotkey(args or ""),
    "paste": paste,
}

NEEDS_CONTROL = {
    name for name in ACTIONS
    if name not in {"hotkey"}
}


def _drag_args(args) -> tuple[int, int]:
    parts = str(args or "0,0").replace(" ", "").split(",")
    dx = int(float(parts[0])) if parts and parts[0] else 0
    dy = int(float(parts[1])) if len(parts) > 1 and parts[1] else 0
    return dx, dy


def execute(action: str, ctrl: Control | None, args: str = ""):
    """Run one named GUI action. Raises on anything unknown or impossible."""
    action = str(action or "").strip().lower()
    fn = ACTIONS.get(action)
    if fn is None:
        raise ValueError(
            f"Unknown action {action!r}. Valid: {sorted(ACTIONS)}"
        )
    if action in NEEDS_CONTROL and ctrl is None:
        raise ControlNotFoundError(f"Action {action!r} requires a control.")
    return fn(ctrl, args)


def valid_actions() -> list[str]:
    return sorted(ACTIONS)
