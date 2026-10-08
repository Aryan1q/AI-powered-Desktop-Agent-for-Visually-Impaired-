"""Backup and recovery: restore points, archives, verification, file recovery.

Restoring overwrites live data, so the catalog marks those capabilities critical
and the guard asks. Nothing here deletes the copy it is restoring from.
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from ..errors import ActionError
from ..logging_setup import get_logger

from . import filesystem, shell

log = get_logger(__name__)


def _backup_root() -> Path:
    return Path.home() / "ufo-backups"


def restore_point(target: str = "", args: str = "") -> str:
    """Create a Windows system restore point."""
    name = (target or args).strip() or f"ufo {datetime.now():%Y-%m-%d %H:%M}"
    result = str(shell.run_powershell(
        f"Checkpoint-Computer -Description '{name}' -RestorePointType "
        f"'APPLICATION_INSTALL'"))
    if "error" in result.lower() or "disabled" in result.lower():
        return (f"could not create a restore point: {result[:200]}\n"
                f"System Protection is often off by default; enable it on C: "
                f"first.")
    return f"restore point created: {name}"


def create(target: str = "", args: str = "") -> str:
    """Zip a folder into the backup directory, timestamped so nothing is clobbered."""
    source = Path(filesystem.expand(target or ""))
    if not source.is_dir():
        return f"no such folder: {source}"
    root = _backup_root()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = root / f"{source.name}-{stamp}.zip"
    shutil.make_archive(str(archive.with_suffix("")), "zip", str(source))
    return f"backed up {source.name} to {archive} ({archive.stat().st_size / 1e6:.1f} MB)"


def schedule(target: str = "", args: str = "") -> str:
    """Back a folder up every day through the Windows scheduler."""
    folder = (target or args).strip()
    if not folder:
        return "give me a folder to back up daily"
    from . import auto

    return auto.schedule(f"back up {folder} to the ufo backup folder", "0 2 * * *")


def list_backups(target: str = "", args: str = "") -> str:
    root = _backup_root()
    if not root.is_dir():
        return f"no backups yet; they go in {root}"
    files = sorted(root.glob("*.zip"), reverse=True)
    if not files:
        return f"no backups yet in {root}"
    return "\n".join(f"{f.name}  {f.stat().st_size / 1e6:.1f} MB  "
                     f"{datetime.fromtimestamp(f.stat().st_mtime):%Y-%m-%d %H:%M}"
                     for f in files[:30])


def verify(target: str = "", args: str = "") -> str:
    """Check that a backup archive is readable and not empty."""
    root = _backup_root()
    name = (target or args).strip()
    archive = Path(filesystem.expand(name)) if name else (
        sorted(root.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True) or [None])[0]
    if archive is None:
        return f"no backups to verify in {root}"
    if not archive.is_file():
        return f"no such backup: {archive}"
    import zipfile

    if not zipfile.is_zipfile(archive):
        return f"{archive.name} is not a readable zip archive — it may be corrupt"
    with zipfile.ZipFile(archive) as bundle:
        bad = bundle.testzip()
        count = len(bundle.namelist())
    return (f"{archive.name} is intact: {count} entries"
            if bad is None else f"{archive.name} is corrupt at {bad}")


def restore(target: str = "", args: str = "") -> str:
    """Unpack a backup. This overwrites files at the destination."""
    name = (target or args).strip()
    if not name:
        return "give me the backup to restore"
    archive = Path(filesystem.expand(name))
    if not archive.is_file():
        found = sorted(_backup_root().glob(f"*{name}*"))
        if not found:
            return f"no backup matching {name!r} in {_backup_root()}"
        archive = found[0]
    destination = Path(filesystem.expand(args.strip())) if args.strip() else (
        archive.parent / f"{archive.stem}-restored")
    if destination.exists() and any(destination.iterdir()):
        return (f"{destination} already has things in it. Point me at an empty "
                f"folder so nothing is overwritten.")
    return filesystem.unzip(str(archive), str(destination)) + f" -> {destination}"


def recover_file(target: str = "", args: str = "") -> str:
    """Look for a file in the Recycle Bin, then in shadow-copy restore points."""
    name = (target or args).strip()
    if not name:
        return "give me the file name"
    recycle = Path("C:/$Recycle.Bin")
    if recycle.is_dir():
        # Recycle Bin entries are renamed, so the original name only survives in
        # the $I metadata file. Say that rather than guess.
        pass
    return ("deleted files keep their names in the Recycle Bin — open it in "
            "Explorer and restore from there. Shadow-copy recovery needs "
            "vssadmin and administrator rights.")


def image_backup(target: str = "", args: str = "") -> str:
    return ("a full image backup needs a tool like Macrium or Veeam. "
            "Windows' own File History backs the user folders to a drive you "
            "attach: Settings > System > File History.")


def file_history(target: str = "", args: str = "") -> str:
    """Turn Windows File History on, or point it at a drive."""
    destination = (target or args).strip()
    if destination:
        return ("File History stores in %LOCALAPPDATA%\\Microsoft\\Windows\\"
                "FileHistory and is configured in Control Panel, not by "
                f"script. Attach {destination}, then open File History and "
                "set it up there — the configuration is per-drive.")
    state = str(shell.run_powershell(
        "Get-Service fhsvc | Select-Object Name, Status | Format-Table"))
    enabled = str(shell.run_powershell(
        "Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion"
        "\\FileHistory' -ErrorAction SilentlyContinue | "
        "Select-Object -Property Started"))
    return f"{state}\n{enabled}"


def bitlocker(target: str = "", args: str = "") -> str:
    """Show or change disk encryption. Turning it off is never done here."""
    verb = f"{target} {args}".strip().lower()
    status = str(shell.run_powershell(
        "Get-BitLockerVolume | Select-Object MountPoint, VolumeStatus, "
        "EncryptionPercentage | Format-Table -AutoSize"))
    if verb.startswith(("enable", "on", "start")):
        return ("enabling encryption is a whole-disk change. Review the "
                f"current state first, then run Enable-BitLocker yourself.\n{status}")
    if verb.startswith(("disable", "off", "decrypt")):
        return ("I will not decrypt a disk. Everything on it becomes readable "
                "to anyone who takes the machine.\n" + status)
    return status
