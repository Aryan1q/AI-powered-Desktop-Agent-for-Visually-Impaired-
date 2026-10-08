"""Scheduling: run a task now, later, on a repeat, or on a cron expression.

A plain thread, not a service dependency. `Scheduler` keeps schedules in a
JSON file, wakes on the next due time, and hands the task to a `submit`
callback. It supports:

  * one-shot delays ("in 20 minutes")
  * natural offsets ("in 10 minutes", "at 17:30", "tomorrow 08:00")
  * repeats (hourly/daily/weekly/weekdays, plus `every N minutes`)
  * 5-field cron (`ufo schedule add "0 9 * * 1-5" --task "..."`)
  * catch-up for schedules missed while the machine was off
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from ..errors import SchedulerError
from ..logging_setup import get_logger, record
from ..utils import text as textutil
from .task import Task, TaskPriority, TaskStatus

log = get_logger(__name__)

REPEATS = ("hourly", "daily", "weekly", "weekdays", "weekends", "monthly")


@dataclass
class Schedule:
    """When a task should run, and what it is."""

    task_id: str
    goal: str = ""
    next_run: float = 0.0
    cron: str = ""
    every_seconds: float = 0.0
    repeat: str = ""
    priority: TaskPriority = TaskPriority.NORMAL
    enabled: bool = True
    last_run: float = 0.0
    runs: int = 0
    misses: int = 0
    created: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)
    command: str = ""
    app: str = ""
    allow: list[str] = field(default_factory=list)
    mode: str = "computer"
    dry_run: bool = False

    @property
    def is_recurring(self) -> bool:
        return bool(self.cron or self.every_seconds or self.repeat)

    def __post_init__(self) -> None:
        # Callers (CLI, HTTP API, JSON) may hand us "high"/"HIGH"/garbage.
        # Normalising here keeps `to_dict`/`to_task` from blowing up later.
        self.priority = _priority(self.priority)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "goal": self.goal, "next_run": self.next_run,
            "cron": self.cron, "every_seconds": self.every_seconds,
            "repeat": self.repeat, "priority": self.priority.label,
            "enabled": self.enabled, "last_run": self.last_run, "runs": self.runs,
            "misses": self.misses, "created": self.created, "tags": self.tags,
            "command": self.command, "app": self.app, "allow": self.allow,
            "mode": self.mode, "dry_run": self.dry_run,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Schedule":
        return cls(
            task_id=str(data.get("task_id", "")),
            goal=str(data.get("goal", "")),
            next_run=float(data.get("next_run", 0.0)),
            cron=str(data.get("cron", "")),
            every_seconds=float(data.get("every_seconds", 0.0)),
            repeat=str(data.get("repeat", "")),
            priority=_priority(data.get("priority")),
            enabled=bool(data.get("enabled", True)),
            last_run=float(data.get("last_run", 0.0)),
            runs=int(data.get("runs", 0)),
            misses=int(data.get("misses", 0)),
            created=float(data.get("created", 0.0) or time.time()),
            tags=list(data.get("tags", [])),
            command=str(data.get("command", "")),
            app=str(data.get("app", "")),
            allow=list(data.get("allow", [])),
            mode=str(data.get("mode", "computer")),
            dry_run=bool(data.get("dry_run", False)),
        )

    @property
    def schedule_type(self) -> str:
        """One of cron / recurring / absolute — how `next_run` is recomputed."""
        if self.cron:
            return "cron"
        if self.every_seconds or self.repeat:
            return "recurring"
        return "absolute"

    def to_task(self) -> Task:
        return Task(
            goal=self.goal, priority=self.priority, app=self.app,
            allow=list(self.allow), tags=list(self.tags), command=self.command,
            mode=self.mode, dry_run=self.dry_run, scheduled_for=time.time(),
        )

    def describe(self) -> str:
        when = ("cron " + self.cron) if self.cron else (
            f"every {self.every_seconds:.0f}s" if self.every_seconds
            else (f"{self.repeat}" if self.repeat
                  else (time.strftime("%Y-%m-%d %H:%M", time.localtime(self.next_run))
                        if self.next_run else "unscheduled")))
        state = "enabled" if self.enabled else "disabled"
        return (f"[{self.task_id}] {state}  {when:<22} "
                f"{textutil.truncate(self.goal, 55)}"
                + (f"  (ran {self.runs}x)" if self.runs else ""))


def _priority(value) -> TaskPriority:
    """Coerce 'high' / 'HIGH' / TaskPriority.HIGH / 30 / junk -> TaskPriority.

    Note `str(TaskPriority.HIGH)` is "TaskPriority.HIGH" on Python 3.11+ for a
    plain `int, Enum` mixin, so enum members must be handled before the string
    lookup or an explicit priority silently degrades to NORMAL.
    """
    if isinstance(value, TaskPriority):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return TaskPriority(value)
        except ValueError:
            return TaskPriority.NORMAL
    try:
        return TaskPriority[str(value).strip().upper()]
    except (KeyError, AttributeError):
        return TaskPriority.NORMAL


# --------------------------------------------------------------------------
# natural-language time
# --------------------------------------------------------------------------
_UNITS = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1, "s": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60, "m": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600, "h": 3600,
    "day": 86400, "days": 86400, "d": 86400,
    "week": 604800, "weeks": 604800, "w": 604800,
}


def parse_when(text: str, now: float | None = None) -> float:
    """Parse a human time expression into a Unix timestamp.

    Understands: "in 20 minutes", "20m", "in 2 hours", "at 17:30",
    "tomorrow 08:00", "tomorrow", "tonight", "next monday 09:00".
    """
    now = now or time.time()
    base = datetime.fromtimestamp(now)
    raw = textutil.collapse_ws(text).lower()
    if not raw:
        raise SchedulerError("Empty time expression.")

    # "in <n> <unit>" / "<n><unit>"
    m = re.search(r"(?:in\s+)?(\d+(?:\.\d+)?)\s*"
                  r"(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)\b",
                  raw)
    if m and ("in " in raw or len(raw) <= 12):
        seconds = float(m.group(1)) * _UNITS[m.group(2)]
        return now + seconds

    time_of_day = re.search(r"\b(\d{1,2}):(\d{2})\b", raw)
    tomorrow = "tomorrow" in raw
    tonight = "tonight" in raw
    weekday = next((d for d in ("monday", "tuesday", "wednesday", "thursday",
                                "friday", "saturday", "sunday")
                    if d in raw), "")

    if time_of_day:
        hour, minute = int(time_of_day.group(1)), int(time_of_day.group(2))
        if hour > 23 or minute > 59:
            raise SchedulerError(f"Invalid time of day in {text!r}.")
        target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if tonight and target <= base:
            target += timedelta(days=1)
        elif tomorrow:
            target += timedelta(days=1)
        elif not weekday and target <= base:
            target += timedelta(days=1)
        if weekday:
            delta = (["monday", "tuesday", "wednesday", "thursday", "friday",
                      "saturday", "sunday"].index(weekday) - base.weekday()) % 7
            target += timedelta(days=delta)
            if target <= base:
                target += timedelta(days=7)
        return target.timestamp()

    if tomorrow:
        return (base + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0).timestamp()
    if tonight:
        return (base + timedelta(days=1)).replace(
            hour=20, minute=0, second=0, microsecond=0).timestamp()
    if "now" in raw:
        return now
    if weekday:
        names = ["monday", "tuesday", "wednesday", "thursday", "friday",
                 "saturday", "sunday"]
        delta = (names.index(weekday) - base.weekday()) % 7 or 7
        return (base + timedelta(days=delta)).replace(
            hour=9, minute=0, second=0, microsecond=0).timestamp()

    raise SchedulerError(
        f"Could not understand {text!r}. Try: 'in 20 minutes', 'at 17:30', "
        "'tomorrow 08:00', 'weekdays 09:00', or a cron expression."
    )


def parse_repeat(text: str) -> tuple[str, float]:
    """'every 30 minutes' / 'hourly' / 'daily' -> (repeat, every_seconds)."""
    raw = textutil.collapse_ws(text).lower()
    if not raw:
        return "", 0.0
    if raw in REPEATS:
        return raw, 0.0
    m = re.search(r"every\s+(\d+)?\s*"
                  r"(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)\b",
                  raw)
    if m:
        count = float(m.group(1)) if m.group(1) else 1.0
        return "", count * _UNITS[m.group(2)]
    m = re.search(r"^(?:every\s+)?(\d+)\s*"
                  r"(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)$",
                  raw)
    if m:
        return "", float(m.group(1)) * _UNITS[m.group(2)]
    raise SchedulerError(
        f"Could not understand the repeat {text!r}. Try: hourly, daily, weekdays, "
        "or 'every 30 minutes'."
    )


# --------------------------------------------------------------------------
# cron
# --------------------------------------------------------------------------
_FIELD_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_DAYS = {d: i for i, d in enumerate(
    ["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}


def parse_cron(expression: str) -> list[set[int]]:
    """Parse a 5-field cron into per-field allowed value sets."""
    parts = expression.split()
    if len(parts) != 5:
        raise SchedulerError(
            f"A cron expression needs 5 fields "
            f"(minute hour day-of-month month day-of-week); got {len(parts)}: "
            f"{expression!r}"
        )
    out: list[set[int]] = []
    for index, (part, (low, high)) in enumerate(zip(parts, _FIELD_RANGES)):
        values: set[int] = set()
        for chunk in part.split(","):
            chunk = chunk.strip().lower()
            if not chunk:
                raise SchedulerError(f"Empty field in cron expression {expression!r}.")
            step = 1
            if "/" in chunk:
                chunk, _, step_text = chunk.partition("/")
                if not step_text.isdigit() or int(step_text) < 1:
                    raise SchedulerError(f"Bad step {step_text!r} in {expression!r}.")
                step = int(step_text)
            if chunk in ("*", "?"):
                start, end = low, high
            elif "-" in chunk.lstrip("-"):
                a, _, b = chunk.partition("-")
                start, end = _cron_value(a, index), _cron_value(b, index)
            else:
                start = end = _cron_value(chunk, index)
            if start > end:
                raise SchedulerError(
                    f"Range {chunk!r} is backwards in {expression!r}."
                )
            values.update(range(start, end + 1, step))
        out.append(values)
    return out


def _cron_value(token: str, index: int) -> int:
    token = token.strip().lower()
    if index == 3 and token[:3] in _MONTHS:
        return _MONTHS[token[:3]]
    if index == 4 and token[:3] in _DAYS:
        value = _DAYS[token[:3]]
        return 0 if value == 0 else 7 if token == "7" else value
    try:
        value = int(token)
    except ValueError as e:
        raise SchedulerError(f"Bad cron value {token!r}.") from e
    low, high = _FIELD_RANGES[index]
    if index == 4 and value == 7:
        value = 0
    if not low <= value <= high:
        raise SchedulerError(
            f"Cron value {value} is out of range for field {index + 1} "
            f"({low}-{high})."
        )
    return value


def cron_matches(fields: list[set[int]], moment: datetime) -> bool:
    minute, hour, dom, month, dow = fields
    if moment.minute not in minute:
        return False
    if moment.hour not in hour:
        return False
    if month and moment.month not in month:
        return False
    dom_restricted = dom != set(range(1, 32))
    dow_restricted = dow != set(range(0, 7))
    if dom_restricted and dow_restricted:
        return moment.day in dom or ((moment.weekday() + 1) % 7) in dow
    if dom_restricted and moment.day not in dom:
        return False
    if dow_restricted and (moment.weekday() + 1) % 7 not in dow:
        return False
    return True


def next_cron(expression: str, after: float | None = None,
              horizon_days: int = 400) -> float:
    """Next timestamp matching a cron expression, minute by minute."""
    fields = parse_cron(expression)
    start = datetime.fromtimestamp(after or time.time()).replace(second=0, microsecond=0)
    if not cron_matches(fields, start):
        start += timedelta(minutes=1)
    limit = start + timedelta(days=horizon_days)
    step = timedelta(minutes=1)
    probe = start
    while probe <= limit:
        if cron_matches(fields, probe):
            return probe.timestamp()
        probe += step
    raise SchedulerError(
        f"Cron expression {expression!r} has no matching time in the next "
        f"{horizon_days} days."
    )


def describe_when(ts: float) -> str:
    if not ts:
        return "unscheduled"
    delta = ts - time.time()
    if delta < 0:
        return f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))} (in the past)"
    if delta < 90:
        return f"in {delta:.0f}s"
    if delta < 5400:
        return f"in {delta / 60:.0f} min"
    if delta < 86400 * 1.5:
        return f"in {delta / 3600:.1f} h"
    return time.strftime("%a %Y-%m-%d %H:%M", time.localtime(ts))


# --------------------------------------------------------------------------
def _new_id() -> str:
    """Collision-free even when several schedules are added in the same tick."""
    import uuid

    return "sch_" + uuid.uuid4().hex[:8]


# --------------------------------------------------------------------------
class Scheduler:
    """Owns the schedule list and a single background ticker thread."""

    def __init__(self, path: str | Path,
                 submit: Optional[Callable[[Task], None]] = None,
                 jitter: int = 5, catch_up: bool = False):
        self.path = Path(path)
        self.submit = submit
        self.jitter = max(0, int(jitter))
        self.catch_up = catch_up
        self._schedules: dict[str, Schedule] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Load eagerly: a fresh Scheduler over an existing file should not
        # quietly look empty (a silent 'all my schedules vanished' failure).
        self.load()

    # ---- persistence ----
    def load(self) -> "Scheduler":
        with self._lock:
            self._schedules = {}
            if not self.path.exists():
                return self
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                log.warning("could not read schedules from %s (%s)", self.path, e)
                return self
            for item in raw.get("schedules", []):
                if isinstance(item, dict):
                    s = Schedule.from_dict(item)
                    if s.task_id:
                        self._schedules[s.task_id] = s
        return self

    def save(self) -> None:
        with self._lock:
            payload = {"version": 1, "updated": time.time(),
                       "schedules": [s.to_dict() for s in self._schedules.values()]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                           encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            log.warning("could not save schedules: %s", e)

    # ---- CRUD ----
    def add(self, goal: str, when: str = "now", repeat: str = "", cron: str = "",
            task_id: str = "", priority: TaskPriority = TaskPriority.NORMAL,
            **kwargs) -> Schedule:
        goal = textutil.collapse_ws(goal)
        if not goal:
            raise SchedulerError("A schedule needs a task description.")
        # A bare 5-field expression is a cron schedule, not a time phrase.
        if not cron and len(str(when).split()) == 5:
            try:
                parse_cron(str(when))
                cron = str(when).strip()
            except SchedulerError:
                pass
        schedule = Schedule(
            task_id=task_id or _new_id(),
            goal=goal, cron=cron.strip(), priority=priority, **kwargs,
        )
        if cron:
            schedule.next_run = next_cron(cron)
        else:
            schedule.next_run = parse_when(when)
            if repeat:
                schedule.repeat, schedule.every_seconds = parse_repeat(repeat)
        with self._lock:
            self._schedules[schedule.task_id] = schedule
        self.save()
        record("schedule_add", id=schedule.task_id, when=schedule.next_run, cron=cron)
        return schedule

    def get(self, task_id: str) -> Schedule:
        with self._lock:
            s = self._schedules.get(task_id)
        if s is None:
            raise SchedulerError(f"No schedule with id {task_id!r}.")
        return s

    def remove(self, task_id: str) -> bool:
        with self._lock:
            removed = self._schedules.pop(task_id, None) is not None
        if removed:
            self.save()
            record("schedule_remove", id=task_id)
        return removed

    def enable(self, task_id: str, enabled: bool = True) -> Schedule:
        s = self.get(task_id)
        s.enabled = enabled
        if enabled and not s.next_run:
            s.next_run = self._compute_next(s)
        self.save()
        return s

    def list(self, enabled_only: bool = False) -> list[Schedule]:
        with self._lock:
            items = list(self._schedules.values())
        if enabled_only:
            items = [s for s in items if s.enabled]
        return sorted(items, key=lambda s: s.next_run or float("inf"))

    def due(self, now: float | None = None) -> list[Schedule]:
        now = now or time.time()
        return [s for s in self.list(enabled_only=True) if s.next_run and s.next_run <= now]

    # ---- ticking ----
    def _compute_next(self, s: Schedule, after: float | None = None) -> float:
        after = after or time.time()
        if s.cron:
            return next_cron(s.cron, after)
        if s.every_seconds:
            return after + s.every_seconds
        if s.repeat == "hourly":
            return after + 3600
        if s.repeat == "daily":
            return after + 86400
        if s.repeat == "weekly":
            return after + 604800
        if s.repeat == "weekdays":
            return _next_weekday(after)
        if s.repeat == "weekends":
            return _next_weekend(after)
        if s.repeat == "monthly":
            return after + 30 * 86400
        return 0.0

    def tick(self, now: float | None = None) -> list[Task]:
        """Fire everything that is due, and reschedule the recurring ones.

        Called by the background thread, and directly by `ufo schedule tick`
        so the behaviour is testable without waiting.
        """
        now = now or time.time()
        fired: list[Task] = []
        for s in self.due(now):
            if self.catch_up and s.last_run and now - s.last_run > 86400:
                s.misses += int((now - s.last_run) / 86400)
                log.warning("schedule %s missed %d day(s)", s.task_id, s.misses)
            task = s.to_task()
            fired.append(task)
            s.last_run = now
            s.runs += 1
            if s.is_recurring:
                jitter = (self.jitter * ((hash(s.task_id) % 7) - 3) / 3.0) if self.jitter else 0
                s.next_run = self._compute_next(s, now + abs(jitter))
            else:
                s.enabled = False
                s.next_run = 0.0
            record("schedule_fire", id=s.task_id, goal=s.goal[:120])
            if self.submit is not None:
                try:
                    self.submit(task)
                except Exception as e:
                    log.error("scheduled task %s could not be submitted: %s",
                              s.task_id, e)
        if fired:
            self.save()
        self.tick_triggers()
        return fired

    def tick_triggers(self) -> list:
        """Evaluate the "when this happens" rules. Every tick, fired or not.

        Folder watchers and context triggers are not schedules: they have no time
        of their own, only a condition. `watch_folder` promises a tick, so the
        tick is where the promise is kept. Failures are logged and swallowed —
        a broken trigger must not stop the scheduler.
        """
        try:
            from ..actions import triggers

            return triggers.check_now()
        except Exception as e:
            log.warning("trigger evaluation failed: %s: %s", type(e).__name__, e)
            return []

    def start(self, interval: float = 20.0) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self.load()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, args=(interval,), name="ufo-scheduler", daemon=True)
        self._thread.start()
        log.info("scheduler started; %d schedule(s) loaded", len(self._schedules))
        return True

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def _loop(self, interval: float) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                log.error("scheduler tick failed: %s", e)
            self._stop.wait(max(1.0, interval))

    def stats(self) -> dict:
        with self._lock:
            items = list(self._schedules.values())
        return {
            "total": len(items),
            "enabled": sum(1 for s in items if s.enabled),
            "recurring": sum(1 for s in items if s.is_recurring),
            "due_now": len(self.due()),
            "path": str(self.path),
        }

    def describe(self) -> str:
        items = self.list()
        return "\n".join(s.describe() for s in items) if items else "no schedules"


def _next_weekday(after: float) -> float:
    base = datetime.fromtimestamp(after)
    for offset in range(1, 8):
        moment = base + timedelta(days=offset)
        if moment.weekday() < 5:
            return moment.replace(hour=9, minute=0, second=0, microsecond=0).timestamp()
    return after + 86400


def _next_weekend(after: float) -> float:
    base = datetime.fromtimestamp(after)
    for offset in range(1, 8):
        moment = base + timedelta(days=offset)
        if moment.weekday() >= 5:
            return moment.replace(hour=10, minute=0, second=0, microsecond=0).timestamp()
    return after + 86400


_default: Scheduler | None = None


def get_scheduler(path: str | Path | None = None,
                  submit: Optional[Callable[[Task], None]] = None) -> Scheduler:
    """Process-wide scheduler.

    Named `get_scheduler`, not `scheduled`, so `from ..core import scheduler`
    still yields this module rather than shadowing it with a callable.
    """
    from ..config import active

    global _default
    if _default is None:
        cfg = active()
        _default = Scheduler(path or cfg.storage.schedule_path, submit,
                             jitter=cfg.schedule.jitter_seconds,
                             catch_up=cfg.schedule.catch_up_missed)
    elif submit is not None:
        _default.submit = submit
    return _default


def set_default(s: Scheduler | None) -> None:
    global _default
    _default = s
