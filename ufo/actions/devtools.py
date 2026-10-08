"""Developer tools: run, build, test, dependencies, environments, git.

Every command runs in the directory the user names and nothing else. There is no
"pick a project for me": guessing a working directory is how a build ends up
cleaning the wrong tree.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..errors import ActionError
from ..logging_setup import get_logger

from . import filesystem, shell

log = get_logger(__name__)

#: Tools this module knows how to drive, and how to ask for a version.
TOOLS = ("git", "node", "npm", "python", "pip", "docker", "go", "cargo", "mvn",
         "gradle", "dotnet", "java", "rustc")


def _project(target: str = "") -> Path:
    path = Path(filesystem.expand(target or "."))
    if not path.is_dir():
        raise ActionError(f"no such folder: {path}")
    return path


def _run(command: list[str], cwd: Path, timeout: int = 300) -> str:
    """Run a tool in `cwd`, capturing output. Never raises on a non-zero exit."""
    try:
        done = subprocess.run(command, cwd=str(cwd), capture_output=True,
                              text=True, timeout=timeout, shell=False)
    except FileNotFoundError:
        raise ActionError(f"{command[0]} is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        return f"{command[0]} ran for over {timeout}s and was stopped"
    out = (done.stdout or "") + (done.stderr or "")
    tail = out.strip().splitlines()[-40:]
    header = "" if done.returncode == 0 else f"(exit {done.returncode})\n"
    return header + "\n".join(tail) or "(no output)"


# --------------------------------------------------------------------------
# project
# --------------------------------------------------------------------------
def open_project(target: str = "", args: str = "") -> str:
    """Open a folder in Explorer, or in VS Code if it is installed."""
    path = _project(target or ".")
    editor = shutil.which("code")
    if editor:
        subprocess.Popen([editor, str(path)])
        return f"opened {path} in VS Code"
    shell.run_powershell(f'explorer.exe "{path}"')
    return f"opened {path} in Explorer"


def detect(target: str = "", args: str = "") -> str:
    """What kind of project this is, from the files present."""
    path = _project(target or ".")
    markers = {
        "package.json": "node", "pyproject.toml": "python", "setup.py": "python",
        "requirements.txt": "python", "Cargo.toml": "rust", "go.mod": "go",
        "pom.xml": "java (maven)", "build.gradle": "java (gradle)",
        ".csproj": "dotnet", "CMakeLists.txt": "c++", "Makefile": "make",
    }
    found = [kind for name, kind in markers.items() if (path / name).exists()]
    git = (path / ".git").is_dir()
    lines = [f"{path}"]
    if found:
        lines.append("project type: " + ", ".join(sorted(set(found))))
    else:
        lines.append("no build file recognised")
    lines.append("git repository: yes" if git else "not a git repository")
    from . import system

    lines.append("running as administrator"
                 if system.is_admin() else "running as a normal user")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# run, build, test
# --------------------------------------------------------------------------
def run(target: str = "", args: str = "") -> str:
    """Run the project's own run script or entry point."""
    path = _project(target)
    package = path / "package.json"
    if package.is_file():
        scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        name = args.strip() or ("start" if "start" in scripts else "dev")
        if name not in scripts:
            return (f"package.json has no {name!r} script. It has: "
                    f"{', '.join(sorted(scripts)) or 'none'}")
        return _run(["npm", "run", name], path)
    if (path / "main.py").is_file() or (path / "app.py").is_file():
        entry = "main.py" if (path / "main.py").is_file() else "app.py"
        return _run([sys.executable, entry], path)
    if (path / "Cargo.toml").is_file():
        return _run(["cargo", "run"], path)
    return f"{path.name} has no run script I recognise"


def build(target: str = "", args: str = "") -> str:
    path = _project(target or ".")
    if (path / "package.json").is_file():
        return _run(["npm", "run", args.strip() or "build"], path)
    if (path / "Cargo.toml").is_file():
        return _run(["cargo", "build"], path)
    if (path / "go.mod").is_file():
        return _run(["go", "build", "./..."], path)
    if (path / "pyproject.toml").is_file() or (path / "setup.py").is_file():
        return _run([sys.executable, "-m", "build"], path)
    return f"nothing in {path} says how to build itself"


def test(target: str = "", args: str = "") -> str:
    """Run the project's tests with its own runner."""
    path = _project(target or ".")
    if (path / "package.json").is_file():
        return _run(["npm", "test"], path, timeout=900)
    if (path / "Cargo.toml").is_file():
        return _run(["cargo", "test"], path, timeout=900)
    if (path / "tests").is_dir() or (path / "test").is_dir():
        return _run([sys.executable, "-m", "unittest", "discover", "-v"], path,
                    timeout=900)
    return f"no test directory or test script found in {path}"


def deps(target: str = "", args: str = "") -> str:
    """Install or list dependencies."""
    path = _project(target or ".")
    if (path / "package.json").is_file():
        if "install" in f"{target} {args}".lower():
            return _run(["npm", "install"], path, timeout=900)
        return _run(["npm", "ls", "--depth=0"], path)
    requirements = path / "requirements.txt"
    if requirements.is_file():
        if "install" in f"{target} {args}".lower():
            return _run([sys.executable, "-m", "pip", "install", "-r",
                         str(requirements)], path, timeout=900)
        return str(requirements.read_text(encoding="utf-8"))
    return f"no dependency file in {path}"


def venv(target: str = "", args: str = "") -> str:
    """Create or activate a virtual environment."""
    path = _project(target or ".")
    env = path / ".venv"
    if not env.is_dir():
        if "create" in f"{target} {args}".lower() or not env.is_dir():
            _run([sys.executable, "-m", "venv", str(env)], path)
            return f"created {env}"
    python = env / "Scripts" / "python.exe"
    if not python.is_file():
        python = env / "bin" / "python"
    if not python.is_file():
        return f"no virtual environment at {env}"
    return _run([str(python), "-m", "pip", "list"], path)


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------
def git_status(target: str = "", args: str = "") -> str:
    if not shutil.which("git"):
        return "git is not installed"
    return _run(["git", "status", "--short", "--branch"], _project(target or "."))


def git_log(target: str = "", args: str = "") -> str:
    path = _project(target or ".")
    count = args.strip() if args.strip().isdigit() else "10"
    return _run(["git", "log", f"-{count}", "--oneline", "--decorate"], path)


def git_diff(target: str = "", args: str = "") -> str:
    return _run(["git", "diff"], _project(target or "."))


def git_branch(target: str = "", args: str = "") -> str:
    """List or create a branch. Creating one is a change, so the catalog
    marks it and the guard asks."""
    path = _project(target or ".")
    name = args.strip()
    if not name:
        return _run(["git", "branch", "--list"], path)
    return _run(["git", "switch", "-c", name], path)


def tools(target: str = "", args: str = "") -> str:
    return "\n".join(f"{tool:<10}{shutil.which(tool) or '(not installed)'}"
                     for tool in TOOLS)


# --------------------------------------------------------------------------
# servers and environments
# --------------------------------------------------------------------------
def server(target: str = "", args: str = "") -> str:
    """Start a project's dev server in the background."""
    path = _project(target or ".")
    package = path / "package.json"
    if package.is_file():
        scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        name = args.strip() or ("dev" if "dev" in scripts else "start")
        if name not in scripts:
            return (f"no {name!r} script. package.json has: "
                    f"{', '.join(sorted(scripts)) or 'none'}")
        return str(subprocess.Popen(
            ["npm", "run", name], cwd=str(path),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)) and \
            f"started `npm run {name}` in {path}"
    if (path / "manage.py").is_file():
        return str(subprocess.Popen(
            [sys.executable, "manage.py", "runserver"], cwd=str(path))) and \
            f"started the Django dev server in {path}"
    return f"{path.name} has no dev server script I recognise"


def containers(target: str = "", args: str = "") -> str:
    """List or run docker containers."""
    if not shutil.which("docker"):
        return ("docker is not installed. Install Docker Desktop, or run WSL "
                "and `wsl --install -d Ubuntu` first.")
    verb = f"{target} {args}".strip().lower()
    if verb.startswith("run"):
        return _run(["docker", "run", "--rm", "-it", args.strip()],
                    _project(target), timeout=120)
    return _run(["docker", "ps", "-a"], _project(target))


def wsl(target: str = "", args: str = "") -> str:
    """Run a command in WSL, or list the distributions."""
    if not shutil.which("wsl"):
        return ("WSL is not installed. `wsl --install -d Ubuntu` needs an "
                "administrator shell and a reboot.")
    if not (target or args).strip():
        return str(shell.run_powershell("wsl --list --verbose"))
    return _run(["wsl"] + (f"{target} {args}".split()), _project(target))


def ssh(target: str = "", args: str = "") -> str:
    """Run a command on another machine over SSH."""
    host = (target or args).strip()
    if not host:
        return "give me user@host"
    if not shutil.which("ssh"):
        return "ssh is not installed; Windows 11 has it in Optional features"
    return str(shell.run_powershell(f"Start-Process ssh {host}"))


def deploy(target: str = "", args: str = "") -> str:
    """Run a project's own deploy command, in its directory."""
    path = _project(target or ".")
    script = args.strip()
    if not script:
        return ("give me the deploy command to run, e.g. "
                "deploy ./app 'npm run deploy'")
    return _run(["powershell", "-Command", script], path, timeout=1800)


# --------------------------------------------------------------------------
# code reading and writing
#
# Rewriting code from a one-line description is what a model is for. These route
# to the agent loop rather than pretending a regex can refactor a program.
# --------------------------------------------------------------------------
def _to_agent(instruction: str, cwd: Path) -> str:
    from ..config import active

    root = Path(active().storage.home) / "code-requests"
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = root / f"{stamp}.md"
    path.write_text(f"# in {cwd}\n\n{instruction}\n", encoding="utf-8")
    return (f"written to {path}.\nRun it with: ufo run \"{instruction}\" "
            f"--allow all --cwd {cwd}")


def explain(target: str = "", args: str = "") -> str:
    return _to_agent(f"explain this code: {target} {args}".strip(),
                     _project(target))


def refactor(target: str = "", args: str = "") -> str:
    return _to_agent(f"refactor this code: {target} {args}".strip(),
                     _project(target))


def generate_tests(target: str = "", args: str = "") -> str:
    return _to_agent(f"write tests for: {target} {args}".strip(),
                     _project(target))


def debug_error(target: str = "", args: str = "") -> str:
    """Read a traceback and say which frame is the problem."""
    text = f"{target} {args}".strip()
    if "Traceback" not in text:
        from ..config import active

        path = Path(active().storage.log_root) / "last-error.txt"
        if not path.is_file():
            return "give me the traceback to read"
        text = path.read_text(encoding="utf-8", errors="replace")
    frames = re.findall(r'File "([^"]+)", line (\d+), in (\w+)', text)
    if not frames:
        return "no Python traceback found in that text"
    where, line, function = frames[-1]
    cause = ""
    match = re.search(r"^(\w+(?:Error|Exception|Warning))\b", text, re.M)
    if match:
        cause = match.group(1)
    return (f"deepest frame: {Path(where).name}:{line} in {function}()"
            + (f"\nraised: {cause}" if cause else ""))


def write_script(target: str = "", args: str = "") -> str:
    return _to_agent(f"write a script that does: {target} {args}".strip(),
                     _project(target))
