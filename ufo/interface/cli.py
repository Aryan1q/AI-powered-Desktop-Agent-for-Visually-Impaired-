"""The command line.

One entry point (`ufo`) with subcommands, so the surface is discoverable
instead of 40 flags on one command:

    ufo run "task"                 run a task now (the common case)
    ufo plan "task"                decompose first, then run each step
    ufo do "what to do"            a single non-agent action, no model call
    ufo ask "question"              one-shot look at the screen and answer
    ufo windows | screenshot | read | see | clip    inspection
    ufo queue ...   ufo schedule ...  ufo daemon ...   background work
    ufo serve                      the HTTP API + dashboard
    ufo memory ...  ufo config ...  ufo secrets ...  ufo doctor

`ufo doctor` exists because an agent that cannot see the screen, cannot reach
the model, or has no allowlist is the single most common failure, and each of
those is a one-command diagnosis.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .. import __version__
from ..config import Config, set_active
from ..errors import ConfigError, UFOError
from ..logging_setup import configure, get_logger, run as run_logging
from ..utils import text as textutil
from ..utils import win32

log = get_logger(__name__)

PROG = "ufo"


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="A whole-laptop automation agent for Windows.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            f"  {PROG} run \"summarize today's emails in Notepad\"\n"
            f"  {PROG} run \"type hello\" --exe notepad.exe --allow Notepad\n"
            f"  {PROG} run \"rename every .txt in Downloads\" --dry-run\n"
            f"  {PROG} run \"convert report.pdf to word, change 2024 to 2025, "
            f"convert it back to pdf\"\n"
            f"  {PROG} pdf roundtrip --file report.pdf --replace 2024 2025\n"
            f"  {PROG} do volume 30\n"
            f"  {PROG} windows\n"
            f"  {PROG} serve --port 8765\n"
            f"  {PROG} schedule add \"0 9 * * 1-5\" --task \"clear my inbox\"\n"
            f"  {PROG} daemon start\n"
            f"  {PROG} doctor\n\n"
            "Safety: nothing is allowed until you allowlist it (--allow / ALLOWED_APPS).\n"
            "Emergency stop: Ctrl+Alt+Q at any time, or type 'stop' at a prompt.\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"ufo {__version__}")
    parser.add_argument("-c", "--config", help="path to a JSON/YAML config file")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="warnings only")
    parser.add_argument("--home", help="override the state directory (~/.ufo)")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true",
                        help="plan and narrate every action, but change nothing")
    common.add_argument("--yes", action="store_true",
                        help="do not ask for confirmation (still respects the allowlist)")
    common.add_argument("--confirm-all", action="store_true",
                        help="ask before every single action")
    common.add_argument("--voice", action="store_true", help="enable spoken narration")
    common.add_argument("--no-voice", action="store_true", help="disable spoken narration")
    common.add_argument("--max-steps", type=int, help="step limit for this run")
    common.add_argument("--max-cost", type=float, help="USD ceiling for this run")
    common.add_argument("--out", help="for `run` a skill: where to write its output")
    common.add_argument("--headed", action="store_true",
                        help="for `run` a skill: show its browser while it works")
    common.add_argument("--force-agent", action="store_true",
                        help="ignore skills and send this to the model instead")

    scope = argparse.ArgumentParser(add_help=False)
    scope.add_argument("--allow", action="append", default=[],
                       help="regex a window title must match to be touched (repeatable)")
    scope.add_argument("--deny", action="append", default=[],
                       help="regex that always blocks a window (repeatable)")
    scope.add_argument("--allow-all", action="store_true",
                       help="allow every window. Only use this deliberately.")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # -- run --
    p = sub.add_parser("run", parents=[common, scope],
                       help="automate a task with the model")
    p.add_argument("task", nargs="?", default="",
                   help="what to do, in your own words")
    p.add_argument("--exe", help="launch this program and attach to its new window")
    p.add_argument("--title", help="regex for the launched window's title")
    p.add_argument("--mode", choices=["computer", "app", "plan"], default="computer",
                   help="whole laptop (default), one window, or plan-then-run")
    p.add_argument("--provider", help="only ollama is allowed; any other value "
                                   "is refused while llm.offline is on")
    p.add_argument("--model", help="model id override")
    p.add_argument("--input", action="store_true", help="read the task from stdin")

    # -- plan --
    p = sub.add_parser("plan", help="decompose a task into steps and show the plan")
    p.add_argument("task")

    # -- listen (phone microphone) --
    p = sub.add_parser("listen", parents=[common, scope],
                       help="take spoken instructions from a phone on the same Wi-Fi")
    p.add_argument("--host", default=None,
                   help="interface to bind (default from phone.host, 0.0.0.0)")
    p.add_argument("--port", type=int, default=None,
                   help="port for the phone to connect to (default from phone.port)")
    p.add_argument("--auto", action="store_true",
                   help="run spoken instructions without asking")
    p.set_defaults(func=cmd_listen)

    # -- pdf (scripted round trip) --
    p = sub.add_parser("pdf", parents=[common],
                       help="scripted PDF workflows, no model in the loop")
    ps = p.add_subparsers(dest="pdf_action")
    pr = ps.add_parser("roundtrip",
                       help="PDF -> Word -> edit -> PDF via ilovepdf.com")
    pr.add_argument("--file", required=False, help="the source PDF")
    pr.add_argument("--replace", nargs=2, metavar=("OLD", "NEW"),
                    help='the text to change, e.g. --replace "2024" "2025"')
    pr.add_argument("--out", help="where to write the new PDF (default: beside the source)")
    pr.add_argument("--headed", action="store_true",
                    help="show the browser while it works")
    p.set_defaults(func=cmd_pdf)

    # -- do --
    p = sub.add_parser("do", parents=[common, scope],
                       help="run one deterministic action, no model needed")
    p.add_argument("action", help="an action name, e.g. volume, screenshot, list_windows")
    p.add_argument("args", nargs="*", help="arguments for the action")
    p.set_defaults(func=cmd_do)

    p = sub.add_parser("caps", help="list or search the capability catalog")
    p.add_argument("query", nargs="?", default="", help="search the catalog")
    p.add_argument("args", nargs="*", help="extra category numbers to list")
    p.set_defaults(func=cmd_caps)

    # -- inspection --
    sub.add_parser("windows", help="list every open window and whether it is allowed").set_defaults(
        func=cmd_windows)
    p = sub.add_parser("screenshot", help="save a screenshot of the screen")
    p.add_argument("out", nargs="?", default="", help="output PNG path")
    p.set_defaults(func=cmd_screenshot)
    p = sub.add_parser("read", help="read the text of a window")
    p.add_argument("title", nargs="?", default="", help="window title fragment; default foreground")
    p.set_defaults(func=cmd_read)
    p = sub.add_parser("see", help="print the annotated control list for a window")
    p.add_argument("title", nargs="?", default="")
    p.set_defaults(func=cmd_see)
    p = sub.add_parser("clip", help="clipboard")
    p.add_argument("what", choices=["get", "set", "clear"], nargs="?", default="get")
    p.add_argument("text", nargs="*")
    p.set_defaults(func=cmd_clip)

    # -- queue --
    p = sub.add_parser("queue", help="manage the task queue")
    qs = p.add_subparsers(dest="queue_command", metavar="<action>")
    q = qs.add_parser("add", parents=[scope], help="add a task")
    q.add_argument("task")
    q.add_argument("--priority", choices=["low", "normal", "high", "critical"], default="normal")
    q.add_argument("--max-steps", type=int, default=0)
    qs.add_parser("list", help="show the queue")
    q = qs.add_parser("remove", help="remove a task")
    q.add_argument("id")
    qs.add_parser("clear", help="empty the queue")
    q = qs.add_parser("cancel", help="cancel a queued task")
    q.add_argument("id", nargs="?", default="")
    qs.add_parser("start", help="run queued tasks in this process")
    p.set_defaults(func=cmd_queue, queue_parser=p)

    # -- schedule --
    p = sub.add_parser("schedule", help="recurring and one-shot scheduled tasks")
    ss = p.add_subparsers(dest="schedule_command", metavar="<action>")
    q = ss.add_parser("add", parents=[scope], help="schedule a task")
    q.add_argument("when", help="'in 20 minutes', 'at 17:30', 'tomorrow 08:00', "
                                "or a 5-field cron expression")
    q.add_argument("--task", required=True, help="what to run")
    q.add_argument("--repeat", help="hourly | daily | weekly | weekdays | 'every 30 minutes'")
    q.add_argument("--app", default="")
    q.add_argument("--priority", choices=["low", "normal", "high", "critical"], default="normal")
    q.add_argument("--id", default="", help="explicit schedule id")
    ss.add_parser("list", help="show schedules")
    q = ss.add_parser("remove", help="delete a schedule")
    q.add_argument("id")
    ss.add_parser("tick", help="fire everything that is due right now")
    p.set_defaults(func=cmd_schedule, schedule_parser=p)

    # -- daemon --
    p = sub.add_parser("daemon", help="background worker process")
    ds = p.add_subparsers(dest="daemon_command", metavar="<action>")
    ds.add_parser("start", help="run in the foreground until stopped")
    q = ds.add_parser("background", help="start detached")
    q.add_argument("--max-tasks", type=int, default=0)
    ds.add_parser("status", help="is it running?")
    ds.add_parser("stop", help="ask a running daemon to stop")
    ds.add_parser("panic", help="trip the emergency stop for every process")
    p.set_defaults(func=cmd_daemon, daemon_parser=p)

    # -- serve --
    p = sub.add_parser("serve", help="run the local HTTP API and dashboard")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--no-dashboard", action="store_true")
    p.set_defaults(func=cmd_serve)

    # -- memory --
    p = sub.add_parser("memory", help="what the agent remembers")
    ms = p.add_subparsers(dest="memory_command", metavar="<action>")
    ms.add_parser("show", help="show facts and recent episodes")
    q = ms.add_parser("remember", help="store a fact")
    q.add_argument("key")
    q.add_argument("value")
    q = ms.add_parser("forget", help="delete a fact")
    q.add_argument("key")
    ms.add_parser("clear", help="delete everything")
    ms.add_parser("stats", help="summary numbers")
    p.set_defaults(func=cmd_memory, memory_parser=p)

    # -- secrets --
    p = sub.add_parser("secrets", help="local credential vault")
    ss = p.add_subparsers(dest="secrets_command", metavar="<action>")
    q = ss.add_parser("set")
    q.add_argument("name")
    q.add_argument("value")
    q = ss.add_parser("get")
    q.add_argument("name")
    q = ss.add_parser("remove")
    q.add_argument("name")
    ss.add_parser("list", help="names only, never values")
    p.set_defaults(func=cmd_secrets, secrets_parser=p)

    # -- config --
    p = sub.add_parser("config", help="configuration")
    cs = p.add_subparsers(dest="config_command", metavar="<action>")
    cs.add_parser("show", help="print the effective config")
    q = cs.add_parser("init", help="write a starter config file")
    q.add_argument("--path", default="")
    q.add_argument("--force", action="store_true")
    q = cs.add_parser("set", help="set config values, e.g. 'safety.dry_run=true'")
    q.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    q = cs.add_parser("allow", help="add a window title pattern to the allowlist")
    q.add_argument("patterns", nargs="+")
    q = cs.add_parser("deny", help="add a pattern to the denylist")
    q.add_argument("patterns", nargs="+")
    p.set_defaults(func=cmd_config, config_parser=p)

    # -- diagnostics --
    sub.add_parser("doctor", help="check every subsystem and report what is broken").set_defaults(
        func=cmd_doctor)
    sub.add_parser("logs", help="list recent runs").set_defaults(func=cmd_logs)
    p = sub.add_parser("replay", help="print a run's step log")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_replay)

    return parser


# --------------------------------------------------------------------------
# config assembly
# --------------------------------------------------------------------------
def load_config(args: argparse.Namespace) -> Config:
    overrides: dict[str, Any] = {}

    if getattr(args, "home", None):
        home = Path(args.home).expanduser()
        # Set the env var before loading, not just as an override: it is what
        # decides which config.json is read, so an override alone would merge
        # ~/.ufo/config.json's settings into an unrelated home directory.
        os.environ["UFO_HOME"] = str(home)
        overrides["storage.home"] = str(home)
        overrides["storage.log_root"] = str(home / "logs")
        overrides["storage.memory_path"] = str(home / "memory.json")
        overrides["storage.history_path"] = str(home / "history.jsonl")
        overrides["storage.tasks_path"] = str(home / "tasks.json")
        overrides["storage.schedule_path"] = str(home / "schedule.json")
        overrides["storage.state_path"] = str(home / "state.json")
        overrides["storage.secrets_path"] = str(home / "secrets.json")

    allow = list(getattr(args, "allow", None) or [])
    allow += [p.strip() for p in os.getenv("ALLOWED_APPS", "").split(",") if p.strip()]
    if getattr(args, "allow_all", False):
        allow = [".*"]
    if allow:
        overrides["safety.allowlist"] = allow
    if getattr(args, "deny", None):
        overrides["safety.denylist"] = list(args.deny)

    if getattr(args, "dry_run", False):
        overrides["safety.dry_run"] = True
    if getattr(args, "yes", False):
        # --yes means "no destructive confirmations", not "no policy at all".
        overrides["safety.confirm_risk_at_or_above"] = 4
    if getattr(args, "confirm_all", False):
        overrides["safety.confirm_all"] = True
    if getattr(args, "voice", False):
        overrides["voice.enabled"] = True
    if getattr(args, "no_voice", False):
        overrides["voice.enabled"] = False
    if getattr(args, "max_steps", None):
        overrides["automation.max_steps"] = int(args.max_steps)
    if getattr(args, "max_cost", None):
        overrides["safety.max_cost_usd_per_run"] = float(args.max_cost)
    if getattr(args, "provider", None):
        overrides["llm.provider"] = args.provider
    if getattr(args, "model", None):
        overrides["llm.model"] = args.model
    if getattr(args, "host", None):
        overrides["server.host"] = args.host
    if getattr(args, "port", None):
        overrides["server.port"] = int(args.port)
    if getattr(args, "no_dashboard", False):
        overrides["server.dashboard"] = False
    if getattr(args, "verbose", False):
        overrides["storage.log_level"] = "DEBUG"
    if getattr(args, "quiet", False):
        overrides["storage.log_level"] = "WARNING"

    try:
        cfg = Config.load(getattr(args, "config", None))
    except ConfigError as e:
        raise SystemExit(f"{PROG}: {e}")
    cfg.apply_overrides(overrides)
    set_active(cfg.ensure_dirs())
    return cfg


def _task_from_args(args: argparse.Namespace, goal: str) -> Any:
    from ..core.task import Task, TaskPriority

    return Task(
        goal=goal,
        priority=TaskPriority[str(getattr(args, "priority", "normal")).upper()],
        allow=list(getattr(args, "allow", []) or []),
        deny=list(getattr(args, "deny", []) or []),
        dry_run=bool(getattr(args, "dry_run", False)),
        confirm=bool(getattr(args, "confirm_all", False)),
        max_steps=int(getattr(args, "max_steps", 0) or 0),
        mode=getattr(args, "mode", "computer") or "computer",
        app=getattr(args, "app", "") or getattr(args, "title", "") or "",
    )


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def _maybe_run_skill(goal: str, cfg: Config, args: argparse.Namespace) -> int | None:
    """Run a known skill if this instruction is one. None means "not a skill"."""
    from ..skills import RoundtripError, match_roundtrip, run_roundtrip

    try:
        invocation = match_roundtrip(goal)
    except Exception:
        return None
    if invocation is None:
        return None

    print(invocation.describe())
    if not invocation.source.exists():
        print(f"That file is no longer there: {invocation.source}", file=sys.stderr)
        return 1

    if getattr(args, "dry_run", False):
        print("[dry run] no browser would be opened and nothing would be changed")
        print(f"          source: {invocation.source}")
        print(f"          {invocation.old!r} -> {invocation.new!r}")
        return 0

    def say(message: str) -> None:
        print(f"  {message}", flush=True)

    try:
        result = run_roundtrip(invocation,
                               headless=not getattr(args, "headed", False),
                               progress=say)
    except RoundtripError as e:
        print(f"\nFailed: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    print(f"\n{result.describe()}")
    return 0 if result.ok else 1


def cmd_pdf(args: argparse.Namespace, cfg: Config) -> int:
    """`ufo pdf roundtrip` — the scripted PDF -> Word -> edit -> PDF flow."""
    from ..skills import RoundtripError, roundtrip

    action = str(getattr(args, "pdf_action", "") or "roundtrip").lower()
    if action != "roundtrip":
        print(f"Unknown pdf action {action!r}. Try: ufo pdf roundtrip --file X.pdf "
              f"--replace OLD NEW", file=sys.stderr)
        return 2

    source = getattr(args, "file", None)
    if not source:
        print("Which PDF? Pass --file path/to.pdf", file=sys.stderr)
        return 2
    replace = list(getattr(args, "replace", None) or [])
    if len(replace) != 2:
        print('What should change? Pass --replace "old text" "new text"',
              file=sys.stderr)
        return 2

    path = Path(source).expanduser()
    if not path.exists():
        print(f"No such file: {path}", file=sys.stderr)
        return 2

    if getattr(args, "dry_run", False):
        print("[dry run] nothing would be changed")
        print(f"          {path}")
        print(f"          {replace[0]!r} -> {replace[1]!r}")
        return 0

    try:
        result = roundtrip(path, replace[0], replace[1],
                           out_dir=getattr(args, "out", None),
                           headless=not getattr(args, "headed", False),
                           progress=lambda m: print(f"  {m}", flush=True))
    except RoundtripError as e:
        print(f"\nFailed: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    print(f"\n{result.describe()}")
    return 0 if result.ok else 1


def _maybe_run_capability(goal: str, cfg: Config, args: argparse.Namespace) -> int | None:
    """Run a catalog capability if this instruction names one. None means "not one".

    Placed before the skills and the model: a named capability has a known
    handler, so there is nothing to reason about and no reason to spend a model
    call guessing at it. The guard still decides whether it may actually run.
    """
    from ..capabilities import nlu
    from ..capabilities.executor import Executor

    if not nlu.interpret_compound(goal) and not nlu.interpret(goal):
        return None

    dry_run = getattr(args, "dry_run", False)
    executor = Executor(cfg=cfg, narrate=lambda message: print(f"  {message}", flush=True))
    result = executor.preview(goal) if dry_run else executor.handle(goal)
    if result.outcome == "UNSUPPORTED":
        return None            # let the agent have it after all
    print(result.describe())
    return 0 if result.ok else 1


def cmd_run(args: argparse.Namespace, cfg: Config) -> int:
    goal = args.task
    if getattr(args, "input", False) or (not goal and not sys.stdin.isatty()):
        goal = sys.stdin.read().strip()
    if not goal:
        print("Nothing to do. Pass a task:  ufo run \"type hello in Notepad\"", file=sys.stderr)
        return 2

    # A catalog capability is a named, deterministic handler, so it runs before
    # the model is consulted. Skills drive their own browser and must not be
    # gated on the window allowlist; capabilities go through the guard instead.
    if not getattr(args, "force_agent", False):
        handled = _maybe_run_capability(goal, cfg, args)
        if handled is not None:
            return handled

    if not getattr(args, "force_agent", False):
        handled = _maybe_run_skill(goal, cfg, args)
        if handled is not None:
            return handled

    if not cfg.safety.allowlist:
        print(
            "No window is allowed, so the agent would have nothing it is permitted to touch.\n"
            "Add at least one:  ufo run \"...\" --allow Notepad\n"
            "or allow everything deliberately: --allow-all",
            file=sys.stderr,
        )
        return 2

    from ..core.runner import Runner
    from ..safety.emergency import emergency
    from ..voice import HotkeyListener, Narrator, beep_pattern

    narrator = Narrator(enabled=cfg.voice.enabled, rate=cfg.voice.rate,
                        volume=cfg.voice.volume,
                        console=cfg.voice.confirmation_style != "voice")
    emergency.start_hotkey_watch(cfg.safety.emergency_hotkey)
    listener = HotkeyListener()
    listener.register(cfg.safety.emergency_hotkey, lambda: emergency.trip(
        "panic hotkey pressed"), name="emergency")
    listener.register("ctrl+alt+p", lambda: narrator.say(
        f"Step budget used: {len(history[-1:])}"), name="status")
    listener.start()

    task = _task_from_args(args, goal)

    if args.exe:
        from ..actions import shell
        from ..perception import uia

        before = uia.snapshot_handles()
        try:
            shell.launch(args.exe)
        except UFOError as e:
            print(f"Could not launch {args.exe}: {e}", file=sys.stderr)
            return 1
        pattern = args.title or ".*"
        if not any(uia.is_allowed(uia.WindowInfo(h, t), cfg.safety.allowlist)
                   for h, t in _visible_titles()):
            pass
        try:
            uia.find_new_window(pattern, before,
                                timeout=cfg.automation.window_timeout)
        except UFOError as e:
            print(f"{e}\nThe app started but no matching window appeared.", file=sys.stderr)
            return 1
        task.app = args.title or ""
        task.mode = "app"

    if cfg.safety.dry_run:
        narrator.say(f"Dry run. I will not change anything. Task: {goal}")

    history: list[str] = []
    runner = Runner(cfg, narrator=narrator)
    runner.run = _traced_run(runner, history)  # type: ignore[method-assign]
    try:
        with run_logging(goal, cfg):
            done = runner.run(task)
    except KeyboardInterrupt:
        narrator.say("Stopped.")
        return 130
    finally:
        listener.stop()
        emergency.stop_hotkey_watch()

    beep_pattern("done" if done.status.successful else "error")
    print()
    print(f"Result: {done.result.summary()}")
    if done.result.evidence:
        print(f"Evidence: {textutil.truncate(done.result.evidence, 300)}")
    if done.log_dir:
        print(f"Logs: {done.log_dir}")
    return 0 if done.status.successful else 1


def _traced_run(runner: Any, history: list[str]) -> Any:
    original = runner.run

    def wrapper(task):
        done = original(task)
        history.append(done.result.summary())
        return done

    return wrapper


def _visible_titles() -> list[tuple[int, str]]:
    from ..perception import uia

    return [(w.handle, w.title) for w in uia.list_all_windows()]


def cmd_listen(args: argparse.Namespace, cfg: Config) -> int:
    """Take spoken instructions from a phone on the same Wi-Fi.

    Opens a small page on the network; the phone's own browser does the
    speech recognition and sends back text. Every instruction is read back and
    confirmed here unless --auto is given, so nothing spoken can silently take
    over the machine.
    """
    from ..safety.emergency import emergency
    from ..voice import HotkeyListener, build_narrator, get_persona
    from ..voice.phone import PhoneListener, PhoneListenerConfig

    persona = get_persona(cfg)
    narrator = build_narrator(cfg, console=cfg.voice.confirmation_style != "voice")

    host = args.host if args.host is not None else cfg.phone.host
    port = args.port if args.port is not None else cfg.phone.port

    if not cfg.safety.allowlist and not args.auto:
        print("No window is on the allowlist, so an instruction could not be carried\n"
              "out even if confirmed. Add one first:  ufo config allow Notepad",
              file=sys.stderr)
        return 2

    auto_run = bool(args.auto or cfg.phone.auto_run)
    if auto_run:
        print("WARNING: --auto means spoken instructions run without confirmation.",
              file=sys.stderr)
        if not args.yes:
            reply = input("Type 'yes' to continue: ").strip().lower()
            if reply != "yes":
                print("Cancelled.")
                return 2

    listener = PhoneListener(PhoneListenerConfig(
        host=host, port=port,
        language=cfg.phone.language, transcription=cfg.phone.transcription,
        auto_run=auto_run, stt_model=cfg.voice.stt_model,
        max_instruction_chars=cfg.phone.max_instruction_chars,
    ))
    listener.set_persona(persona)

    try:
        bound_port = listener.start()
    except OSError as e:
        print(str(e), file=sys.stderr)
        return 1

    url = f"http://{host if host not in ('0.0.0.0', '') else _lan_ip()}:{bound_port}/"
    bar = "=" * 62
    print(bar)
    print(f"  UFO is listening for spoken instructions\n")
    print(f"  On your phone, open:   {url}")
    print(f"  (same Wi-Fi network; tap Allow when it asks for the microphone)")
    print(f"  Transcribing on:       "
          f"{'the phone (no API key needed)' if cfg.phone.transcription == 'browser' else 'the laptop (local whisper)'}")
    print(f"  Persona:               {cfg.voice.persona}"
          + (f" -> addresses you as {cfg.voice.address}" if cfg.voice.address else ""))
    print(f"  Mode:                  {'AUTO — runs without confirmation' if auto_run else 'confirms every instruction'}")
    print(f"  Voice:                 {narrator.backend_name}")
    print(bar)
    narrator.say(persona.wake(), chime=True)

    def replier(text: str) -> str:
        """Called on the phone's thread: read the instruction back."""
        line = persona.repeat(text)
        narrator.say(line)
        return line

    listener.set_replier(replier)

    emergency.start_hotkey_watch(cfg.safety.emergency_hotkey)
    hotkeys = HotkeyListener()
    hotkeys.register(cfg.safety.emergency_hotkey,
                     lambda: emergency.trip("panic hotkey pressed"), name="emergency")
    hotkeys.start()

    print("\nWaiting. Ctrl+C to stop.\n")
    try:
        while True:
            instruction = listener.wait_for(timeout=1.0)
            if instruction is None:
                if emergency.tripped:
                    narrator.say(persona.emergency())
                    print("Emergency stop tripped. Exiting.", file=sys.stderr)
                    return 130
                continue
            if not instruction.text:
                print("(nothing intelligible was heard — try again)", file=sys.stderr)
                continue

            goal = instruction.text
            print(f"\n[phone] {goal}")

            if not auto_run:
                narrator.say(persona.repeat(goal))
                if not _confirm("Run this? [y/N] "):
                    narrator.say(persona.blocked("I have not touched anything."))
                    print("skipped.")
                    continue

            if not cfg.safety.allowlist and not args.allow_all:
                narrator.say(persona.blocked(
                    "Nothing is on the allowlist, so I may not touch any window."))
                print("blocked: the allowlist is empty. "
                      "Add one with:  ufo config allow Notepad", file=sys.stderr)
                continue

            narrator.say(persona.acknowledge("starting"), chime=True)
            ns = argparse.Namespace(**vars(args))
            ns.task = goal
            ns.input = False
            ns.dry_run = bool(getattr(args, "dry_run", False))
            ns.allow = list(cfg.safety.allowlist)
            ns.deny = list(cfg.safety.denylist)
            try:
                cmd_run(ns, cfg)
            except KeyboardInterrupt:
                print("interrupted")
    except KeyboardInterrupt:
        print("\nstopped.")
        return 130
    finally:
        listener.stop()
        hotkeys.stop()
        emergency.stop_hotkey_watch()
    return 0


def _confirm(prompt: str) -> bool:
    try:
        reply = input(prompt).strip().lower()
    except EOFError:
        return False
    return reply in ("y", "yes")


def _lan_ip() -> str:
    from ..voice.phone import _lan_ip as impl

    return impl()


def cmd_plan(args: argparse.Namespace, cfg: Config) -> int:
    from ..agent.planner import Planner
    from ..perception import uia

    context = ", ".join(w.title for w in uia.list_all_windows()[:20])
    plan = Planner(cfg=cfg).plan(args.task, context)
    print(plan.describe())
    if plan.needs_clarification:
        print(f"\nAnswer this and re-run:\n  {plan.clarification}")
    if cfg.safety.dry_run:
        print("\n(dry run: nothing was executed)")
    return 0


# --------------------------------------------------------------------------
# deterministic single actions
# --------------------------------------------------------------------------
DETERMINISTIC_ACTIONS: dict[str, Callable[[list[str]], str]] = {}


def _register(name: str):
    def wrap(fn):
        DETERMINISTIC_ACTIONS[name] = fn
        return fn
    return wrap


@_register("windows")
def _do_windows(_args: list[str]) -> str:
    from ..perception import uia

    from ..config import active

    allow = active().safety.allowlist
    lines = []
    for w in uia.list_all_windows():
        mark = "OK " if uia.is_allowed(w, allow) else "-- "
        lines.append(f"{mark}[{w.handle}] {w.title} ({w.process_name})"
                     + ("  [front]" if w.is_foreground else ""))
    return "\n".join(lines) or "no visible windows"


@_register("screenshot")
def _do_screenshot(args: list[str]) -> str:
    from ..perception import screen
    from ..utils import image as imageutil

    path = args[0] if args else str(
        Path.home() / "Pictures" / f"ufo_shot_{time.strftime('%Y%m%d_%H%M%S')}.png")
    cap = screen.capture_full()
    imageutil.save(cap.image, path)
    return f"saved {cap.width}x{cap.height} screenshot to {path}"


@_register("read")
def _do_read(args: list[str]) -> str:
    from ..perception import uia

    if args:
        target = " ".join(args)
        for w in uia.list_all_windows():
            if target.lower() in w.title.lower():
                return uia.window_text(w.wrapper) or "(no text found)"
        return f"no window matching {target!r}"
    fg = uia.foreground_window()
    return uia.window_text(fg.wrapper) if fg else "no focused window"


@_register("see")
def _do_see(args: list[str]) -> str:
    from ..perception import uia, vision
    from ..config import active

    target = " ".join(args)
    info = None
    for w in uia.list_all_windows():
        if not target or target.lower() in w.title.lower():
            info = w
            break
    if info is None:
        return f"no window matching {target!r}"
    if not uia.is_allowed(info, active().safety.allowlist):
        return (f"{info.title!r} is not in the allowlist. "
                f"Add it with: {PROG} ... --allow \"{info.title}\"")
    obs = vision.observe_window(info.wrapper, active())
    return obs.describe(verbose=True)


@_register("volume")
def _do_volume(args: list[str]) -> str:
    from ..actions import system

    return system.set_volume(int(args[0])) if args else f"volume is {system.get_volume()}%"


@_register("mute")
def _do_mute(args: list[str]) -> str:
    from ..actions import system

    return system.mute(not (args and args[0].lower() in ("off", "false", "unmute")))


@_register("lock")
def _do_lock(_args: list[str]) -> str:
    from ..actions import system

    return system.lock()


@_register("start")
def _do_start(_args: list[str]) -> str:
    from ..actions import system

    return system.start_menu()


@_register("settings")
def _do_settings(args: list[str]) -> str:
    from ..actions import system

    return system.open_settings(args[0] if args else "")


@_register("ps")
def _do_ps(args: list[str]) -> str:
    from ..actions import process

    return process.top_processes(int(args[0]) if args else 10,
                                 args[1] if len(args) > 1 else "memory")


@_register("kill")
def _do_kill(args: list[str]) -> str:
    from ..actions import process

    if not args:
        return "usage: do kill <name|pid> [--force]"
    force = "--force" in args
    target = [a for a in args if not a.startswith("--")][0]
    return (process.terminate(int(target), force=True) if target.isdigit()
            else process.kill_by_name(target, force=force))


@_register("open")
def _do_open(args: list[str]) -> str:
    from ..actions import shell

    if not args:
        return "usage: do open <program|file|url>"
    target = " ".join(args)
    if "://" in target:
        return shell.open_url(target)
    return shell.launch(target, wait=2.0) and f"launched {target}"


@_register("run")
def _do_run(args: list[str]) -> str:
    from ..actions import shell

    if not args:
        return "usage: do run <command>"
    return str(shell.run_powershell(" ".join(args)))


@_register("ls")
def _do_ls(args: list[str]) -> str:
    from ..actions import filesystem

    return filesystem.tree(args[0] if args else ".")


@_register("ip")
def _do_ip(_args: list[str]) -> str:
    from ..actions import network

    return f"local {network.local_ip()}, public {network.public_ip()}"


def cmd_caps(args: argparse.Namespace, cfg: Config) -> int:
    """`ufo caps` — the catalog, or a search of it."""
    from ..capabilities.executor import describe_all
    from ..capabilities import nlu

    query = str(getattr(args, "query", "") or "").strip()
    if not query:
        print(describe_all())
        return 0

    numbers = [int(a) for a in (args.args or []) if str(a).isdigit()]
    hits = nlu.search(query, limit=20)
    if not hits and numbers:
        print(describe_all(numbers))
        return 0
    for cap, phrase, score in hits:
        print(f"{cap.name:<22} {cap.summary}   (matched {phrase!r}, "
              f"{score:.2f})")
    return 0 if hits else 1


def cmd_do(args: argparse.Namespace, cfg: Config) -> int:
    action = str(args.action).lower().replace("-", "_")
    fn = DETERMINISTIC_ACTIONS.get(action)
    if fn is None:
        return _do_capability(action, [str(a) for a in (args.args or [])], cfg)
    try:
        output = fn([str(a) for a in (args.args or [])])
    except UFOError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(output)
    return 0


def _do_capability(action: str, argv: list[str], cfg: Config) -> int:
    """`ufo do <capability>` for anything in the catalog but not registered above.

    The catalog is far wider than the hand-written action table, so an unknown
    name is checked there before it is called a typo. It still runs through the
    guard: a wider surface must not mean a weaker gate.
    """
    from ..capabilities import catalog
    from ..capabilities.executor import Executor
    from ..capabilities.nlu import Plan, Step

    cap = catalog.get(action)
    if cap is None:
        print(f"Unknown action {action!r}. Available: "
              f"{', '.join(sorted(DETERMINISTIC_ACTIONS))}, or any name from "
              f"`ufo caps`", file=sys.stderr)
        return 2

    target = argv[0] if argv else ""
    args = " ".join(argv[1:])
    # A capability at or above the confirm threshold needs a human to answer.
    # `ufo do` is the one place a console is definitely present, so it wires the
    # confirmer here; piped or redirected output gets no prompt and the guard's
    # fail-safe denial applies instead of hanging on input().
    executor = Executor(cfg=cfg, narrate=lambda message: print(f"  {message}", flush=True),
                        confirm=_console_confirm if sys.stdin.isatty() else None)
    result = executor.run_plan(
        Plan(action, [Step(cap.name, target, args, "", "")]))
    print(result.describe())
    return 0 if result.ok else 1


def _console_confirm(message: str, risk: object) -> bool:
    """Ask on the console. Mirrors the agent loop's prompt, panic phrase included."""
    from ..safety.emergency import emergency

    try:
        answer = input(f"[CONFIRM {getattr(risk, 'name', '?')}] {message} "
                       "— 'yes' / 'no' / 'stop': ")
    except (EOFError, KeyboardInterrupt):
        return False
    answer = answer.strip().lower()
    if answer in ("stop", "cancel", "abort"):
        emergency.trip("panic phrase at a confirmation prompt")
        return False
    return answer in ("y", "yes")


def _delegate(action: str, args: list[str], cfg: Config) -> int:
    """Run one deterministic action from a dedicated subcommand."""
    try:
        output = DETERMINISTIC_ACTIONS[action](list(args))
    except UFOError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(output)
    return 0


def cmd_windows(args: argparse.Namespace, cfg: Config) -> int:
    return _delegate("windows", [], cfg)


def cmd_screenshot(args: argparse.Namespace, cfg: Config) -> int:
    return _delegate("screenshot", [args.out] if args.out else [], cfg)


def cmd_read(args: argparse.Namespace, cfg: Config) -> int:
    return _delegate("read", [args.title] if args.title else [], cfg)


def cmd_see(args: argparse.Namespace, cfg: Config) -> int:
    return _delegate("see", [args.title] if args.title else [], cfg)


def cmd_clip(args: argparse.Namespace, cfg: Config) -> int:
    from ..actions import keyboard

    if args.what == "set":
        print(keyboard.set_clipboard(" ".join(args.text)))
    elif args.what == "clear":
        print(keyboard.set_clipboard(""))
    else:
        print(keyboard.get_clipboard())
    return 0


# --------------------------------------------------------------------------
# queue
# --------------------------------------------------------------------------
def cmd_queue(args: argparse.Namespace, cfg: Config) -> int:
    from ..core.queue import get_queue

    action = args.queue_command
    q = get_queue(cfg.storage.tasks_path)
    if action == "add":
        task = _task_from_args(args, args.task)
        q.add(task)
        print(f"queued {task.id}: {task.goal}")
        print("Run it now with:  ufo daemon start   (or: ufo run \"...\" for a one-off)")
        return 0
    if action == "list":
        print(q.describe())
        print(json.dumps(q.stats(), indent=2))
        return 0
    if action == "remove":
        removed = q.remove(args.id)
        print("removed" if removed else "not found")
        return 0 if removed else 1
    if action == "clear":
        print(f"cleared {q.clear()} task(s)")
        return 0
    if action == "cancel":
        if not args.id:
            print(f"cancelled {q.cancel_all()} task(s)")
            return 0
        try:
            q.cancel(args.id)
        except UFOError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"cancelled {args.id}")
        return 0
    if action == "start":
        from ..core.daemon import Daemon

        return Daemon(cfg).run()
    print("usage: ufo queue {add,list,remove,clear,cancel,start}", file=sys.stderr)
    return 2


# --------------------------------------------------------------------------
# schedule
# --------------------------------------------------------------------------
def cmd_schedule(args: argparse.Namespace, cfg: Config) -> int:
    from ..core.scheduler import get_scheduler
    from ..core.task import TaskPriority

    action = args.schedule_command
    s = get_scheduler(cfg.storage.schedule_path)
    if action == "add":
        try:
            schedule = s.add(
                args.task, when=args.when, repeat=args.repeat or "",
                task_id=args.id, priority=TaskPriority[str(args.priority).upper()],
                app=args.app,
                allow=list(getattr(args, "allow", []) or []),
            )
        except UFOError as e:
            print(f"{e}", file=sys.stderr)
            return 1
        print(schedule.describe())
        return 0
    if action == "list":
        s.load()
        print(s.describe())
        return 0
    if action == "remove":
        print("removed" if s.remove(args.id) else "not found")
        return 0
    if action == "tick":
        fired = s.load().tick()
        for t in fired:
            print(f"due: {t.goal}")
        print(f"{len(fired)} task(s) fired")
        return 0
    print("usage: ufo schedule {add,list,remove,tick}", file=sys.stderr)
    return 2


# --------------------------------------------------------------------------
# daemon
# --------------------------------------------------------------------------
def cmd_daemon(args: argparse.Namespace, cfg: Config) -> int:
    from ..core.daemon import Daemon, status

    action = args.daemon_command
    if action == "start":
        d = Daemon(cfg)
        _wire_hotkeys(cfg, d)
        d.run()
        return 0
    if action == "background":
        d = Daemon(cfg)
        _wire_hotkeys(cfg, d)
        d.run(background=True)
        time.sleep(1.0)
        print(json.dumps(status(cfg), indent=2))
        return 0
    if action == "status":
        state = status(cfg)
        print(json.dumps(state, indent=2) if state else "not running")
        return 0
    if action == "stop":
        d = Daemon(cfg)
        print("stop requested" if d.request_stop() else "could not signal the daemon")
        return 0
    if action == "panic":
        from ..safety.emergency import emergency

        emergency.trip("panic requested from the CLI")
        print("emergency stop tripped in this process. "
              "For other processes use Ctrl+Alt+Q on the keyboard.")
        return 0
    print("usage: ufo daemon {start,background,status,stop,panic}", file=sys.stderr)
    return 2


def _wire_hotkeys(cfg: Config, d: Any) -> None:
    from ..safety.emergency import emergency
    from ..voice import HotkeyListener

    emergency.start_hotkey_watch(cfg.safety.emergency_hotkey)
    emergency.on_trip(d.panic)
    listener = HotkeyListener()
    listener.register(cfg.safety.emergency_hotkey,
                      lambda: emergency.trip("panic hotkey pressed"), name="emergency")
    listener.start()


# --------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------
def cmd_serve(args: argparse.Namespace, cfg: Config) -> int:
    from .server import serve

    cfg.server.enabled = True
    if args.host:
        cfg.server.host = args.host
    if args.port:
        cfg.server.port = args.port
    if args.no_dashboard:
        cfg.server.dashboard = False
    return serve(cfg)


# --------------------------------------------------------------------------
# memory / secrets / config
# --------------------------------------------------------------------------
def cmd_memory(args: argparse.Namespace, cfg: Config) -> int:
    from ..core.memory import get_memory

    store = get_memory(cfg.storage.memory_path)
    action = args.memory_command
    if action == "show":
        print(store.brief(limit=10) or "nothing remembered yet")
        for episode in store.episodes(10):
            mark = "ok " if episode.success else "   "
            print(f"  {mark}{time.strftime('%Y-%m-%d %H:%M', time.localtime(episode.at))}  "
                  f"{textutil.truncate(episode.task, 80)}  -> {episode.outcome}")
        return 0
    if action == "remember":
        store.remember(args.key, args.value, source="cli")
        print(f"remembered {args.key}")
        return 0
    if action == "forget":
        print("forgotten" if store.forget(args.key) else "not found")
        return 0
    if action == "clear":
        store.clear()
        print("memory cleared")
        return 0
    if action == "stats":
        print(json.dumps(store.stats(), indent=2))
        return 0
    print("usage: ufo memory {show,remember,forget,clear,stats}", file=sys.stderr)
    return 2


def cmd_secrets(args: argparse.Namespace, cfg: Config) -> int:
    from ..safety.secrets import SecretStore

    store = SecretStore(cfg.storage.secrets_path)
    action = args.secrets_command
    if action == "set":
        store.set(args.name, args.value)
        print(f"stored {args.name} in {cfg.storage.secrets_path}")
        return 0
    if action == "get":
        value = store.get(args.name)
        if not value:
            print("not set", file=sys.stderr)
            return 1
        print(value)
        return 0
    if action == "remove":
        print("removed" if store.unset(args.name) else "not found")
        return 0
    if action == "list":
        print("\n".join(store.names()) or "(no secrets stored)")
        return 0
    print("usage: ufo secrets {set,get,remove,list}", file=sys.stderr)
    return 2


def cmd_config(args: argparse.Namespace, cfg: Config) -> int:
    action = args.config_command
    if action == "show":
        data = cfg.to_dict()
        for key in ("llm",):
            if isinstance(data.get(key), dict) and "api_key" in data[key]:
                data[key].pop("api_key")
        print(json.dumps(data, indent=2, default=str))
        return 0
    if action == "init":
        path = Path(args.path) if args.path else cfg._default_config_path()
        if path.exists() and not args.force:
            print(f"{path} already exists. Use --force to overwrite.", file=sys.stderr)
            return 1
        target = path
        data = cfg.to_dict()
        data.pop("config_path", None)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        print(f"wrote {target}")
        return 0
    if action == "set":
        pairs = {}
        for pair in args.pairs:
            if "=" not in pair:
                print(f"expected key=value, got {pair!r}", file=sys.stderr)
                return 2
            key, _, value = pair.partition("=")
            pairs[key.strip()] = value.strip()
        try:
            cfg.apply_overrides(pairs)
        except ConfigError as e:
            print(str(e), file=sys.stderr)
            return 2
        path = cfg.save()
        print(f"saved {path}")
        return 0
    if action == "allow":
        current = list(cfg.safety.allowlist)
        for pattern in args.patterns:
            if pattern not in current:
                current.append(pattern)
        cfg.safety.allowlist = current
        path = cfg.save()
        print(f"allowlist now: {', '.join(current)}\nsaved {path}")
        return 0
    if action == "deny":
        current = list(cfg.safety.denylist)
        current.extend(p for p in args.patterns if p not in current)
        cfg.safety.denylist = current
        path = cfg.save()
        print(f"denylist now: {', '.join(current)}\nsaved {path}")
        return 0
    print("usage: ufo config {show,init,set,allow,deny}", file=sys.stderr)
    return 2


# --------------------------------------------------------------------------
# diagnostics
# --------------------------------------------------------------------------
def _model_missing_locally(model: str) -> tuple[str, str] | None:
    """Ask Ollama whether it has `model`. None means it does, or cannot be asked.

    Three outcomes, and the difference matters: the server is down (fix: start
    it), the server is up but has no such model (fix: pull it), or it answers and
    the model is there. Collapsing the first two into silence was the original
    bug — an unreachable server and a wrong model name are different problems
    with different fixes, and reporting neither left the user guessing.
    """
    import json
    import urllib.request

    try:
        from ..agent.llm import _LOCAL_BASE_URLS

        base = _LOCAL_BASE_URLS.get("ollama", "")
        root = base[:-3] if base.endswith("/v1") else base
        with urllib.request.urlopen(f"{root}/api/tags", timeout=4) as resp:
            tags = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return (
            "the Ollama server is not answering on 127.0.0.1:11434",
            "start it with:  ollama serve   (or open Ollama and leave it running)",
        )

    names: set[str] = set()
    for entry in tags.get("models", []) or []:
        for key in ("name", "model"):
            if entry.get(key):
                names.add(str(entry[key]))

    wanted = (model or "").strip()
    if not wanted or not names:
        return None

    # Ollama lets you say `gemma3` for `gemma3:4b`, so a bare family name matches
    # any of its tags. An explicit tag must match exactly, though: `gemma3:99b`
    # is a model that is not installed, and reporting it as present would send
    # the user off to a 404 they just spent a minute trying to avoid.
    def family(name: str) -> str:
        return name.split(":", 1)[0].strip().lower()

    for n in names:
        if n.strip().lower() == wanted.lower():
            return None
    if ":" not in wanted and any(family(n) == family(wanted) for n in names):
        return None

    listed = ", ".join(sorted(names)) or "(none)"
    return (
        f"the model {wanted!r} is not on the Ollama server",
        f"pull it with:  ollama pull {wanted}   "
        f"(installed: {listed})",
    )


def cmd_default(args: argparse.Namespace, cfg: Config) -> int:
    """What `python main.py` with no arguments does.

    The goal is one command that starts the app, but starting it and having it
    fail three seconds later is worse than saying what is wrong. So this checks
    the things that make `listen` unusable, names the exact fix for each, and
    refuses to bind a port that is about to serve nothing.

    It is not `doctor`: doctor is a full report of everything, including things
    that do not matter for voice. This is the short list that blocks startup.
    """
    from ..voice.phone import local_transcription_available

    # Everything that would make startup useless, collected before anything is
    # started. One list, reported together: making a user fix one problem, rerun,
    # and hit the next is a poor first run.
    blockers: list[tuple[str, str]] = []

    if not cfg.llm.offline and cfg.llm.provider != "ollama":
        blockers.append((
            f"llm.provider is {cfg.llm.provider!r} with llm.offline off",
            f"set it with:  ufo config set llm.provider ollama  "
            f"(and  ufo config set llm.offline true)",
        ))

    if cfg.phone.transcription == "local" and not local_transcription_available():
        blockers.append((
            "phone.transcription is 'local' but faster-whisper is not installed",
            "install it with:  pip install faster-whisper   "
            "(or use the phone's own recogniser:  "
            "ufo config set phone.transcription browser)",
        ))

    if not cfg.safety.allowlist:
        blockers.append((
            "the allowlist is empty, so no window could be acted on",
            "add one with:  ufo config allow Notepad",
        ))

    # A model that is not pulled is the single most likely reason a fresh install
    # does nothing, and it is invisible until a task is spoken: the phone takes
    # the instruction, it is confirmed, and only then does the 404 arrive. So it
    # is worth a few hundred milliseconds at startup to ask the server what it
    # actually has.
    if not blockers and cfg.llm.provider == "ollama":
        missing = _model_missing_locally(cfg.llm.model)
        if missing:
            blockers.append(missing)

    if blockers:
        word = "things" if len(blockers) > 1 else "thing"
        print(f"Cannot start yet. {len(blockers)} {word} to fix:\n", file=sys.stderr)
        for what, fix in blockers:
            print(f"  - {what}\n      {fix}\n", file=sys.stderr)
        print("Run  ufo doctor  for the full picture.", file=sys.stderr)
        return 2

    # Everything that would stop startup is checked, so from here on the
    # failures are the ones only a real request can cause.
    print("Starting up. Open the printed URL on your phone, then hold the\n"
          "button and speak. Ctrl+C stops it.\n")
    return cmd_listen(args, cfg)


def cmd_doctor(args: argparse.Namespace, cfg: Config) -> int:
    """Check every subsystem. Every failure mode gets a specific fix line."""
    checks: list[tuple[str, bool, str]] = []

    checks.append(("Windows desktop", win32.is_windows(),
                   f"DPI awareness: {win32.dpi_awareness_set()}"))

    try:
        import pywinauto  # type: ignore  # noqa: F401

        checks.append(("pywinauto (UI Automation)", True, "installed"))
    except ImportError:
        checks.append(("pywinauto (UI Automation)", False,
                       "pip install pywinauto"))

    # The remaining core requirements are all import-time optional in the code,
    # so a missing one degrades quietly instead of failing loudly. psutil is
    # the expensive case: without it every process listing falls back to a
    # slow Win32 path that takes minutes where psutil takes milliseconds.
    try:
        import psutil  # type: ignore  # noqa: F401

        checks.append(("psutil (process/memory)", True, "installed"))
    except ImportError:
        checks.append(("psutil (process/memory)", False,
                       "pip install psutil - process listings will be very slow"))

    try:
        from ..perception import uia

        windows = uia.list_all_windows()
        checks.append(("Window enumeration", True, f"{len(windows)} visible window(s)"))
    except Exception as e:
        checks.append(("Window enumeration", False, str(e)))

    try:
        from ..perception import screen

        cap = screen.capture_full()
        checks.append(("Screen capture", True,
                       f"{cap.width}x{cap.height} at ({cap.bbox[0]},{cap.bbox[1]})"))
    except Exception as e:
        checks.append(("Screen capture", False,
                       f"{e} — a locked or secure desktop has nothing to capture"))

    try:
        from ..perception import ocr

        engine = ocr.engine_name()
        checks.append(("OCR (optional)", bool(engine),
                       f"engine: {engine}" if engine
                       else "none installed — the agent falls back to vision only"))
    except Exception as e:
        checks.append(("OCR (optional)", False, str(e)))

    provider = cfg.llm.provider
    try:
        from ..agent import llm

        # `offline` has to come across: without it this check runs against the
        # default (on) rather than what the user actually set, so a config with
        # offline off was reported as "offline is on" — the exact opposite of
        # the truth, on the screen the user is reading to fix a problem.
        client = llm.Client(llm.LLMConfig(
            provider=provider, model=cfg.llm.model, timeout=15,
            base_url=cfg.llm.base_url, fallbacks=cfg.llm.fallbacks,
            offline=cfg.llm.offline,
        ))
        key_set = bool(client.api_key) or provider == "ollama"
        checks.append((f"LLM ({provider})", key_set,
                       f"model {client.model}, key {'found' if client.api_key else 'not needed'}"
                       if key_set else
                       f"missing API key — set it with  $env:{_key_var(provider)}=\"...\""))
    except UFOError as e:
        checks.append((f"LLM ({provider})", False, str(e)))
    except Exception as e:
        checks.append((f"LLM ({provider})", False, f"{type(e).__name__}: {e}"))

    allowed = cfg.safety.allowlist
    checks.append(("Allowlist", bool(allowed),
                   f"{len(allowed)} pattern(s): {', '.join(allowed[:5])}" if allowed
                   else "empty — nothing is permitted. Add one: ufo config allow Notepad"))

    if allowed:
        try:
            from ..perception import uia

            windows = uia.list_all_windows()
            matches = [w.title for w in windows if uia.is_allowed(w, allowed)]
            checks.append(("Allowlist matches", bool(matches),
                           f"{len(matches)} open window(s) match: "
                           f"{', '.join(matches[:3])}" if matches
                           else "no open window matches — open the app, or adjust the pattern"))
        except Exception as e:
            checks.append(("Allowlist matches", False, str(e)))

    checks.append(("Emergency stop", True,
                   f"hotkey {cfg.safety.emergency_hotkey} — press it at any time"))
    checks.append(("State directory", True, str(cfg.storage.home)))

    for name, ok, detail in checks:
        mark = "OK  " if ok else "FAIL"
        print(f"[{mark}] {name:<26} {detail}")

    failures = [c for c in checks if not c[1]]
    if failures:
        print(f"\n{len(failures)} check(s) failed. The agent will not work until "
              f"they are fixed.")
    return 0 if not failures else 1


def _key_var(provider: str) -> str:
    from ..agent.llm import ENV_KEYS

    keys = ENV_KEYS.get(provider)
    return keys[0] if keys else "LLM_PROVIDER"


def cmd_logs(args: argparse.Namespace, cfg: Config) -> int:
    root = cfg.storage.log_root
    if not root.exists():
        print("no runs yet")
        return 0
    runs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name)
    if not runs:
        print("no runs yet")
        return 0
    for run in runs[-20:]:
        summary = run / "run.json"
        line = run.name
        if summary.exists():
            try:
                data = json.loads(summary.read_text(encoding="utf-8"))
                line += f"  {data.get('status', '?'):<8} {data.get('duration_seconds', 0):>6.1f}s  " \
                        f"{textutil.truncate(str(data.get('task', '')), 60)}"
            except (OSError, json.JSONDecodeError):
                pass
        print(line)
    return 0


def cmd_replay(args: argparse.Namespace, cfg: Config) -> int:
    run_dir = cfg.storage.log_root / args.run_id
    if not run_dir.exists():
        print(f"no such run: {run_dir}", file=sys.stderr)
        return 1
    trace = run_dir / "trace.jsonl"
    if not trace.exists():
        print(f"no trace in {run_dir}")
        return 1
    for line in trace.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "meta":
            print(f"RUN {event['run_id']}  {event['status']}  "
                  f"{event['duration_seconds']}s  {event['task']}")
            continue
        print(f"  +{event.get('t', 0):>7.2f}s  {event.get('event', ''):<12} "
              f"{textutil.truncate(json.dumps({k: v for k, v in event.items() if k not in ('t', 'event')}), 160)}")
    return 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
NEEDS_CONFIG = {
    "run", "plan", "do", "windows", "screenshot", "read", "see", "clip",
    "queue", "schedule", "daemon", "serve", "memory", "secrets", "config",
    "doctor", "logs", "replay",
}


def _force_utf8_console() -> None:
    """Window titles and app names routinely contain characters the default
    Windows console codepage cannot encode, which would crash any command that
    prints one. Switch the streams to UTF-8 with replacement instead."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def main(argv: Optional[list[str]] = None) -> int:
    _force_utf8_console()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        # No subcommand at all: `python main.py`. The start-up preflight below
        # needs the same attributes the `listen` parser would have set, so they
        # are filled in here rather than letting cmd_default guess.
        args.command = "default"
        args.host = None
        args.port = None
        args.auto = False
        args.exe = None
        args.title = None
        args.allow = []
        args.deny = []
        args.allow_all = False
        args.dry_run = False
        args.yes = False
        args.confirm_all = False
        args.voice = False
        args.no_voice = False
        args.max_steps = None
        args.max_cost = None
        args.mode = "computer"
        args.provider = None
        args.model = None
        args.input = False
        args.task = ""

    win32.dpi_awareness_set()

    try:
        cfg = load_config(args)
    except ConfigError as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return 2
    configure(cfg)

    handler: Optional[Callable[[argparse.Namespace, Config], int]] = getattr(args, "func", None)
    if handler is None and args.command == "default":
        # No subcommand was named, so argparse never attached a `func`.
        handler = cmd_default
    if handler is None:
        handler = cmd_run
    try:
        return handler(args, cfg)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except UFOError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        log.debug("unhandled error", exc_info=True)
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        print("Re-run with -v for the full traceback.", file=sys.stderr)
        return 1
