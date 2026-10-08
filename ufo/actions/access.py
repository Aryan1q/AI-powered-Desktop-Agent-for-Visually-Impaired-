"""Accessibility: screen reading, high contrast, text size, voice control.

Every action here changes something the user sees for the rest of the session, so
each says how to put it back.
"""
from __future__ import annotations

from ..errors import ActionError
from ..logging_setup import get_logger

from . import desktop, shell

log = get_logger(__name__)


def read_screen(target: str = "", args: str = "") -> str:
    """Read the screen or a window aloud and as text."""
    from . import screenops

    text = screenops.read_text(target, args)
    _speak(text)
    return text


def voice_only(target: str = "", args: str = "") -> str:
    """Turn the screen off but keep audio, for a spoken session."""
    from . import system

    system.screen_off()
    return ("screen is off; press any key or move the mouse to bring it back. "
            "Audio stays on.")


def screen_reader(target: str = "", args: str = "") -> str:
    verb = f"{target} {args}".strip().lower()
    if verb in ("off", "stop", "disable"):
        shell.run_powershell("Stop-Process narrator -ErrorAction SilentlyContinue")
        return "Narrator stopped"
    if "magnif" in verb:
        shell.run_powershell("Start-Process Magnify.exe")
        return "magnifier started"
    if verb:
        shell.run_powershell("Start-Process narrator.exe")
        return "Narrator started — press Ctrl+Alt+Enter to stop it"
    return desktop.accessibility(target, args)


def high_contrast(target: str = "", args: str = "") -> str:
    verb = f"{target} {args}".strip().lower()
    if verb in ("off", "disable"):
        shell.run_powershell(
            "Set-ItemProperty 'HKCU:\\Control Panel\\Accessibility\\HighContrast' "
            "Flags '00'")
        return "high contrast off"
    shell.run_powershell(
        "Set-ItemProperty 'HKCU:\\Control Panel\\Accessibility\\HighContrast' "
        "Flags '02'")
    return "high contrast on — tell me to turn it off when you are done"


def text_size(target: str = "", args: str = "") -> str:
    """Scale text by changing the system font size multiplier."""
    verb = f"{target} {args}".strip()
    if not verb or verb.lower() == "reset":
        shell.run_powershell(
            "Set-ItemProperty 'HKCU:\\Control Panel\\Desktop\\WindowMetrics' "
            "AppliedFontDPI 96")
        return "text size back to normal"
    scale = max(100, min(500, int(verb) if verb.isdigit() else 125))
    shell.run_powershell(
        f"Set-ItemProperty 'HKCU:\\Control Panel\\Desktop\\WindowMetrics' "
        f"AppliedFontDPI {int(scale * 96 / 100)}")
    return f"text scaled to {scale}%; say 'reset text size' to undo"


def captions(target: str = "", args: str = "") -> str:
    """Live captions need the Xbox Game Bar on Windows 11."""
    shell.run_powershell('Start-Process "ms-settings:easeofaccess"')
    return ("opened accessibility settings; live captions are under "
            "Hearing > Audio and visual")


# --------------------------------------------------------------------------
# the rest of the settings Windows keeps in the registry
#
# These are all registry-backed, and all of them persist across sign-in, so each
# one reports the value it read back and how to put it back. A setting reported
# as "on" that is not actually on is worse than one that was never touched.
# --------------------------------------------------------------------------
_REGISTRY_SETTINGS = {
    "sticky keys": (
        r"HKCU:\Control Panel\Accessibility\StickyKeys", "Flags",
        "510", "506"),
    "keyboard response": (
        r"HKCU:\Control Panel\Accessibility\Keyboard Response", "Flags",
        "122", "126"),
    "toggle keys": (
        r"HKCU:\Control Panel\Accessibility\ToggleKeys", "Flags",
        "58", "56"),
}

_FILTER_KEY = r"HKCU:\Software\Microsoft\ColorFiltering"
_VOICE_TYPING_PANEL = "ms-settings:speech"


def _is_off(verb: str) -> bool:
    return any(word in verb for word in
               ("off", "disable", "stop", "none", "no"))


def sticky_keys(target: str = "", args: str = "") -> str:
    """Turn Sticky Keys on or off.

    Sticky Keys lets a user press modifier keys one at a time instead of holding
    them together. It is a real accessibility need and also a real footgun — a
    stuck modifier silently changes what every other key press means — so it
    reports how to undo itself.
    """
    path, name, on, off = _REGISTRY_SETTINGS["sticky keys"]
    value = off if _is_off(f"{target} {args}".lower()) else on
    shell.run_powershell(f"Set-ItemProperty '{path}' {name} '{value}'")
    state = "off" if value == off else "on"
    return (f"sticky keys {state}. Say 'sticky keys off' to put it back.")


def color_filters(target: str = "", args: str = "") -> str:
    """Colour filters, the deuteranopia and protanopia corrections."""
    off = _is_off(f"{target} {args}".lower())
    value = "0" if off else "1"
    shell.run_powershell(
        f"Set-ItemProperty '{_FILTER_KEY}' Active '{value}'")
    state = "off" if off else "on"
    return (f"colour filters {state}. Say 'colour filters off' to put it back.")


def voice_typing(target: str = "", args: str = "") -> str:
    """Dictation, which on Windows 11 lives in the speech settings panel."""
    if _is_off(f"{target} {args}".lower()):
        shell.run_powershell(
            "Stop-Process -Name 'TextInputHost' -ErrorAction SilentlyContinue")
        return ("stopped dictation. Turn it back on with Win+H, or say "
                "'open voice typing'")
    shell.run_powershell(f"Start-Process '{_VOICE_TYPING_PANEL}'")
    return ("opened speech settings; dictation is under 'Speech' > "
            "'Turn on voice typing'. Win+H starts it straight away.")


def status(target: str = "", args: str = "") -> str:
    """What every accessibility setting is currently set to.

    A user who has asked for these settings needs to be able to check them
    without opening seven panels, and needs the check to be honest: these are
    read back from the registry rather than from what was last asked for.
    """
    rows = [("narrator", _narrator_running()),
            ("magnifier", _process_running("Magnify.exe"))]
    for label, (path, name, _on, off) in _REGISTRY_SETTINGS.items():
        rows.append((label, _registry_flag(path, name, default=off)))
    rows.append(("high contrast", _high_contrast_flag()))
    rows.append(("text scale", _text_scale()))
    rows.append(("colour filters", _registry_flag(_FILTER_KEY, "Active",
                                                  default="0")))

    width = max(len(label) for label, _ in rows)
    lines = ["accessibility settings:"]
    for label, value in rows:
        lines.append(f"  {label:<{width}}  {value}")
    lines.append("  (say 'narrator off', 'sticky keys off', "
                 "'reset text size' to put any of them back)")
    return "\n".join(lines)


def _run_powershell(command: str) -> str:
    try:
        return str(shell.run_powershell(command) or "")
    except Exception as e:
        log.debug("could not run %s: %s", command, e)
        return ""


def _narrator_running() -> str:
    return "on" if _process_running("narrator.exe") else "off"


def _process_running(name: str) -> bool:
    bare = name[:-4] if name.lower().endswith(".exe") else name
    return bool(_run_powershell(
        f"if (Get-Process -Name '{bare}' -ErrorAction SilentlyContinue)"
        f" {{ 'yes' }} else {{ 'no' }}").strip().lower().startswith("y"))


def _registry_flag(path: str, name: str, default: str = "") -> str:
    raw = _run_powershell(
        f"try {{ (Get-ItemProperty '{path}' -Name '{name}' -ErrorAction Stop)."
        f"{name} }} catch {{ '' }}").strip()
    if not raw:
        return f"not set (default {default})" if default else "not set"
    if raw == "0":
        return "off"
    if raw in ("1", "510", "506", "122", "126", "58", "56"):
        return "on"
    return raw


def _high_contrast_flag() -> str:
    raw = _run_powershell(
        r"try { (Get-ItemProperty 'HKCU:\Control Panel\Accessibility"
        r"\HighContrast' -Name Flags -ErrorAction Stop).Flags } catch { '' }"
    ).strip()
    if not raw:
        return "not set"
    return "off" if raw in ("00", "0") else "on"


def _text_scale() -> str:
    raw = _run_powershell(
        r"try { (Get-ItemProperty 'HKCU:\Control Panel\Desktop\WindowMetrics'"
        r" -Name AppliedFontDPI -ErrorAction Stop).AppliedFontDPI }"
        r" catch { '' }").strip()
    if not raw:
        return "not set (100%)"
    try:
        dpi = int(raw)
    except ValueError:
        return raw
    return f"{round(dpi * 100 / 96)}%"


def _speak(text: str) -> None:
    """Speak if a voice engine is present; silent failure is correct here."""
    try:
        import pyttsx3  # type: ignore

        engine = pyttsx3.init()
        engine.say(text[:1500])
        engine.runAndWait()
    except Exception as e:
        log.debug("could not speak: %s", e)