"""Screen and keyboard: capture, describe, OCR, find and click, typing.

Backs "what is on my screen", "save a screenshot" and "type hello into
Notepad". The vision side reuses `perception`, which already owns the desktop
session; the typing side reuses `keyboard`, so the guard's keystroke rules apply
unchanged.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from ..errors import ActionError
from ..logging_setup import get_logger

from . import keyboard, mouse, window

log = get_logger(__name__)


def capture(target: str = "", args: str = "") -> str:
    """Save a screenshot. target is the path, or a window title."""
    from ..perception import screen

    if target and not target.lower().endswith((".png", ".jpg", ".jpeg")):
        found = window.find(target, timeout=2.0)
        if found is None:
            return f"no window matching {target!r}"
        image = screen.capture_window(found).image
        destination = Path.home() / "Pictures" / "Screenshots" / f"{found.title[:40]}.png"
    else:
        image = screen.capture_full().image
        destination = Path(target) if target else (
            Path.home() / "Pictures" / "Screenshots" / "screenshot.png")
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(destination))
    return f"saved {image.width}x{image.height} to {destination}"


def describe(target: str = "", args: str = "") -> str:
    """An annotated inventory of what is on screen, or of one window."""
    from ..perception import vision

    if target:
        found = window.find(target, timeout=2.0)
        if found is None:
            return f"no window matching {target!r}"
        return vision.observe_window(found).describe(verbose=True)
    return vision.observe_screen().describe(verbose=True)


def read_text(target: str = "", args: str = "") -> str:
    """OCR the screen, or the window named by target."""
    from ..perception import ocr, screen, uia

    if target:
        found = window.find(target, timeout=2.0)
        if found is None:
            return f"no window matching {target!r}"
        text = uia.window_text(found)
        if text.strip():
            return text
        image = screen.capture_window(found).image
    else:
        image = screen.capture_full().image
    result = ocr.read_text(image)
    if not getattr(result, "text", "").strip():
        return ("I could not read any text on screen. OCR needs "
                "`pip install rapidocr-onnxruntime` or Tesseract.")
    return result.text


def find_and_click(target: str = "", args: str = "") -> str:
    """Click a control by its visible name. Goes through the guard like any click."""
    from ..perception import uia

    if not target:
        return "give me the name of what to click"
    found = window.find(args or "", timeout=2.0)
    if found is None:
        return f"no window matching {args!r}"
    control = uia.find_control(found, name=target, timeout=5.0)
    if control is None:
        return f"no control called {target!r} in {found.title!r}"
    rect = control.rectangle()
    mouse.click_at((rect.mid_point().x, rect.mid_point().y))
    return f"clicked {target!r} in {found.title!r}"


def wait_for(target: str = "", args: str = "") -> str:
    """Wait for a window to appear. target is the title, args the timeout."""
    timeout = float(args) if args.replace(".", "", 1).isdigit() else 10.0
    found = window.find(target, timeout=timeout)
    return f"{found.title!r} appeared" if found else f"no window {target!r} after {timeout}s"


def type_text(target: str = "", args: str = "") -> str:
    """Type into whatever has focus. Args may be 'slow' to type slowly."""
    text = args or target
    if not text:
        return "give me the text to type"
    if "slow" in f"{target} {args}".lower():
        keyboard.type_slow(text)
    else:
        keyboard.type_text(text)
    return f"typed {len(text)} character(s)"


def undo(target: str = "", args: str = "") -> str:
    keyboard.key_tap("ctrl+z")
    return "undid the last thing"


def scroll(target: str = "", args: str = "") -> str:
    """Scroll the window under the cursor. target is 'up'/'down', args the amount."""
    direction = (target or args or "down").lower()
    amount = 3
    for piece in f"{target} {args}".split():
        if piece.lstrip("-").isdigit():
            amount = int(piece)
    if "up" in direction:
        mouse.scroll_at(amount * 120)
    else:
        mouse.scroll_at(-amount * 120)
    return f"scrolled {direction} by {amount}"


# --------------------------------------------------------------------------
# GUI verbs as capabilities
#
# `actions.gui` already implements click, double-click, right-click, drag,
# scroll, drag-and-drop, type, paste, hotkeys and the rest, against a UI Automation
# control. It was reachable only from the agent loop, so "double-click the file"
# had no capability to route to and `ufo do click` did not exist.
#
# `gui_action` is the bridge: it turns a written target ("the Save button in
# Notepad") into a window plus a control, then runs the real verb. When the
# control cannot be found it says so and names what is on screen, rather than
# clicking whatever happens to be under the cursor.
# --------------------------------------------------------------------------
def gui_action(target: str = "", args: str = "") -> str:
    """Run one GUI verb against a named control. Used by every *_gui capability."""
    from ..capabilities import catalog
    from . import gui

    verb = (target or "").strip()
    if not verb:
        return f"give me an action: {', '.join(gui.valid_actions())}"
    if verb not in gui.ACTIONS:
        return f"{verb!r} is not a GUI action. Valid: {', '.join(gui.valid_actions())}"

    subject = args.strip()
    if not subject:
        return (f"say what to {verb.replace('_', ' ')} on, e.g. "
                f"{verb} 'the Save button in Notepad'")

    window_title, control_name = _split_target(subject)
    if verb == "hotkey":
        return str(gui.execute("hotkey", None, args or control_name))

    from ..perception import uia

    found = uia.find_window(window_title or ".*", timeout=3.0)
    if found is None:
        return (f"no window matching {window_title!r}. Open windows: "
                + ", ".join(w.title for w in uia.list_all_windows()))

    from ..config import active

    allow = active().safety.allowlist
    if not uia.is_allowed(found, allow):
        return (f"{found.title!r} is not in the allowlist, so I will not touch it. "
                f"Add it with: ufo config allow {found.title}")

    control = uia.find_control(found, control_name)
    if control is None and verb not in ("scroll",):
        listing = [f"{c.name} [{c.type}]" for c in uia.list_controls(found)[:12]]
        return (f"no control matching {control_name!r} in {found.title!r}. "
                f"What is there: {'; '.join(listing) or '(nothing readable)'}")

    return str(gui.execute(verb, control, args))


def _split_target(subject: str) -> tuple[str, str]:
    """"the Save button in Notepad" -> ("Notepad", "the Save button")."""
    text = subject.strip()
    m = re.search(r"(?i)\s+(?:in|inside|on)\s+(?:the\s+)?(?:window\s+)?"
                  r"([\"']?)(.{1,60}?)(?:\1)$", text)
    if m and m.group(2).strip():
        return m.group(2).strip(), text[:m.start()].strip() or text
    return "", text


# ---- one capability per verb, so each has its own risk and its own triggers ----
def click_gui(target: str = "", args: str = "") -> str:
    """Click a named control."""
    return gui_action("click", args)


def double_click_gui(target: str = "", args: str = "") -> str:
    """Double-click a named control."""
    return gui_action("double_click", args)


def right_click_gui(target: str = "", args: str = "") -> str:
    """Right-click a named control."""
    return gui_action("right_click", args)


def hover_gui(target: str = "", args: str = "") -> str:
    """Hover a named control."""
    return gui_action("hover", args)


def drag_gui(target: str = "", args: str = "") -> str:
    """Drag a control by an offset, e.g. 'the file' then '200,0'."""
    return gui_action("drag", args)


def invoke_gui(target: str = "", args: str = "") -> str:
    """Invoke a control's default action (press a button properly)."""
    return gui_action("invoke", args)


def get_text_gui(target: str = "", args: str = "") -> str:
    """Read a control's text."""
    return gui_action("get_text", args)


def set_text_gui(target: str = "", args: str = "") -> str:
    """Replace a control's text."""
    return gui_action("set_text", args)


def append_text_gui(target: str = "", args: str = "") -> str:
    """Append to a control's text."""
    return gui_action("append_text", args)


def select_combo_gui(target: str = "", args: str = "") -> str:
    """Pick an item from a combo box or list."""
    return gui_action("select", args)


def toggle_gui(target: str = "", args: str = "") -> str:
    """Tick or untick a checkbox."""
    return gui_action("toggle", args)


def slider_gui(target: str = "", args: str = "") -> str:
    """Move a slider, e.g. 'the volume' then '40'."""
    return gui_action("set_slider", args)


def expand_gui(target: str = "", args: str = "") -> str:
    """Expand a tree or disclosure control."""
    return gui_action("expand", args)


def collapse_gui(target: str = "", args: str = "") -> str:
    """Collapse a tree or disclosure control."""
    return gui_action("collapse", args)


def hotkey(target: str = "", args: str = "") -> str:
    """Press a shortcut such as ctrl+s or alt+tab."""
    keys = (args or target).strip()
    if not keys:
        return "give me the keys, e.g. hotkey ctrl+s"
    from . import gui

    return str(gui.execute("hotkey", None, keys))


def paste_gui(target: str = "", args: str = "") -> str:
    """Paste into the focused control."""
    from . import gui

    return str(gui.execute("paste", None, args))


def select_all(target: str = "", args: str = "") -> str:
    """Select everything in the focused control."""
    from . import gui

    return str(gui.execute("hotkey", None, "ctrl+a"))


def copy_gui(target: str = "", args: str = "") -> str:
    """Copy from the focused control."""
    from . import gui

    return str(gui.execute("hotkey", None, "ctrl+c"))


def annotate(target: str = "", args: str = "") -> str:
    """Save an annotated screenshot: the screen with every control numbered."""
    from ..perception import screen, vision

    destination = Path.home() / "Pictures" / "Screenshots" / "annotated.png"
    destination.parent.mkdir(parents=True, exist_ok=True)
    obs = vision.observe_screen()
    vision.save_frame(obs, destination)
    return (f"annotated {len(obs.controls)} control(s) into {destination}. "
            f"Open it with: ufo see")
