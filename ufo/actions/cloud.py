"""Cloud drives and sync: status, sync, upload, download, share links.

Each provider is a command-line tool. Where the tool is not installed the
handler names it, because "not installed" and "failed" are different problems
and the user needs to know which one they have.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from ..errors import ActionError
from ..logging_setup import get_logger

from . import filesystem, shell

log = get_logger(__name__)

PROVIDERS = {
    "onedrive": "rclone",
    "google drive": "rclone",
    "gdrive": "rclone",
    "dropbox": "rclone",
    "box": "rclone",
}


def _tool_for(name: str) -> str:
    return PROVIDERS.get(name.strip().lower(), "rclone")


def _require(name: str) -> str:
    tool = _tool_for(name)
    if not shutil.which(tool):
        raise ActionError(
            f"{name or 'cloud sync'} needs {tool}, which is not installed. "
            f"See https://rclone.org/install/ — then `rclone config` to connect "
            f"an account.")
    return tool


def status(target: str = "", args: str = "") -> str:
    """What sync tools exist and what remotes are configured."""
    tool = "rclone"
    if not shutil.which(tool):
        return (f"{tool} is not installed, so no cloud provider can be reached. "
                f"See https://rclone.org/install/")
    remotes = str(shell.run_powershell(f"{tool} listremotes"))
    return remotes.strip() or f"{tool} is installed but no remote is configured"


def sync(target: str = "", args: str = "") -> str:
    """Sync a local folder with a remote. target is the remote, args the folder."""
    tool = _require(target)
    folder = args.strip() or str(Path.home())
    if "listremotes" not in args:
        return (f"usage: sync <remote> <local folder>, e.g. "
                f"sync onedrive: ~/Documents")
    return str(shell.run_powershell(f"{tool} sync {target} {folder}"))


def upload(target: str = "", args: str = "") -> str:
    """Copy a file to a remote."""
    tool = _require(args)
    source = filesystem.expand(target)
    if not source.is_file():
        return f"no such file: {source}"
    return str(shell.run_powershell(f"{tool} copy {source} {args}"))


def download(target: str = "", args: str = "") -> str:
    """Copy a file from a remote to a local path."""
    tool = _require(target)
    return str(shell.run_powershell(f"{tool} copy {target} {args}"))


def share_link(target: str = "", args: str = "") -> str:
    """Create a share link for a file on a remote."""
    tool = _require(target)
    return str(shell.run_powershell(f"{tool} link {args}"))


def cloud_backup(target: str = "", args: str = "") -> str:
    """Back a folder up to a remote on a schedule."""
    from . import auto

    return auto.schedule(f"upload {target or 'the backup folder'} to cloud",
                         args or "0 3 * * *")


def mounted(target: str = "", args: str = "") -> str:
    """Where the known cloud folders live on this machine."""
    home = Path.home()
    rows = []
    for name in ("OneDrive", "Google Drive", "Dropbox", "Box"):
        for parent in (home, home / "AppData/Local"):
            path = parent / name
            if path.is_dir():
                rows.append(str(path))
    return "\n".join(rows) or "no cloud folders are synced locally"