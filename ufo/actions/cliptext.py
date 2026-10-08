"""Clipboard and text tools: read, write, clear, transform, count.

Backs "what is on my clipboard", "clear my clipboard" and "uppercase this".
Text work runs on whatever is on the clipboard unless a target is given, which is
what makes it usable one-handed.
"""
from __future__ import annotations

import json
import re

from ..errors import ActionError
from ..logging_setup import get_logger

from . import keyboard

log = get_logger(__name__)


def get(target: str = "", args: str = "") -> str:
    return keyboard.get_clipboard() or "(the clipboard is empty)"


def set_clip(target: str = "", args: str = "") -> str:
    """Put text on the clipboard. target is the text, args is a fallback."""
    text = target or args
    if not text:
        return "give me the text to copy"
    keyboard.set_clipboard(text)
    return f"copied {len(text)} character(s)"


def clear(target: str = "", args: str = "") -> str:
    keyboard.set_clipboard("")
    return "clipboard cleared"


def history(target: str = "", args: str = "") -> str:
    """Windows does not expose clipboard history without a running helper."""
    return ("Windows does not keep a clipboard history by default. Enable it in "
            "Settings > System > Clipboard, or turn on Win+V.")


def change_case(target: str = "", args: str = "") -> str:
    text = target or keyboard.get_clipboard()
    if not text:
        return "the clipboard is empty"
    wanted = args.strip().lower() or "upper"
    transforms = {
        "upper": str.upper, "uppercase": str.upper, "up": str.upper,
        "lower": str.lower, "lowercase": str.lower, "down": str.lower,
        "title": str.title, "capitalise": str.title, "capitalize": str.title,
        "sentence": _sentence, "reverse": lambda s: s[::-1],
    }
    fn = transforms.get(wanted)
    if fn is None:
        return f"say one of: {', '.join(sorted(set(transforms)))}"
    result = fn(text)
    keyboard.set_clipboard(result)
    return result


def _sentence(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return ""
    return cleaned[0].upper() + cleaned[1:].lower() + "."


def count(target: str = "", args: str = "") -> str:
    """Word, character, line or sentence counts for the clipboard or a target."""
    text = target if target and "\n" not in target else args
    if not text or len(text.split()) <= 1:
        text = keyboard.get_clipboard()
    if not text:
        return "the clipboard is empty"
    return (f"{len(text.split())} words, {len(text)} characters, "
            f"{text.count(chr(10)) + 1} lines, "
            f"{len(re.findall(r'[.!?]+', text))} sentences")


def transform(target: str = "", args: str = "") -> str:
    """Sort, deduplicate or unique-line a list of lines."""
    text = args if len(args.split()) > 1 else keyboard.get_clipboard()
    if not text:
        return "the clipboard is empty"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    verb = target.strip().lower()
    if verb in ("sort", "unique", "dedupe", "deduplicate"):
        seen, out = set(), []
        for line in lines:
            if verb == "sort":
                out.append(line)
            elif line.casefold() not in seen:
                seen.add(line.casefold())
                out.append(line)
        result = sorted(out, key=str.casefold) if verb == "sort" else out
    elif verb in ("strip", "trim"):
        result = [line for line in lines]
    else:
        return "say sort, unique or strip"
    text_out = "\n".join(result)
    keyboard.set_clipboard(text_out)
    return text_out


def replace(target: str = "", args: str = "") -> str:
    """Literal find and replace across the clipboard. target='old|new'."""
    old, _, new = target.partition("|")
    if not old:
        return "give me 'find|replace'"
    text = args if len(args.split()) > 1 else keyboard.get_clipboard()
    if not text:
        return "the clipboard is empty"
    result = text.replace(old, new)
    keyboard.set_clipboard(result)
    count = text.count(old)
    return f"replaced {count} occurrence(s)" if count else "no match found"


def format_json(target: str = "", args: str = "") -> str:
    """Pretty-print or compact JSON from the clipboard."""
    text = args if len(args.split()) > 1 else keyboard.get_clipboard()
    if not text:
        return "the clipboard is empty"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return f"that is not valid JSON: {e.msg} at line {e.lineno}"
    indent = None if "compact" in target.lower() else 2
    result = json.dumps(data, indent=indent, ensure_ascii=False, sort_keys=True)
    keyboard.set_clipboard(result)
    return result
