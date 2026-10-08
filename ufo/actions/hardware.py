"""Hardware and monitoring: battery, disks, processes, temperature, addresses.

Backs "check my battery", "what is eating my RAM" and "how much disk space is
left". Everything here is read-only unless the capability name says otherwise:
monitoring a machine must never be the thing that changes it.
"""
from __future__ import annotations

from ..errors import ActionError
from ..logging_setup import get_logger

from . import network, process

log = get_logger(__name__)


def status(target: str = "", args: str = "") -> str:
    """The one-glance answer: is this machine healthy right now."""
    lines = [str(process.system_summary())]
    disk = process.disk_summary()
    if disk:
        lines.append(str(disk))
    battery_line = battery()
    if battery_line:
        lines.append(battery_line)
    return "\n".join(line for line in lines if line)


def battery(target: str = "", args: str = "") -> str:
    from . import system

    return system.battery() or "no battery reported (this may be a desktop)"


def processes(target: str = "", args: str = "") -> str:
    count = 10
    if target:
        try:
            count = max(1, int(target))
        except ValueError:
            pass
    return process.top_processes(count, args or "memory")


def kill_process(target: str = "", args: str = "") -> str:
    """End a process by name or pid. `args` may carry 'force'."""
    if not target:
        raise ActionError("give me a process name or a pid")
    force = "force" in f"{target} {args}".lower()
    if target.strip().isdigit():
        return process.terminate(int(target.strip()), force=force)
    return process.kill_by_name(target.strip(), force=force)


def disk_usage(target: str = "", args: str = "") -> str:
    return process.disk_usage(target) if target else process.disk_summary()


def storage_report(target: str = "", args: str = "") -> str:
    """What is filling the disk, biggest folders first."""
    import shutil
    from pathlib import Path

    root = Path(target).expanduser() if target else Path.home()
    if not root.exists():
        return f"no such folder: {root}"
    rows = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        try:
            if child.is_dir():
                used = sum(f.stat().st_size for f in child.rglob("*")
                           if f.is_file() and not f.is_symlink())
            else:
                used = child.stat().st_size
        except OSError:
            continue
        rows.append((used, child))
    rows.sort(reverse=True, key=lambda row: row[0])
    total = shutil.disk_usage(root).total
    lines = [f"{total / 1e9:.1f} GB on {_drive_of(root)}",
             "biggest things here:"]
    for used, child in rows[:12]:
        lines.append(f"  {used / 1e6:>9.1f} MB  {child.name}")
    return "\n".join(lines)


def _drive_of(path) -> str:
    import os

    return os.path.splitdrive(str(path))[0] or "this drive"


def ip_address(target: str = "", args: str = "") -> str:
    if "public" in f"{target} {args}".lower():
        return f"public {network.public_ip()}"
    return f"local {network.local_ip()}"


def temperature(target: str = "", args: str = "") -> str:
    """Thermals are not exposed to user-mode code on most machines."""
    try:
        import wmi  # type: ignore

        readings = [f"{s} {int(getattr(s, 'CurrentTemperature', 273) - 273)}C"
                    for s in wmi.WMI().Win32_PerfFormattedData_Counters_ThermalZoneInformation()]  # noqa: E501
        return ", ".join(readings) or "no thermal zones reported"
    except Exception:
        return ("this machine does not report a temperature to user-mode code; "
                "check the vendor app or BIOS")


def system_info(target: str = "", args: str = "") -> str:
    from . import system

    return str(system.info())


def uptime(target: str = "", args: str = "") -> str:
    from . import system

    return system.uptime()


def startup_report(target: str = "", args: str = "") -> str:
    items = process.startup_items()
    return "\n".join(str(i) for i in items) if items else "no startup items found"


def devices(target: str = "", args: str = "") -> str:
    """Connected hardware, and whether Windows is happy with each one."""
    return str(shell_powershell(
        "Get-PnpDevice -PresentOnly | Where-Object {$_.Class -in "
        "'USB','HIDClass','Display','Net','AudioEndpoint','DiskDrive'} | "
        "Select-Object FriendlyName, Status | Format-Table -AutoSize"))


def eject(target: str = "", args: str = "") -> str:
    """Safely eject removable drives."""
    drive = (target or args).strip().upper()
    if drive and not drive.endswith(":"):
        drive = f"{drive}:"
    if not drive:
        return str(shell_powershell(
            "Get-Volume | Where-Object {$_.DriveType -eq 'Removable'} | "
            "Select-Object DriveLetter, FileSystemLabel | Format-Table"))
    result = str(shell_powershell(
        f"$v=Get-Volume -DriveLetter '{drive.rstrip(':')}' -ErrorAction Stop; "
        f"$d=(Get-Partition -DiskNumber $v.DiskNumber).DiskNumber; "
        f"Dismount-Volume -DriveLetter '{drive.rstrip(':')}' -Force"))
    return f"ejected {drive}" if "error" not in result.lower() else f"could not eject {drive}: {result[:200]}"


def free_port(target: str = "", args: str = "") -> str:
    """Which process holds a TCP port. target is the port number."""
    port = (target or args).strip()
    if not port.isdigit():
        return "give me a port number"
    result = str(shell_powershell(f"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue"))
    pids = {row.split()[-1] for row in result.splitlines() if row.strip() and row.split()[-1].isdigit()}
    if not pids:
        return f"nothing is listening on port {port}"
    names = []
    for pid in pids:
        try:
            names.append(f"{process.info(int(pid)).name} (pid {pid})")
        except Exception:
            names.append(f"pid {pid}")
    return f"port {port} is held by {', '.join(names)}"


def disk_check(target: str = "", args: str = "") -> str:
    """Run the filesystem consistency check on a drive."""
    drive = (target or args).strip().upper() or "C"
    if not drive.endswith(":"):
        drive = f"{drive}:"
    result = str(shell_powershell(f"chkdsk {drive} /scan"))
    return f"checked {drive}:\n{result[:600]}"


def drivers(target: str = "", args: str = "") -> str:
    return str(shell_powershell(
        "Get-CimInstance Win32_PnPSignedDriver | Where-Object {$_.DeviceName} | "
        "Select-Object DeviceName, DriverVersion | Format-Table -AutoSize"))


def health(target: str = "", args: str = "") -> str:
    """The one-glance hardware verdict: disk, memory, thermals, battery."""
    parts = [str(process.system_summary()), str(process.disk_summary() or ""),
             battery(), temperature()]
    return "\n".join(p for p in parts if p)


def shell_powershell(command: str) -> str:
    from . import shell

    return str(shell.run_powershell(command))
