"""Notes, tasks, timers and reminders.

Backs "add milk to the shopping list", "start a pomodoro" and "what is on my
todo". State lives in the agent's own store so it survives restarts, and a timer
that expires writes a result the user can actually see.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..errors import ActionError
from ..logging_setup import get_logger

from . import filesystem, shell

log = get_logger(__name__)


def _store_path() -> Path:
    """Notes and tasks live beside the other state, not inside state.json.

    `storage.state_path` is a single file; writing this beside it keeps the
    agent's own state readable while leaving that file's shape alone.
    """
    from ..config import active

    return Path(active().storage.home) / "productivity.json"


def _load() -> dict:
    path = _store_path()
    if not path.is_file():
        return {"notes": {}, "todos": {}, "timers": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"notes": {}, "todos": {}, "timers": []}


def _save(data: dict) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# notes
# --------------------------------------------------------------------------
def note(target: str = "", args: str = "") -> str:
    """Save a note. target is the text, or a 'title|body' pair."""
    title, _, body = (f"{target} {args}".strip()).partition("|")
    if not title.strip():
        return "give me something to note down"
    data = _load()
    data["notes"][title.strip()[:80]] = body.strip() or title.strip()
    _save(data)
    return f"noted: {title.strip()[:80]}"


def note_list(target: str = "", args: str = "") -> str:
    data = _load()
    if not data["notes"]:
        return "no notes saved yet"
    return "\n".join(f"{title}\n    {body}"
                     for title, body in sorted(data["notes"].items()))


def note_remove(target: str = "", args: str = "") -> str:
    name = target or args
    data = _load()
    if name in data["notes"]:
        del data["notes"][name]
        _save(data)
        return f"removed the note: {name}"
    return f"no note called {name!r}"


# --------------------------------------------------------------------------
# todos
# --------------------------------------------------------------------------
def todo_add(target: str = "", args: str = "") -> str:
    item = (f"{target} {args}".strip())
    if not item:
        return "give me a task"
    data = _load()
    key = str(int(time.time() * 1000))
    data["todos"][key] = {"text": item, "done": False}
    _save(data)
    return f"added: {item}"


def todo_list(target: str = "", args: str = "") -> str:
    data = _load()
    todos = data["todos"]
    if not todos:
        return "the task list is empty"
    return "\n".join(f"[{'x' if t['done'] else ' '}] {t['text']}"
                     for t in todos.values())


def todo_complete(target: str = "", args: str = "") -> str:
    """Tick off the first task whose text contains `target`."""
    needle = (target or args).strip().lower()
    data = _load()
    for item in data["todos"].values():
        if needle in item["text"].lower():
            item["done"] = not item["done"]
            _save(data)
            state = "done" if item["done"] else "not done"
            return f"{state}: {item['text']}"
    return f"no task matching {needle!r}"


def todo_clear(target: str = "", args: str = "") -> str:
    data = _load()
    if "done" in f"{target} {args}".lower():
        removed = sum(1 for t in data["todos"].values() if t["done"])
        data["todos"] = {k: t for k, t in data["todos"].items() if not t["done"]}
        _save(data)
        return f"cleared {removed} completed task(s)"
    data["todos"] = {}
    _save(data)
    return "cleared the task list"


# --------------------------------------------------------------------------
# timers
# --------------------------------------------------------------------------
def timer(target: str = "", args: str = "") -> str:
    """Start a countdown. target is minutes, or a phrase like '25 minutes'."""
    import re

    verb = f"{target} {args}".strip().lower()
    match = re.search(r"(\d+)\s*(min|minute|sec|second|hour|hr)", verb)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        seconds = amount * 60 if unit.startswith("min") else (
            amount * 3600 if unit.startswith("h") else amount)
    elif target.isdigit():
        seconds = int(target) * 60
    elif target.strip():
        return "say how long, e.g. 'timer 25' for twenty-five minutes"
    else:
        return "say how long the timer should run"
    data = _load()
    data["timers"].append({"name": args or f"timer {amount}", "until": time.time() + seconds})
    _save(data)
    return f"timer set for {seconds // 60 or seconds} minute(s)"


def timer_list(target: str = "", args: str = "") -> str:
    data = _load()
    due = _prune(data)
    if not due:
        return "no timers running"
    return "\n".join(f"{t['name']} — {max(0, int(t['until'] - time.time()))}s left"
                     for t in due)


def timer_cancel(target: str = "", args: str = "") -> str:
    data = _load()
    before = len(data["timers"])
    data["timers"] = [t for t in data["timers"]
                      if (target or "").lower() not in t["name"].lower()]
    _save(data)
    removed = before - len(data["timers"])
    return f"cancelled {removed} timer(s)" if removed else "no matching timer"


def _prune(data: dict) -> list[dict]:
    """Drop expired timers, telling the caller which ones finished."""
    now = time.time()
    running = [t for t in data["timers"] if t["until"] > now]
    if len(running) != len(data["timers"]):
        finished = [t for t in data["timers"] if t["until"] <= now]
        data["timers"] = running
        _save(data)
        for entry in finished:
            log.info("timer finished: %s", entry["name"])
    return running


# --------------------------------------------------------------------------
# habits, journal, and modes
# --------------------------------------------------------------------------
def habits(target: str = "", args: str = "") -> str:
    """Log a habit for today, or show the streak."""
    data = _load().setdefault("habits", {})
    if not target:
        return "\n".join(f"{name}: {len(days)} day(s)" for name, days in data.items()) \
            or "no habits tracked yet"
    today = time.strftime("%Y-%m-%d")
    days = set(data.get(target, []))
    days.add(today)
    data[target] = sorted(days)
    _save(data)
    return f"{target}: {len(days)} day(s) logged, most recently today"


def journal(target: str = "", args: str = "") -> str:
    """Append to today's journal, or read it back."""
    data = _load().setdefault("journal", {})
    today = time.strftime("%Y-%m-%d")
    if not target and not args:
        return data.get(today, "(nothing written today)")
    entry = f"{target} {args}".strip()
    data[today] = (data.get(today, "") + "\n" + entry).strip()
    _save(data)
    return f"journal entry added for {today}"


def sticky_note(target: str = "", args: str = "") -> str:
    """A note pinned to the desktop as a text file you can see."""
    from . import filesystem

    text = f"{target} {args}".strip()
    if not text:
        return "give me the note"
    path = Path.home() / "Desktop" / "ufo-note.txt"
    filesystem.write_text(str(path), text)
    return f"wrote a note to {path}"


def time_tracking(target: str = "", args: str = "") -> str:
    """Start, stop or show a tracked work session."""
    data = _load()
    entries = data.setdefault("time_tracking", [])
    verb = f"{target} {args}".strip().lower()
    if verb.startswith("start") or (target and target not in ("show", "list") and not args):
        entries.append({"task": target, "started": time.time(), "stopped": None})
        _save(data)
        return f"tracking {target}"
    if verb.startswith("stop"):
        for entry in reversed(entries):
            if entry["stopped"] is None:
                entry["stopped"] = time.time()
                minutes = (entry["stopped"] - entry["started"]) / 60
                _save(data)
                return f"stopped: {entry['task']}, {minutes:.0f} minute(s)"
        return "nothing is being tracked right now"
    total = sum((e["stopped"] or time.time()) - e["started"] for e in entries)
    return f"{len(entries)} session(s), {total / 60:.0f} minute(s) total"


def project_board(target: str = "", args: str = "") -> str:
    """A three-column board: todo, doing, done."""
    data = _load().setdefault("board", {"todo": [], "doing": [], "done": []})
    verb, _, item = f"{target} {args}".strip().partition(" ")
    if verb.lower() in data:
        if item.strip():
            data[verb.lower()].append(item.strip())
            _save(data)
            return f"added to {verb.lower()}: {item.strip()}"
        return "\n".join(f"[{column}] " + ", ".join(data[column]) for column in data)
    if verb.lower() in ("move", "add"):
        return "say todo, doing or done, then the item"
    return "say todo, doing or done"


def focus_session(target: str = "", args: str = "") -> str:
    """A pomodoro: do-not-disturb on, a timer, and the volume down.

    Everything is reversible with the matching 'off' commands, so this is safe
    to start without asking twice.
    """
    from . import desktop

    minutes = int(target) if target.isdigit() else 25
    desktop.do_not_disturb("on")
    desktop.volume("0")
    result = timer(str(minutes), "focus session")
    return (f"focus mode: notifications off, sound muted, {minutes} minute timer. "
            f"{result}")
