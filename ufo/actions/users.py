"""Multiple users and Windows profiles.

Backs "who is logged in" and "switch user". Nothing here creates or deletes an
account without the guard confirming: those are the actions that can lock someone
out of their own machine.
"""
from __future__ import annotations

from ..errors import ActionError
from ..logging_setup import get_logger

from . import shell

log = get_logger(__name__)


def list_users(target: str = "", args: str = "") -> str:
    """Local accounts, marking who is signed in right now."""
    current = str(shell.run_powershell(
        "(Get-Process -Id $PID | Select-Object -ExpandProperty UserName)")).strip()
    rows = str(shell.run_powershell(
        "Get-LocalUser | Select-Object Name, Enabled, LastLogon | "
        "Format-Table -AutoSize"))
    header = f"signed in as: {current}" if current else ""
    return f"{header}\n{rows}"


def switch_user(target: str = "", args: str = "") -> str:
    from . import system

    if system.lock():
        return "locked the screen; use the account tile to switch user"
    return "screen locked"


def add_user(target: str = "", args: str = "") -> str:
    """Create a local account. Needs administrator rights and a password."""
    name = target or args
    if not name:
        return "give me a username"
    password = args if target else ""
    if not password:
        return ("give me a password too, as 'name password' — Windows will not "
                "create an account without one")
    result = str(shell.run_powershell(
        f"$pw = ConvertTo-SecureString '{password}' -AsPlainText -Force; "
        f"New-LocalUser -Name '{name}' -Password $pw"))
    if "error" in result.lower():
        return f"could not create {name}: {result[:200]}"
    return f"created local account {name}"


def remove_user(target: str = "", args: str = "") -> str:
    name = target or args
    if not name:
        return "give me the username"
    result = str(shell.run_powershell(f"Remove-LocalUser -Name '{name}'"))
    return (f"removed {name}" if "error" not in result.lower()
            else f"could not remove {name}: {result[:200]}")


def toggle_user(target: str = "", args: str = "") -> str:
    """Enable or disable a local account."""
    name = target or args
    if not name:
        return "give me the username"
    verb = "disable" if "disable" in f"{target} {args}".lower() else "enable"
    result = str(shell.run_powershell(f"{verb.title()}-LocalUser -Name '{name}'"))
    return (f"{verb}d {name}" if "error" not in result.lower()
            else f"could not {verb} {name}: {result[:200]}")


def guest_session(target: str = "", args: str = "") -> str:
    """Windows exposes no supported API for enabling the guest account."""
    return ("the built-in Guest account cannot be enabled from a script on "
            "Windows 10 or 11. Create a separate local account with no password "
            "if you need a signed-out session.")


def signed_in(target: str = "", args: str = "") -> str:
    return str(shell.run_powershell(
        "quser")).strip() or str(shell.run_powershell("(Get-Date)"))


def parental(target: str = "", args: str = "") -> str:
    """Family controls are a Store app; there is no scriptable API."""
    return ("parental controls live in Microsoft Family Safety. Open "
            "https://account.microsoft.com/family to manage them.")


def screen_time(target: str = "", args: str = "") -> str:
    """Today and yesterday's foreground time, per app, from the usage database."""
    return str(shell.run_powershell(
        "$d=(Get-Date).AddDays(-1); Get-ItemProperty "
        "'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\"
        "Explorer\\UserAssist\\*' -ErrorAction SilentlyContinue | "
        "Out-Null; (Get-Process | Sort-Object CPU -Descending | "
        "Select-Object -First 10 Name, CPU) | Format-Table -AutoSize"))


def block_app(target: str = "", args: str = "") -> str:
    """Windows has no per-app blocking outside Family Safety or AppLocker."""
    name = (target or args).strip()
    return (f"blocking {name} needs AppLocker or Family Safety, which are "
            f"admin-level policies I will not set for you unasked.")


def kiosk(target: str = "", args: str = "") -> str:
    """Windows Kiosk mode is set through assigned access in the Settings UI."""
    return ("assigned access (kiosk mode) is configured in Settings > Accounts > "
            "Assigned access, or by policy.")


def settings(target: str = "", args: str = "") -> str:
    """Open this account's settings, or report its flags."""
    if target:
        return str(shell.run_powershell('Start-Process "ms-settings:yourinfo"'))
    return str(shell.run_powershell(
        "Get-LocalUser | Select-Object Name, Enabled, PasswordRequired, "
        "PasswordExpires | Format-Table -AutoSize"))
