"""Shell execution: PowerShell, cmd, and direct process launches.

The most powerful tool in the box, so it is also the most tightly policed
(see safety.policy._CRITICAL_SHELL / _EXFIL_SHELL). Notes that matter:

  * `-NoProfile` and non-interactive flags are forced on, so a user's profile
    script cannot hijack an agent run or hang it on a prompt.
  * Output is captured, truncated, and redacted before it is ever returned to
    the model.
  * `run_powershell` accepts a script or a list of statements, never a string
    that the caller has to escape by hand.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import ShellCommandError
from ..logging_setup import get_logger, record
from ..safety.secrets import redact
from ..utils import text as textutil

log = get_logger(__name__)

DEFAULT_TIMEOUT = 120.0
MAX_OUTPUT = 20_000

POWERSHELL = "powershell"
PS7 = "pwsh"
CMD = "cmd"


@dataclass
class CommandResult:
    command: str
    returncode: int
    stdout: str
    stderr: str
    duration: float = 0.0
    timed_out: bool = False
    cwd: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        parts = []
        if self.stdout.strip():
            parts.append(self.stdout.strip())
        if self.stderr.strip():
            parts.append("[stderr]\n" + self.stderr.strip())
        return redact("\n".join(parts))[:MAX_OUTPUT]

    def to_dict(self) -> dict:
        # stdout/stderr are redacted too: `echo $SECRET` and
        # `Get-Content key.txt` both put a live credential in the output, and
        # this dict is what lands in the run log and the JSONL trace.
        return {
            "command": redact(self.command), "returncode": self.returncode,
            "ok": self.ok, "timed_out": self.timed_out,
            "duration": round(self.duration, 2), "cwd": self.cwd,
            "stdout": redact(self.stdout)[:4000],
            "stderr": redact(self.stderr)[:2000],
        }

    def __str__(self) -> str:
        head = f"exit {self.returncode}" if not self.timed_out else "timed out"
        body = self.output.strip()
        if not body:
            return f"OK ({head})"
        return f"{head}: {textutil.truncate(body, 600)}"


@dataclass
class Shell:
    """A configured command runner."""

    shell: str = POWERSHELL
    timeout: float = DEFAULT_TIMEOUT
    cwd: str = ""
    env: dict = field(default_factory=dict)
    encoding: str = "utf-8"

    def _base_flags(self) -> list[str]:
        if self.shell in (POWERSHELL, PS7):
            return [
                self.shell, "-NoProfile", "-NonInteractive", "-NoLogo",
                "-ExecutionPolicy", "Bypass", "-Command", "-",
            ]
        if self.shell == CMD:
            return [CMD, "/d", "/c"]
        return [self.shell, "-c"]

    def run(self, command: str, timeout: float | None = None,
            cwd: str = "", env: dict | None = None,
            input_text: str = "") -> CommandResult:
        import time

        argv = self._base_flags()
        stdin = input_text
        if self.shell in (POWERSHELL, PS7):
            argv[-1] = "-"  # read the script from stdin, so quoting is never an issue
            stdin = command if not input_text else input_text
        else:
            argv.append(command)

        run_env = {**os.environ, **(self.env or {}), **(env or {})}
        run_cwd = cwd or self.cwd or None
        started = time.time()
        try:
            proc = subprocess.run(
                argv, input=stdin or None, capture_output=True, text=True,
                encoding=self.encoding, errors="replace", timeout=timeout or self.timeout,
                cwd=run_cwd, env=run_env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt" else 0,
            )
            result = CommandResult(command, proc.returncode, proc.stdout or "",
                                   proc.stderr or "", time.time() - started,
                                   False, str(run_cwd or ""))
        except subprocess.TimeoutExpired as e:
            result = CommandResult(command, -1,
                                   _as_text(e.stdout), _as_text(e.stderr),
                                   time.time() - started, True, str(run_cwd or ""))
        except FileNotFoundError as e:
            raise ShellCommandError(
                f"Shell {self.shell!r} is not installed: {e}", returncode=-1
            ) from e

        record("shell", command=redact(command)[:400], returncode=result.returncode,
               timed_out=result.timed_out, duration=round(result.duration, 2))
        log.debug("shell exit=%s cmd=%s", result.returncode, redact(command)[:200])
        return result

    def ok(self, command: str, **kwargs) -> CommandResult:
        result = self.run(command, **kwargs)
        if not result.ok:
            raise ShellCommandError(
                f"Command failed ({result.returncode}): {result.output[:400]}",
                returncode=result.returncode, stdout=result.stdout, stderr=result.stderr,
            )
        return result


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


# --------------------------------------------------------------------------
# module-level convenience wrappers
# --------------------------------------------------------------------------
def run_powershell(script: str | list[str], timeout: float = DEFAULT_TIMEOUT,
                    cwd: str = "", raise_on_error: bool = False) -> CommandResult:
    if isinstance(script, (list, tuple)):
        script = "\n".join(str(s) for s in script)
    shell = Shell(shell=_powershell_path(), timeout=timeout, cwd=cwd)
    result = shell.run(script)
    if raise_on_error and not result.ok:
        raise ShellCommandError(result.output[:400], returncode=result.returncode,
                                stdout=result.stdout, stderr=result.stderr)
    return result


def run_cmd(command: str, timeout: float = DEFAULT_TIMEOUT,
            cwd: str = "", raise_on_error: bool = False) -> CommandResult:
    shell = Shell(shell=CMD, timeout=timeout, cwd=cwd)
    result = shell.run(command)
    if raise_on_error and not result.ok:
        raise ShellCommandError(result.output[:400], returncode=result.returncode,
                                stdout=result.stdout, stderr=result.stderr)
    return result


def _powershell_path() -> str:
    import shutil

    return shutil.which(PS7) or shutil.which(POWERSHELL) or POWERSHELL


def ps(script: str | list[str], **kwargs) -> str:
    """Run PowerShell and return just the output text."""
    return run_powershell(script, **kwargs).output


# --------------------------------------------------------------------------
# process launching
# --------------------------------------------------------------------------
# Apps worth launching detached, so their UI is not a child console window.
DETACHED_EXES = {
    "notepad.exe", "explorer.exe", "msedge.exe", "chrome.exe", "firefox.exe",
    "calc.exe", "mspaint.exe", "wordpad.exe", "outlook.exe", "excel.exe",
    "winword.exe", "powerpnt.exe", "onenote.exe", "teams.exe", "spotify.exe",
    "slack.exe", "discord.exe", "vscode.exe", "code.exe", "zoom.exe",
    "snippingtool.exe", "photosviewer.exe", "magnify.exe", "narrator.exe",
}


def launch(exe: str, args: str = "", cwd: str = "", wait: float = 0.0,
           new_console: bool = False) -> int:
    """Start a program without a blocking shell. Returns the new process id."""
    import subprocess

    argv = [exe] + shlex.split(args, posix=False) if args else [exe]
    flags = 0
    if os.name == "nt":
        flags |= subprocess.DETACHED_PROCESS if new_console else 0
        if exe.lower() in DETACHED_EXES:
            flags |= subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd or None, close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL if os.name == "nt" else subprocess.PIPE,
            stderr=subprocess.DEVNULL if os.name == "nt" else subprocess.PIPE,
            creationflags=flags,
        )
    except FileNotFoundError as e:
        raise ShellCommandError(f"Program not found: {exe}") from e
    record("launch", exe=exe, args=args[:200], pid=proc.pid)
    if wait:
        import time

        time.sleep(wait)
    return proc.pid


def open_path(path: str | os.PathLike) -> str:
    """Open a file/folder/URL with whatever app Windows associates with it."""
    import subprocess

    target = str(path)
    try:
        os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
    except AttributeError:
        subprocess.Popen(["xdg-open", target])
    except Exception as e:
        raise ShellCommandError(f"Cannot open {target}: {e}") from e
    return f"opened {target}"


def open_url(url: str) -> str:
    import re

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        raise ShellCommandError(f"Not a URL: {url!r}")
    if re.match(r"^[a-z]+://", url, re.IGNORECASE) and url.lower().startswith("file:"):
        return open_path(url[7:])
    return open_path(url)


def here() -> Path:
    return Path(__file__).resolve().parent


def python_exe() -> str:
    return sys.executable


def which(name: str) -> str:
    import shutil

    found = shutil.which(name)
    if not found:
        raise ShellCommandError(f"{name!r} is not on PATH.")
    return found


def env_dump(names: list[str] | None = None) -> dict:
    """Read environment variables, redacting anything secret-shaped."""
    from ..safety.secrets import scrub_data

    source = {k: v for k, v in os.environ.items()
              if names is None or k.upper() in {n.upper() for n in names}}
    return scrub_data(source)
