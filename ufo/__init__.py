"""UFO — a whole-laptop automation agent for Windows.

The package is layered so each concern can be tested and replaced alone:

    config / logging   environment + persistence plumbing
    safety             policy, confirmation gate, redaction, global abort
    perception         what is on screen: UIA tree, pixels, OCR, change detection
    actions            everything the agent may do to the machine
    agent              observe -> reason -> plan -> act -> verify loops
    core               task model, memory, queue, scheduler, daemon
    interface          CLI, HTTP API, local dashboard, OS notifications
    voice              TTS, STT, global hotkeys, audible feedback

Importing this package loads any `.env` so that keys are in `os.environ` before
a config or LLM client reads them. A real environment variable always wins.
"""

__version__ = "2.0.0"
__all__ = ["__version__"]

from . import env as _env  # noqa: E402  (must run before anything reads a key)

_env.load_env()

