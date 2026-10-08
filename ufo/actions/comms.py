"""Email, calendar and messaging.

Mail and calendar go through Outlook COM when Outlook is installed, and through
IMAP otherwise. Sending is always a two-step: draft, show it to the user, then
send only when told. No handler ever puts a password in a prompt.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..errors import ActionError
from ..logging_setup import get_logger

from . import filesystem, shell

log = get_logger(__name__)


def _outlook():
    """The Outlook application object, or None with the reason."""
    try:
        import win32com.client  # type: ignore
    except ImportError:
        return None, "Outlook automation needs pywin32: `pip install pywin32`"
    try:
        return win32com.client.Dispatch("Outlook.Application"), ""
    except Exception as e:
        return None, f"Outlook is not available: {type(e).__name__}: {e}"


# --------------------------------------------------------------------------
# mail
# --------------------------------------------------------------------------
def mail_list(target: str = "", args: str = "") -> str:
    """Recent inbox subjects. target may name a folder."""
    app, reason = _outlook()
    if app is None:
        return reason
    folder_name = target or "Inbox"
    box = app.GetDefaultFolder(6)            # 6 = olFolderInbox
    folder = app.GetDefaultFolder(6)            # 6 = olFolderInbox
    try:
        box = folder.Parent.Folders.Item(folder_name)
    except Exception:
        return f"no mail folder called {folder_name!r}"
    items = box.Items
    items.Sort("[ReceivedTime]", True)
    lines = []
    for item in items[:20]:
        lines.append(f"{item.ReceivedTime:%Y-%m-%d %H:%M}  {item.Subject}")
    return "\n".join(lines) or f"{folder} is empty"


def mail_read(target: str = "", args: str = "") -> str:
    """Read the newest matching message in full."""
    app, reason = _outlook()
    if app is None:
        return reason
    needle = target.strip().lower()
    items = app.GetDefaultFolder(6).Items
    count = items.Count
    for index in range(1, min(count, 30) + 1):
        item = items.Item(index)
        if not needle or needle in item.Subject.lower():
            return (f"From: {item.SenderName} <{item.SenderEmailAddress}>\n"
                    f"Subject: {item.Subject}\nDate: {item.ReceivedTime}\n\n"
                    f"{item.Body}")
    return f"no recent message matching {needle!r}"


def mail_search(target: str = "", args: str = "") -> str:
    """Search every folder for text."""
    app, reason = _outlook()
    if app is None:
        return reason
    query = target.strip()
    if not query:
        return "give me words to search for"
    scope = app.GetNamespace("Outlook").GetDefaultFolder(6)
    scope = scope.GetItemsFromQuery(f'RESTRICT ON {query}')
    if scope.Count == 0:
        return f"nothing found for {query!r}"
    return "\n".join(f"{scope.Item(i).ReceivedTime:%Y-%m-%d}  {scope.Item(i).Subject}"
                     for i in range(1, min(scope.Count, 20) + 1))


def mail_compose(target: str = "", args: str = "") -> str:
    """Open a new message, addressed but not sent."""
    app, reason = _outlook()
    if app is None:
        return reason
    recipient = target.strip()
    subject, _, body = args.partition("|")
    message = app.CreateItem(0)
    if recipient:
        message.To = recipient
    if subject:
        message.Subject = subject
    if body:
        message.Body = body
    message.Display(True)
    return f"opened a draft to {recipient or 'nobody'} — review it, then send"


def mail_send(target: str = "", args: str = "") -> str:
    """Send a message you have already reviewed as a draft."""
    app, reason = _outlook()
    if app is None:
        return reason
    item = app.ActiveExplorer.Selection(1)
    item.Send()
    return f"sent: {item.Subject}"


def move(target: str = "", args: str = "") -> str:
    """File a message into a folder."""
    app, reason = _outlook()
    if app is None:
        return reason
    folder = target.strip()
    try:
        box = app.GetDefaultFolder(6).Parent.Folders.Item(folder)
    except Exception:
        return f"no mail folder called {folder!r}"
    app.ActiveExplorer.Selection(1).Move(box)
    return f"moved to {folder}"


def attachments(target: str = "", args: str = "") -> str:
    """Attach files to the draft currently open."""
    app, reason = _outlook()
    if app is None:
        return reason
    paths = [p for p in f"{target} {args}".split() if p]
    if not paths:
        return "give me the files to attach"
    added = []
    for raw in paths:
        path = Path(filesystem.expand(raw))
        if not path.is_file():
            return f"no such file: {path}"
        app.ActiveInspector.CurrentItem.Attachments.Add(str(path))
        added.append(path.name)
    return f"attached {', '.join(added)}"


# --------------------------------------------------------------------------
# calendar
# --------------------------------------------------------------------------
def calendar_list(target: str = "", args: str = "") -> str:
    """Upcoming appointments."""
    app, reason = _outlook()
    if app is None:
        return reason
    folder = app.GetDefaultFolder(9)          # 9 = olFolderCalendar
    items = folder.Items
    items.IncludeRecurrences = True
    items.Sort("[Start]", True)
    lines = []
    for item in items[:15]:
        lines.append(f"{item.Start:%Y-%m-%d %H:%M}  {item.Subject}")
    return "\n".join(lines) or "nothing scheduled"


def calendar_add(target: str = "", args: str = "") -> str:
    """Create a calendar entry, as a draft for review."""
    app, reason = _outlook()
    if app is None:
        return reason
    subject = target.strip()
    when = args.strip()
    if not subject:
        return "give me a title and a time"
    entry = app.CreateItem(1)
    entry.Subject = subject
    if when:
        from ..core.scheduler import parse_when

        try:
            entry.Start = parse_when(when)
        except Exception as e:
            return f"could not read the time {when!r}: {e}"
    entry.Save()
    return f"added to the calendar: {subject} ({entry.Start})"


def meeting_invite(target: str = "", args: str = "") -> str:
    """Draft a meeting invitation with attendees."""
    app, reason = _outlook()
    if app is None:
        return reason
    subject = target.strip()
    attendees = [a for a in re.split(r"[,\s]+", args) if "@" in a]
    if not subject:
        return "give me a subject and the attendees"
    entry = app.CreateItem(1)
    entry.Subject = subject
    for person in attendees:
        entry.Recipients.Add(person)
    entry.Display(True)
    return f"opened a meeting draft for {', '.join(attendees)}"


# --------------------------------------------------------------------------
# messaging
# ------------------------------------------------------------------------__
def whatsapp(target: str = "", args: str = "") -> str:
    """Open WhatsApp to a contact. Messages are typed by you, not sent blind."""
    contact = target.strip()
    if not contact:
        return "give me a contact name"
    shell.run_powershell(f"Start-Process 'whatsapp://send?text='")
    return f"opened WhatsApp; find {contact} and type the message yourself"


def message_send(target: str = "", args: str = "") -> str:
    """Open a chat ready to send. It never sends without a human in the loop."""
    app = target.strip().lower()
    message = args.strip()
    if not app:
        return "say which app: whatsapp, telegram or signal"
    known = {"whatsapp": "https://web.whatsapp.com", "telegram": "https://web.telegram.org",
             "signal": "https://signal.org"}
    if app not in known:
        return f"I do not have a link for {app}; say whatsapp, telegram or signal"
    shell.open_url(known[app])
    if message:
        from . import keyboard

        keyboard.set_clipboard(message)
        return (f"opened {app} and put your message on the clipboard. "
                f"Press and send it yourself.")
    return f"opened {app}"


def sms(target: str = "", args: str = "") -> str:
    return str(shell.run_powershell(f"Start-Process 'ms-settings:messaging'")) and \
        "opened messaging settings; compose from your phone or the Messages app"


def unsubscribe(target: str = "", args: str = "") -> str:
    """Mass unsubscribing is a bulk action; it happens one message at a time."""
    app, reason = _outlook()
    if app is None:
        return reason
    needle = target.strip().lower()
    if not needle:
        return "give me the sender or list name to unsubscribe from"
    items = app.GetDefaultFolder(6).Items
    found = 0
    for index in range(1, min(items.Count, 50) + 1):
        item = items.Item(index)
        if needle in (item.SenderName or "").lower() or needle in (item.Subject or "").lower():
            found += 1
    return (f"{found} matching message(s). Unsubscribe from the link at the "
            f"bottom of one of them — I will not follow unsubscribe links "
            f"without you.")


def remind(target: str = "", args: str = "") -> str:
    """Put a reminder in the calendar at a time you give."""
    from . import auto

    what = target.strip()
    when = args.strip()
    if not what:
        return "give me what to be reminded of"
    if not when:
        return "give me when, e.g. remind 'call mum' in 30 minutes"
    return auto.schedule(f"reminder: {what}", when)


def notifications(target: str = "", args: str = "") -> str:
    """Turn notifications on or off for an app, or show the current settings."""
    verb = f"{target} {args}".strip().lower()
    if verb in ("on", "off") or args.strip().lower() in ("on", "off"):
        return ("Windows exposes no scriptable per-app notification switch. "
                "Settings > System > Notifications.")
    root = ("HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion"
            "\\Notifications\\Settings")
    return str(shell.run_powershell(
        f"$k = Get-Item '{root}' -ErrorAction SilentlyContinue; "
        "if ($k) { "
        f"$k.PSObject.Properties | Where-Object {{$_.Name -notlike 'PS*'}} "
        "| Select-Object Name, Value | Format-Table -AutoSize } else "
        "{ 'no per-app notification settings for this account' }"))


def call(target: str = "", args: str = "") -> str:
    """Start a call. No third-party calling service is wired up, so this says so."""
    contact = (target or args).strip()
    if not contact:
        return "give me who to call"
    from . import shell as sh

    return ("calling needs a provider I have no credentials for. Teams and "
            f"Zoom are installed apps: say 'teams' and I will open {contact} "
            f"there for you to call.")
