"""Text helpers: truncation, similarity, key-string parsing, redaction glue."""
from __future__ import annotations

import difflib
import re
import unicodedata

_WS = re.compile(r"\s+")


def collapse_ws(text: str) -> str:
    return _WS.sub(" ", str(text or "")).strip()


def combine_fields(target: str, args: str) -> str:
    """Both halves of a capability call, as one string.

    A capability call arrives as `capability:target:args` — "put the report in
    my Documents" is a target *and* an argument, and the user supplied both. The
    idiom this replaces, `target or args`, silently throws `args` away whenever
    `target` is set, which is precisely the case where both were present: 54
    capabilities were dropping the second half of what the user typed.

    Empty halves vanish, so this is also the right way to normalise a call where
    the user only gave one part.
    """
    return " ".join(part for part in (str(target or "").strip(),
                                      str(args or "").strip()) if part)


def truncate(text: str, limit: int = 80, ellipsis: str = "…") -> str:
    text = str(text or "")
    text = collapse_ws(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(ellipsis))] + ellipsis


def one_line(text: str, limit: int = 160) -> str:
    return truncate(str(text or "").replace("\r", " ").replace("\n", " ⏎ "), limit)


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, collapse_ws(a).lower(),
                                   collapse_ws(b).lower()).ratio()


def changed(a: str, b: str, threshold: float = 0.98) -> bool:
    return similarity(a, b) < threshold


def is_blank(text: str) -> bool:
    return not collapse_ws(text)


def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in str(text or ""))


def fit(text: str, limit: int) -> str:
    """Truncate to a *display* width, not a codepoint count."""
    out, width = [], 0
    for ch in str(text or ""):
        w = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if width + w > limit:
            break
        out.append(ch)
        width += w
    return "".join(out)


def parse_hotkey(spec: str) -> list[str]:
    """'ctrl+shift+s' -> ['ctrl','shift','s']; order is modifier-then-key."""
    parts = [p.strip().lower() for p in str(spec or "").split("+") if p.strip()]
    if not parts:
        raise ValueError("Empty hotkey")
    return parts


def describe_hotkey(spec: str) -> str:
    pretty = {"ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt", "shift": "Shift",
              "win": "Win", "cmd": "Win", "meta": "Win", "esc": "Esc"}
    return "+".join(pretty.get(p, p.capitalize()) for p in parse_hotkey(spec))


_KEY_ALIASES = {
    "control": "ctrl", "cmd": "win", "meta": "win", "command": "win",
    "escape": "esc", "return": "enter", "del": "delete", "ins": "insert",
    "pgup": "pageup", "pgdn": "pagedown", "caps": "capslock",
    "num": "numlock", "scroll": "scrolllock", "spacebar": "space",
}


def normalize_key(token: str) -> str:
    token = str(token or "").strip().lower()
    if len(token) == 1:
        return token
    if token.startswith("{") and token.endswith("}"):
        token = token[1:-1]
    return _KEY_ALIASES.get(token, token)


def action_signature(action: str, target: str, args: str = "") -> str:
    """Stable key used to detect an agent looping on the same thing."""
    return "|".join((str(action).lower(), collapse_ws(target).lower(),
                     collapse_ws(args).lower()[:60]))


def bullets(items, marker: str = "-") -> str:
    return "\n".join(f"{marker} {i}" for i in items)


def numbered(items, start: int = 1) -> str:
    return "\n".join(f"[{i}] {v}" for i, v in enumerate(items, start))


def natural_join(items, conjunction: str = "and") -> str:
    items = [str(i) for i in items]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} {conjunction} {items[-1]}"


_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str, limit: int = 48) -> str:
    """A filesystem- and dict-key-safe form of `text`.

    Falls back to "task" rather than an empty string: an empty key would let
    two unrelated episodes collide on the same memory entry.
    """
    return _SLUG.sub("-", (text or "").lower()).strip("-")[:limit] or "task"
