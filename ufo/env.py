"""Load API keys from a `.env` file.

Standard library only — no `python-dotenv` dependency to install, and no
network at any point. The file is read once, at import time, before any config
or LLM client is built.

Precedence, highest first:

    1. a real environment variable (what you set for this shell, or the
       persistent user variable)
    2. `~/.ufo/.env`
    3. `.env` beside `main.py` in the project directory

A real environment variable always wins, so setting a key for one run
(`$env:GEMINI_API_KEY=...`) overrides the file without editing it, and CI can
inject keys without a file existing at all.

Nothing here logs, echoes, reprs or returns a value. A loader that prints a
secret into a log file has moved the problem rather than solved it.
"""
from __future__ import annotations

import os
from pathlib import Path

from .logging_setup import get_logger

log = get_logger(__name__)

# Only these names are ever read out of a .env file. A .env next to a config
# file is a convenient place to put all sorts of things; an allowlist keeps an
# unrelated or malformed line from reaching os.environ.
ALLOWED = frozenset({
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
})


def candidate_paths() -> list[Path]:
    """Where a .env is looked for, lowest priority first."""
    home = Path.home() / ".ufo" / ".env"
    project = Path(__file__).resolve().parent.parent / ".env"
    return [project, home]


def parse(text: str) -> dict[str, str]:
    """Parse .env content into a dict. Malformed lines are skipped, not fatal."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key not in ALLOWED:
            continue
        value = value.strip()
        # Strip one matching layer of quotes; a value may legitimately contain
        # the other quote character.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            # An unquoted value may carry a trailing comment, as in
            # `GEMINI_API_KEY=abc  # rotated 2026-01`. Keeping it would send
            # "abc  # rotated 2026-01" to the API as the key.
            for sep in ("  #", "\t#", " #"):
                index = value.find(sep)
                if index > 0:
                    value = value[:index]
                    break
            value = value.strip()
        if value:
            out[key] = value
    return out


def load_env(*, force: bool = False) -> dict[str, str]:
    """Populate `os.environ` from the first .env found. Returns what it set.

    Keys already present in the environment are left alone, so this is safe to
    call repeatedly and never clobbers a deliberate setting.
    """
    if os.environ.get("UFO_NO_DOTENV") and not force:
        return {}

    found: dict[str, str] = {}
    for path in candidate_paths():
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        found = parse(text)
        if found:
            log.debug("Read %d key(s) from %s", len(found), path)
            break

    applied: dict[str, str] = {}
    for key, value in found.items():
        if os.environ.get(key):
            log.debug("%s already set in the environment; .env ignored", key)
            continue
        os.environ[key] = value
        applied[key] = value

    if applied:
        # Names only. A value in a log line is a leaked key.
        log.info("Loaded from .env: %s", ", ".join(sorted(applied)))
    return applied


def env_status() -> dict[str, bool]:
    """Which keys are present, for `ufo doctor`. Never the values."""
    return {key: bool(os.environ.get(key)) for key in sorted(ALLOWED)}


def write_env(values: dict[str, str], path: Path | None = None) -> Path:
    """Write or update a .env, keeping any keys it already had.

    Used by `ufo secrets` so a key can be stored without being echoed to the
    terminal or typed into a file by hand.
    """
    target = path or (Path.home() / ".ufo" / ".env")
    existing = parse(_read(target)) if target.exists() else {}
    merged = {**existing, **{k: v for k, v in values.items() if v}}
    unknown = sorted(set(merged) - ALLOWED)
    if unknown:
        raise ValueError(f"Refusing to write unknown key(s): {', '.join(unknown)}")

    target.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"{k}={merged[k]}" for k in sorted(merged)) + "\n"
    target.write_text(body, encoding="utf-8")
    _restrict(target)
    return target


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return ""


def _restrict(path: Path) -> None:
    """Owner-only where the platform supports it.

    On Windows this clears the inheritance bit on the ACL, which is the
    equivalent of `chmod 600`. Failure is not fatal — the value is already on
    disk and refusing to write it would help nobody.
    """
    try:
        import os as _os
        import stat

        _os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except (OSError, NotImplementedError, ImportError):
        pass
    if os.name != "nt":
        return
    try:
        import subprocess

        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r",
             f"{os.environ.get('USERNAME', '')}:(R,W)"],
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


load_env()
