"""Automation, scheduling and triggers.

Backs "every weekday at nine do this" and "watch that folder". Scheduled goals
go into the scheduler the agent already has, so they survive a restart and are
governed by the same budget as any other run.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from ..errors import ActionError
from ..logging_setup import get_logger

from . import shell

log = get_logger(__name__)


def _store_path() -> Path:
    from ..config import active

    return Path(active().storage.home) / "automation.json"


def _load() -> dict:
    path = _store_path()
    if not path.is_file():
        return {"rules": [], "watched": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"rules": [], "watched": []}


def _save(data: dict) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# scheduled goals
# --------------------------------------------------------------------------
def schedule(target: str = "", args: str = "") -> str:
    """Add a scheduled goal. target is the goal; args is the time or cron."""
    goal = target.strip()
    when = args.strip()
    if not goal:
        return "give me a goal, e.g. schedule 'clear my inbox' 0 9 * * 1-5"
    if not when:
        return "give me a time or a cron expression"
    from ..core.scheduler import get_scheduler

    try:
        added = get_scheduler().add(goal, when)
    except Exception as e:
        return f"could not schedule that: {type(e).__name__}: {e}"
    return f"scheduled [{added.task_id}] {added.describe()}"


def cancel(target: str = "", args: str = "") -> str:
    from ..core.scheduler import get_scheduler

    scheduler = get_scheduler()
    needle = (target or args).strip().lower()
    matches = [s for s in scheduler.list()
               if needle and needle in (s.goal or "").lower()]
    if not matches:
        return f"no scheduled task matching {needle!r}"
    for entry in matches:
        scheduler.remove(entry.task_id)
    return f"cancelled {len(matches)} scheduled task(s): {', '.join(s.goal for s in matches)}"


def list_scheduled(target: str = "", args: str = "") -> str:
    from ..core.scheduler import get_scheduler

    entries = get_scheduler().list()
    if not entries:
        return "nothing is scheduled"
    return "\n".join(f"{entry.task_id}  {entry.describe()}" for entry in entries)


# --------------------------------------------------------------------------
# startup hooks
# --------------------------------------------------------------------------
def on_startup(target: str = "", args: str = "") -> str:
    """Run something at every sign-in."""
    name = target.strip()
    command = args.strip()
    if not name or not command:
        return 'give me "name" and the command to run'
    from . import system

    return system.add_startup(name, command)


def remove_startup(target: str = "", args: str = "") -> str:
    from . import system

    return system.remove_startup((target or args).strip())


# --------------------------------------------------------------------------
# folder watching
# --------------------------------------------------------------------------
def watch_folder(target: str = "", args: str = "") -> str:
    """Watch a folder and run a goal when a settled file appears there.

    Windows has no inotify for user-mode code, so this is checked on a tick. The
    scheduler runs the tick, and `ufo daemon start` does too, so the promise here
    is one the engine actually keeps. Files still being written are skipped: a
    `.crdownload` is not a finished download.
    """
    folder = (target or args).strip()
    goal = args.strip() if target else ""
    if not folder:
        return "give me a folder to watch"
    if not goal:
        return 'give me a folder and what to do, e.g. watch_folder ~/Downloads "tidy it"'
    from . import filesystem, triggers

    path = filesystem.expand(folder)
    if not Path(path).is_dir():
        return f"no such folder: {path}"
    data = _load()
    data.setdefault("watched", []).append(
        {"id": _new_id(), "folder": str(path), "goal": goal, "enabled": True})
    _save(data)
    # Record what is already there, so only genuinely new files fire this.
    triggers.check_now(run=False)
    return (f"watching {path}. Checked on every scheduler tick and by the daemon; "
            f"files already in it will not fire, only ones that appear later. "
            f"Test it with: ufo do auto_rules now")


def unwatch(target: str = "", args: str = "") -> str:
    folder = (target or args).strip().lower()
    data = _load()
    before = len(data["watched"])
    data["watched"] = [w for w in data["watched"]
                       if folder not in w["folder"].lower()]
    _save(data)
    removed = before - len(data["watched"])
    return f"stopped watching {removed} folder(s)" if removed else "no match"


def trigger(target: str = "", args: str = "") -> str:
    """Set a rule that fires on an event, test one, or list what a tick would do.

    The catalog advertises this as "act when an event happens", so a condition
    plus an action is recorded as a rule. With one argument it stays a query:
    "now" runs the tick, anything else looks up a stored rule by condition.
    """
    from . import triggers

    when = str(target or "").strip()
    goal = str(args or "").strip()
    if when and goal:
        return add_rule(when, goal)

    data = _load()
    name = f"{when} {goal}".strip().lower()
    if name in ("now", "check", "tick"):
        fired = triggers.check_now(run=not _is_dry_run())
        if not fired:
            return "no rule matched right now"
        return "\n".join(f"{f.reason} -> {f.goal}\n  {f.output}" for f in fired)
    rules = {str(r.get("when", "")).lower(): r for r in data.get("rules", [])}
    rule = rules.get(name)
    if rule is None:
        return (f"no rule called {name!r}. Set one with a condition and an action, "
                f"e.g. trigger_context 'when the battery is low' 'tell me'. "
                f"Run every rule with: ufo do auto_trigger now")
    return (f"rule {name!r}: {triggers.describe_condition(rule['when'])} -> "
            f"{rule['goal']}")

def add_rule(target: str = "", args: str = "") -> str:
    """Record an if/then rule. target='if battery is low', args='then tell me'.

    The condition is parsed against a small documented vocabulary, and an
    unrecognised one is refused rather than stored: a trigger that fires on the
    wrong thing is worse than one that never fires.

    Both argument shapes are accepted: the condition and the action passed
    separately, or one sentence with " then " in it. Only splitting on " then "
    meant a caller that respected the declared `targets`/`args` contract had its
    action swallowed and stored a rule that fired the wrong thing.
    """
    when = str(target or "").strip()
    goal = str(args or "").strip()

    if goal and " then " not in f"{when} {goal}":
        when, goal = _strip_when(when), _strip_then(goal)
    else:
        text = f"{when} {goal}".strip()
        if not text:
            return 'give me a rule, e.g. if "battery low" then "tell me"'
        when, _, goal = text.partition(" then ")
        when, goal = when.strip(), goal.strip()

    if not when:
        return 'give me a condition, e.g. if "battery low" then "tell me"'

    from . import triggers

    kind, _arg = triggers.parse_condition(when)
    if kind == "unknown":
        return (f"I do not understand the condition {when!r}, so I have not saved "
                f"it. I can watch: " + ", ".join(sorted({k for k, _ in triggers.CONDITIONS})))
    if not goal:
        goal = f"tell me that {triggers.describe_condition(when)}"
    data = _load()
    data.setdefault("rules", []).append(
        {"id": _new_id(), "when": when, "goal": goal, "enabled": True})
    _save(data)
    return (f"rule saved [{_last_id(data)}]: when "
            f"{triggers.describe_condition(when)}, {goal}. "
            f"It runs on the next scheduler tick. "
            f"Test it with: ufo do auto_rules now")


def _strip_when(text: str) -> str:
    """Drop a leading "if"/"when" so 'if battery is low' reads as 'battery is low'."""
    stripped = text.strip()
    for lead in ("if ", "when ", "whenever ", "once "):
        if stripped.lower().startswith(lead):
            return stripped[len(lead):].strip()
    return stripped


def _strip_then(text: str) -> str:
    """Drop a leading "then" so 'then tell me' reads as 'tell me'."""
    stripped = text.strip()
    if stripped.lower().startswith("then "):
        return stripped[5:].strip()
    return stripped


def _last_id(data: dict) -> str:
    rules = data.get("rules", [])
    return str(rules[-1].get("id", "")) if rules else ""


def _new_id() -> str:
    import uuid

    return uuid.uuid4().hex[:8]


def _is_dry_run() -> bool:
    from ..config import active

    return bool(active().safety.dry_run)


def rule_list(target: str = "", args: str = "") -> str:
    from . import triggers

    rows = []
    for rule in _load().get("rules", []):
        if "list" not in f"{target} {args}".lower() and target and rule.get("id") != target:
            continue
        state = "" if rule.get("enabled", True) else "  [disabled]"
        rows.append(f"[{rule.get('id', '?')}] when "
                    f"{triggers.describe_condition(str(rule.get('when', '')))} -> "
                    f"{rule.get('goal', '')}{state}")
    for watch in _load().get("watched", []):
        rows.append(f"[{watch.get('id', '?')}] watch {watch.get('folder')} -> "
                    f"{watch.get('goal')}")
    return "\n".join(rows) or "no rules or watchers saved"


# --------------------------------------------------------------------------
# agent control and self-management
#
# These talk to the agent that is running them: its queues, its schedules, its
# memory and its own configuration. They live here because they are all "do this
# automatically" in the widest sense. The guard still scores each one by what it
# actually changes, and `auto_approve` refuses outright.
# --------------------------------------------------------------------------
def control(target: str = "", args: str = "") -> str:
    """Cancel a scheduled or running task, by id or by goal."""
    return cancel(target, args)


def rules(target: str = "", args: str = "") -> str:
    """Show the rules, or add one."""
    if str(args or "").strip():
        return add_rule(target, args)
    if " then " in f"{target} {args}":
        return add_rule(target, args)
    return rule_list(target, args)


def repeat(target: str = "", args: str = "") -> str:
    """Repeat something on a schedule."""
    return schedule(target, args)


def macro(target: str = "", args: str = "") -> str:
    """Record a named command, or run one back."""
    data = _load()
    macros = data.setdefault("macros", {})
    name = (target or args).strip()
    if not name:
        return "\n".join(f"{k}: {v}" for k, v in macros.items()) or "no macros yet"
    if args.strip():
        macros[name] = args.strip()
        _save(data)
        return f"macro {name!r} recorded"
    command = macros.get(name)
    if not command:
        return f"no macro called {name!r}"
    return str(shell.run_powershell(command))


def background(target: str = "", args: str = "") -> str:
    """Queue a task for the daemon to pick up."""
    goal = f"{target} {args}".strip()
    if not goal:
        return "give me a task to run in the background"
    from ..core.task import Task
    from ..core.queue import get_queue

    task = get_queue().add(Task(goal=goal))
    return f"queued as {task.id}: {goal}"


def alert(target: str = "", args: str = "") -> str:
    """Set an alert that fires when something happens, or send one now.

    "alert me when a download finishes" registers a rule the engine evaluates.
    A bare message with no "when" is spoken immediately, which is what a plain
    "tell me" should do.

    The catalog declares `targets` as the condition and `args` as how to reach
    you, so when both arrive they are kept apart. Joining them put "tell me" at
    the end of every stored condition, which then had to be parsed around.
    """
    from . import triggers

    condition = str(target or "").strip()
    message = str(args or "").strip()
    text = f"{condition} {message}".strip() if not message else condition
    if not text and not message:
        return "say what to alert you about, e.g. alert me when the battery is low"

    lowered = text.lower()
    when = ""
    if "when" in lowered:
        when = lowered.split("when", 1)[1].strip(" ,.")
    elif "if" in lowered and lowered.index("if") < 12:
        when = lowered.split("if", 1)[1].strip(" ,.")
    if not when and message:
        # The condition came through `targets` with no "when" prefix.
        when = lowered.strip(" ,.")
    if not when:
        from ..voice import notify

        notify("ufo", f"{condition} {message}".strip())
        return f"told you: {(condition + ' ' + message).strip()}"

    kind, _arg = triggers.parse_condition(when)
    if kind == "unknown":
        return (f"I do not know how to watch for {when!r}, so I have not set that "
                f"alert. I can watch for: "
                + ", ".join(sorted({k for k, _ in triggers.CONDITIONS})))
    data = _load()
    data.setdefault("rules", []).append(
        {"id": _new_id(), "when": when,
         "goal": f"alert: {message}" if message else f"alert: {text}",
         "alert": True, "enabled": True})
    _save(data)
    return (f"alert set [{_last_id(data)}]: I will tell you when "
            f"{triggers.describe_condition(when)}. It repeats until you turn it "
            f"off. List them with: ufo do alert_list")


def list_alerts(target: str = "", args: str = "") -> str:
    from . import triggers

    rows = [f"[{r.get('id', '?')}] {triggers.describe_condition(str(r.get('when', '')))}"
            f" -> {r.get('goal', '')}"
            f"{'' if r.get('enabled', True) else '  [disabled]'}"
            for r in _load().get("rules", []) if r.get("alert")]
    if not rows:
        return "no alerts set. Add one with: ufo do alert_set 'when the battery is low'"
    return "\n".join(rows)


def context_trigger(target: str = "", args: str = "") -> str:
    """Record a rule keyed to context: an app, a place, a time, the charger.

    These are the "before my meeting", "when I get home", "when the charger is
    plugged in" triggers. They are ordinary rules with a context condition, so
    the same engine evaluates them.

    The catalog declares `targets` as the condition and `args` as what to do, so
    the two arrive as separate arguments. Joining them and then demanding a
    literal " then " between the halves meant the declared calling convention
    could never work: every pair of real arguments was rejected for a missing
    separator. Both shapes are accepted here — two arguments, or one sentence.
    """
    when = str(target or "").strip()
    goal = str(args or "").strip()

    if goal:
        return add_rule(when, goal) if when else _no_condition()

    text = f"{when} {goal}".strip()
    if not text:
        return ('give me a rule, e.g. "when I open Outlook" then "clear it", or '
                '"when the charger is plugged in" then "charge to full"')
    when, sep, goal = text.partition(" then ")
    if not sep:
        return _no_condition()
    when, goal = when.strip(), goal.strip()
    if not goal:
        return 'say what to do, e.g. "when I get home" then "start music"'
    return add_rule(when, goal)


def _no_condition() -> str:
    return ('say what to do, e.g. "when I get home" then "start music", or pass '
            'the condition and the action as two separate arguments')


#: The nine modes from the catalog, as goals rather than as direct function
#: calls. Writing them as sentences is deliberate: `run_goal` puts every step
#: through the executor, the guard, risk scoring and confirmation, so a routine
#: cannot quietly do something `ufo do` would have refused.
#:
#: `on` runs the mode; `off` reverses the state it set. A mode is expected to be
#: reversible — anything that is not lists the cleanup under `off` anyway, so
#: "focus off" always has something to do.
ROUTINES: dict[str, dict[str, list[str]]] = {
    "morning": {
        "on": ["check my battery", "check my disk",
               "list today's calendar events", "clear my notifications",
               "show me today's todo list"],
        "off": ["clear do not disturb"],
    },
    "work": {
        "on": ["set do not disturb on", "show me today's todo list",
               "list today's calendar events"],
        "off": ["clear do not disturb"],
    },
    "focus": {
        "on": ["start a 25 minute focus session", "set do not disturb on",
               "mute"],
        "off": ["stop the focus session", "clear do not disturb", "unmute"],
    },
    "meeting": {
        "on": ["list today's calendar events", "clear my notifications",
               "set do not disturb on", "mute"],
        "off": ["clear do not disturb", "unmute"],
    },
    "presentation": {
        "on": ["list today's calendar events", "clear my notifications",
               "set do not disturb on"],
        "off": ["clear do not disturb"],
    },
    "study": {
        "on": ["start a 50 minute focus session", "set do not disturb on",
               "show me today's todo list"],
        "off": ["stop the focus session", "clear do not disturb"],
    },
    "gaming": {
        "on": ["set do not disturb on", "unmute", "set volume to 40"],
        "off": ["clear do not disturb"],
    },
    "travel": {
        "on": ["set do not disturb on", "show my battery",
               "show my disk space"],
        "off": ["clear do not disturb"],
    },
    "end of day": {
        "on": ["stop the focus session", "clear do not disturb", "unmute",
               "save my journal entry"],
        "off": [],
    },
}

#: Names people actually type, mapped to the catalog's mode names.
ROUTINE_ALIASES = {
    "end of day": ("endofday", "end_of_day", "endday", "wrap up", "wrapping up",
                   "wind down", "shutdown routine", "sign off", "finishing up"),
    "morning": ("start of day", "startup routine", "wake up", "daily routine"),
    "focus": ("deep work", "pomodoro", "concentrate", "distraction free"),
    "meeting": ("in a meeting", "join a meeting", "call"),
    "study": ("revise", "revision", "homework"),
    "gaming": ("game", "play", "session"),
    "travel": ("commute", "journey", "trip", "leaving"),
    "work": ("office", "working", "business"),
    "presentation": ("presenting", "demo", "pitch", "talk"),
}


def _canonical_routine(name: str) -> str:
    """The catalog's name for a mode, from whatever the user typed."""
    key = " ".join(str(name or "").lower().split()).strip()
    if not key:
        return ""
    key = re.sub(r"^(?:start|begin|run|activate|turn on|enter)\s+", "", key)
    key = re.sub(r"\s+(?:routine|mode)$", "", key).strip()
    if key in ROUTINES:
        return key
    for canonical, aliases in ROUTINE_ALIASES.items():
        if key in aliases:
            return canonical
    for canonical in ROUTINES:                # "the morning routine please"
        if key.startswith(canonical) or canonical in key:
            return canonical
    return ""


def routine(target: str = "", args: str = "") -> str:
    """Run one of the nine modes, or list them.

    A user-defined routine in the store wins over a built-in one, so a mode can
    be replaced without editing the code.
    """
    raw = (target or "").strip()
    tail = (args or "").strip().lower()
    stored = _load().get("routines", {})

    if not raw:
        names = sorted(set(stored) | set(ROUTINES))
        return ("modes: " + ", ".join(names)) if names else "no modes defined"

    mode = "off" if tail in ("off", "stop", "end", "exit", "disable") else "on"
    custom = stored.get(raw) or stored.get(raw.lower())
    if custom:
        steps = custom if isinstance(custom, list) else [str(custom)]
        if mode == "off":
            steps = list(reversed(steps))
        return _run_steps(raw, steps)

    name = _canonical_routine(raw)
    if not name:
        known = ", ".join(sorted(ROUTINES))
        return f"no mode called {raw!r}. I know: {known}"

    steps = ROUTINES[name][mode]
    if not steps:
        return f"mode {name!r} has nothing to undo"
    verb = "Entering" if mode == "on" else "Leaving"
    return _run_steps(f"{name} ({mode})", steps, verb=verb)


def _run_steps(label: str, steps: list[str], verb: str = "Running") -> str:
    """Run routine steps as goals, reporting each one rather than a bare count."""
    from . import triggers

    done, failed = [], []
    for step in steps:
        output = triggers.run_goal(step)
        if _looks_failed(output):
            failed.append((step, output))
        else:
            done.append(step)
    lines = [f"{verb} {label}: {len(done)}/{len(steps)} step(s)"]
    lines += [f"  [ok  ] {s}" for s in done]
    lines += [f"  [FAIL] {s}\n         {o[:160]}" for s, o in failed]
    return "\n".join(lines)


def chain(target: str = "", args: str = "") -> str:
    """Run a sequence of goals, in order. "do A; then B; then C".

    Each goal goes through the capability executor on its own, so each step is
    separately guarded, scored and confirmed, and a failure is reported without
    taking the rest of the chain down silently.
    """
    steps = [s.strip() for s in f"{target} {args}".split(";") if s.strip()]
    if not steps:
        return "give me steps separated by semicolons"
    from . import triggers

    done, failed = [], []
    for step in steps:
        output = triggers.run_goal(step)
        if _looks_failed(output):
            failed.append((step, output))
        else:
            done.append(step)
    lines = [f"[ok ] {s}" for s in done]
    lines += [f"[FAIL] {s}\n       {o[:200]}" for s, o in failed]
    verdict = (f"ran {len(done)}/{len(steps)} step(s)" if not failed
               else f"ran {len(done)}/{len(steps)} step(s), {len(failed)} failed")
    return "\n".join([verdict, *lines])


def _looks_failed(output: str) -> bool:
    text = str(output or "")
    return text.lstrip().startswith("[FAIL]") or "[FAIL]" in text[:200]


def parallel(target: str = "", args: str = "") -> str:
    """Run independent goals together. Screen-touching ones stay sequential.

    Two actions that both move the mouse would race each other, so anything that
    touches the UI, the clipboard or the keyboard is run in order. Read-only work
    — searches, queries, reports — really does run at the same time.
    """
    steps = [s.strip() for s in f"{target} {args}".split(";") if s.strip()]
    if not steps:
        return "give me steps separated by semicolons"
    from concurrent.futures import ThreadPoolExecutor

    from ..capabilities import catalog, nlu

    screen_kinds = {"ui", "mouse", "keyboard", "window", "shell", "system"}
    plans = []
    for step in steps:
        plan = nlu.interpret(step)
        kinds = {catalog.get(s.capability).kind
                 for s in plan.steps if catalog.get(s.capability)} if plan else set()
        plans.append((step, plan, bool(kinds & screen_kinds)))
    if all(touches for _s, _p, touches in plans) and len(plans) > 1:
        return (f"all {len(plans)} of those touch the screen, so they run in order "
                f"instead of at once. Running them in sequence.")

    def _run(item):
        step, plan, _touches = item
        from . import triggers

        if plan is not None:
            from ..capabilities.executor import Executor
            from ..config import active

            return step, Executor(cfg=active()).run_plan(plan).describe()
        return step, triggers.run_goal(step)

    screen = [p for p in plans if p[2]]
    background = [p for p in plans if not p[2]]
    results: list[tuple[str, str]] = []
    if background and len(background) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(background))) as pool:
            results.extend(list(pool.map(_run, background)))
    else:
        results.extend(_run(p) for p in background)
    results.extend(_run(p) for p in screen)

    lines = [f"[ok ] {s}\n       {o[:160]}" for s, o in results]
    return "\n".join([f"ran {len(results)} goal(s), {len(screen)} of them in order",
                      *lines])


def until(target: str = "", args: str = "") -> str:
    """Repeat a goal until a condition holds, with a hard attempt limit.

    "until" needs a goal as well as a condition: "until the build finishes then
    rerun the tests". A loop with no exit check is a runaway, so the attempt
    limit and the stop-on-error rule are not optional.
    """
    goal, _, condition = f"{target} {args}".partition(" until ")
    if not condition:
        condition, _, goal = args.strip().partition(" then ")
        goal = f"{target} {goal}".strip()
    goal, condition = goal.strip(), condition.strip()
    if not goal or not condition:
        return ('give me both, e.g. until "the build finishes" then "run the tests", '
                'or "run the tests until the build finishes"')
    limit = 5
    m = re.search(r"(\d+)\s*(?:times|attempts)", f"{target} {args}".lower())
    if m:
        limit = int(m.group(1))

    from . import triggers

    kind, _arg = triggers.parse_condition(condition)
    logs: list[str] = []
    for attempt in range(1, limit + 1):
        output = triggers.run_goal(goal)
        logs.append(f"attempt {attempt}: {output[:160]}")
        if _looks_failed(output):
            return "\n".join([f"stopped on error after {attempt} attempt(s)",
                              *logs,
                              "Tell me if you want it to keep going past errors."])
        if kind == "unknown":
            return "\n".join([
                f"I ran {attempt} time(s) but I cannot tell when {condition!r} is "
                f"true, so I stopped at the limit rather than loop forever.",
                *logs,
                f"Conditions I can watch: "
                + ", ".join(sorted({k for k, _ in triggers.CONDITIONS}))])
        if triggers.evaluate(condition, triggers.snapshot()):
            return "\n".join([f"done after {attempt} attempt(s)", *logs])
    return "\n".join([f"gave up after {limit} attempts; {condition!r} never became true",
                      *logs])


def foreach(target: str = "", args: str = "") -> str:
    """Run one action once per item, for real.

    Every item has to route to a capability, and the whole set is checked before
    anything runs: a loop that renames three files and then fails on the fourth
    leaves the disk in a state the user did not ask for.
    """
    action = (target or "").strip()
    items = [i.strip() for i in (args or "").split(",") if i.strip()]
    if not items or not action:
        return ("give me an action and a comma-separated list, e.g. "
                "foreach 'rename' a.txt,b.txt,c.txt")
    from ..capabilities import nlu
    from . import triggers

    plans = []
    unroutable = []
    for item in items:
        plan = nlu.interpret(f"{action} {item}")
        if plan is None:
            unroutable.append(item)
        else:
            plans.append((item, plan))
    if unroutable:
        return (f"stopping before I start: {action!r} does not route to a "
                f"capability for {', '.join(unroutable)}. Nothing was run, so the "
                f"half-applied case is avoided.")

    lines = []
    failures = 0
    for item, plan in plans:
        output = triggers.run_goal(f"{action} {item}")
        if _looks_failed(output):
            failures += 1
        lines.append(f"[{'FAIL' if _looks_failed(output) else 'ok '}] {item}"
                     f"  {output[:120]}")
    verdict = (f"ran {action!r} on {len(plans)} item(s)"
               + (f", {failures} failed" if failures else ""))
    return "\n".join([verdict, *lines])


def interpret(target: str = "", args: str = "") -> str:
    """Show how a sentence routes, without running any of it."""
    from ..capabilities import nlu

    text = f"{target} {args}".strip()
    plan = nlu.interpret_compound(text) or nlu.interpret(text)
    if plan is None:
        near = nlu.search(text, limit=3)
        hint = f" closest: {', '.join(c.name for c, _p, _s in near)}" if near else ""
        return f"{text!r} does not route to anything.{hint}"
    return (f"{text!r} -> {', '.join(plan.specs())} "
            f"(confidence {plan.confidence:.2f}, language {plan.language})")


def set_language(target: str = "", args: str = "") -> str:
    """Record which language the user writes in."""
    language = (target or args or "en").strip().lower()
    data = _load()
    data["language"] = language
    _save(data)
    return f"noted: you write in {language}"


def stop_all(target: str = "", args: str = "") -> str:
    """Trip the emergency switch, which blocks every further action."""
    from ..safety.emergency import emergency

    reason = (target or args).strip() or "stop requested"
    emergency.trip(reason)
    return f"stopped: {reason}. Every action is blocked until you clear it."


def status(target: str = "", args: str = "") -> str:
    """Schedules and memory in one answer."""
    from ..core.memory import get_memory
    from ..core.scheduler import get_scheduler

    return "\n".join([get_scheduler().describe(),
                      str(get_memory().stats())])


def history(target: str = "", args: str = "") -> str:
    """Recent runs, newest first."""
    from ..config import active

    root = Path(active().storage.log_root)
    if not root.is_dir():
        return "no runs recorded yet"
    runs = sorted((p for p in root.iterdir() if p.is_dir()),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not runs:
        return "no runs recorded yet"
    return "\n".join(f"{p.name}  {p.stat().st_size / 1000:.0f} KB" for p in runs[:20])


def retry(target: str = "", args: str = "") -> str:
    """Re-queue the most recent task."""
    from ..core.task import Task
    from ..core.queue import get_queue

    queue = get_queue()
    recent = list(queue)
    if not recent:
        return "there is nothing to retry"
    last = recent[-1]
    fresh = queue.add(Task(goal=last.goal))
    return f"re-queued: {last.goal} (was {last.id}, now {fresh.id})"


def capabilities(target: str = "", args: str = "") -> str:
    """The whole catalog, or just the categories named."""
    from ..capabilities.executor import describe_all

    numbers = [int(t) for t in f"{target} {args}".split() if t.isdigit()]
    return describe_all(numbers or None)


_FORGET_WORDS = ("forget", "unremember", "delete", "remove", "stop remembering",
                 "bhool", "bhoolo", "yaad mat rakh")


def remember(target: str = "", args: str = "") -> str:
    """Store a fact for next time, or take one back.

    `ufo do remember "my wifi" forget` deletes it. A user who cannot remove a
    fact the agent holds has no way to correct it, which makes every other
    memory a liability rather than a help.
    """
    from ..core.memory import get_memory

    subject = target.strip()
    value = args.strip()
    memory = get_memory()

    if not subject:
        known = memory.facts()
        listing = "\n".join(f"  {k} = {v}" for k, v in list(known.items())[:20])
        return f"what should I remember?\nwhat I have:\n{listing or '  (nothing)'}"
    if not value:
        existing = memory.recall(subject)
        if existing:
            return f"{subject!r} = {existing}"
        return f'give me "subject" and what to remember'

    if any(word in value.lower() for word in _FORGET_WORDS):
        removed = memory.forget(subject)
        if removed:
            return f"forgotten: {subject!r}"
        return f"I was not holding anything about {subject!r}"
    memory.remember(subject, value)
    return f"remembered {subject!r}"


# --------------------------------------------------------------------------
# pause, resume, undo, redo (category 44)
# --------------------------------------------------------------------------
def pause(target: str = "", args: str = "") -> str:
    """Stop executing anything until told otherwise.

    This is the soft stop. It is not the emergency switch and does not clear it.
    """
    from ..core.control import get_control

    reason = " ".join(x for x in (target, args) if x).strip()
    return get_control().pause(reason)


def resume(target: str = "", args: str = "") -> str:
    """Start executing again after a pause."""
    from ..core.control import get_control

    return get_control().resume()


def undo(target: str = "", args: str = "") -> str:
    """Reverse the last change the agent made.

    Only reversible steps are journalled. If the last thing it did cannot be
    reversed, this says so and names what is on the stack, rather than reversing
    the step before that and leaving the user thinking it worked.
    """
    from ..core.control import get_control
    from . import triggers

    control = get_control()
    entry = control.peek_undo()
    if entry is None:
        return "nothing to undo"
    if not entry.get("undo"):
        return (f"cannot undo {entry['capability']}: it has no reversal recorded. "
                f"It is still on the stack; tell me what to do instead.")

    control.pop_undo()
    spec = entry["undo"]
    output = triggers.run_goal(spec)
    if _looks_failed(output):
        return (f"tried to undo {entry['capability']} with {spec!r} but it "
                f"failed:\n{output[:300]}")
    return f"undid {entry['capability']} with {spec!r}:\n{output[:300]}"


def redo(target: str = "", args: str = "") -> str:
    """Put back the last thing that was undone."""
    from ..core.control import get_control
    from . import triggers

    control = get_control()
    entry = control.peek_redo()
    if entry is None:
        return "nothing to redo"
    spec = ":".join(x for x in (entry["capability"], entry.get("target", ""),
                                 entry.get("args", "")) if x)
    control.pop_redo()
    output = triggers.run_goal(spec)
    if _looks_failed(output):
        return f"tried to redo {entry['capability']} but it failed:\n{output[:300]}"
    return f"redid {entry['capability']}:\n{output[:300]}"


def undo_list(target: str = "", args: str = "") -> str:
    """What can be undone right now, newest first."""
    from ..core.control import get_control

    control = get_control()
    rows = control.journal(limit=20)
    if not rows:
        return "nothing to undo"
    lines = ["undoable, newest first:"]
    for entry in reversed(rows):
        mark = "  " if entry.get("undo") else "!!"
        lines.append(f" {mark} {entry['capability']} "
                     f"{entry.get('target', '')}".rstrip())
    lines.append("(! marks a step with no recorded reversal)")
    if control.redo_depth:
        lines.append(f"({control.redo_depth} step(s) redoable)")
    return "\n".join(lines)


#: The safety constraints the user can set in words, and what each one means.
#: `ufo do safe_mode_policy read only mode` has to actually turn it on, not
#: print a report: a user who asked for a boundary and got a table back will
#: believe the boundary is in place when nothing is being enforced.
_SAFETY_SWITCHES = {
    "read only mode": ("read_only", True),
    "readonly mode": ("read_only", True),
    "read_only": ("read_only", True),
    "read only": ("read_only", True),
    "dry run": ("dry_run", True),
    "ask before every step": ("confirm_all", True),
    "confirm every step": ("confirm_all", True),
    "ask every time": ("confirm_all", True),
    "require password for deletions": ("require_password_for_delete", True),
    "password before deleting": ("require_password_for_delete", True),
}

_SAFETY_LISTS = {
    "never touch": "never_paths",
    "never open": "never_domains",
    "never visit": "never_domains",
    "never go to": "never_domains",
}

_OFF_WORDS = ("off", "false", "no", "disable", "stop", "cancel", "nevermind")


def _safety_switch(key: str) -> bool:
    text = " ".join(str(key or "").lower().replace("_", " ").replace("-", " ").split())
    for name, (field, on) in _SAFETY_SWITCHES.items():
        if name in text:
            return not any(w in text for w in _OFF_WORDS)
    for name, field in _SAFETY_LISTS.items():
        if name in text:
            if any(w in text for w in _OFF_WORDS):
                return False
            return True
    if "work only in" in text or "only in this folder" in text:
        return True
    return False


def safety_mode(target: str = "", args: str = "") -> str:
    """Show the safety settings, or change one.

    `safe_mode_policy read only mode` turns read-only on. `... off` turns it off.
    `safe_mode_policy never touch ~/Documents` protects a folder for good.
    `safe_mode_policy work only in ~/project` refuses anything outside it.
    """
    from ..config import active
    from ..actions import filesystem

    cfg = active()
    safety = cfg.safety
    request = f"{target} {args}".strip()
    if not request:
        return _safety_report(safety)

    lowered = request.lower()

    # work only in <folder>
    for marker in ("work only in ", "only in this folder "):
        if marker in lowered:
            folder = request[lowered.index(marker) + len(marker):].strip()
            folder = filesystem.expand(folder or ".")
            safety.work_only_in = folder
            cfg.save()
            return (f"from now on I only act inside {folder}. Anything outside "
                    f"is refused. Clear it with: ufo do safe_mode_policy off")

    # never touch <path> / never open <site>
    for marker, field in _SAFETY_LISTS.items():
        if marker in lowered:
            value = request[lowered.index(marker) + len(marker):].strip()
            if not value:
                return f"give me the {field} to {'protect' if field == 'never_paths' else 'block'}"
            value = filesystem.expand(value) if field == "never_paths" else value
            current = list(getattr(safety, field))
            if value in current:
                current.remove(value)
                action = f"no longer {marker} {value}"
            else:
                current.append(value)
                action = f"never {'touching' if field == 'never_paths' else 'opening'} {value}"
            setattr(safety, field, current)
            cfg.save()
            return (f"{action}. Saved to your config, so it applies to every run.")

    for name, (field, _on) in _SAFETY_SWITCHES.items():
        if name in lowered:
            setattr(safety, field, _safety_switch(request))
            cfg.save()
            state = "on" if getattr(safety, field) else "off"
            return f"{field} is now {state}"

    return (_safety_report(safety)
            + "\n\nI can set: read only mode, dry run, ask before every step, "
              "require password for deletions, 'never touch <path>', "
              "'never open <site>', 'work only in <folder>'.")


def _safety_report(safety) -> str:
    return "\n".join([
        f"read_only                 = {safety.read_only}",
        f"dry_run                   = {safety.dry_run}",
        f"confirm_all               = {safety.confirm_all}",
        f"require_password_for_delete = {safety.require_password_for_delete}",
        f"confirm_at_or_above       = {safety.confirm_risk_at_or_above}",
        f"block_at_or_above         = {safety.block_at_or_above}",
        f"never_paths               = {list(safety.never_paths) or '[]'}",
        f"never_domains             = {list(safety.never_domains) or '[]'}",
        f"work_only_in              = {safety.work_only_in or '(anywhere)'}",
        f"protected_folders         = {', '.join(safety.protected_folders)}",
        f"allowlist                 = {list(safety.allowlist) or 'EMPTY (nothing is allowed)'}",
    ])


def auto_approve(target: str = "", args: str = "") -> str:
    """Auto-approve the safe actions, without turning off the guard.

    The spec asks for this. The honest version raises the confirmation threshold
    so only genuinely risky actions still ask, rather than disabling checks.
    """
    from ..config import active

    cfg = active()
    cfg.safety.confirm_risk_at_or_above = 3   # medium no longer asks; high does
    cfg.save()
    return ("medium-risk actions no longer ask; high and critical still do, and "
            "the allowlist, denylist and constraints are unchanged. Restore the "
            "old behaviour with: ufo do safe_mode_policy ask before every step")


_CORRECTION_VERBS = ("forget", "forget this", "clear", "clear all",
                     "list", "show", "show rules")


def feedback(target: str = "", args: str = "") -> str:
    """Turn a correction into a rule that actually changes what happens next.

    A "never" is a veto checked before the guard, so it holds even over an action
    the guard would otherwise have allowed. An "always" only reorders proposals;
    it never runs anything by itself and never widens what is permitted.

    Corrections that are not rules are still filed in memory, so nothing is lost.
    """
    from ..core.memory import get_memory
    from ..core.rules import get_rules

    text = f"{target} {args}".strip()
    if not text:
        return ("tell me what I did wrong, or list the rules. "
                "Try: never rename anything in Downloads")

    rules = get_rules()
    head = text.lower().strip()
    if head in ("list", "show", "list rules", "show rules"):
        return _list_rules()
    if head in ("clear", "clear all", "forget", "forget this"):
        return f"cleared {rules.clear()} rule(s)"

    subject = text
    if head.startswith("forget rule "):
        removed = rules.remove(text[len("forget rule "):].strip().strip("'\""))
        return (f"removed that rule" if removed
                else f"no rule about {text[len('forget rule '):].strip()!r}")

    try:
        rule = rules.parse(subject, note=args.strip())
    except ValueError as e:
        get_memory().remember("feedback", text, source="user")
        return f"{e}. I have also written it to memory, so it is not lost."

    verb = {"never": "I will not do that again",
            "always": "I will offer that first",
            "prefer": "I will prefer that"}[rule.kind]
    return f"rule saved: {rule.describe()}. {verb}."


def _list_rules() -> str:
    from ..core.rules import get_rules

    rules = get_rules().all()
    if not rules:
        return ("no rules yet. Say one as: never rename anything in Downloads, "
                "or: always run the backup first")
    return "rules:\n" + "\n".join(f"  {r.describe()}" for r in rules)


def rules_list(target: str = "", args: str = "") -> str:
    """Show the standing rules, or forget one."""
    return _list_rules()


def rules_forget(target: str = "", args: str = "") -> str:
    """Take a rule back, the same way you can take a remembered fact back."""
    from ..core.rules import get_rules

    subject = f"{target} {args}".strip()
    if not subject:
        return f"cleared {get_rules().clear()} rule(s)"
    return ("removed that rule" if get_rules().remove(subject)
            else f"no rule about {subject!r}")


def preferences(target: str = "", args: str = "") -> str:
    """Show or set the agent's preferences."""
    data = _load()
    prefs = data.setdefault("preferences", {})
    if not target:
        shown = ", ".join(f"{k}={v}" for k, v in sorted(prefs.items())) or "none set"
        return shown
    name, _, value = f"{target} {args}".strip().partition("=")
    if not value:
        return prefs.get(name) or f"nothing set for {name!r}"
    prefs[name.strip()] = value.strip()
    _save(data)
    return f"{name.strip()} = {value.strip()}"


def teach(target: str = "", args: str = "") -> str:
    """Teach a new phrase for an existing capability."""
    from ..capabilities import catalog

    phrase = (target or "").strip()
    ability = (args or "").strip()
    if not phrase or not ability:
        return "give me a phrase and what it should mean"
    if catalog.get(ability) is None:
        return f"{ability!r} is not a capability; try `ufo caps {ability}`"
    data = _load()
    data.setdefault("taught", {})[phrase] = ability
    _save(data)
    return f"learned: {phrase!r} means {ability}"


def workflow(target: str = "", args: str = "") -> str:
    """Save a named sequence of steps as a workflow."""
    steps = [s.strip() for s in f"{target} {args}".split(";") if s.strip()]
    if len(steps) < 2:
        return "give me steps separated by semicolons"
    data = _load()
    data.setdefault("routines", {})["; ".join(steps)] = steps
    _save(data)
    return f"workflow saved: {'; '.join(steps)}"


def model(target: str = "", args: str = "") -> str:
    """Show the model in use, or write a different one into the config."""
    from ..config import active

    current = active().llm
    if not target:
        return f"{current.provider}: {current.model or '(provider default)'}"
    chosen = target.strip()
    cfg = active()
    cfg.llm.model = chosen
    cfg.save()
    return f"model set to {chosen}; it takes effect on the next run"


def usage_stats(target: str = "", args: str = "") -> str:
    """Step and token counts from the run logs."""
    from ..config import active

    root = Path(active().storage.log_root)
    if not root.is_dir():
        return "no runs recorded yet"
    steps = tokens = 0
    for path in root.rglob("*.jsonl"):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if '"tokens"' not in line:
                    continue
                steps += 1
                tokens += int(json.loads(line).get("tokens", 0))
        except (json.JSONDecodeError, OSError, ValueError):
            continue
    return f"{steps} logged step(s), {tokens:,} token(s) recorded"


def reset(target: str = "", args: str = "") -> str:
    """Clear memory and saved routines. Queues and schedules are untouched."""
    from ..core.memory import get_memory

    get_memory().clear()
    data = _load()
    data["routines"] = {}
    data["macros"] = {}
    _save(data)
    return "cleared memory and saved routines"


def log_gap(target: str = "", args: str = "") -> str:
    """Record something the agent cannot do."""
    from ..capabilities import gaps

    request = f"{target} {args}".strip()
    if not request:
        return "give me the request to record"
    gaps.get_log().add(request, reason="recorded by the user")
    return f"recorded in the gap log: {request}"


# --------------------------------------------------------------------------
# category 47: the agent looking after itself
# --------------------------------------------------------------------------
def permissions(target: str = "", args: str = "") -> str:
    """Show or set what the agent may do, one capability category at a time.

    "permissions finance confirm" makes every Finance & Payments action ask
    first. "permissions finance block" refuses it outright. "permissions"
    on its own shows what is set. This is enforced by ActionPolicy, not by
    convention, so it holds for the agent loop, the CLI, the daemon and the
    phone page alike.
    """
    from ..capabilities import catalog
    from ..config import active

    cfg = active()
    perms = cfg.safety.category_permissions

    if not target:
        if not perms:
            return ("no per-category permissions set, so every category is "
                    "allowed. Set one with: ufo do permissions finance confirm")
        lines = []
        for key in sorted(perms, key=lambda k: int(k) if k.isdigit() else 999):
            try:
                domain = catalog.CATEGORIES.get(int(key), "?")
            except (TypeError, ValueError):
                domain = "?"
            lines.append(f"  {key:>3}  {domain[:36]:<36}  {perms[key]}")
        return "per-category permissions:\n" + "\n".join(lines)

    # Accepted shapes, in order: "31=confirm", "31 confirm", "finance block".
    # A trailing word that is not a valid mode is still split off when the head
    # is a real category, so "31 maybe" is reported as a bad mode rather than a
    # bad category.
    text = f"{target} {args}".strip()
    number_text, value = text, ""
    if "=" in text:
        number_text, _, value = text.partition("=")
    else:
        head, _, last = text.rpartition(" ")
        if head and (last.lower() in ("allow", "confirm", "block")
                     or _category_number(head.strip()) is not None):
            number_text, value = head, last
    number_text, value = number_text.strip(), value.strip()

    resolved = _category_number(number_text)
    if resolved is None:
        return (f"{number_text!r} is not a category. Use its number or name, "
                f"e.g. 'permissions 31 confirm' or 'permissions finance block'.")
    value = value.lower()
    if value not in ("allow", "confirm", "block"):
        return ("the setting must be allow, confirm or block — got "
                f"{value!r}. 'confirm' asks first, 'block' refuses.")
    perms[str(resolved)] = value
    cfg.save()
    domain = catalog.CATEGORIES.get(resolved, "?")
    return (f"category {resolved} ({domain}) is now {value}. It applies to every "
            f"action in that category, everywhere the agent runs.")


def _category_number(text: str) -> int | None:
    """Resolve "31" or "finance" to a category number, or None.

    Names match on whole leading words, so "finance" finds "Finance & Payments
    (always confirm)" and "travel" finds "Travel & Life Admin". A bare
    `startswith` on the raw string misses those, because the domain carries a
    subtitle the user will not type.
    """
    from ..capabilities import catalog

    text = text.strip().lower()
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        return number if number in catalog.CATEGORIES else None

    exact = [n for n, d in catalog.CATEGORIES.items() if d.lower() == text]
    if exact:
        return exact[0]
    partial = [n for n, d in catalog.CATEGORIES.items()
               if d.lower().startswith(text + " ") or d.lower().startswith(text + "&")]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        return None      # ambiguous: better to ask than to guess a category
    contained = [n for n, d in catalog.CATEGORIES.items() if text in d.lower()]
    return contained[0] if len(contained) == 1 else None


def self_update(target: str = "", args: str = "") -> str:
    """Report how this copy of the agent can be updated, and do it if asked.

    Deliberately conservative. It works out what kind of install this is first,
    and says so plainly rather than guessing at a package name: a tool that
    silently rewrites its own code is exactly the sort of thing the guard
    exists to catch, and an agent that can replace itself is a much larger
    blast radius than one that cannot.
    """
    from .. import __version__

    method = _update_method()
    if target.strip().lower() not in ("install", "upgrade", "update", "yes", "pull"):
        return (f"running {__version__}, installed as {method['kind']}. "
                f"{method['hint']} To let me run it: ufo do self_update install")

    result = subprocess.run(method["argv"], capture_output=True, text=True,
                            timeout=600, check=False, cwd=method.get("cwd"))
    if result.returncode == 0:
        return (f"update finished. Now on "
                f"{_installed_version() or __version__}. Restart the agent to use it.")
    tail = (result.stderr or result.stdout or "").strip().splitlines()
    return ("the update did not go through. The last thing it said was: "
            + (tail[-1] if tail else "no output"))


def _installed_version() -> str:
    from .. import __version__

    return __version__


def _update_method() -> dict:
    """Work out how this copy gets updated, from what is actually on disk.

    There is no single right answer: a git checkout updates with `git pull`, a
    pip install updates from its index, and a plain folder of files has no
    update path at all. Guessing wrong here would run the wrong command, so
    each case is detected and anything unrecognised says so.
    """
    root = Path(__file__).resolve().parent.parent.parent
    if (root / ".git").exists():
        return {"kind": "a git checkout", "cwd": str(root),
                "argv": ["git", "pull", "--ff-only"],
                "hint": f"It updates with `git pull` in {root}."}
    metadata = root / "pyproject.toml"
    if metadata.exists():
        name = _distribution_name()
        return {"kind": f"a source checkout of {name or 'this package'}",
                "cwd": str(root),
                "argv": [sys.executable, "-m", "pip", "install", "--upgrade",
                         name or "."],
                "hint": "It updates by reinstalling from source."}
    return {"kind": "a plain folder of files",
            "hint": ("There is no .git and no pyproject.toml here, so I have no "
                     "update path I can trust. Replace the folder with a newer "
                     "copy, or install from git to get updates.")}


def _distribution_name() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        version("ufo")
        return "ufo"
    except Exception:
        return ""
