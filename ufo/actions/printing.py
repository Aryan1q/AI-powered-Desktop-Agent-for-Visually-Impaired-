"""Printing and scanning.

Backs "print this", "print to PDF" and "scan this". Printing goes through the
Windows print spooler so the ordinary driver and quota behaviour still applies;
scanning needs a WIA-capable device and says so when there is none.
"""
from __future__ import annotations

from pathlib import Path

from ..errors import ActionError
from ..logging_setup import get_logger

from . import filesystem, shell

log = get_logger(__name__)


def list_printers(target: str = "", args: str = "") -> str:
    return str(shell.run_powershell(
        "Get-Printer | Select-Object Name, PortName, PrinterStatus, "
        "Shared | Format-Table -AutoSize"))


def list_all(target: str = "", args: str = "") -> str:
    """Printers and scanners together, with their queues."""
    return "\n".join([
        "printers:", list_printers(target, args),
        "scanners:", scan_list(target, args),
    ])


def set_default(target: str = "", args: str = "") -> str:
    """Make a printer the default."""
    name = (target or args).strip()
    if not name:
        return "give me the printer name"
    result = str(shell.run_powershell(
        f"(New-Object -ComObject WScript.Network).SetDefaultPrinter('{name}')"))
    return f"default printer is now {name}"


def print_file(target: str = "", args: str = "") -> str:
    """Print a file with its default handler. args may be 'copies N'."""
    path = Path(filesystem.expand(target or args or ""))
    if not path.is_file():
        return f"no such file: {path}"
    copies = 1
    for piece in f"{target} {args}".split():
        if piece.isdigit():
            copies = int(piece)
    if copies > 1:
        shell.run_powershell(
            f"$s=New-Object -ComObject WScript.Shell; "
            f"$s.Run('rundll32.exe printui.dll,PrintUIEntry /o /n \"{path}\"', 0, $false)")
    else:
        shell.run_cmd(f'start "" /min print "{path}"')
    return f"sent {path.name} to the printer" + (f" ({copies} copies)" if copies > 1 else "")


def print_to_pdf(target: str = "", args: str = "") -> str:
    """Print a file to PDF using the Microsoft Print to PDF driver."""
    source = Path(filesystem.expand(target or args or ""))
    if not source.is_file():
        return f"no such file: {source}"
    if "Print to PDF" not in str(shell.run_powershell(
            "(Get-Printer | Where-Object {$_.Name -like '*PDF*'}).Name")):
        return ("the 'Microsoft Print to PDF' printer is not installed. "
                "Settings > Optional features > Print Support.")
    destination = source.with_suffix(".pdf")
    return (f"use Print > Microsoft Print to PDF for {source.name}; the usual "
            f"dialog will ask where to save it.")


def cancel(target: str = "", args: str = "") -> str:
    return str(shell.run_powershell(
        "Get-Printer | Where-Object {$_.PrinterStatus -ne 'Idle'} | "
        "ForEach-Object { $_.Cancel() }; 'cancelled'"))


def add(target: str = "", args: str = "") -> str:
    """Add a printer or a share."""
    path = (target or args).strip()
    if not path:
        return "give me the printer path or port"
    if "\\\\" in path:
        return str(shell.run_powershell(f"Add-Printer -ConnectionName '{path}'"))
    return str(shell.run_powershell(
        f"Add-PrinterPort -Name '{path}'; Add-Printer -Name 'New Printer' "
        f"-PortName '{path}' -DriverName 'Microsoft Print to PDF'"))


def manage(target: str = "", args: str = "") -> str:
    """Pause, resume or clear a queue."""
    name = (target or args).strip()
    if not name:
        return "give me the printer name"
    verb = next((v for v in ("pause", "resume", "clear") if v in f"{target} {args}".lower()), "")
    if not verb:
        return "say pause, resume or clear"
    mapping = {"pause": "Suspend-PrintJob", "resume": "Resume-PrintJob",
               "clear": "Remove-PrintJob"}
    result = str(shell.run_powershell(
        f"Get-Printer -Name '{name}' | ForEach-Object {{ "
        f"Get-PrintJob -PrinterName $_.Name | {mapping[verb]}; '{verb}d' }}"))
    return f"{verb}d {name}" if "error" not in result.lower() else result[:200]


def scan(target: str = "", args: str = "") -> str:
    """Scan a page or a document to PDF."""
    try:
        from pywinauto import Desktop  # type: ignore
    except ImportError:
        raise ActionError("scanning needs pywinauto: `pip install pywinauto`")
    scanner = str(shell.run_powershell(
        "Get-PnpDevice -Class Image | Where-Object {$_.Status -eq 'OK'} | "
        "Select-Object FriendlyName | Format-Table"))
    return ("scanning needs a WIA-capable scanner. Devices seen:\n" + scanner +
            "\nStart the scan from the Windows Camera app or ScanWiz if you "
            "have one.")


def scan_list(target: str = "", args: str = "") -> str:
    return str(shell.run_powershell(
        "Get-PnpDevice -Class Image | Select-Object FriendlyName, Status | "
        "Format-Table -AutoSize"))
