"""Meetings and work tools: notes, follow-ups, tickets, timesheets, expenses.

There is no work tracker this agent can drive without credentials it should not
hold. Where the data lives in a local file, it is read and written here; where it
lives in a service, the handler says which service and opens it.
"""
from __future__ import annotations

import csv
import json
import time
from datetime import date
from pathlib import Path

from ..errors import ActionError
from ..logging_setup import get_logger

from . import filesystem, shell

log = get_logger(__name__)


def _store(name: str) -> Path:
    from ..config import active

    return Path(active().storage.home) / name


def _load(name: str, default: dict) -> dict:
    path = _store(name)
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _save(name: str, data: dict) -> None:
    path = _store(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# meetings
# --------------------------------------------------------------------------
def meeting_control(target: str = "", args: str = "") -> str:
    """Join, mute or leave a Teams or Zoom meeting.

    Both apps read their own state, and neither exposes a supported scriptable
    API, so this opens the app and gives the keystroke to do it.
    """
    app = target.strip().lower()
    verb = args.strip().lower()
    names = {"teams": "Microsoft Teams", "zoom": "Zoom"}
    if app not in names:
        return "say teams or zoom, then join, mute or leave"
    shell.run_powershell(f"Start-Process {names[app]}")
    return (f"opened {names[app]}. Joining and muting are done from its window; "
            f"use Win+Shift+A in Teams to mute.")


def meeting_notes(target: str = "", args: str = "") -> str:
    """Record notes for a meeting, with a timestamp."""
    entry = f"{target} {args}".strip()
    if not entry:
        return "give me the note"
    data = _load("meetings.json", {"notes": []})
    data["notes"].append({"at": time.strftime("%Y-%m-%d %H:%M"), "text": entry})
    _save("meetings.json", data)
    return f"note added at {data['notes'][-1]['at']}"


def meeting_followup(target: str = "", args: str = "") -> str:
    """Turn a note into a task."""
    from . import productivity

    item = f"{target} {args}".strip()
    if not item:
        return "give me the follow-up"
    productivity.todo_add(item)
    return f"follow-up added to the task list: {item}"


def meeting_list(target: str = "", args: str = "") -> str:
    data = _load("meetings.json", {"notes": []})
    notes = data["notes"]
    if not notes:
        return "no meeting notes yet"
    return "\n".join(f"[{n['at']}] {n['text']}" for n in notes[-20:])


# --------------------------------------------------------------------------
# work tracking
# --------------------------------------------------------------------------
def work_ticket(target: str = "", args: str = "") -> str:
    """Record a ticket locally. It does not reach Jira, Linear or GitHub."""
    body = f"{target} {args}".strip()
    if not body:
        return 'give me "ticket title" and the detail'
    data = _load("tickets.json", {"tickets": []})
    data["tickets"].append({
        "id": f"T-{len(data['tickets']) + 1}",
        "title": target.strip(), "detail": args.strip(),
        "opened": date.today().isoformat()})
    _save("tickets.json", data)
    entry = data["tickets"][-1]
    return f"{entry['id']}: {entry['title']} (local only, not in your tracker)"


def work_timesheet(target: str = "", args: str = "") -> str:
    """Log hours to a local CSV timesheet."""
    hours = 0.0
    for piece in f"{target} {args}".split():
        try:
            hours = float(piece)
            break
        except ValueError:
            continue
    if hours <= 0:
        return "give me the hours, e.g. timesheet 1.5"
    task = (target if not target.replace(".", "").isdigit() else args).strip()
    path = _store("timesheet.csv")
    new_file = not path.is_file()
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new_file:
            writer.writerow(["date", "task", "hours"])
        writer.writerow([date.today().isoformat(), task or "unspecified", hours])
    return f"logged {hours}h on {task or 'unspecified'} -> {path.name}"


def work_expense(target: str = "", args: str = "") -> str:
    """Log an expense to a local CSV."""
    amount = 0.0
    for piece in f"{target} {args}".split():
        try:
            amount = float(piece.lstrip("$"))
            break
        except ValueError:
            continue
    if amount <= 0:
        return "give me the amount, e.g. expense 12.50 lunch"
    what = f"{target} {args}".replace(f"{amount:g}", "").strip(" $")
    path = _store("expenses.csv")
    new_file = not path.is_file()
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new_file:
            writer.writerow(["date", "item", "amount"])
        writer.writerow([date.today().isoformat(), what or "unspecified", f"{amount:.2f}"])
    return f"logged {amount:.2f} for {what or 'unspecified'} -> {path.name}"


def crm_update(target: str = "", args: str = "") -> str:
    """CRM entries need a provider login this agent does not hold."""
    return ("a CRM update needs credentials for the CRM itself. I will not "
            "ask you to paste an API key here — set it in the vault with "
            "`ufo secrets set` and tell me which system, then I will use it.")


def work_report(target: str = "", args: str = "") -> str:
    """Summarise the local timesheet and expenses."""
    hours, expenses = 0.0, 0.0
    timesheet, ledger = _store("timesheet.csv"), _store("expenses.csv")
    if timesheet.is_file():
        for row in list(csv.reader(open(timesheet, encoding="utf-8")))[1:]:
            try:
                hours += float(row[2])
            except (IndexError, ValueError):
                continue
    if ledger.is_file():
        for row in list(csv.reader(open(ledger, encoding="utf-8")))[1:]:
            try:
                expenses += float(row[2])
            except (IndexError, ValueError):
                continue
    return (f"tracked: {hours:.1f} hour(s), {expenses:.2f} in expenses.\n"
            f"Files: {timesheet}, {ledger}")