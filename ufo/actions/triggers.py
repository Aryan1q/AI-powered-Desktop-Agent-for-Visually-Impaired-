"""Triggers: the "when this happens, do that" engine.

This module is what makes automation actually run. Everything here is a pair of
a *condition* and a *goal*, and the condition is a small, documented vocabulary
parsed from ordinary English. When a condition becomes true the goal is handed to
the capability `Executor`, so a triggered task is guarded exactly like a typed
one: same allowlist, same denylist, same risk scoring, same confirmation.

Design notes that matter more than the code:

* **Fail closed on an unknown condition.** "when the flux capacitor charges" is
  stored and reported as unsupported, not silently treated as `always`. A trigger
  that fires on the wrong thing is worse than one that never fires.
* **One tick, one evaluation.** `check_now()` is the only entry point. The
  scheduler calls it, the daemon calls it, and `ufo do auto_rules now` calls it,
  so a trigger behaves identically whoever woke it up.
* **State is explicit.** Folder watching and repeat-until keep a cursor in
  `automation.json` rather than in memory, so a restart does not replay a
  trigger that has already fired.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..logging_setup import get_logger

log = get_logger(__name__)

#: A download is still landing while its browser holds these extensions open.
_PARTIAL_SUFFIXES = (".crdownload", ".part", ".partial", ".tmp", ".download",
                     ".!ut", ".opdownload")

#: Conditions we understand, with the words that introduce them. The first
#: pattern that matches the text wins, so the list is ordered most specific
#: first: "on battery" must be tried before the bare word "battery".
CONDITIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # A download is named as a finished download. The bare word "download" is not
    # enough: "a file appears in C:/Users/x/Downloads" names a folder, and matching
    # that as a download event watches the wrong thing entirely.
    ("download_finished", (
        r"download (?:finishes|finished|completes|completed|is done|done)",
        r"a download (?:finishes|completes)",
        r"डाउनलोड (?:पूरा|हो)",
    )),
    ("file_appeared", (
        r"file appear", r"a file", r"new file", r"something (?:lands|appears)",
        r"फ़ाइल", r"नई फ़ाइल",
    )),
    ("cpu_high", (r"cpu", r"processor load", r"high load", r"heavy load")),
    # A page changing is watched by `web_monitor`, which records the URL and a
    # hash. The trigger just names the condition; it does not fetch the page.
    ("website_changed", (r"website changes", r"site changes", r"page changes",
                         r"webpage changes", r"page is updated",
                         r"website (?:is )?updated", r"content changes")),
    ("battery_low", (r"battery (?:is )?low", r"low battery", r"battery low",
                     r"बैटरी कम", r"बैटरी लो")),
    ("on_battery", (r"on battery", r"unplugged", r"battery (?:is )?off",
                    r"बैटरी पर")),
    ("charger_plugged", (r"charger", r"plugged in", r"charging",
                         r"on mains", r"on charge", r"on ac", r"मुख्य बिजली",
                         r"चार्जर")),
    ("screen_locked", (r"screen (?:is )?locked", r"lock ?screen",
                       r"workstation lock", r"(?:when |if )?locked",
                       r"session locked", r"स्क्रीन लॉक")),
    ("meeting_starts", (r"meeting", r"call starts", r"appointment", r"मीटिंग")),
    # "email arrives from bob" has to be tried before the looser `message from`.
    ("email_from", (r"email (?:arrives )?from", r"(?:e-?mail|mail|message|mail)"
                    r"\s+(?:arrives\s+)?from", r"from \S+@\S+", r"ईमेल")),
    # "when I get home" is checked against the network, so it must be tried before
    # the broader `on_wifi` patterns below.
    ("arrived_home", (r"get home", r"arrive home", r"reach home", r"back home",
                      r"get to (?:the )?(?:office|home|work)", r"घर पहुँच")),
    ("on_wifi", (r"connected to", r"on wifi", r"wi-fi", r"वाईफ़")),
    ("at_night", (r"at night", r"night", r"late", r"रात")),
    ("morning", (r"in the morning", r"morning")),
    ("weekend", (r"weekend", r"sat|sun|saturday|sunday|सप्ताहाहार")),
    ("after_close", (r"after i close", r"when i close", r"after closing",
                     r"on window close", r"window clos", r"app clos", r"बंद")),
    ("network_connected", (r"network connects", r"internet (?:comes|returns)",
                           r"connection (?:is )?(?:back|restored)",
                           r"connected to (?:the )?internet", r"back online",
                           r"wifi connects", r"नेटवर्क")),
    ("while_away", (r"while i'?m away", r"while away", r"i'?m away", r"दूर")),
    ("idle", (r"idle", r"inactive", r"निष्क्रिय")),
    ("on_battery_or_charging", (r"battery", r"चार्ज")),
    # "app" is last: it is the loosest pattern and would otherwise swallow
    # "open the door" and "app startup list" alike.
    ("app_open", (r"app is open", r"application is open", r"is open", r"app",
                  r"application", r"when i open", r"ऐप")),
    ("always", (r"always", r"every time", r"हमेशा")),
)

#: Windows says the workstation is locked; the window list comes back empty.
_LOCKED_STATES = ("Locked", "LogonUI", "Screen-saver")


@dataclass
class Context:
    """The facts a condition is allowed to ask about.

    Deliberately cheap and local: no network calls, nothing that could hang the
    scheduler tick. Each field is filled in by `snapshot()`, and a caller that
    only cares about one thing can build a Context with just that field.
    """

    now: float = field(default_factory=time.time)
    foreground: str = ""          # foreground window title
    window_titles: list[str] = field(default_factory=list)
    on_wifi: str = ""             # SSID, empty when not on Wi-Fi
    on_mains: bool = False        # charger plugged in
    battery_percent: int | None = None
    battery_charging: bool = False
    screen_locked: bool = False
    cpu_percent: int | None = None
    idle_seconds: float = 0.0

    @property
    def hour(self) -> int:
        return time.localtime(self.now).tm_hour

    @property
    def is_weekend(self) -> bool:
        return time.localtime(self.now).tm_wday >= 5

    def describe(self) -> str:
        bits = [time.strftime("%H:%M", time.localtime(self.now))]
        if self.foreground:
            bits.append(f"foreground={self.foreground!r}")
        if self.on_wifi:
            bits.append(f"wifi={self.on_wifi!r}")
        bits.append("on mains" if self.on_mains else "on battery")
        if self.battery_percent is not None:
            bits.append(f"battery={self.battery_percent}%")
        if self.screen_locked:
            bits.append("screen locked")
        if self.cpu_percent is not None:
            bits.append(f"cpu={self.cpu_percent}%")
        if self.idle_seconds >= 60:
            bits.append(f"idle {int(self.idle_seconds // 60)}m")
        return ", ".join(bits)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------
def parse_condition(text: str) -> tuple[str, str]:
    """Return (kind, argument) for a condition written in English.

    A condition we do not recognise comes back as ("unknown", ""). Callers must
    treat that as unsupported rather than matching it.
    """
    raw = " ".join(str(text or "").lower().split())
    if not raw:
        return ("unknown", "")

    arg = _condition_argument(raw)
    for kind, patterns in CONDITIONS:
        if any(re.search(p, raw) for p in patterns):
            return (kind, arg)
    return ("unknown", "")


def _condition_argument(raw: str) -> str:
    """Pull the interesting noun out of a condition: the folder, app, SSID, sender.

    Ordered most specific first. A general "capture whatever follows a keyword"
    rule is last, because it loses to any of these and gets the wrong answer when
    the noun comes before the keyword, as in "notepad is open".
    """
    patterns = (
        # "notepad is open", "chrome is open"
        r"(?i)^([\w .+-]{2,40}?)\s+is\s+open",
        # "get home on HomeNet" — the network is named after the arrival phrase,
        # which none of the "connected to" shapes below match.
        r"(?i)(?:get|arrive|reach)\s+home\s+on\s+([\w.-]{2,40})",
        # "connected to HomeWifi", "on wifi HomeWifi"
        r"(?i)(?:connected to|on wifi|on wi-fi|on the)\s+([\w.-]{2,40})",
        # "a file appears in C:/Users/x/Downloads"
        r"(?i)(?:in|inside|under)\s+[\"']?([^\"',;!?]+?)[\"']?"
        r"(?:\s+(?:appears?|lands?|arrives?|happens?))?\s*$",
        # "the download finishes in ~/Downloads" style, folder last
        r"(?i)(?:folder|folder called|directory)\s+[\"']?([^\"',;!?]+?)[\"']?\s*$",
        # "email from boss@x.com", "mail from bob"
        r"(?i)\bfrom\s+([\w.@+-]{2,60})",
        # a hostname, when the SSID happens to look like one
        r"(?i)\b((?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+)\b",
        # "when I open Notepad"
        r"(?i)(?:when i open|open|app|application)\s+([\w .+-]{2,40})$",
    )
    for pattern in patterns:
        m = re.search(pattern, raw)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ""


def describe_condition(text: str) -> str:
    """Plain-English rendering of a condition, for the list command."""
    kind, arg = parse_condition(text)
    labels = {
        "download_finished": "a download finishes",
        "file_appeared": f"a file appears in {arg}" if arg else "a file appears",
        "cpu_high": "CPU load is high",
        "battery_low": "battery is low",
        "on_battery": "the machine is on battery",
        "charger_plugged": "the charger is plugged in",
        "screen_locked": "the screen is locked",
        "meeting_starts": "a meeting starts",
        "email_from": f"mail arrives from {arg}" if arg else "mail arrives",
        "website_changed": (f"{arg} changes" if arg
                            else "a watched page changes"),
        "on_wifi": f"connected to {arg}" if arg else "connected to Wi-Fi",
        "arrived_home": (f"you are back on {arg}" if arg
                         else "you are back on a network"),
        "at_night": "it is night",
        "morning": "it is morning",
        "weekend": "it is the weekend",
        "after_close": "after an app is closed",
        "network_connected": "the network comes back",
        "while_away": "you are away",
        "app_open": f"{arg} is open" if arg else "an app is open",
        "idle": "the machine is idle",
        "on_battery_or_charging": "battery state changes",
        "always": "always",
        "unknown": f"not understood: {text!r}",
    }
    return labels.get(kind, kind)


# --------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------
def evaluate(condition: str, context: Context, store: dict | None = None) -> bool:
    """Is this condition true right now?

    Fails closed: an unrecognised condition is False, never True.
    """
    kind, arg = parse_condition(condition)
    if kind == "unknown":
        return False

    if kind == "always":
        return True
    if kind == "at_night":
        return context.hour >= 21 or context.hour < 6
    if kind == "morning":
        return 5 <= context.hour < 12
    if kind == "weekend":
        return context.is_weekend
    if kind == "on_battery":
        return not context.on_mains
    if kind == "charger_plugged":
        return context.on_mains or context.battery_charging
    if kind == "on_battery_or_charging":
        return context.on_mains or context.battery_charging or not context.on_mains
    if kind == "screen_locked":
        return context.screen_locked
    if kind == "cpu_high":
        return (context.cpu_percent or 0) >= 85
    if kind == "battery_low":
        if context.battery_charging:
            return False
        return context.battery_percent is not None and context.battery_percent <= 25
    if kind == "idle":
        return context.idle_seconds >= 300
    if kind == "while_away":
        return context.idle_seconds >= 900
    if kind == "meeting_starts":
        return _meeting_within_minutes(context, 5)
    if kind == "app_open":
        needle = arg.lower()
        return any(needle in t.lower() for t in context.window_titles) if needle \
            else bool(context.foreground)
    if kind == "on_wifi":
        if not arg:
            return bool(context.on_wifi)
        return context.on_wifi.lower() == arg.lower()
    if kind == "arrived_home":
        # There is no GPS here, so "I got home" is inferred from joining a
        # network, and any SSID named in the condition is respected.
        if not arg:
            return bool(context.on_wifi)
        return context.on_wifi.lower() == arg.lower()
    if kind == "network_connected":
        return bool(context.on_wifi)
    if kind == "after_close":
        # True for one tick after a named app stops being in the window list.
        state = (store or {}).get("was_open") or {}
        titles = [t.lower() for t in context.window_titles]
        fired = []
        for name in list(state):
            if name not in titles:
                fired.append(name)
        if store is not None:
            store["was_open"] = {t.lower() for t in context.window_titles}
        return bool(fired)
    if kind in ("download_finished", "file_appeared"):
        return _folder_has_new_file(kind, arg, store)
    if kind == "email_from":
        return False     # needs a mail check, which the tick does not do
    if kind == "website_changed":
        # `web_monitor` owns the fetch and the hash. A bare "the website
        # changes" has no URL to check, so it stays false until one is set,
        # rather than claiming a page was watched that never was.
        return False
    return False


def _meeting_within_minutes(context: Context, minutes: int) -> bool:
    """A meeting starting soon. Reads the local calendar, never a remote one."""
    try:
        from . import comms

        text = str(comms.calendar_list("", ""))
    except Exception as e:                       # a missing Outlook is not a crash
        log.debug("meeting check failed: %s", e)
        return False
    if not text or "not available" in text.lower():
        return False
    soon = time.time() + minutes * 60
    for line in text.splitlines():
        m = re.search(r"(\d{1,2}):(\d{2})", line)
        if not m:
            continue
        hour, minute = int(m.group(1)), int(m.group(2))
        target = time.localtime(context.now)
        when = time.mktime((target.tm_year, target.tm_mon, target.tm_mday,
                            hour, minute, 0, 0, 0, -1))
        if context.now <= when <= soon:
            return True
    return False


def _watch_roots() -> list[Path]:
    """Where "something downloaded" is looked for."""
    home = Path.home()
    return [home / "Downloads",
            home / "Desktop",
            home / "Videos" / "Downloads"]


def _folder_for(kind: str, arg: str) -> Path | None:
    if arg:
        candidate = Path(arg).expanduser()
        return candidate if candidate.exists() else None
    for root in _watch_roots():
        if root.is_dir():
            return root
    return None


def _folder_has_new_file(kind: str, arg: str, store: dict | None) -> bool:
    """True when a *settled* file appeared since the last tick.

    A file that is still being downloaded is skipped: it has a temporary
    extension, and treating it as finished is how an automation ends up acting on
    half a video.
    """
    folder = _folder_for(kind, arg)
    if folder is None or not folder.is_dir():
        return False
    key = f"seen:{str(folder).lower()}"
    if store is None:
        return False
    # JSON has no set type, so what came back from disk is a list. Coerce, or
    # every tick after the first would fail on `.add`.
    raw_seen = store.get(key)
    seen: set[str] = set(raw_seen) if isinstance(raw_seen, (list, tuple, set)) else set()
    store[key] = seen

    if not store.get(f"seeded:{key}"):
        # First run records the current contents without firing, otherwise every
        # file already in the folder would be "new" the moment a rule is added.
        # The marker is explicit because an empty folder would otherwise stay in
        # this state forever and could never fire at all.
        for item in folder.iterdir():
            if not item.name.lower().endswith(_PARTIAL_SUFFIXES):
                seen.add(item.name)
        store[f"seeded:{key}"] = True
        return False

    fresh = []
    for item in folder.iterdir():
        if item.name.lower().endswith(_PARTIAL_SUFFIXES):
            continue
        if item.name in seen:
            continue
        try:                       # still growing means still downloading
            if item.stat().st_size == 0:
                continue
        except OSError:
            continue
        fresh.append(item.name)
        seen.add(item.name)
    return bool(fresh)


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------
def run_goal(goal: str, context: Context | None = None,
             confirm: Callable[[str, Any], bool] | None = None) -> str:
    """Run one goal through the capability executor, guard and all."""
    from ..capabilities.executor import Executor
    from ..config import active

    cfg = active()
    executor = Executor(cfg=cfg, confirm=confirm,
                        narrate=lambda message: log.info("%s", message))
    result = executor.handle(goal)
    if result.steps:
        return result.describe()
    return result.text or f"nothing to do for {goal!r}"


def notify(text: str) -> None:
    """Tell the user something, on every channel available."""
    try:
        from ..voice import notify as say

        say("ufo", text)
    except Exception as e:
        log.debug("voice notification failed: %s", e)
    try:
        from . import fileops

        root = _log_dir()
        root.mkdir(parents=True, exist_ok=True)
        with (root / "alerts.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")
    except Exception as e:
        log.debug("could not write the alert log: %s", e)


def _log_dir() -> Path:
    from ..config import active

    return Path(active().storage.home) / "triggers"


# --------------------------------------------------------------------------
# the tick
# --------------------------------------------------------------------------
@dataclass
class Fired:
    rule: str
    goal: str
    reason: str
    output: str = ""


def snapshot() -> Context:
    """Gather the facts a condition can ask about. All local, all cheap."""
    context = Context()
    try:
        from ..perception import uia

        windows = uia.list_all_windows()
        context.window_titles = [w.title for w in windows]
        context.foreground = next((w.title for w in windows if w.is_foreground), "")
        context.screen_locked = any(w.title in _LOCKED_STATES or w.process_name
                                    in ("LogonUI.exe", "LockApp.exe") for w in windows)
    except Exception as e:
        log.debug("no window state: %s", e)

    try:
        from . import hardware

        battery = hardware.battery("")
        percent = re.search(r"(\d+)%", str(battery))
        if percent:
            context.battery_percent = int(percent.group(1))
        context.battery_charging = "charging" in str(battery).lower()
        context.on_mains = context.battery_charging or context.battery_percent == 100
    except Exception as e:
        log.debug("no battery state: %s", e)

    try:
        import psutil  # type: ignore

        context.cpu_percent = int(psutil.cpu_percent(interval=0.2))
    except Exception:
        pass

    try:
        from . import network

        context.on_wifi = _current_ssid(network)
    except Exception as e:
        log.debug("no wifi state: %s", e)

    try:
        from ..win32 import idle_seconds

        context.idle_seconds = float(idle_seconds())
    except Exception:
        pass

    return context


def _current_ssid(network) -> str:
    text = str(network.wifi_list("", ""))
    for line in text.splitlines():
        m = re.search(r"^\s*(\S+)\s+(?:\d+\s+)?(?:Secured|Open|Authenticated)",
                      line, re.IGNORECASE)
        if m:
            return m.group(1).strip("*")
    m = re.search(r"(?im)^\s*SSID\s*:\s*(\S+)", text)
    return m.group(1).strip() if m else ""


def check_now(context: Context | None = None, *, run: bool = True,
              confirm: Callable[[str, Any], bool] | None = None) -> list[Fired]:
    """Evaluate every rule and act. This is the engine the scheduler ticks.

    `run=False` reports what *would* fire without running anything, which is what
    `ufo do auto_rules now --dry-run` needs.
    """
    from . import auto

    context = context or snapshot()
    store = _state()
    fired: list[Fired] = []

    for rule in _load_store().get("rules", []):
        condition = str(rule.get("when", ""))
        goal = str(rule.get("goal", ""))
        if not condition or not goal:
            continue
        if not evaluate(condition, context, store):
            continue
        kind, _arg = parse_condition(condition)
        if kind == "unknown":
            continue                      # never act on a condition we cannot read
        repeat = float(rule.get("every", 0) or 0)
        if repeat and not _due(rule, repeat, context.now):
            continue
        record = {"reason": describe_condition(condition)}
        if run:
            if rule.get("alert"):
                notify(f"{describe_condition(condition)}: {goal}")
                record["output"] = "alerted"
            else:
                record["output"] = run_goal(goal, context, confirm=confirm)
        fired.append(Fired(rule=str(rule.get("id", "")), goal=goal, **record))
        if rule.get("once"):
            _disable(rule.get("id", ""))

    # folder watchers are rules with an implicit "a file appeared" condition
    for rule in _load_store().get("watched", []):
        folder = str(rule.get("folder", ""))
        goal = str(rule.get("goal", ""))
        if not folder or not goal:
            continue
        if not evaluate(f"a file appears in {folder}", context, store):
            continue
        record = {"reason": f"a file appeared in {folder}"}
        if run:
            record["output"] = run_goal(goal, context, confirm=confirm)
        fired.append(Fired(rule=str(rule.get("id", "")), goal=goal, **record))

    _save_store(_load_store())
    _save_state(store)
    return fired


def _due(rule: dict, repeat: float, now: float) -> bool:
    last = float(rule.get("fired_at", 0) or 0)
    return now - last >= repeat


def _disable(rule_id: str) -> None:
    if not rule_id:
        return
    data = _load_store()
    for rule in data.get("rules", []):
        if rule.get("id") == rule_id:
            rule["enabled"] = False
    _save_store(data)


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------
def _load_store() -> dict:
    from . import auto

    return auto._load()


def _save_store(data: dict) -> None:
    from . import auto

    auto._save(data)


def _state_path() -> Path:
    from ..config import active

    return Path(active().storage.home) / "trigger-state.json"


def _state() -> dict:
    import json

    path = _state_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    import json

    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Sets are not JSON. They are the natural type for the "seen" cursors, so
    # they are written as sorted lists rather than dropped.
    plain = {k: (sorted(v) if isinstance(v, (set, frozenset)) else v)
             for k, v in state.items()}
    try:
        path.write_text(json.dumps(plain, indent=2, default=str), encoding="utf-8")
    except OSError as e:
        log.debug("could not save trigger state: %s", e)


__all__ = [
    "Context", "Fired", "check_now", "describe_condition", "evaluate",
    "parse_condition", "run_goal", "snapshot",
]
