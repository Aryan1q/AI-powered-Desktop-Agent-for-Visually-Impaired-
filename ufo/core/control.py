"""Category 44: the user driving the agent, not the other way round.

Four verbs here are not "do a thing on the machine" — they act on the agent
itself, and they are easy to fake:

* **pause / resume** must actually gate execution, not print a message. A pause
  that does not stop anything is worse than no pause, because the user believes
  the agent has stopped.
* **undo / redo** must walk a real journal of what the agent changed. An undo
  that cannot name what it is reversing has to say so rather than guess, because
  guessing here means touching the wrong file.

The journal is append-only and lives next to the rest of the agent's state, so
it survives a restart: undoing the last action still works in a new process.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger(__name__)

#: How many steps the journal keeps. Undo is a "just now" affordance, not an
#: audit log; `history` covers the long range.
JOURNAL_LIMIT = 200


class Paused(Exception):
    """Raised when an action is attempted while the agent is paused."""


class Control:
    """Pause state and the undo journal.

    One process-wide instance, like the emergency switch and the guard: a second
    agent in the same process must not be able to unpause the first.
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._paused = False
        self._pause_reason = ""
        self._journal: list[dict] = []
        self._load()

    # -- pause ------------------------------------------------------------
    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def pause_reason(self) -> str:
        return self._pause_reason

    def pause(self, reason: str = "") -> str:
        """Stop executing. Does not clear the emergency switch."""
        with self._lock:
            self._paused = True
            self._pause_reason = str(reason or "").strip() or "paused by request"
            self._save()
        return f"paused ({self._pause_reason}). Nothing further runs until you resume."

    def resume(self) -> str:
        """Start executing again.

        This does not clear a tripped emergency switch. The hotkey stop and a
        spoken "pause" are different things, and a user who hit the stop button
        must clear that deliberately.
        """
        from ..safety.emergency import active_reason, emergency

        with self._lock:
            was = self._paused
            self._paused = False
            reason = self._pause_reason
            self._pause_reason = ""
            self._save()
        if not was:
            return "not paused"
        if emergency.tripped:
            return (f"resumed, but the emergency stop is still tripped "
                    f"({active_reason()}). Clear it with: reset the stop")
        return f"resumed{f' from {reason}' if reason else ''}"

    def check(self) -> None:
        """Refuse to act while paused. Called from the executor, not the CLI."""
        if self._paused:
            raise Paused(f"the agent is paused ({self._pause_reason})")

    # -- undo journal -----------------------------------------------------
    def record(self, capability: str, target: str = "", args: str = "",
               undo: str = "", reversible: bool = True) -> None:
        """Note an action, so `undo` can speak about it later.

        Both kinds of change are recorded. `reversible=False` means the step
        changed the machine but nothing here knows how to put it back, and `undo`
        needs that fact: if the entry were dropped instead, undo would reach past
        it and reverse the step *before* a deletion, which is exactly the sort of
        confident wrong answer this journal is meant to prevent.
        """
        with self._lock:
            self._journal.append({
                "capability": capability,
                "target": target,
                "args": args,
                "undo": undo,
                "reversible": bool(reversible),
            })
            del self._journal[:-JOURNAL_LIMIT]
            self._save()

    def can_undo(self) -> bool:
        return bool(self._journal)

    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    @property
    def redo_depth(self) -> int:
        return len(self._redo_stack)

    @property
    def undo_depth(self) -> int:
        return len(self._journal)

    def peek_undo(self) -> dict | None:
        with self._lock:
            return dict(self._journal[-1]) if self._journal else None

    def peek_redo(self) -> dict | None:
        with self._lock:
            return dict(self._redo_stack[-1]) if self._redo_stack else None

    def pop_undo(self) -> dict | None:
        """Take the newest entry off the undo stack onto the redo stack."""
        with self._lock:
            if not self._journal:
                return None
            entry = self._journal.pop()
            self._redo_stack.append(entry)
            del self._redo_stack[:-JOURNAL_LIMIT]
            self._save()
            return entry

    def pop_redo(self) -> dict | None:
        with self._lock:
            if not self._redo_stack:
                return None
            entry = self._redo_stack.pop()
            self._journal.append(entry)
            self._save()
            return entry

    def journal(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self._journal[-limit:]]

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._redo_stack: list[dict] = []
            return
        except (OSError, ValueError) as e:
            log.debug("could not read the control journal: %s", e)
            self._redo_stack = []
            return
        self._paused = bool(data.get("paused"))
        self._pause_reason = str(data.get("pause_reason", ""))
        self._journal = [e for e in data.get("journal", []) if isinstance(e, dict)]
        self._redo_stack = [e for e in data.get("redo", []) if isinstance(e, dict)]

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({
                "paused": self._paused,
                "pause_reason": self._pause_reason,
                "journal": self._journal,
                "redo": self._redo_stack,
            }, indent=2), encoding="utf-8")
        except OSError as e:
            # Losing the journal is survivable; refusing to pause is not, so the
            # in-memory state stands even when it cannot be written down.
            log.debug("could not write the control journal: %s", e)


_control: Control | None = None


def get_control() -> Control:
    """The process-wide pause state and undo journal."""
    global _control
    if _control is None:
        from ..config import active

        path = Path(active().storage.home) / "control.json"
        _control = Control(path)
    return _control


def reset_for_tests(path: Path | None = None) -> Control:
    """Replace the process-wide control state. For tests."""
    global _control
    if path is None:
        from ..config import active

        path = Path(active().storage.home) / "control.json"
    _control = Control(path)
    return _control


__all__ = ["Control", "Paused", "get_control", "reset_for_tests", "JOURNAL_LIMIT"]
