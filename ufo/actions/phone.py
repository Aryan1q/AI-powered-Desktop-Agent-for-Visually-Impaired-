"""Phone and device link: messages, notifications, casting, handoff.

Windows exposes a phone through the Phone Link app and a few broadcast
receivers. Where there is no API, the handler opens the app and says which
control to use, rather than claiming a control it cannot reach.
"""
from __future__ import annotations

from ..errors import ActionError
from ..logging_setup import get_logger

from . import shell

log = get_logger(__name__)


def _phone_link() -> str:
    return str(shell.run_powershell(
        "Get-AppxPackage *YourPhone* | Select-Object Name, Version | Format-Table"))


def open_app(target: str = "", args: str = "") -> str:
    shell.run_powershell('Start-Process "ms-phone-link:"')
    return "opened Phone Link"


def notifications(target: str = "", args: str = "") -> str:
    """Show this machine's notifications, or the phone's."""
    if "phone" in f"{target} {args}".lower():
        shell.run_powershell('Start-Process "ms-phone-link:"')
        return "opened Phone Link — its notifications are on the phone screen"
    return str(shell.run_powershell(
        "Get-CimInstance -Namespace root/notificationconfig -ClassName "
        "MSFT_UserNotificationSettings -ErrorAction SilentlyContinue | "
        "Select-Object App, Enabled | Format-Table -AutoSize"))


def reply(target: str = "", args: str = "") -> str:
    """Reply to the latest message on the linked phone."""
    shell.run_powershell('Start-Process "ms-phone-link:"')
    return ("opened Phone Link. Replying happens on the phone, so the message "
            "keeps its real sender.")


def send_file(target: str = "", args: str = "") -> str:
    """Share a file to the phone through the Nearby Share prompt."""
    from . import filesystem

    path = filesystem.expand(target)
    if not path.is_file():
        return f"no such file: {path}"
    shell.run_powershell(f'Start-Process "{path}"')
    return (f"opened {path.name}. Choose Nearby Share in the share sheet to "
            f"send it to your phone.")


def cast(target: str = "", args: str = "") -> str:
    """Open the cast panel."""
    from . import keyboard

    keyboard.hotkey("win+k")
    return "opened the cast panel (Win+K)"


def clipboard(target: str = "", args: str = "") -> str:
    """Sync the clipboard with the phone, if Phone Link supports it."""
    return ("Phone Link syncs the clipboard only while it is running. "
            "Open it from the taskbar to enable that.")


def find(target: str = "", args: str = "") -> str:
    """Ring the phone, so a lost one can be located."""
    shell.run_powershell('Start-Process "ms-phone-link:"')
    return ("opened Phone Link — use Find My Device on the phone itself, which "
            "works even without the PC")


def hotspot(target: str = "", args: str = "") -> str:
    return ("the hotspot is a quick-settings tile. Press Win+A, then the "
            "mobile hotspot tile.")


def contacts(target: str = "", args: str = "") -> str:
    """Contacts live in the phone's account, not on this machine."""
    return str(shell.run_powershell(
        "Get-ChildItem \"$env:APPDATA\\Microsoft\\Contacts\" -ErrorAction "
        "SilentlyContinue | Select-Object Name | Format-Table"))