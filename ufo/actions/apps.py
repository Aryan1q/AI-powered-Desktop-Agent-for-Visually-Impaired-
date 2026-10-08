"""Apps, window management and Windows software.

Backs "list installed apps", "open notepad", "what windows are open" and the
startup-entry actions. Installing and uninstalling go through winget when it is
there, and say how to get it when it is not — they never reach for a download.
"""
from __future__ import annotations

import json
import re

from ..errors import ActionError
from ..logging_setup import get_logger

from . import filesystem, gui, shell, system, window

log = get_logger(__name__)


def _on_path(name: str) -> bool:
    """`shell.which` raises when the tool is absent; a probe is what callers want."""
    import shutil

    return bool(shutil.which(name))


# --------------------------------------------------------------------------
# software inventory
# --------------------------------------------------------------------------
def list_installed(target: str = "", args: str = "") -> str:
    """Installed programs, newest first. `target` filters by name."""
    rows = _winget_list()
    if not rows:
        rows = _registry_programs()
    if not rows:
        return ("could not read the installed-programs list. "
                "`winget list` runs nothing if winget is missing.")
    if target:
        needle = target.lower()
        rows = [r for r in rows if needle in r[0].lower()]
        if not rows:
            return f"nothing installed matching {target!r}"
    return "\n".join(f"{name}  {version}".rstrip() for name, version in rows[:200])


def _winget_list() -> list[tuple[str, str]]:
    if not _on_path("winget"):
        return []
    result = shell.run_powershell(
        "winget list --accept-source-agreements --disable-interactivity")
    rows = []
    for line in str(result).splitlines()[2:]:
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() != "name":
            rows.append((parts[0], parts[1]))
    return rows


def _registry_programs() -> list[tuple[str, str]]:
    import winreg  # type: ignore

    paths = [(winreg.HKEY_LOCAL_MACHINE,
              r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE,
              r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_CURRENT_USER,
              r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")]
    found: dict[str, str] = {}
    for hive, path in paths:
        try:
            with winreg.OpenKey(hive, path) as key:
                index = 0
                while True:
                    try:
                        sub = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(key, sub) as entry:
                            name = winreg.QueryValueEx(entry, "DisplayName")[0]
                            try:
                                version = winreg.QueryValueEx(entry, "DisplayVersion")[0]
                            except OSError:
                                version = ""
                    except OSError:
                        continue
                    if name:
                        found[name] = str(version)
        except OSError:
            continue
    return sorted(found.items(), key=lambda row: row[0].lower())


def open_app(target: str = "", args: str = "") -> str:
    if not target:
        return "give me the name of an app"
    if shell.launch(target, wait=2.0):
        return f"launched {target}"
    return f"could not launch {target!r} — is the name an executable?"


def install(target: str = "", args: str = "") -> str:
    """Install through winget. Never downloads from a URL the model invented."""
    name = target or args
    if not name:
        return "give me the app name"
    if not _on_path("winget"):
        raise ActionError(
            "winget is not installed, so I cannot install anything safely. "
            "Install App Installer from the Microsoft Store, or get it from "
            "https://aka.ms/getwinget")
    return str(shell.run_powershell(
        f'winget install --id "{name}" --accept-package-agreements '
        f'--accept-source-agreements --disable-interactivity'))


def uninstall(target: str = "", args: str = "") -> str:
    name = target or args
    if not name:
        return "give me the app name"
    if not _on_path("winget"):
        raise ActionError("winget is not installed, so I cannot uninstall anything.")
    return str(shell.run_powershell(
        f'winget uninstall --id "{name}" --accept-source-agreements '
        f'--disable-interactivity'))


def update(target: str = "", args: str = "") -> str:
    name = target or args
    if not _on_path("winget"):
        raise ActionError("winget is not installed, so I cannot check for updates.")
    scope = f'--id "{name}" ' if name else "--all "
    return str(shell.run_powershell(
        f"winget upgrade {scope}--accept-source-agreements "
        f"--accept-package-agreements --disable-interactivity"))


def update_all(target: str = "", args: str = "") -> str:
    return update("", "")


def repair(target: str = "", args: str = "") -> str:
    """Run the built-in system repair checks."""
    if not _on_path("sfc"):
        return "System File Checker is not available here"
    return str(shell.run_powershell("sfc /scannow"))


# --------------------------------------------------------------------------
# startup entries
# --------------------------------------------------------------------------
def add_startup(target: str = "", args: str = "") -> str:
    name, command = _name_and_command(target, args)
    if not name or not command:
        return 'give me "name" and the command to run'
    return system.add_startup(name, command)


def remove_startup(target: str = "", args: str = "") -> str:
    name = target or args
    if not name:
        return "give me the startup entry name"
    return system.remove_startup(name)


def startup_report(target: str = "", args: str = "") -> str:
    from . import process

    items = process.startup_items()
    if not items:
        return "no startup entries found"
    return "\n".join(str(item) for item in items)


def _name_and_command(target: str, args: str) -> tuple[str, str]:
    if "|" in f"{target} {args}":
        name, _, command = f"{target} {args}".partition("|")
        return name.strip(), command.strip()
    return (target or "").strip(), (args or "").strip()


def toggle_startup(target: str = "", args: str = "") -> str:
    name = target or args
    if not name:
        return "give me the startup entry name"
    return str(shell.run_powershell(
        f"$k='HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run';"
        f"$v=(Get-ItemProperty $k).'{name}';"
        f"if($v){{Set-ItemProperty $k -Name '{name}' -Value '';"
        f"\"disabled: $name\"}}else{{Set-ItemProperty $k -Name '{name}' "
        f"-Value 'cmd /c exit';\"enabled: $name\"}}"))


def bloatware(target: str = "", args: str = "") -> str:
    """List the preinstalled apps Windows lets you uninstall."""
    if not _on_path("winget"):
        return "winget is not installed, so I cannot list Appx packages."
    return str(shell.run_powershell(
        "Get-AppxPackage | Where-Object {$_.NonRemovable -eq $false} "
        "| Select-Object Name, Version | Format-Table -AutoSize"))


# --------------------------------------------------------------------------
# defaults and fonts
# --------------------------------------------------------------------------
def set_default_app(target: str = "", args: str = "") -> str:
    kind = (target or "").strip().lower()
    value = (args or "").strip()
    if kind in ("browser",):
        shell.run_powershell(
            f'Start-Process "ms-settings:defaultapps?registeredappuser={value}"')
        return f"opening the default-app settings for {value}"
    shell.run_powershell(
        f'Start-Process "ms-settings:defaultapps"')
    return "opened Default apps; pick the program there"


def list_default_apps(target: str = "", args: str = "") -> str:
    return str(shell.run_powershell(
        "Get-ItemProperty HKCU:\\Software\\Microsoft\\Windows\\Shell\\"
        "Associations\\UrlAssociations\\http\\UserChoice | "
        "Select-Object ProgId"))


def fonts(target: str = "", args: str = "") -> str:
    return str(shell.run_powershell(
        "(New-Object -ComObject Shell.Application).Namespace(0x14).Items() "
        "| Select-Object -ExpandProperty Name"))


def browser_extensions(target: str = "", args: str = "") -> str:
    """Extensions live inside the browser profile, so this opens it."""
    name = target or args.lower()
    profile = {"chrome": "chrome://extensions", "edge": "edge://extensions",
               "firefox": "about:addons"}.get(name)
    if profile is None:
        return "say chrome, edge or firefox"
    shell.open_url(profile)
    return f"opened {profile}"


def run_as_admin(target: str = "", args: str = "") -> str:
    command = target or args
    if not command:
        return "give me the program to run"
    shell.run_powershell(
        f'Start-Process -FilePath "{command}" -Verb RunAs')
    return f"{command} asked for administrator rights"


def pin_taskbar(target: str = "", args: str = "") -> str:
    """Pinning is not scriptable on Windows 10+; say so rather than pretend."""
    name = target or args
    return (f"Windows does not allow pinning from a script. "
            f"Open Start, find {name}, right-click and choose Pin to taskbar.")


def unpin_taskbar(target: str = "", args: str = "") -> str:
    name = target or args
    return (f"Windows does not allow unpinning from a script. "
            f"Right-click the {name} icon and choose Unpin from taskbar.")


# --------------------------------------------------------------------------
# windows
# --------------------------------------------------------------------------
def manage_window(target: str = "", args: str = "") -> str:
    """focus|maximize|minimize|restore|close|move|resize a window by title."""
    action, _, title = f"{target} {args}".strip().lower().partition(" ")
    verbs = {
        "focus": lambda t: window.focus(t), "activate": lambda t: window.focus(t),
        "maximize": lambda t: window.maximize(t),
        "minimize": lambda t: window.minimize(t),
        "restore": lambda t: window.restore(t),
        "close": lambda t: window.close(t),
        "switch": lambda t: window.switch_to(t),
    }
    fn = verbs.get(action)
    if fn is None:
        return "say focus, maximize, minimize, restore, close or switch, then the title"
    return str(fn(title.strip()))


def virtual_desktops(target: str = "", args: str = "") -> str:
    """Windows exposes virtual desktops only through a keyboard gesture."""
    verb = f"{target} {args}".strip().lower()
    if "new" in verb:
        from . import keyboard

        keyboard.hotkey("ctrl+win+d")
        return "opened a new virtual desktop"
    if any(w in verb for w in ("left", "back", "previous")):
        from . import keyboard

        keyboard.hotkey("ctrl+win+left")
        return "moved to the previous desktop"
    if any(w in verb for w in ("right", "next", "forward")):
        from . import keyboard

        keyboard.hotkey("ctrl+win+right")
        return "moved to the next desktop"
    return "use Win+Ctrl+D for a new desktop, Win+Ctrl+Left/Right to switch"
