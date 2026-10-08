"""Actions: everything the agent is permitted to do to this machine.

Every function here is a *primitive*. None of them consult the safety policy —
that is the guard's job (see safety.guard.ActionGuard). Keeping the two apart
means the policy can be tested exhaustively without a desktop session, and
these functions can be unit-tested with a fake screen.
"""
from __future__ import annotations

from . import filesystem, gui, keyboard, mouse, network, process, shell, system, window

__all__ = [
    "filesystem",
    "gui",
    "keyboard",
    "mouse",
    "network",
    "process",
    "shell",
    "system",
    "window",
]
