"""Troubleshooting and repair: diagnostics, the speed-up recipe, resets.

Everything here is either a measurement or a well-known safe repair. The
destructive repairs (network reset, cache wipe) say what they will touch before
doing it, because the catalog marks them high-risk and the guard asks.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from ..errors import ActionError
from ..logging_setup import get_logger

from . import network, process, shell

log = get_logger(__name__)


def diagnose(target: str = "", args: str = "") -> str:
    """A broad health check, most-likely-problems first."""
    findings = []
    memory = process.top_processes(5, "memory")
    findings.append(f"top memory users:\n{memory}")
    disk = process.disk_summary()
    if disk:
        findings.append(str(disk))
    if not network.is_online():
        findings.append("no internet connection")
        findings.append(str(network.ip_config()))
    findings.append(str(process.system_summary()))
    return "\n\n".join(findings)


def speed_up(target: str = "", args: str = "") -> str:
    """The safe half of "make it faster": report what is actually slow.

    Nothing is killed or disabled without being named first. Turning off
    startup entries or killing a process is the user's call, and the guard asks
    about each one.
    """
    lines = ["Top memory users:", str(process.top_processes(8, "memory")),
             "", "Top CPU users:", str(process.top_processes(8, "cpu")),
             "", "Startup entries:", _startup_summary(),
             "", str(process.disk_summary() or "")]
    lines.append("\nTell me which to stop and I will do that one.")
    return "\n".join(line for line in lines if line is not None)


def _startup_summary() -> str:
    items = process.startup_items()
    if not items:
        return "  none found"
    return "\n".join(f"  {item}" for item in items[:12])


def reset_network(target: str = "", args: str = "") -> str:
    """Flush DNS and reset the stack. Drops live connections."""
    steps = [shell.run_powershell("ipconfig /flushdns"),
             shell.run_powershell("netsh winsock reset"),
             shell.run_powershell("netsh int ip reset")]
    return "network stack reset — this drops current connections and a reboot helps"


def sfc_dism(target: str = "", args: str = "") -> str:
    """The Windows system file repair, in the order Microsoft recommends."""
    sfc = str(shell.run_powershell("sfc /scannow"))
    dism = str(shell.run_powershell("DISM /Online /Cleanup-Image /RestoreHealth"))
    return f"sfc: {'done' if 'found no' in sfc.lower() or 'repaired' in sfc.lower() else 'ran'}\n{sfc[:400]}\n\ndism: {dism[:400]}"


def events(target: str = "", args: str = "") -> str:
    """Recent application errors, which is where crashes leave a trace."""
    return str(shell.run_powershell(
        "Get-WinEvent -FilterHashtable @{LogName='Application'; Level=2} "
        "-MaxEvents 15 | Select-Object TimeCreated, ProviderName, "
        "Id, Message | Format-List"))


def crash_cause(target: str = "", args: str = "") -> str:
    """Group recent errors by faulting module — the pattern is the answer."""
    return str(shell.run_powershell(
        "Get-WinEvent -FilterHashtable @{LogName='Application'; Level=2} "
        "-MaxEvents 40 | Group-Object {$_.Message -replace '(?s).*?(\\S+\\.\\w+).*','$1'} "
        "| Sort-Object Count -Descending | Select-Object Count, Name | "
        "Format-Table -AutoSize"))


def clear_cache(target: str = "", args: str = "") -> str:
    """Clear temp files. Names the folder it empties."""
    root = Path(tempfile.gettempdir())
    removed = 0
    freed = 0
    for item in root.iterdir():
        try:
            if item.is_file():
                freed += item.stat().st_size
                item.unlink()
                removed += 1
            elif item.is_dir():
                freed += _wipe(item)
                removed += 1
        except OSError:
            continue
    return f"removed {removed} temp item(s), about {freed / 1e6:.0f} MB from {root}"


def _wipe(path: Path) -> int:
    """Empty a directory, skipping anything another process holds open."""
    freed = 0
    for child in path.rglob("*"):
        try:
            if child.is_file() and not child.is_symlink():
                freed += child.stat().st_size
                child.unlink()
        except OSError:
            continue
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass
    return freed


def office_repair(target: str = "", args: str = "") -> str:
    return ("repairing Office means repairing the installation: "
            "Control Panel > Programs > Microsoft Office > Change > Online Repair")


def reset_app(target: str = "", args: str = "") -> str:
    """Clear an app's local cache so it rebuilds it."""
    name = (target or args).strip()
    if not name:
        return "give me the app name"
    root = Path.home() / "AppData" / "Local" / name
    if not root.is_dir():
        return f"no local data folder for {name} at {root}"
    import shutil

    shutil.rmtree(root, ignore_errors=True)
    return f"cleared {root}"


def rollback_driver(target: str = "", args: str = "") -> str:
    return str(shell.run_powershell(
        "pnputil /enum-devices /problem"))


def safe_mode(target: str = "", args: str = "") -> str:
    return ("to reach Safe Mode from a script you need admin rights and a "
            "reboot: bcdedit /set {default} safeboot minimal")


def sound(target: str = "", args: str = "") -> str:
    """Diagnose an audio problem."""
    lines = [str(network.firewall_status())]
    result = str(shell.run_powershell(
        "Get-CimInstance Win32_SoundDevice | Select-Object Name, Status | "
        "Format-Table -AutoSize"))
    lines.append(result)
    return "\n".join(lines)


def printer(target: str = "", args: str = "") -> str:
    return str(shell.run_powershell(
        "Get-Printer | Select-Object Name, PortName, PrinterStatus | "
        "Format-Table -AutoSize"))


def bluetooth(target: str = "", args: str = "") -> str:
    return str(shell.run_powershell(
        "Get-PnpDevice -Class Bluetooth | Select-Object Status, FriendlyName | "
        "Format-Table -AutoSize"))


def black_screen(target: str = "", args: str = "") -> str:
    """The usual causes, in the order they are worth checking."""
    return "\n".join([
        "1. Press Ctrl+Shift+Esc — if Task Manager appears, it is a shell problem.",
        "2. `explorer.exe` from Run restores the desktop and taskbar.",
        "3. Check the event log:  ufo run \"show recent application errors\"",
        "4. Last resort: sfc /scannow then DISM /RestoreHealth.",
    ])
