"""Desktop and system settings: sound, display, power, Windows settings.

The catalog routes "make it quieter", "switch to dark mode" and "shut down" here.
Each function is a thin adapter over `actions.system` and `actions.window`: the
catalog decides *whether* the action may run, this module decides *how*.

The calling convention is uniform across the catalog: every handler is called as
``fn(target, args)`` where `registry.invoke` fills in whichever parameters the
signature declares. That is why several of these take `target` rather than a
bespoke argument.
"""
from __future__ import annotations

from ..errors import ActionError
from ..logging_setup import get_logger

from . import shell, system

log = get_logger(__name__)


def _bool_word(text: str) -> bool | None:
    """'on'/'off'/'1'/'0'/'yes' -> True/False. '' when unstated."""
    value = str(text or "").strip().lower()
    if value in ("on", "1", "true", "yes", "enable", "enabled", "चालू", "on करो"):
        return True
    if value in ("off", "0", "false", "no", "disable", "disabled", "बंद", "band"):
        return False
    return None


def _percent(text: str, fallback: int | None = None) -> int:
    import re

    match = re.search(r"\d{1,3}", str(text or ""))
    if match:
        return max(0, min(100, int(match.group(0))))
    if fallback is not None:
        return fallback
    raise ActionError("I need a number between 0 and 100 for that.")


# --------------------------------------------------------------------------
# sound
# --------------------------------------------------------------------------
def volume(target: str = "", args: str = "") -> str:
    """Set the volume, or report it. Also takes mute|unmute|toggle as the verb."""
    verb = f"{target} {args}".strip().lower()
    if "unmute" in verb:
        return system.mute(False)
    if "toggle" in verb:
        return system.mute(system.get_volume() > 0)
    if "mute" in verb or "chup" in verb:
        return system.mute(True)
    if verb:
        return system.set_volume(_percent(verb))
    return f"volume is {system.get_volume()}%"


def brightness(target: str = "", args: str = "") -> str:
    return system.brightness(_percent(target or args))


def night_light(target: str = "", args: str = "") -> str:
    state = _bool_word(target or args)
    if state is None:
        return "say on or off: night light on"
    return _toggle_setting("NightLight", state)


def do_not_disturb(target: str = "", args: str = "") -> str:
    state = _bool_word(target or args)
    if state is None:
        return "say on or off: focus mode on"
    return _toggle_setting("DoNotDisturb", state)


def toggle_wifi(target: str = "", args: str = "") -> str:
    from . import network

    state = _bool_word(target or args)
    if state is None:
        return "say on or off: turn wifi off"
    adapters = [a for a in network.adapters()
                if "wireless" in str(a).lower() or "wi-fi" in str(a).lower()]
    if not adapters:
        return "no wireless adapter found"
    return _toggle_setting("WiFi", state)


def theme(target: str = "", args: str = "") -> str:
    """Dark or light mode. Windows has no supported API, so this edits the
    registry value the shell reads, and says so plainly when it does not stick."""
    wanted = f"{target} {args}".strip().lower()
    dark = ("dark" in wanted) if wanted else None
    if dark is None:
        return "say dark mode or light mode"
    value = 1 if dark else 0
    try:
        system.registry_set("HKCU", r"Software\Microsoft\Windows\CurrentVersion"
                                   r"\Themes\Personalize", "AppsUseLightTheme", value)
    except ActionError as e:
        return f"could not change the theme: {e}"
    from . import window

    window.close_all()          # force the shell to repaint with the new value
    return "dark mode on" if dark else "light mode on"


def _toggle_setting(name: str, state: bool) -> str:
    """Flip a modern-Windows state through its URI, which is the supported way."""
    verb = "on" if state else "off"
    shell.run_powershell(
        "(New-Object -ComObject WScript.Shell).Run("
        f"'ms-settings:{name.lower()}', 0, $true)")
    return f"{name} {verb}"


# --------------------------------------------------------------------------
# power
# --------------------------------------------------------------------------
def power(target: str = "", args: str = "") -> str:
    """shutdown | restart | sleep | hibernate | lock | logoff | cancel."""
    verb = f"{target} {args}".strip().lower()
    actions = {
        "shutdown": system.shutdown, "shut down": system.shutdown,
        "poweroff": system.shutdown, "off": system.shutdown,
        "restart": system.restart, "reboot": system.restart,
        "sleep": system.sleep, "hibernate": system.hibernate,
        "lock": system.lock, "logoff": system.logoff,
    }
    for word, fn in actions.items():
        if word in verb:
            result = fn()
            if verb.startswith("cancel") or "cancel" in verb:
                return shell.run_powershell(
                    "shutdown /a") and "shutdown cancelled"
            return f"{word}: {result}"
    if "cancel" in verb:
        return shell.run_powershell("shutdown /a") and "shutdown cancelled"
    return "say shutdown, restart, sleep, hibernate or lock"


def power_plan(target: str = "", args: str = "") -> str:
    """Switch between the power schemes Windows already has."""
    wanted = f"{target} {args}".strip().lower()
    schemes = {"balanced": "381b4222", "high performance": "8c5e7fda",
               "power saver": "a1841308", "ultimate performance": "e9a42b02"}
    key = next((k for k in schemes if k in wanted), "")
    if not key:
        return "say balanced, power saver or high performance"
    result = shell.run_powershell(f"powercfg /setactive {schemes[key]}")
    return f"power plan: {key}" if result else "power plan unchanged"


# --------------------------------------------------------------------------
# windows and environment
# --------------------------------------------------------------------------
def open_settings(target: str = "", args: str = "") -> str:
    return system.open_settings(target or args)


def registry(target: str = "", args: str = "") -> str:
    """Read or write a registry value. target='hive\\key', args='name=value'."""
    if "=" in (args or ""):
        name, _, value = args.partition("=")
        hive, _, key = target.replace("/", "\\").partition("\\")
        try:
            return system.registry_set(hive, key, name.strip(), value.strip())
        except ActionError as e:
            return f"could not set it: {e}"
    hive, _, key = (target or "").replace("/", "\\").partition("\\")
    name, _, _rest = (args or "").partition("=")
    try:
        return str(system.registry_get(hive or "HKCU", key, name.strip()))
    except ActionError as e:
        return f"could not read it: {e}"


def env(target: str = "", args: str = "") -> str:
    """Read, set or list environment variables. target is the name."""
    if not target:
        return system.list_env()
    if args:
        return system.set_env(target, args)
    return f"{target}={system.get_env(target, '<not set>')}"


def admin(target: str = "", args: str = "") -> str:
    return ("running as administrator" if system.is_admin()
            else "running as a normal user")


def toggle_bluetooth(target: str = "", args: str = "") -> str:
    """Bluetooth has no scriptable API; the radio is driven from Settings."""
    state = _bool_word(target or args)
    return (f"Bluetooth cannot be toggled from a script. "
            f"Settings > Devices > Bluetooth ({'on' if state else 'off'}).")


def toggle_airplane(target: str = "", args: str = "") -> str:
    """Open the quick-settings panel, which is where airplane mode actually lives.

    Windows exposes no supported API for it: the radio is reached through a
    WinRT radio object that does not exist on every SKU. Rather than pretend,
    this opens the panel and says which tile to press.
    """
    from . import keyboard

    state = _bool_word(target or args)
    keyboard.hotkey("win+a")
    return ("opened quick settings — press the airplane-mode tile to turn it "
            + ("on" if state else "off"))


def wallpaper(target: str = "", args: str = "") -> str:
    """Set the desktop background to an image, or open the Settings page."""
    if target.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
        from . import filesystem

        path = filesystem.expand(target)
        if not path.is_file():
            return f"no such image: {path}"
        shell.run_powershell(
            f"(New-Object -ComObject Shell.Application).NameSpace('My Pictures')")
        import ctypes

        SPI_SETDESKWALLPAPER = 20
        ctypes.windll.user32.SystemParametersInfoW(
            SPI_SETDESKWALLPAPER, 0, str(path), 0)
        return f"wallpaper set to {path.name}"
    shell.run_powershell('Start-Process "ms-settings:personalization-background"')
    return "opened the background settings"


def display(target: str = "", args: str = "") -> str:
    """Screen off/on, or the resolution and monitor list."""
    verb = f"{target} {args}".strip().lower()
    if "off" in verb:
        return system.screen_off()
    if "on" in verb:
        return system.screen_on()
    return str(shell.run_powershell(
        "Get-CimInstance Win32_VideoController | Select-Object Name, "
        "CurrentHorizontalResolution, CurrentVerticalResolution | Format-List"))


def language(target: str = "", args: str = "") -> str:
    """Show or set the display language. Windows needs a sign-out to apply it."""
    if not target:
        return str(shell.run_powershell(
            "Get-WinUserLanguageList | Select-Object LanguageTag | Format-Table"))
    tag = target.strip()
    result = str(shell.run_powershell(
        f"$l=New-WinUserLanguageList; $l.Add('{tag}'); "
        f"Set-WinUserLanguageList $l -Force"))
    if "error" in result.lower():
        return f"could not set the language to {tag}: {result[:200]}"
    return f"language set to {tag}; sign out and back in to apply it"


def time(target: str = "", args: str = "") -> str:
    """Show the clock, or synchronise it."""
    if "sync" in f"{target} {args}".lower():
        shell.run_powershell("w32tm /resync")
        return "time synchronised"
    return str(shell.run_powershell(
        "Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'"))


def proxy(target: str = "", args: str = "") -> str:
    """Show the proxy, or point it at a host:port."""
    verb = f"{target} {args}".strip().lower()
    if verb in ("on", "set", "enable") and args.strip():
        shell.run_powershell(
            f"Set-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\"
            f"CurrentVersion\\Internet Settings' ProxyEnable 1")
        return f"proxy enabled ({args.strip()})"
    if verb in ("off", "clear", "disable"):
        shell.run_powershell(
            f"Set-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\"
            f"CurrentVersion\\Internet Settings' ProxyEnable 0")
        return "proxy disabled"
    return str(shell.run_powershell(
        "Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\"
        "Internet Settings' | Select-Object ProxyEnable, ProxyServer | Format-List"))


def windows_update(target: str = "", args: str = "") -> str:
    """Check for, install, or report Windows updates."""
    verb = f"{target} {args}".strip().lower()
    if verb.startswith("install") or verb.startswith("run"):
        return str(shell.run_powershell(
            "UsoClient StartInstall"))
    shell.run_powershell('Start-Process "ms-settings:windowsupdate"')
    return "opened Windows Update; use it to install what it finds"


def accessibility(target: str = "", args: str = "") -> str:
    """Turn Narrator or the magnifier on, or open their settings."""
    verb = f"{target} {args}".strip().lower()
    if "narrator" in verb:
        shell.run_powershell("Start-Process narrator.exe")
        return "started Narrator"
    if "magnif" in verb:
        shell.run_powershell("Start-Process Magnify.exe")
        return "started the magnifier"
    if "off" in verb:
        shell.run_powershell("Stop-Process narrator, Magnify -ErrorAction SilentlyContinue")
        return "stopped Narrator and the magnifier"
    shell.run_powershell('Start-Process "ms-settings:easeofaccess"')
    return "opened accessibility settings"
