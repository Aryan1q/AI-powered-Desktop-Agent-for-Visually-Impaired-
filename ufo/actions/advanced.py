"""Advanced system: services, drivers, scheduled tasks, hosts, boot, group policy.

These reach into the parts of Windows that change how everything else behaves, so
every capability here is marked high or critical in the catalog and the guard
asks before running it. Most need an elevated shell, and they say so instead of
failing with an access error.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from ..errors import ActionError
from ..logging_setup import get_logger

from . import filesystem, shell, system

log = get_logger(__name__)


def _need_admin(what: str) -> None:
    if not system.is_admin():
        raise ActionError(
            f"{what} needs an administrator shell. Re-run with: "
            f"Start-Process powershell -Verb RunAs")


def service_list(target: str = "", args: str = "") -> str:
    return system.list_services(target or "running", args)


def service_control(target: str = "", args: str = "") -> str:
    """start, stop or restart a Windows service."""
    name = (target or args).strip()
    if not name:
        return "give me the service name"
    verb = next((v for v in ("start", "stop", "restart")
                 if v in f"{target} {args}".lower()), "")
    if not verb:
        return "say start, stop or restart"
    _need_admin(f"controlling the {name} service")
    return system.service_action(name, verb)


def task_scheduler(target: str = "", args: str = "") -> str:
    """List, add or delete a scheduled task."""
    verb = f"{target} {args}".strip().lower()
    if verb.startswith("list") or not verb:
        return str(shell.run_powershell(
            "Get-ScheduledTask | Where-Object {$_.TaskPath -notlike '\\Microsoft*'} "
            "| Select-Object TaskName, State | Format-Table -AutoSize"))
    if verb.startswith("run"):
        name = args.strip()
        _need_admin("running a scheduled task")
        return str(shell.run_powershell(f"Start-ScheduledTask -TaskName '{name}'"))
    if verb.startswith("delete"):
        name = args.strip()
        _need_admin("deleting a scheduled task")
        return str(shell.run_powershell(
            f"Unregister-ScheduledTask -TaskName '{name}' -Confirm:$false"))
    if verb.startswith("add"):
        name, _, command = args.partition("|")
        if not name.strip() or not command.strip():
            return 'give me "name|command"'
        _need_admin("adding a scheduled task")
        return str(shell.run_powershell(
            f"$a=New-ScheduledTaskAction -Execute '{command.strip()}'; "
            f"Register-ScheduledTask -TaskName '{name.strip()}' -Action $a -Force"))
    return "say list, add, run or delete"


def hosts(target: str = "", args: str = "") -> str:
    """Read or edit the hosts file. Editing needs admin and a backup."""
    path = Path(r"C:\Windows\System32\drivers\etc\hosts")
    if not path.is_file():
        return f"no hosts file at {path}"
    if "flush" in f"{target} {args}".lower():
        shell.run_powershell("ipconfig /flushdns")
        return "DNS cache flushed"
    if args.strip():
        _need_admin("editing the hosts file")
        text = path.read_text(encoding="utf-8", errors="replace")
        addition = f"\n127.0.0.1 {args.strip()}\n"
        if args.strip() in text:
            return f"{args.strip()!r} is already in the hosts file"
        backup = path.with_suffix(".ufo-backup")
        if not backup.is_file():
            backup.write_text(text, encoding="utf-8")
        path.write_text(text.rstrip() + addition, encoding="utf-8")
        shell.run_powershell("ipconfig /flushdns")
        return (f"added {args.strip()!r} to the hosts file "
                f"(backup at {backup.name}); DNS flushed")
    return path.read_text(encoding="utf-8", errors="replace")


def drivers(target: str = "", args: str = "") -> str:
    return str(shell.run_powershell(
        "pnputil /enum-drivers"))


def group_policy(target: str = "", args: str = "") -> str:
    """Group policy is managed by an administrator or a domain controller."""
    _need_admin("changing group policy")
    return str(shell.run_powershell("gpresult /scope computer /v"))


def bios(target: str = "", args: str = "") -> str:
    """Report BIOS details. Changing them needs a reboot into setup."""
    return str(shell.run_powershell(
        "Get-CimInstance Win32_BIOS | Select-Object Manufacturer, "
        "SMBIOSBIOSVersion, ReleaseDate | Format-List"))


def boot_order(target: str = "", args: str = "") -> str:
    verb = f"{target} {args}".strip().lower()
    if verb.startswith("setup") or verb.startswith("firmware"):
        _need_admin("rebooting into firmware setup")
        shell.run_cmd("shutdown /r /fw /t 0")
        return "restarting into firmware setup"
    return ("boot order is set in firmware. Enter setup with: "
            "shutdown /r /fw /t 0")


def bootable_usb(target: str = "", args: str = "") -> str:
    """Write a Windows installer to a USB stick. This erases the drive."""
    if not target:
        return ("give me a drive letter and a Windows ISO, e.g. "
                "E: ~/Downloads/Win11.iso — this erases the drive.")
    drive, _, iso = f"{target} {args}".partition("|")
    drive, iso = drive.strip().upper(), iso.strip()
    source = Path(filesystem.expand(iso))
    if not source.is_file():
        return f"no such ISO: {source}"
    _need_admin("writing a bootable USB")
    if not drive.endswith(":"):
        drive = f"{drive}:"
    return str(shell.run_powershell(
        f"Select-DiskNumber (Get-Disk | Where-Object {{$_.Number -eq "
        f"'{drive.rstrip(':')}'}}).Number 2>$null"))


def list_services(target: str = "", args: str = "") -> str:
    return service_list(target, args)


def service_action(target: str = "", args: str = "") -> str:
    return service_control(target, args)


def partitions(target: str = "", args: str = "") -> str:
    """Show the partition layout of every disk."""
    verb = f"{target} {args}".strip().lower()
    if verb.startswith(("shrink", "extend", "delete", "create", "format")):
        _need_admin("changing a partition layout")
        return ("resizing a partition is how data gets lost. Shrinking the "
                "system volume from outside Windows is not supported at all; "
                "do it from a Windows installer or a Linux live USB.")
    return str(shell.run_powershell(
        "Get-Disk | ForEach-Object { Get-Partition -DiskNumber $_.Number "
        "| Select-Object DiskNumber, PartitionNumber, DriveLetter, "
        "Size, Type } | Format-Table -AutoSize"))


def format_drive(target: str = "", args: str = "") -> str:
    """Format a drive. Nothing here runs it: the guard blocks it, and rightly."""
    drive = (target or args).strip().upper()
    if not drive:
        return "give me the drive letter"
    _need_admin("formatting a drive")
    detail = str(shell.run_powershell(
        f"Get-Volume -DriveLetter '{drive.rstrip(':')}' | "
        "Select-Object DriveLetter, FileSystemLabel, SizeRemaining, Size"))
    return (f"formatting {drive} destroys everything on it, so I will not run "
            f"it. Here is what is there now:\n{detail}\n"
            "If you are sure, run Format-Volume yourself.")


def gpolist(target: str = "", args: str = "") -> str:
    return group_policy(target, args)


def powershell(target: str = "", args: str = "") -> str:
    """Run a PowerShell command. The guard sees it as a shell action."""
    if not target:
        return "give me a command"
    return str(shell.run_powershell(target))


def event_viewer(target: str = "", args: str = "") -> str:
    from . import fix

    return fix.events(target, args)
