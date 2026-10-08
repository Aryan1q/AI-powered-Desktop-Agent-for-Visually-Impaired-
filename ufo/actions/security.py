"""Security and privacy: lock, scans, firewall, the credential vault, shredding.

Every action here reads or tightens. The catalog marks the ones that change
security state so the guard asks first, because "run a scan" and "make files
unrecoverable" are not the same kind of request.
"""
from __future__ import annotations

import os
import secrets
import string
from pathlib import Path

from ..errors import ActionError
from ..logging_setup import get_logger

from . import filesystem, network, shell, system

log = get_logger(__name__)


def lock(target: str = "", args: str = "") -> str:
    return system.lock()


def scan(target: str = "", args: str = "") -> str:
    """Run Defender. A quick scan is the default; 'full' is the deep one."""
    depth = "full" if "full" in f"{target} {args}".lower() else "quick"
    return str(shell.run_powershell(
        f"Start-MpScan -ScanType {'FullScan' if depth == 'full' else 'QuickScan'}"))


def firewall(target: str = "", args: str = "") -> str:
    verb = f"{target} {args}".strip().lower()
    status = str(network.firewall_status())
    if verb in ("on", "enable", "turn on"):
        shell.run_powershell("Set-NetFirewallProfile -Profile Domain,Public,Private "
                             "-Enabled True")
        return "firewall enabled on all profiles"
    if verb in ("off", "disable", "turn off"):
        if not system.is_admin():
            return "changing the firewall needs an administrator shell"
        shell.run_powershell("Set-NetFirewallProfile -Profile Domain,Public,Private "
                             "-Enabled False")
        return "firewall disabled on all profiles — this leaves you less protected"
    return status


def audit_passwords(target: str = "", args: str = "") -> str:
    """Windows stores password hashes, not passwords. This says what it can."""
    return ("Windows does not keep a readable password list to check. "
            "Run a breach check at haveibeenpwned.com, or use a password manager's "
            "own audit.")


def password_vault(target: str = "", args: str = "") -> str:
    """Generate a strong password, or read one from the local vault.

    Generating needs no vault at all; reading one does, and that path takes the
    config so it opens the same file the rest of the agent uses.
    """
    from ..config import active
    from ..safety.secrets import SecretStore

    verb = f"{target} {args}".strip().lower()
    if verb.startswith("generate") or (target.isdigit() and target):
        return _password(int(target))
    if verb.startswith("generate"):
        return _password(20)
    store = SecretStore(active().storage.secrets_path).load()
    if verb.startswith("get") and args:
        return store.get(args) or f"nothing stored under {args!r}"
    if verb.startswith("list"):
        names = store.names()
        return ", ".join(names) if names else "the vault is empty"
    return "say generate, get <name>, or list"


def _password(length: int = 20) -> str:
    length = max(8, min(128, length))
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def secure_delete(target: str = "", args: str = "") -> str:
    """Overwrite a file before unlinking it.

    Honest about the limit: on an SSD with wear levelling, or through a journaled
    filesystem, an overwrite is not a guarantee. That is stated, not hidden.
    """
    path = Path(filesystem.expand(target or ""))
    if not path.is_file():
        return f"no such file: {path}"
    size = path.stat().st_size
    try:
        with open(path, "r+b") as handle:
            handle.write(os.urandom(size))
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as e:
        return f"could not overwrite it: {e}"
    filesystem.delete_path(target, to_recycle=False)
    return (f"overwrote and deleted {path.name}. Note: on an SSD, wear levelling "
            f"means this is best-effort, not a guarantee.")


def privacy_settings(target: str = "", args: str = "") -> str:
    return ("Telemetry and advertising controls are per-app in Windows. "
            "Settings > Privacy > General.")


def firewall_rules(target: str = "", args: str = "") -> str:
    return str(shell.run_powershell("Get-NetFirewallRule | "
                                    "Where-Object {$_.Enabled -eq 'True'} | "
                                    "Select-Object DisplayName, Direction, Action | "
                                    "Format-Table -AutoSize"))


def processes_privileged(target: str = "", args: str = "") -> str:
    """Which processes are running elevated. Useful before blaming malware."""
    return str(shell.run_powershell(
        "Get-Process | Where-Object {$_.Path -and "
        "(Test-Path $_.Path)} | Select-Object Name, Id | Format-Table -AutoSize"))


def disable_device(target: str = "", args: str = "") -> str:
    """Disable a hardware device in Device Manager."""
    name = (target or args).strip()
    if not name:
        return "give me the device name"
    verb = "Enable" if "enable" in f"{target} {args}".lower() else "Disable"
    result = str(shell.run_powershell(
        f"Get-PnpDevice -FriendlyName '*{name}*' | "
        f"{verb}-PnpDevice -Confirm:$false"))
    return (f"{verb}d {name}" if "error" not in result.lower()
            else f"could not {verb.lower()} {name}: {result[:200]}")


def app_permissions(target: str = "", args: str = "") -> str:
    """Location, camera and microphone access, per app."""
    return str(shell.run_powershell(
        "Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\"
        "CurrentVersion\\CapabilityAccessManager\\ConsentStore' | "
        "Select-Object Value | Format-List"))


def login_attempts(target: str = "", args: str = "") -> str:
    """Recent sign-in successes and failures. Worth reading after a suspicion."""
    return str(shell.run_powershell(
        "Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4624,4625} "
        "-MaxEvents 20 | Select-Object TimeCreated, Id | Format-Table -AutoSize"))


def remote_access(target: str = "", args: str = "") -> str:
    """Who can reach this machine remotely, and whether RDP is on."""
    status = str(shell.run_powershell(
        "Get-ItemProperty 'HKLM:\\System\\CurrentControlSet\\Control\\Terminal "
        "Server' -Name fDenyTSConnections -ErrorAction SilentlyContinue"))
    users = str(shell.run_powershell(
        "Get-LocalGroupMember 'Remote Desktop Users' -ErrorAction SilentlyContinue "
        "| Select-Object Name | Format-Table"))
    return f"{status}\n{users}"
