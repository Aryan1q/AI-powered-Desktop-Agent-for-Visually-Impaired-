"""File and folder operations: find, organise, rename in bulk, archive, recycle.

This is the module behind "find that pdf", "empty the recycle bin" and "zip the
Downloads folder". It composes `actions.filesystem`, which already enforces the
recursion and clobber rules, so nothing here needs to re-implement them.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from ..errors import ActionError, FilesystemError
from ..logging_setup import get_logger

from . import filesystem, shell

log = get_logger(__name__)


def find(target: str = "", args: str = "") -> str:
    """Find files. target is a folder or a glob; args narrows by content."""
    if not target:
        return "give me something to look for, e.g. *.pdf"
    root = Path(filesystem.expand(target))
    if root.is_file():
        return str(root)
    pattern = args if args and any(c in args for c in "*?") else "*"
    hits = filesystem.search(str(root), pattern=pattern)
    if not hits:
        return f"nothing matching {pattern!r} under {root}"
    return "\n".join(str(h) for h in hits[:50])


def organise(target: str = "", args: str = "") -> str:
    """Tidy one folder: sort loose files into type folders.

    Only the named folder is touched, and only its immediate children move.
    """
    root = Path(filesystem.expand(target or "~/Downloads"))
    if not root.is_dir():
        return f"no such folder: {root}"
    kinds = {
        ".pdf": "PDFs", ".docx": "Docs", ".doc": "Docs", ".txt": "Docs",
        ".xlsx": "Sheets", ".csv": "Sheets", ".pptx": "Slides",
        ".jpg": "Images", ".jpeg": "Images", ".png": "Images", ".gif": "Images",
        ".mp4": "Video", ".mkv": "Video", ".mp3": "Audio", ".wav": "Audio",
        ".zip": "Archives", ".7z": "Archives", ".exe": "Installers",
        ".msi": "Installers",
    }
    moved = []
    for child in sorted(root.iterdir()):
        if not child.is_file() or child.name.startswith("."):
            continue
        folder = kinds.get(child.suffix.lower())
        if not folder:
            continue
        destination = root / folder
        try:
            filesystem.make_dir(str(destination))
            shutil.move(str(child), str(filesystem.unique_path(destination / child.name)))
            moved.append(f"{child.name} -> {folder}/")
        except (OSError, FilesystemError) as e:
            moved.append(f"{child.name} NOT moved: {e}")
    if not moved:
        return f"{root} is already tidy"
    return f"organised {len(moved)} file(s) in {root}:\n" + "\n".join(moved)


def batch_rename(target: str = "", args: str = "") -> str:
    """Rename many files by extension or by a search/replace rule.

    `target` is the folder (or glob), `args` is e.g. '.txt .md' to convert
    extensions, or 'find|replace' to rewrite text in the names.
    """
    raw = target or ""
    root = Path(filesystem.expand(raw)) if not any(c in raw for c in "*?") else None
    pattern = raw
    if root is not None:
        if not root.is_dir():
            return f"no such folder: {root}"
        pattern = "*"
    else:
        root = Path(filesystem.expand(raw)).parent
    files = [p for p in filesystem.search(str(root), pattern=pattern) if p.is_file()]
    if not files:
        return f"no files matched {pattern!r} under {root}"

    old, _, new = args.partition("|")
    if "|" in args:                       # a find/replace rule on the name
        old, new = old.strip(), new.strip()
        def rewrite(name: str) -> str:
            return name.replace(old, new) if old else name
    elif args.strip():                    # an extension change
        old_ext, new_ext = args.split()[0], (args.split() + [""])[1]
        def rewrite(name: str) -> str:
            return name + new_ext if name.lower().endswith(old_ext.lower()) else name
    else:
        return "say what to rename: '.txt .md' or 'old|new'"

    changed = []
    for path in files:
        updated = rewrite(path.name)
        if updated and updated != path.name:
            try:
                filesystem.rename(str(path), updated)
                changed.append(f"{path.name} -> {updated}")
            except (OSError, FilesystemError) as e:
                changed.append(f"{path.name} NOT renamed: {e}")
    if not changed:
        return f"nothing to rename in {root}"
    return f"renamed {len(changed)} file(s):\n" + "\n".join(changed)


def bulk_move(target: str = "", args: str = "") -> str:
    """Move everything matching a glob into a destination folder."""
    source, _, destination = target.partition("|")
    if not destination:
        return "give me 'source-glob|destination'"
    src = Path(filesystem.expand(source.strip()))
    dest = Path(filesystem.expand(destination.strip()))
    if not dest.is_dir():
        return f"no such folder: {dest}"
    items = [p for p in filesystem.search(str(src.parent), pattern=src.name)]
    if not items:
        return f"nothing matched {src.name!r}"
    moved = []
    for item in items:
        try:
            moved.append(filesystem.move(str(item), str(dest / item.name)))
        except (OSError, FilesystemError) as e:
            moved.append(f"{item.name} NOT moved: {e}")
    return "\n".join(moved)


def empty_recycle_bin(target: str = "", args: str = "") -> str:
    """Empty the Recycle Bin for every drive, reporting the real count."""
    from . import system

    emptied, errors = 0, []
    for drive in _drives():
        command = (f"Clear-RecycleBin -DriveLetter {drive} -Force -ErrorAction Stop; "
                   f"Write-Output 'ok'")
        result = shell.run_powershell(command)
        text = str(result)
        if "ok" in text.lower():
            emptied += 1
        elif "non-terminating" in text.lower() or "cannot find" in text.lower():
            continue          # nothing in that bin is not a failure
        else:
            errors.append(f"{drive}: {text[:120]}")
    if errors:
        return "partly done:\n" + "\n".join(errors)
    if not emptied:
        return "the Recycle Bin was already empty"
    return f"emptied the Recycle Bin on {emptied} drive(s)"


def _drives() -> list[str]:
    out = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        if os.path.exists(f"{letter}:\\"):
            out.append(letter)
    return out


def delete(target: str = "", args: str = "") -> str:
    """Delete to the Recycle Bin unless `args` says otherwise."""
    if not target:
        return "give me a path"
    force = "force" in args.lower() or "permanent" in args.lower()
    return filesystem.delete_path(target, recursive="recursive" in args.lower(),
                                  to_recycle=not force)


def reveal(target: str = "", args: str = "") -> str:
    """Open Explorer with the file selected."""
    if not target:
        return "give me a path"
    shell.run_powershell(
        f'explorer.exe /select,"{filesystem.expand(target)}"')
    return f"revealed {filesystem.expand(target)}"


def archive(target: str = "", args: str = "") -> str:
    """Zip or unzip. target is the path; args is 'zip' or 'unzip'."""
    if not target:
        return "give me a folder to zip, or a .zip to unpack"
    verb = args.strip().lower()
    path = filesystem.expand(target)
    if verb.startswith("unzip") or path.suffix.lower() == ".zip":
        return filesystem.unzip(target, args.split("|")[1] if "|" in args else "")
    if verb.startswith("7z"):
        shell.run_powershell(
            f'Compress-Archive -Path "{path}" -DestinationPath "{path}.zip"')
        return f"{path}.zip"
    return filesystem.zip_folder(target)


def permissions(target: str = "", args: str = "") -> str:
    path = filesystem.expand(target or ".")
    if not path.exists():
        return f"no such path: {path}"
    return shell.run_powershell(f'icacls "{path}"')


def visibility(target: str = "", args: str = "") -> str:
    """Show or set the hidden flag."""
    path = filesystem.expand(target or ".")
    if not path.exists():
        return f"no such path: {path}"
    if "hide" in args.lower():
        shell.run_powershell(f'attrib +h "{path}"')
        return f"hidden: {path.name}"
    if "show" in args.lower():
        shell.run_powershell(f'attrib -h "{path}"')
        return f"visible again: {path.name}"
    return str(filesystem.file_info(target))


def encrypt(target: str = "", args: str = "") -> str:
    """EFS encrypt a file. This is a real security control, so it says what it did."""
    path = filesystem.expand(target or "")
    if not path.is_file():
        return f"no such file: {path}"
    shell.run_powershell(f'cipher /e "{path}"')
    return f"encrypted with your Windows account: {path.name}"