"""Filesystem primitives: read, write, search, move, copy, archive, clipboard.

Deliberately does NOT include `delete`/`rmtree` shortcuts by default — the
`DESTRUCTIVE_*` functions must be called by name so a model cannot reach them
by accident, and the policy layer sees `delete_path` as a high-risk action.
"""
from __future__ import annotations

import fnmatch
import os
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from ..errors import FilesystemError
from ..logging_setup import get_logger, record
from ..utils import text as textutil

log = get_logger(__name__)

# Never descend into these: slow, huge, or full of locks.
SKIP_DIRS = {
    "node_modules", ".git", ".svn", "__pycache__", ".venv", "venv", "env",
    "$RECYCLE.BIN", "System Volume Information", ".mypy_cache", ".pytest_cache",
    "AppData", "ProgramData", "Windows", "Program Files", "Program Files (x86)",
}
MAX_SEARCH_RESULTS = 500
MAX_READ_BYTES = 2_000_000


@dataclass
class Entry:
    path: Path
    is_dir: bool
    size: int = 0
    modified: float = 0.0

    def to_dict(self) -> dict:
        return {
            "path": str(self.path), "type": "dir" if self.is_dir else "file",
            "size": self.size,
            "modified": time_str(self.modified) if self.modified else None,
        }


def time_str(ts: float) -> str:
    import time

    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


# --------------------------------------------------------------------------
# expansion and existence
# --------------------------------------------------------------------------
def expand(path: str) -> Path:
    """Expand env vars, `~`, and shortcuts so users can type what they mean."""
    p = Path(os.path.expandvars(str(path))).expanduser()
    if os.name == "nt":
        # Resolve Windows-style known folders users actually type.
        known = {
            "%DESKTOP%": _known_folder("Desktop"),
            "%DOCUMENTS%": _known_folder("Documents"),
            "%PICTURES%": _known_folder("Pictures"),
            "%DOWNLOADS%": _known_folder("Downloads"),
            "%MUSIC%": _known_folder("Music"),
            "%VIDEOS%": _known_folder("Videos"),
        }
        for token, value in known.items():
            if value and token in str(path).upper():
                p = Path(str(p).replace(token, value))
    return p


def _known_folder(name: str) -> str:
    import ctypes
    from ctypes import wintypes

    CSIDL = {
        "Desktop": 0, "Documents": 5, "Downloads": 40, "Music": 13,
        "Pictures": 39, "Videos": 14, "Favorites": 6,
    }
    if name not in CSIDL:
        return ""
    buf = ctypes.create_unicode_buffer(260)
    try:
        ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL[name], None, 0, buf)
        return buf.value
    except Exception:
        return ""


def exists(path: str) -> bool:
    return expand(path).exists()


def home() -> Path:
    return Path.home()


def cwd() -> Path:
    return Path.cwd()


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------
def read_text(path: str, max_bytes: int = MAX_READ_BYTES, encoding: str = "utf-8") -> str:
    p = expand(path)
    if not p.is_file():
        raise FilesystemError(f"Not a file: {p}")
    data = p.read_bytes()[:max_bytes]
    return data.decode(encoding, errors="replace")


def read_lines(path: str, start: int = 1, count: int = 200) -> str:
    p = expand(path)
    if not p.is_file():
        raise FilesystemError(f"Not a file: {p}")
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        raise FilesystemError(f"Cannot read {p}: {e}") from e
    start = max(1, int(start))
    end = start + max(1, int(count))
    body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, min(end, len(lines) + 1)))
    if len(lines) > end:
        body += f"\n... ({len(lines)} lines total)"
    return body or "(file is empty)"


def list_dir(path: str = ".", pattern: str = "*", show_hidden: bool = True,
             limit: int = 300) -> list[Entry]:
    p = expand(path)
    if not p.is_dir():
        raise FilesystemError(f"Not a directory: {p}")
    out: list[Entry] = []
    for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
        if not show_hidden and child.name.startswith("."):
            continue
        if not fnmatch.fnmatch(child.name, pattern):
            continue
        try:
            st = child.stat()
            out.append(Entry(child, child.is_dir(), st.st_size, st.st_mtime))
        except OSError:
            continue
        if len(out) >= limit:
            break
    return out


def walk(path: str, max_depth: int = 3, skip: Iterable[str] = SKIP_DIRS) -> Iterator[Path]:
    root = expand(path)
    skip = set(skip)
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        depth = len(current.parts) - base_depth
        if depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            yield current / name


def search(root: str, pattern: str = "*", max_depth: int = 6,
          limit: int = MAX_SEARCH_RESULTS, match_content: bool = False,
          case_sensitive: bool = False) -> list[Path]:
    """Find files by glob (or by content when `match_content` is a string)."""
    found: list[Path] = []
    for candidate in walk(root, max_depth=max_depth):
        if fnmatch.fnmatch(candidate.name, pattern) or fnmatch.fnmatch(
            str(candidate), pattern
        ):
            found.append(candidate)
            if len(found) >= limit:
                break
            continue
        if match_content:
            try:
                body = candidate.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            hay = body if case_sensitive else body.lower()
            needle = match_content if case_sensitive else match_content.lower()
            if needle in hay:
                found.append(candidate)
                if len(found) >= limit:
                    break
    record("fs_search", root=str(root), pattern=str(pattern), found=len(found))
    return found


def grep(path: str, pattern: str, max_depth: int = 6,
         limit: int = 200) -> list[tuple[Path, int, str]]:
    """Line-oriented content search with file:line results."""
    import re

    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise FilesystemError(f"Bad search pattern {pattern!r}: {e}") from e
    hits: list[tuple[Path, int, str]] = []
    for candidate in walk(path, max_depth=max_depth):
        try:
            if candidate.stat().st_size > MAX_READ_BYTES:
                continue
            body = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(body.splitlines(), 1):
            if rx.search(line):
                hits.append((candidate, lineno, textutil.truncate(line.strip(), 200)))
                if len(hits) >= limit:
                    return hits
    return hits


def tree(path: str, max_depth: int = 2, limit: int = 200) -> str:
    """An ASCII directory tree — good for handing structure to the model."""
    root = expand(path)
    if not root.exists():
        raise FilesystemError(f"No such path: {root}")
    lines: list[str] = [str(root)]

    def _walk(current: Path, depth: int, prefix: str) -> None:
        if depth <= 0 or len(lines) >= limit:
            return
        try:
            children = sorted(
                (c for c in current.iterdir() if c.name not in SKIP_DIRS),
                key=lambda c: (not c.is_dir(), c.name.lower()),
            )
        except OSError:
            return
        for i, child in enumerate(children):
            if len(lines) >= limit:
                return
            last = i == len(children) - 1
            lines.append(f"{prefix}{'└── ' if last else '├── '}{child.name}"
                         + ("/" if child.is_dir() else ""))
            if child.is_dir():
                _walk(child, depth - 1, prefix + ("    " if last else "│   "))

    _walk(root, max_depth, "")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------
def write_text(path: str, content: str, encoding: str = "utf-8",
               append: bool = False, make_parents: bool = True) -> str:
    p = expand(path)
    try:
        if make_parents:
            p.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(p, mode, encoding=encoding, newline="\n") as fh:
            fh.write(str(content))
    except OSError as e:
        raise FilesystemError(f"Cannot write {p}: {e}") from e
    record("fs_write", path=str(p), bytes=len(str(content)), append=append)
    return f"{'appended to' if append else 'wrote'} {p} ({len(str(content))} chars)"


def write_lines(path: str, lines: Iterable[str], **kwargs) -> str:
    return write_text(path, "\n".join(str(x) for x in lines) + "\n", **kwargs)


def copy(src: str, dst: str, overwrite: bool = False, recursive: bool = True) -> str:
    s, d = expand(src), expand(dst)
    if not s.exists():
        raise FilesystemError(f"No such source: {s}")
    if d.exists() and not overwrite:
        raise FilesystemError(f"Destination already exists: {d}")
    try:
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.is_dir():
            if recursive:
                shutil.copytree(s, d, dirs_exist_ok=overwrite)
            else:
                raise FilesystemError(f"{s} is a directory; pass recursive=True")
        else:
            shutil.copy2(s, d)
    except OSError as e:
        raise FilesystemError(f"Copy failed: {e}") from e
    record("fs_copy", src=str(s), dst=str(d))
    return f"copied {s} to {d}"


def move(src: str, dst: str, overwrite: bool = False) -> str:
    s, d = expand(src), expand(dst)
    if not s.exists():
        raise FilesystemError(f"No such source: {s}")
    if d.exists():
        if not overwrite:
            raise FilesystemError(f"Destination already exists: {d}")
        if d.is_dir():
            d = d / s.name
    try:
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))
    except OSError as e:
        raise FilesystemError(f"Move failed: {e}") from e
    record("fs_move", src=str(s), dst=str(d))
    return f"moved {s} to {d}"


def rename(src: str, new_name: str) -> str:
    s = expand(src)
    return move(str(s), str(s.parent / new_name))


# ---- destructive: named explicitly so the policy layer can see them ----
def delete_path(path: str, recursive: bool = False, to_recycle: bool = True) -> str:
    """Delete a file or directory.

    Prefers the Recycle Bin (`to_recycle=True`) so an accidental delete is
    recoverable by the human — that is the difference between a bad run and a
    lost document.
    """
    p = expand(path)
    if not p.exists():
        raise FilesystemError(f"No such path: {p}")
    # Check `recursive` before the recycle branch. SHFileOperationW removes a
    # whole directory tree regardless of the Recycle Bin, so a non-empty
    # directory would otherwise be destroyed by `recursive=False`.
    if p.is_dir() and not recursive and any(p.iterdir()):
        raise FilesystemError(
            f"{p} is a non-empty directory; pass recursive=True to delete it."
        )
    try:
        if to_recycle:
            _recycle(p)
            record("fs_delete", path=str(p), recycled=True, recursive=recursive)
            return f"moved {p} to the Recycle Bin"
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    except FilesystemError:
        raise
    except OSError as e:
        raise FilesystemError(f"Delete failed: {e}") from e
    record("fs_delete", path=str(p), recycled=False, recursive=recursive)
    return f"permanently deleted {p}"


def _recycle(path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCT(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted",
                                                  wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle",
                                                         wintypes.LPCWSTR)]

    FO_DELETE = 0x0003
    FOF_ALLOWUNDO = 0x0040
    FOF_NOCONFIRMATION = 0x0010
    FOF_SILENT = 0x0004
    FOF_NOERRORUI = 0x0400

    ops = SHFILEOPSTRUCT()
    ops.wFunc = FO_DELETE
    ops.pFrom = str(path) + "\0\0"
    ops.pTo = None
    ops.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(ops))
    if code != 0:
        raise FilesystemError(
            f"Could not move {path} to the Recycle Bin (SHFileOperationW={code})."
        )


def make_dir(path: str, parents: bool = True) -> str:
    p = expand(path)
    try:
        p.mkdir(parents=parents, exist_ok=True)
    except OSError as e:
        raise FilesystemError(f"Cannot create {p}: {e}") from e
    return f"created directory {p}"


def empty_dir(path: str) -> str:
    p = expand(path)
    if not p.is_dir():
        raise FilesystemError(f"Not a directory: {p}")
    removed = 0
    for child in p.iterdir():
        try:
            shutil.rmtree(child) if child.is_dir() else child.unlink()
            removed += 1
        except OSError:
            continue
    record("fs_empty", path=str(p), removed=removed)
    return f"removed {removed} item(s) from {p}"


# --------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------
def unique_path(path: str) -> Path:
    p = expand(path)
    if not p.exists():
        return p
    stem, suffix, parent = p.stem, p.suffix, p.parent
    for i in range(1, 10_000):
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    raise FilesystemError(f"Cannot find a free name near {p}")


def zip_folder(src: str, dest: str = "", compression: int = zipfile.ZIP_DEFLATED) -> str:
    """Zip `src` and return the archive path.

    Returns the path rather than a sentence so that
    `unzip(zip_folder(x))` composes: a model that zips something almost always
    wants to unzip or move the result next.
    """
    s = expand(src)
    if not s.exists():
        raise FilesystemError(f"No such path: {s}")
    d = expand(dest) if dest else s.parent / f"{s.name}.zip"
    if s.is_dir() and d.resolve() != s.resolve() and s.resolve() in d.resolve().parents:
        raise FilesystemError(
            f"Refusing to write the archive inside the folder being zipped: {d}"
        )
    base = s if s.is_dir() else s.parent
    try:
        with zipfile.ZipFile(d, "w", compression) as zf:
            if s.is_file():
                zf.write(s, s.name)
            else:
                for item in walk(s, max_depth=12, skip=()):
                    zf.write(item, item.relative_to(base))
    except OSError as e:
        raise FilesystemError(f"Zip failed: {e}") from e
    record("fs_zip", src=str(s), archive=str(d), bytes=d.stat().st_size)
    return str(d)


def unzip(src: str, dest: str = "") -> str:
    s = expand(src)
    d = expand(dest) if dest else s.parent / s.stem
    if not s.is_file():
        raise FilesystemError(f"Not a file: {s}")
    try:
        d.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(s) as zf:
            for member in zf.namelist():
                target = (d / member).resolve()
                if not str(target).startswith(str(d.resolve())):
                    raise FilesystemError(
                        f"Refusing to extract {member!r}: it escapes the destination."
                    )
            zf.extractall(d)
    except zipfile.BadZipFile as e:
        raise FilesystemError(f"Not a zip file: {s}") from e
    except OSError as e:
        raise FilesystemError(f"Unzip failed: {e}") from e
    record("fs_unzip", src=str(s), dest=str(d))
    return str(d)


def du(path: str) -> str:
    total, count = 0, 0
    for item in walk(path, max_depth=12, skip=()):
        try:
            total += item.stat().st_size
            count += 1
        except OSError:
            continue
    return f"{total / (1024 ** 2):.1f} MB in {count} files"


def file_info(path: str) -> dict:
    p = expand(path)
    if not p.exists():
        raise FilesystemError(f"No such path: {p}")
    st = p.stat()
    info = {
        "path": str(p), "type": "dir" if p.is_dir() else "file",
        "size": st.st_size, "size_human": _human(st.st_size),
        "modified": time_str(st.st_mtime), "created": time_str(st.st_ctime),
    }
    if p.is_file():
        try:
            import mimetypes

            info["mime"] = mimetypes.guess_type(p.name)[0] or "unknown"
        except Exception:
            pass
    return info


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def recent(days: int = 7, root: str = "") -> list[Entry]:
    import time

    base = expand(root) if root else (Path.home() / "Desktop")
    cutoff = time.time() - days * 86400
    out: list[Entry] = []
    for item in walk(str(base), max_depth=4):
        try:
            st = item.stat()
        except OSError:
            continue
        if st.st_mtime >= cutoff:
            out.append(Entry(item, False, st.st_size, st.st_mtime))
    out.sort(key=lambda e: e.modified, reverse=True)
    return out[:MAX_SEARCH_RESULTS]
