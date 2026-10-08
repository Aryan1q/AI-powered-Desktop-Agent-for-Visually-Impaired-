"""Process and Task Manager primitives.

`psutil` is used when available (better data, no WMI) and the Win32 API is
the fallback, so process inspection works on a machine with nothing installed
beyond pywin32.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from ..errors import ProcessError
from ..logging_setup import record
from ..utils import text as textutil

try:
    import psutil  # type: ignore
except ImportError:
    psutil = None  # type: ignore

MAX_LIST = 200


@dataclass
class ProcInfo:
    pid: int
    name: str
    exe: str = ""
    username: str = ""
    cpu: float = 0.0
    memory_mb: float = 0.0
    status: str = ""
    started: float = 0.0
    cmdline: str = ""
    window_title: str = ""
    children: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "pid": self.pid, "name": self.name,
            "exe": textutil.truncate(self.exe, 200),
            "window_title": self.window_title,
            "cpu_percent": round(self.cpu, 1),
            "memory_mb": round(self.memory_mb, 1),
            "status": self.status,
            "started": (time.strftime("%H:%M", time.localtime(self.started))
                        if self.started else None),
        }


def available() -> bool:
    return psutil is not None


def list_processes(sort_by: str = "memory", limit: int = MAX_LIST,
                   details: bool = False) -> list[ProcInfo]:
    """Every running process, ranked by CPU, memory, name or recency.

    `details=False` (the default) reads only what the ranking and the summary
    line need. Asking psutil for a process's username, command line, CPU
    percentage and start time means several extra kernel round-trips *per
    process*, which turns a one-second listing into a ten-second one — so those
    fields are opt-in.
    """
    if psutil is not None:
        attrs = ["pid", "name", "exe", "memory_info"]
        if details:
            attrs += ["username", "cmdline", "cpu_percent", "status", "create_time"]
        out: list[ProcInfo] = []
        for p in psutil.process_iter(attrs):
            try:
                info = p.info
                out.append(ProcInfo(
                    pid=int(info["pid"]),
                    name=str(info.get("name") or ""),
                    exe=str(info.get("exe") or ""),
                    username=str(info.get("username") or ""),
                    cpu=float(info.get("cpu_percent") or 0.0),
                    memory_mb=(info["memory_info"].rss / (1024 ** 2))
                    if info.get("memory_info") else 0.0,
                    status=str(info.get("status") or ""),
                    started=float(info.get("create_time") or 0.0),
                    cmdline=" ".join(info.get("cmdline") or [])[:400],
                ))
            except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
                continue
        return _sort(out, sort_by)[:limit]

    return _sort(_wmi_processes(), sort_by)[:limit]


def _sort(procs: list[ProcInfo], key: str) -> list[ProcInfo]:
    key = (key or "memory").lower()
    if key in ("cpu", "cpu_percent"):
        return sorted(procs, key=lambda p: p.cpu, reverse=True)
    if key in ("name", "process"):
        return sorted(procs, key=lambda p: p.name.lower())
    if key in ("newest", "started", "recent"):
        return sorted(procs, key=lambda p: p.started, reverse=True)
    return sorted(procs, key=lambda p: p.memory_mb, reverse=True)


def _wmi_processes() -> list[ProcInfo]:
    """Enumerate processes without psutil, using the Win32 process API.

    Deliberately not WMI: `winmgmts:` COM queries can block for minutes when
    the WMI service is busy, and a hung process listing freezes the whole
    agent with no timeout and no error. `EnumProcesses` is a direct syscall
    and returns in milliseconds.
    """
    out: list[ProcInfo] = []
    try:
        import win32api  # type: ignore
        import win32process  # type: ignore
    except ImportError:
        return out
    pids = _enum_pids()
    titles = _title_map(set(pids))
    for pid in pids:
        try:
            handle = win32api.OpenProcess(0x1000, False, pid)
            try:
                exe = win32process.GetModuleFileNameEx(handle, 0)
            finally:
                win32api.CloseHandle(handle)
            out.append(ProcInfo(pid=pid, name=os.path.basename(exe), exe=exe,
                                window_title=titles.get(pid, "")))
        except Exception:
            continue
    return out


def _title_map(pids: set[int]) -> dict[int, str]:
    """pid -> window title, in ONE UI Automation pass.

    Calling `window_title_for` per process means a full UIA tree walk for each
    of several hundred processes; on a real machine that takes minutes and
    looks exactly like a hang.
    """
    wanted = pids & _window_pids()
    if not wanted:
        return {}
    titles: dict[int, str] = {}
    try:
        from ..perception import uia

        for w in uia.list_all_windows(include_untitled=False):
            if w.process_id in wanted and w.title:
                titles.setdefault(w.process_id, w.title)
    except Exception:
        return titles
    return titles


def _window_pids() -> set[int]:
    """Cheap pid list of processes that actually own a top-level window."""
    try:
        from ..utils import win32

        return {w.process_id for w in win32.list_windows() if w.process_id}
    except Exception:
        return set()


def _enum_pids() -> list[int]:
    try:
        import win32process  # type: ignore

        return sorted({int(p) for p in win32process.EnumProcesses() if p})
    except Exception:
        return []


def window_title_for(pid: int) -> str:
    from ..utils import win32

    try:
        from ..perception import uia

        for w in uia.list_all_windows(include_untitled=False):
            if w.process_id == pid:
                return w.title
    except Exception:
        pass
    del win32
    return ""


def _basic_processes() -> list[ProcInfo]:
    """pid/name/exe only — no memory, no start time, no window titles.

    Looking a process up by name does not need a memory ranking, and reading
    memory info for every process costs seconds on a busy machine. Anything
    that filters rather than ranks should use this.
    """
    out: list[ProcInfo] = []
    if psutil is not None:
        for p in psutil.process_iter(["pid", "name", "exe"]):
            try:
                out.append(ProcInfo(pid=int(p.info["pid"]),
                                    name=str(p.info.get("name") or ""),
                                    exe=str(p.info.get("exe") or "")))
            except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
                continue
        return out
    return _wmi_processes()


def find(name: str = "", pid: int = 0, limit: int = 20) -> list[ProcInfo]:
    """Locate processes by name substring or exact pid."""
    needle = (name or "").lower()
    hits = [p for p in _basic_processes()
            if (needle in p.name.lower() if needle else True)
            and (pid == 0 or p.pid == pid)]
    return hits[:limit]


def info(pid: int) -> ProcInfo:
    """Details for a single process.

    Goes straight to psutil rather than building the whole system table: a
    full listing reads memory info for every process and costs seconds.
    """
    pid = int(pid)
    if psutil is not None:
        try:
            p = psutil.Process(pid)
            with p.oneshot():
                mem = p.memory_info()
                return ProcInfo(
                    pid=pid, name=p.name(), exe=p.exe(), username=p.username(),
                    cpu=p.cpu_percent(None), memory_mb=mem.rss / (1024 ** 2),
                    status=p.status(), started=p.create_time(),
                    cmdline=" ".join(p.cmdline())[:400],
                )
        except psutil.NoSuchProcess:
            raise ProcessError(f"No process with pid {pid}.") from None
        except psutil.AccessDenied as e:
            raise ProcessError(f"Not permitted to inspect pid {pid}: {e}") from e
    for p in list_processes(limit=10_000):
        if p.pid == pid:
            return p
    raise ProcessError(f"No process with pid {pid}.")


def terminate(pid: int, force: bool = False, tree: bool = False) -> str:
    """Stop a process. `tree` also stops everything it spawned."""
    if psutil is not None:
        try:
            p = psutil.Process(int(pid))
            name = p.name()
            targets = [p] + (p.children(recursive=True) if tree else [])
            for t in targets:
                try:
                    t.kill() if force else t.terminate()
                except Exception:
                    continue
            record("kill", pid=pid, force=force, tree=tree, name=name)
            return f"{'killed' if force else 'stopped'} {name} (pid {pid})" + (
                " and its child processes" if tree else "")
        except psutil.NoSuchProcess:
            raise ProcessError(f"No process with pid {pid}.") from None
        except psutil.AccessDenied as e:
            raise ProcessError(
                f"Access denied stopping pid {pid}. Run the agent as an "
                "administrator if this process is elevated."
            ) from e
    import subprocess

    args = ["taskkill", "/PID", str(pid), "/T" if tree else ""]
    args = [a for a in args if a]
    if force:
        args.append("/F")
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ProcessError(f"taskkill failed: {(proc.stderr or proc.stdout).strip()}")
    record("kill", pid=pid, force=force, tree=tree)
    return f"stopped pid {pid}"


def kill_by_name(name: str, force: bool = False) -> str:
    victims = find(name=name)
    if not victims:
        raise ProcessError(f"No running process matches {name!r}.")
    stopped = []
    for p in victims:
        try:
            terminate(p.pid, force=force)
            stopped.append(f"{p.name}({p.pid})")
        except ProcessError:
            continue
    return f"stopped {len(stopped)} process(es): {', '.join(stopped)}"


def window_for_pid(pid: int):
    from ..perception import uia

    for w in uia.list_all_windows():
        if w.process_id == int(pid):
            return w
    return None


# --------------------------------------------------------------------------
# system performance + startup
# --------------------------------------------------------------------------
def system_summary() -> str:
    """CPU, memory, disk and uptime, in one line — the "why is it slow?" call."""
    parts: list[str] = []
    if psutil is not None:
        parts.append(f"CPU {psutil.cpu_percent(interval=0.4):.0f}%")
        vm = psutil.virtual_memory()
        parts.append(f"RAM {vm.percent:.0f}% of {vm.total / 1e9:.1f} GB")
        parts.append(f"up {textutil.truncate(_uptime(), 40)}")
    for part in disk_summary():
        parts.append(part)
    return " | ".join(parts) or "no metrics available"


def _uptime() -> str:
    if psutil is None:
        return "unknown"
    seconds = time.time() - psutil.boot_time()
    days, rem = divmod(int(seconds), 86400)
    hours = rem // 3600
    return f"{days}d {hours}h"


def disk_usage(path: str = "C:\\") -> dict:
    p = os.path.abspath(os.path.expandvars(path))
    if not os.path.isdir(p):
        p = os.path.abspath(os.sep)
    usage = __import__("shutil").disk_usage(p)
    total = usage.total or 1
    return {
        "path": p,
        "total_gb": round(usage.total / 1e9, 1),
        "used_gb": round(usage.used / 1e9, 1),
        "free_gb": round(usage.free / 1e9, 1),
        "percent_used": round(usage.used / total * 100, 1),
    }


def disk_summary() -> list[str]:
    out = []
    for drive in ("C:\\",):
        try:
            d = disk_usage(drive)
            out.append(f"{drive} {d['free_gb']}/{d['total_gb']} GB free "
                       f"({d['percent_used']}% used)")
        except Exception:
            continue
    return out


def startup_items() -> list[dict]:
    """Registry Run keys plus the Startup folder — the usual 'why is it slow'."""
    items: list[dict] = []
    try:
        import winreg  # type: ignore

        for hive, path in (
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
        ):
            try:
                with winreg.OpenKey(hive, path) as key:
                    i = 0
                    while True:
                        try:
                            name, value, _t = winreg.EnumValue(key, i)
                        except OSError:
                            break
                        items.append({"name": name, "command": value, "source": "registry"})
                        i += 1
            except OSError:
                continue
    except ImportError:
        pass
    for folder in (_startup_dir("user"), _startup_dir("common")):
        if not folder or not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            full = os.path.join(folder, name)
            if os.path.isfile(full):
                items.append({"name": name, "command": full, "source": "startup folder"})
    return items


def _startup_dir(kind: str) -> str:
    import ctypes
    from ctypes import wintypes

    CSIDL = {"user": 7, "common": 24}.get(kind)
    if CSIDL is None:
        return ""
    buf = ctypes.create_unicode_buffer(260)
    try:
        ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL, None, 0, buf)
        return buf.value
    except Exception:
        return ""


def top_processes(n: int = 5, sort_by: str = "memory") -> str:
    procs = list_processes(sort_by=sort_by, limit=n)
    if not procs:
        return "no processes"
    return "\n".join(
        f"  {p.name} (pid {p.pid}) — {p.memory_mb:.0f} MB, {p.cpu:.0f}% CPU"
        + (f", {p.window_title}" if p.window_title else "")
        for p in procs
    )
