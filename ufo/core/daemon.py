"""The daemon: a long-running process that owns the queue, the scheduler and
the global emergency hotkey.

`ufo daemon start` runs this. It picks up tasks dropped into the queue by the
CLI, the HTTP API or a schedule, runs them one at a time, and writes a status
file the CLI and the dashboard can read without talking to the process.

Graceful shutdown matters here: on SIGINT / SIGTERM / a kill file it stops
accepting work, lets the current task finish (or cancels it if configured),
and exits without leaving a half-written state file.
"""
from __future__ import annotations

import os
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..config import Config
from ..logging_setup import get_logger, record
from ..safety.emergency import emergency
from .queue import TaskQueue
from .runner import Runner
from .scheduler import Scheduler
from .task import Task, TaskStatus

log = get_logger(__name__)

POLL_SECONDS = 1.0
STATE_VERSION = 1


@dataclass
class DaemonState:
    pid: int = 0
    started: float = field(default_factory=time.time)
    tasks_done: int = 0
    tasks_failed: int = 0
    current: str = ""
    current_goal: str = ""
    last_result: str = ""
    queue_length: int = 0
    schedules: int = 0
    emergency: bool = False
    stopping: bool = False

    def to_dict(self) -> dict:
        return {
            "pid": self.pid, "started": self.started,
            "uptime_seconds": round(time.time() - self.started, 1),
            "tasks_done": self.tasks_done, "tasks_failed": self.tasks_failed,
            "current": self.current, "current_goal": self.current_goal,
            "last_result": self.last_result, "queue_length": self.queue_length,
            "schedules": self.schedules, "emergency": self.emergency,
            "stopping": self.stopping,
        }


class Daemon:
    def __init__(self, cfg: Config | None = None, *,
                 state_path: str | Path | None = None,
                 stop_file: str | Path | None = None,
                 poll: float = POLL_SECONDS):
        self.cfg = cfg or Config.load().ensure_dirs()
        self.queue = TaskQueue(self.cfg.storage.tasks_path).load()
        self.scheduler = Scheduler(
            self.cfg.storage.schedule_path,
            submit=self.queue.add,
            jitter=self.cfg.schedule.jitter_seconds,
            catch_up=self.cfg.schedule.catch_up_missed,
        ).load()
        self.state_path = Path(state_path or (self.cfg.storage.home / "daemon.json"))
        self.stop_file = Path(stop_file or (self.cfg.storage.home / "daemon.stop"))
        self.poll = max(0.2, float(poll))
        self.state = DaemonState(pid=os.getpid())
        self.runner = Runner(self.cfg)
        self._stop = threading.Event()
        self._current_id = ""
        self._current_cancel = False
        self._lock = threading.Lock()

    # ---- lifecycle ----
    def run(self, max_tasks: int = 0, background: bool = False) -> int:
        if background:
            thread = threading.Thread(target=self.run, args=(max_tasks, False),
                                      name="ufo-daemon", daemon=True)
            thread.start()
            return thread.ident or 0

        self._install_signal_handlers()
        self._clear_stop_file()
        self.scheduler.start()
        self.runner.narrator and None
        self._write_state()
        log.info("daemon started (pid %s); %d queued task(s), %d schedule(s)",
                 self.state.pid, len(self.queue), len(self.scheduler))

        processed = 0
        try:
            while not self._stop.is_set():
                if self.stop_file.exists():
                    log.info("stop file found; shutting down")
                    break
                task = self.queue.next()
                if task is None:
                    self.state.queue_length = len(self.queue)
                    self._write_state()
                    self._stop.wait(self.poll)
                    continue
                self._execute(task)
                processed += 1
                if max_tasks and processed >= max_tasks:
                    log.info("processed %d task(s); exiting as requested", processed)
                    break
        finally:
            self._shutdown()
        return processed

    def _execute(self, task: Task) -> None:
        with self._lock:
            self._current_id = task.id
            self._current_cancel = False
            self.state.current = task.id
            self.state.current_goal = task.goal
        self.queue.update(task)
        self._write_state()
        log.info("running task %s: %s", task.id, task.goal)

        self.runner.should_continue = lambda: not self._current_cancel
        try:
            done = self.runner.run(task)
        except KeyboardInterrupt:
            done.cancel("the daemon was interrupted")
        except Exception as e:
            log.error("task %s failed unexpectedly: %s", task.id, e)
            done.fail(f"{type(e).__name__}: {e}")

        with self._lock:
            self._current_id = ""
            self.state.current = ""
            self.state.current_goal = ""
            if done.status.successful:
                self.state.tasks_done += 1
            else:
                self.state.tasks_failed += 1
            self.state.last_result = done.result.summary()[:300]
        self.queue.remove(task.id)
        self._write_state()
        if done.should_retry:
            log.warning("task %s will be retried (%d/%d)", task.id,
                        done.attempts, done.max_attempts)
            retry = Task.from_dict(done.to_dict())
            retry.status = TaskStatus.PENDING
            retry.result = TaskResult()
            self.queue.add(retry)

    def stop(self, reason: str = "stop requested") -> None:
        log.info("stopping: %s", reason)
        self.state.stopping = True
        with self._lock:
            self._current_cancel = True
        self._stop.set()

    def cancel_current(self) -> bool:
        with self._lock:
            if not self._current_id:
                return False
            self._current_cancel = True
            task_id = self._current_id
        emergency.trip("the user cancelled the running task")
        log.warning("cancelled the running task %s", task_id)
        return True

    def panic(self) -> None:
        emergency.trip("the user pressed the emergency stop hotkey")
        self._write_state()

    def _shutdown(self) -> None:
        self.scheduler.stop()
        self.state.stopping = True
        self.state.queue_length = len(self.queue)
        self._write_state()
        try:
            self.state_path.unlink(missing_ok=True)
        except OSError:
            pass
        log.info("daemon stopped: %d done, %d failed", self.state.tasks_done,
                 self.state.tasks_failed)

    # ---- state file ----
    def _write_state(self) -> None:
        self.state.queue_length = len(self.queue)
        self.state.schedules = len(self.scheduler)
        self.state.emergency = emergency.tripped
        payload = {"version": STATE_VERSION, **self.state.to_dict()}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(__import__("json").dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError as e:
            log.debug("could not write daemon state: %s", e)

    @staticmethod
    def read_state(path: str | Path) -> dict | None:
        import json

        p = Path(path)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not is_alive(data.get("pid", 0)):
            return {**data, "alive": False}
        return {**data, "alive": True}

    # ---- signals ----
    def _install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            self.stop(f"signal {signal.Signals(signum).name}")

        for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass  # not on the main thread, or unsupported on Windows

    def _clear_stop_file(self) -> None:
        try:
            self.stop_file.unlink(missing_ok=True)
        except OSError:
            pass

    # ---- control from another process ----
    def request_stop(self) -> bool:
        try:
            self.stop_file.parent.mkdir(parents=True, exist_ok=True)
            self.stop_file.write_text(str(time.time()), encoding="utf-8")
            return True
        except OSError as e:
            log.error("could not write the stop file: %s", e)
            return False


def is_alive(pid: int) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return True
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def read_state(cfg: Config | None = None) -> dict | None:
    cfg = cfg or Config.load()
    return Daemon.read_state(cfg.storage.home / "daemon.json")


_default: Daemon | None = None


def daemon(cfg: Config | None = None) -> Daemon:
    global _default
    if _default is None:
        _default = Daemon(cfg)
    return _default


def set_default(d: Daemon | None) -> None:
    global _default
    _default = d


def watch_hotkey(d: Daemon, hotkey: str = "ctrl+alt+q") -> bool:
    """Wire the global panic key to a running daemon."""
    return emergency.start_hotkey_watch(hotkey) and emergency.on_trip(d.panic) is None


def status(cfg: Config | None = None) -> dict:
    cfg = cfg or Config.load()
    state = read_state(cfg)
    if state is None:
        return {"running": False}
    return {"running": bool(state.get("alive")), **state}
