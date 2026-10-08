"""Secret detection and redaction.

Two jobs:
  1. `redact()` scrubs anything that looks like a key/token/password out of
     text before it reaches a log, a JSONL trace, or a remote LLM provider.
  2. `SecretStore` holds the user's own credentials locally (for the shell /
     system actions) and never exposes them in prompts or logs.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

_PLACEHOLDER = "[REDACTED]"

# Named-credential patterns: (regex, what it is)
_NAMED = [
    (r"(?i)\b(?:api[_-]?key|apikey|secret[_-]?key|access[_-]?token|refresh[_-]?token|"
     r"client[_-]?secret|auth[_-]?token|bearer)\b\s*[:=]\s*[\"']?([^\s\"',;]{6,})", "named credential"),
    (r"(?i)\b(?:password|passwd|pwd|passphrase|pin)\b\s*[:=]\s*[\"']?([^\s\"',;]{4,})", "password"),
]

# Bare high-entropy tokens for the major providers.
_PROVIDER = [
    (r"sk-[A-Za-z0-9_\-]{16,}", "openai-style key"),
    (r"sk-ant-[A-Za-z0-9_\-]{16,}", "anthropic key"),
    (r"AIza[0-9A-Za-z_\-]{30,}", "google api key"),
    # Google's newer keys use an "AQ." prefix instead of "AIza". Without this
    # a current Google AI Studio key sails straight through the redactor into
    # the run log and the JSONL trace.
    (r"AQ\.[A-Za-z0-9_\-]{20,}", "google api key (AQ)"),
    (r"gh[pousr]_[A-Za-z0-9]{20,}", "github token"),
    (r"xox[baprs]-[A-Za-z0-9\-]{10,}", "slack token"),
    (r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}", "jwt"),
]

_CARD = r"\b(?:\d[ -]?){13,19}\b"
_SSN = r"\b\d{3}-\d{2}-\d{4}\b"

_PATTERNS: list[tuple[re.Pattern, str]] = []
for _rx, _kind in _NAMED + _PROVIDER:
    _PATTERNS.append((re.compile(_rx), _kind))
_PATTERNS.append((re.compile(_CARD), "card number"))
_PATTERNS.append((re.compile(_SSN), "ssn"))

# Environment variables whose values must never be logged.
_SENSITIVE_ENV_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH")

_ENTROPY_ALPHABET = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


def _looks_random(token: str, min_len: int = 20) -> bool:
    if len(token) < min_len:
        return False
    body = token.strip("\"'")
    if len(set(body)) < 8:
        return False
    letters = sum(c in _ENTROPY_ALPHABET for c in body)
    return letters / len(body) > 0.9 and any(c.isdigit() for c in body) and any(c.isupper() for c in body)


def find_secrets(text: str) -> list[tuple[str, str, str]]:
    """Return (start, end, kind) spans that look like secrets."""
    if not text:
        return []
    hits: list[tuple[int, int, str]] = []
    for pattern, kind in _PATTERNS:
        for m in pattern.finditer(text):
            group = 1 if m.re.groups else 0
            start, end = m.span(group)
            if end > start:
                hits.append((start, end, kind))
    for token in re.findall(r"[A-Za-z0-9_\-]{24,}", text):
        if _looks_random(token):
            s = text.find(token)
            if s >= 0:
                hits.append((s, s + len(token), "high-entropy token"))
    return hits


def redact(text: Any, *, placeholder: str = _PLACEHOLDER) -> str:
    """Replace every detected secret in `text` with `placeholder`."""
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else str(text)
    hits = sorted(find_secrets(text), key=lambda h: (h[0], -(h[1] - h[0])))
    out, last = [], 0
    for start, end, _kind in hits:
        if start < last:
            continue
        out.append(text[last:start])
        out.append(placeholder)
        last = end
    out.append(text[last:])
    return "".join(out)


def scrub_data(data: Any, *, depth: int = 0) -> Any:
    """Recursively redact strings in dicts/lists so log payloads are safe."""
    if depth > 12:
        return "<deep>"
    if isinstance(data, str):
        return redact(data)
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            key = str(k)
            if any(h in key.upper() for h in _SENSITIVE_ENV_HINTS):
                out[key] = _PLACEHOLDER
            else:
                out[key] = scrub_data(v, depth=depth + 1)
        return out
    if isinstance(data, (list, tuple)):
        return [scrub_data(v, depth=depth + 1) for v in data]
    return data


class SecretStore:
    """A tiny local credential vault for actions that need a real secret.

    Backed by a JSON file written with owner-only permissions. Values are
    never returned to prompts; callers get a reference name instead.
    """

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._data: dict[str, str] = {}
        self._loaded = False

    def load(self) -> "SecretStore":
        if self._loaded:
            return self
        self._loaded = True
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._data = {}
        return self

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        try:  # owner-only on POSIX; a no-op on Windows
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def set(self, name: str, value: str) -> "SecretStore":
        self.load()
        self._data[name.strip().lower()] = str(value)
        self._flush()
        return self

    def get(self, name: str, default: str = "") -> str:
        self.load()
        return self._data.get(name.strip().lower(), default)

    def unset(self, name: str) -> bool:
        self.load()
        existed = self._data.pop(name.strip().lower(), None) is not None
        if existed:
            self._flush()
        return existed

    def has(self, name: str) -> bool:
        self.load()
        return name.strip().lower() in self._data

    def names(self) -> list[str]:
        self.load()
        return sorted(self._data)

    def from_env(self, name: str, *env_vars: str) -> str:
        """Prefer an env var, fall back to the vault, then to a prompt-free ''."""
        for var in env_vars:
            value = os.getenv(var)
            if value:
                return value
        return self.get(name)

    def __len__(self) -> int:
        self.load()
        return len(self._data)
