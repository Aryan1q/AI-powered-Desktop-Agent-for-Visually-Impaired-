"""Resolving a capability name to real code, and running it.

The catalog stores ``"module:function"`` strings. This module turns one into a
callable, caches the result, and executes it. Three things matter here:

* **Laziness.** `import ufo.actions.devtools` pulls in nothing heavy, but the
  point stands: nothing is imported until a capability is actually used, so a
  broken optional dependency in one domain cannot take the rest down.
* **Honesty about missing pieces.** When an action needs `winget`, `ffmpeg` or
  `python-docx` and it is not installed, the error names the package to install
  instead of raising `NameError: ffmpeg` from three frames down.
* **The spec syntax.** ``capability:argument`` — the thing a person actually
  types, and what `nlu` produces from a sentence — is parsed into the
  ``(target, args)`` pair every handler already takes.
"""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from typing import Any, Callable

from ..errors import ActionError
from ..logging_setup import get_logger, record
from . import catalog
from .catalog import Capability

log = get_logger(__name__)

#: name -> callable(target, args) -> str | object
_HANDLERS: dict[str, Callable[..., Any]] = {}

#: package name -> the command that proves it is installed
_PROBES: dict[str, tuple[str, ...]] = {
    "winget": ("winget",),
    "ffmpeg": ("ffmpeg",),
    "soffice": ("soffice", "libreoffice"),
    "docker": ("docker",),
    "wsl": ("wsl",),
    "obs": ("obs64", "obs"),
    "rembg": (),          # a Python package, checked by import
    "Pillow": (),
    "matplotlib": (),
    "openpyxl": (),
    "python-docx": ("docx",),
    "python-pptx": ("pptx",),
    "pypdf": (),
    "reportlab": (),
    "faster-whisper": (),
    "speechrecognition": ("SpeechRecognition",),
    "WIA": (),
    "sqlite3": (),
    "sympy": (),
    "Outlook COM": (),
    "rclone": ("rclone",),
}


@dataclass
class Resolution:
    """A capability plus the concrete function that implements it."""

    capability: Capability
    function: Callable[..., Any]

    def __call__(self, target: str = "", args: str = "") -> Any:
        return invoke(self.capability, target, args, self.function)


def resolve(name: str) -> Resolution | None:
    """Look up a capability and its implementation, or None."""
    cap = catalog.get(name)
    if cap is None:
        return None
    fn = _load(cap)
    return Resolution(cap, fn)


def _load(cap: Capability) -> Callable[..., Any]:
    cached = _HANDLERS.get(cap.name)
    if cached is not None:
        return cached
    module_name, _, func_name = cap.handler.partition(":")
    module_name = module_name if module_name.startswith("ufo.") else f"ufo.actions.{module_name}"
    try:
        module = importlib.import_module(module_name)
    except Exception as e:  # ImportError, or a module that fails at import time
        raise ActionError(
            f"The {cap.name!r} capability needs {module_name}, which could not be "
            f"loaded: {type(e).__name__}: {e}"
        ) from e
    fn = getattr(module, func_name, None)
    if fn is None or not callable(fn):
        raise ActionError(
            f"{cap.name} is declared as {cap.handler} but that function does "
            "not exist. The catalog and the code are out of step."
        )
    _HANDLERS[cap.name] = fn
    return fn


# --------------------------------------------------------------------------
# argument shapes
# --------------------------------------------------------------------------
_SPEC = re.compile(r"^\s*([a-z0-9_]+)\s*(?::|=)\s*(.*)$", re.IGNORECASE | re.DOTALL)


def split_spec(text: str, default_capability: str = "") -> tuple[str, str, str]:
    """Parse ``cap:target:args`` into (capability, target, args).

    Only the *first* colon is a capability separator, so
    ``web_open:https://x.com`` keeps its URL intact, and a handler that wants
    two arguments asks the caller for ``cap:target|args``.
    """
    raw = str(text or "").strip()
    if not raw:
        return default_capability, "", ""
    capability, target, args = default_capability, "", ""
    if "|" in raw:
        raw, _, args = raw.partition("|")
        args = args.strip()
    if default_capability:
        capability = default_capability
    else:
        head, sep, rest = raw.partition(":")
        if sep and catalog.get(head):
            capability, raw = head.strip(), rest.strip()
    if "::" in raw:
        raw, _, more = raw.partition("::")
        args = f"{more.strip()} {args}".strip()
    target = raw.strip()
    return capability, target, args


def _kwargs_for(cap: Capability, target: str, args: str) -> dict:
    """Most handlers are written as ``fn(target, args)``."""
    return {}


def invoke(cap: Capability, target: str = "", args: str = "",
           function: Callable[..., Any] | None = None) -> Any:
    """Run one capability, adapting to the handler's real signature.

    Handlers are written as ``def thing(target="", args="")`` so the whole
    catalog shares one calling convention, but a handler that wants named
    parameters gets them: ``fn(target=..., args=..., extra=...)`` where the
    extra values come from ``args`` split on ``;`` when the signature asks for
    them. A handler that takes no arguments is called with none.
    """
    import inspect

    fn = function or _load(cap)
    signature = inspect.signature(fn)
    params = signature.parameters
    accepts = lambda *names: [n for n in names if n in params]  # noqa: E731

    target = "" if target is None else str(target)
    args = "" if args is None else str(args)
    extra_names = [n for n in params
                   if n not in ("target", "args") and n not in ("self",)]
    kwargs: dict[str, Any] = {}
    named = accepts("target")
    if named:
        kwargs["target"] = target
    if accepts("args"):
        kwargs["args"] = args
    if extra_names:
        pieces = [p.strip() for p in args.split(";")] if args else []
        for index, name in enumerate(extra_names):
            value = pieces[index] if index < len(pieces) else ""
            if name in ("params", "kwargs") and value:
                try:
                    import json

                    kwargs[name] = json.loads(value)
                    continue
                except Exception:
                    kwargs[name] = value
                    continue
            kwargs[name] = value
    record("capability", action=cap.name, category=cap.category,
           target=target[:120], risk=cap.risk)
    return fn(**kwargs)


# --------------------------------------------------------------------------
# optional dependencies
# --------------------------------------------------------------------------
def missing_dependency(need: str) -> str:
    """The install line for an optional piece, or "" when it is present."""
    if not need:
        return ""
    for item in str(need).replace("|", ";").split(";"):
        item = item.strip()
        if not item:
            continue
        if not _probe(item):
            return item
    return ""


def _probe(need: str) -> bool:
    import shutil

    python_module = {
        "Pillow": "PIL", "python-docx": "docx", "python-pptx": "pptx",
        "pypdf": "pypdf", "reportlab": "reportlab", "matplotlib": "matplotlib",
        "openpyxl": "openpyxl", "rembg": "rembg", "sqlite3": "sqlite3",
        "sympy": "sympy", "faster-whisper": "faster_whisper",
        "speechrecognition": "speech_recognition", "sounddevice": "sounddevice",
        "pyaudio": "pyaudio", "pyttsx3": "pyttsx3", "psutil": "psutil",
        "wmi": "wmi", "wia": "win32com",
    }
    module_name = python_module.get(need.lower(), need)
    try:
        importlib.import_module(module_name)
        return True
    except Exception:
        pass
    for command in _PROBES.get(need, (need,)):
        if shutil.which(command):
            return True
    return False


def install_hint(need: str) -> str:
    """What the user should type to get a missing piece installed."""
    if not need:
        return ""
    for item in str(need).replace("|", ";").split(";"):
        item = item.strip()
        if not item or _probe(item):
            continue
        if item in _PROBES:
            if item == "winget":
                return ("winget is not installed. Install App Installer from "
                        "the Microsoft Store, or get it from "
                        "https://aka.ms/getwinget")
            if item == "ffmpeg":
                return ("ffmpeg is not on PATH. Install it with "
                        "`winget install Gyan.FFmpeg` or from "
                        "https://ffmpeg.org/download.html")
            if item == "soffice":
                return ("LibreOffice is not installed. "
                        "`winget install TheDocumentFoundation.LibreOffice` "
                        "gives document conversion.")
            if item == "rclone":
                return "rclone is not on PATH. See https://rclone.org/install/"
            if item == "WIA":
                return ("Windows Image Acquisition is unavailable. Scanning "
                        "needs a WIA-capable scanner and `pywin32`.")
            if item == "obs":
                return ("OBS is not installed. `winget install obsproject."
                        "obs-studio` if you want recording or streaming.")
            if item == "Outlook COM":
                return ("Outlook is not installed, so mail cannot be read "
                        "through COM. Set up an IMAP account instead.")
            return f"{item} is not installed."
        return f"`pip install {item}`"
    return ""


def require(need: str) -> str:
    """Raise a clear error when a capability's dependency is missing."""
    hint = install_hint(need)
    if hint:
        raise ActionError(hint)
    return ""


# --------------------------------------------------------------------------
# convenience used by the CLI, the agent and the phone page
# --------------------------------------------------------------------------
def run(name: str, target: str = "", args: str = "") -> str:
    """Resolve and run in one step. Returns a printable string."""
    resolution = resolve(name)
    if resolution is None:
        raise ActionError(
            f"Unknown capability {name!r}. "
            f"Try `ufo caps` to see the {len(catalog.ALL)} available."
        )
    result = resolution(target, args)
    return result if isinstance(result, str) else str(result)


def is_known(name: str) -> bool:
    return catalog.get(name) is not None


def catalogue_lines(category: int | None = None) -> list[str]:
    caps = (catalog.by_category(category) if category is not None
            else list(catalog.ALL))
    return [
        f"{cap.name:<22} [{cat_short(cap.category):>4}] {cap.summary}"
        + ("  (confirms first)" if cap.confirm else "")
        for cap in sorted(caps, key=lambda c: (c.category, c.name))
    ]


def cat_short(number: int) -> str:
    return str(number)
