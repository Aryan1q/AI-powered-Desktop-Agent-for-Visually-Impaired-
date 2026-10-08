"""Planning: today, this week, and scheduling a day out.

The plans are built from what the agent already knows — the calendar, the task
list and the scheduler — not invented. An empty plan says which sources were
empty, so the user can tell the difference between "nothing to do" and "I could
not see".
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

from ..errors import ActionError
from ..logging_setup import get_logger

from . import comms, productivity, shell

log = get_logger(__name__)


def _store_path() -> Path:
    from ..config import active

    return Path(active().storage.home) / "plans.json"


def _load() -> dict:
    path = _store_path()
    if not path.is_file():
        return {"days": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"days": {}}


def _save(data: dict) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _slot(date_string: str, item: str) -> str:
    """Add an item to a day. `item` may carry a '09:00' prefix."""
    data = _load()
    entry = data["days"].setdefault(date_string, [])
    if item not in entry:
        entry.append(item)
    _save(data)
    return f"planned for {date_string}: {item}"


def _report(date_string: str) -> str:
    data = _load()
    items = data["days"].get(date_string, [])
    if not items:
        return (f"nothing planned for {date_string}. "
                f"Say 'plan <thing> for <date>' to add to it.")
    return "\n".join([f"{date_string}:"] + [f"  {i}" for i in items])


def day(target: str = "", args: str = "") -> str:
    """Plan or show a single day."""
    today = date.today().isoformat()
    if not target.strip():
        return _report(today)
    item = f"{target} {args}".strip()
    day_string = today
    for piece in item.split():
        if piece.count("-") == 2:
            day_string = piece
            item = item.replace(piece, "").strip()
    if not item:
        return _report(day_string)
    return _slot(day_string, item)


def week(target: str = "", args: str = "") -> str:
    """Show the next seven days of plans."""
    today = date.today()
    data = _load()
    lines = []
    for offset in range(7):
        current = today + timedelta(days=offset)
        key = current.isoformat()
        items = data["days"].get(key, [])
        marker = " (today)" if offset == 0 else ""
        lines.append(f"{key}{marker}: " + (", ".join(items) if items else "—"))
    return "\n".join(lines)


def agenda(target: str = "", args: str = "") -> str:
    """Meetings today, from the calendar."""
    return comms.calendar_list(target, args)


def priorities(target: str = "", args: str = "") -> str:
    """Order the task list by priority word: urgent, important, then the rest."""
    todos = productivity.todo_list()
    if not target.strip():
        return todos
    item = f"{target} {args}".strip()
    return productivity.todo_add(item)


def review(target: str = "", args: str = "") -> str:
    """A daily review: what is planned, what is due, what is overdue."""
    data = _load()
    today = date.today().isoformat()
    upcoming = []
    for key, items in sorted(data["days"].items()):
        if key >= today and items:
            upcoming.append(f"{key}: " + ", ".join(items))
    from ..core.scheduler import get_scheduler

    due = [entry.describe() for entry in get_scheduler().list()]
    return "\n\n".join([
        f"Today: {_report(today)}",
        "\nUpcoming:\n" + ("\n".join(upcoming) or "nothing planned ahead"),
        "\nScheduled:\n" + ("\n".join(due) or "nothing scheduled"),
    ])


def time_block(target: str = "", args: str = "") -> str:
    """Reserve a time in the calendar, e.g. 'deep work 09:00 90'."""
    subject = target.strip()
    rest = args.split()
    when = rest[0] if rest else ""
    minutes = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else 60
    if not subject or not when:
        return 'give me "deep work" and a start time, e.g. "deep work 09:00 90"'
    entry = comms.calendar_add(subject, when)
    return f"{entry} ({minutes} minutes)"