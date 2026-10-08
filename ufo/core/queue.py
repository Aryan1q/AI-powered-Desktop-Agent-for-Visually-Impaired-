"""The task queue: priority ordering, persistence, and cancellation.

Persisted as a single JSON file and rewritten atomically on every change, so a
crash or a `kill` never leaves a queue that cannot be read. Running tasks are
re-queued on load (they were interrupted, not finished) unless the daemon is
deliberately resuming them.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

from ..errors import TaskNotFound
from ..logging_setup import get_logger, record
from .task import Task, TaskPriority, TaskStatus

log = get_logger(__name__)


class TaskQueue:
    def __init__(self, path: str | Path, autosave: bool = True):
        self.path = Path(path)
        self.autosave = autosave
        self._tasks: list[Task] = []
        self._lock = threading.RLock()
        self._cancelled: set[str] = set()
        self._listeners: list[Callable[[Task], None]] = []
        # Load eagerly: a fresh TaskQueue over an existing file should not
        # quietly look empty (a silent 'my queued tasks vanished' failure).
        self.load()
    # ---- persistence ----
    def load(self, requeue_running: bool = True) -> "TaskQueue":
        with self._lock:
            self._tasks = []
            if not self.path.exists():
                return self
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                log.warning("could not read the queue at %s (%s); starting empty",
                            self.path, e)
                return self
            for item in raw.get("tasks", []):
                if not isinstance(item, dict):
                    continue
                try:
                    task = Task.from_dict(item)
                except (TypeError, ValueError):
                    continue
                if task.status is TaskStatus.RUNNING and requeue_running:
                    task.status = TaskStatus.PENDING
                    task.notes.append("re-queued after an interrupted run")
                if not task.status.terminal:
                    self._tasks.append(task)
        return self

    def save(self) -> None:
        with self._lock:
            live = [t for t in self._tasks if not t.status.terminal]
            if not live and not self._tasks:
                return
            payload = {"version": 1, "updated": time.time(),
                       "tasks": [t.to_dict() for t in self._tasks]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                           encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            log.warning("could not save the queue: %s", e)

    def _touch(self) -> None:
        if self.autosave:
            self.save()

    # ---- mutation ----
    def add(self, task: Task) -> Task:
        with self._lock:
            if any(t.id == task.id for t in self._tasks):
                raise ValueError(f"Task {task.id} is already queued.")
            self._tasks.append(task)
        self._touch()
        record("queue_add", id=task.id, goal=task.goal[:120], priority=task.priority.label)
        return task

    def add_many(self, tasks: list[Task]) -> list[Task]:
        for t in tasks:
            self.add(t)
        return tasks

    def get(self, task_id: str) -> Task:
        with self._lock:
            for t in self._tasks:
                if t.id == task_id:
                    return t
        raise TaskNotFound(f"No queued task with id {task_id!r}.")

    def find(self, task_id: str) -> Task | None:
        with self._lock:
            return next((t for t in self._tasks if t.id == task_id), None)

    def remove(self, task_id: str) -> bool:
        with self._lock:
            before = len(self._tasks)
            self._tasks = [t for t in self._tasks if t.id != task_id]
            removed = len(self._tasks) < before
        if removed:
            self._touch()
        return removed

    def update(self, task: Task) -> Task:
        with self._lock:
            for i, t in enumerate(self._tasks):
                if t.id == task.id:
                    self._tasks[i] = task
                    break
            else:
                self._tasks.append(task)
        self._touch()
        return task

    def clear(self, only_pending: bool = True) -> int:
        with self._lock:
            before = len(self._tasks)
            self._tasks = [t for t in self._tasks
                           if not (only_pending and t.status is TaskStatus.PENDING)]
        count = before - len(self._tasks)
        self._touch()
        return count

    # ---- ordering ----
    def _sort_key(self, task: Task):
        return (-int(task.priority), task.scheduled_for or 0, task.created)

    def list(self, status: Optional[TaskStatus] = None, tag: str = "",
             include_terminal: bool = True) -> list[Task]:
        with self._lock:
            items = list(self._tasks)
        if status is not None:
            items = [t for t in items if t.status is status]
        elif not include_terminal:
            items = [t for t in items if not t.status.terminal]
        if tag:
            items = [t for t in items if tag in t.tags]
        return sorted(items, key=self._sort_key)

    def next(self) -> Task | None:
        """The highest-priority task whose scheduled time has arrived.

        This is a *peek*: it does not remove or claim the task, so calling it
        twice returns the same task. The caller is responsible for moving the
        task out of PENDING (`update(..., status=RUNNING)`) or the daemon will
        happily hand the same task to two workers.
        """
        now = time.time()
        with self._lock:
            ready = [t for t in self._tasks
                     if t.status is TaskStatus.PENDING
                     and t.scheduled_for <= now
                     and t.id not in self._cancelled]
        if not ready:
            return None
        return sorted(ready, key=self._sort_key)[0]

    def __len__(self) -> int:
        with self._lock:
            return len(self._tasks)

    def __iter__(self) -> Iterator[Task]:
        return iter(self.list())

    def __bool__(self) -> bool:
        return len(self) > 0

    # ---- cancellation ----
    def cancel(self, task_id: str, reason: str = "cancelled by the user") -> Task:
        task = self.get(task_id)
        self._cancelled.add(task_id)
        task.cancel(reason)
        self.remove(task_id)
        record("queue_cancel", id=task_id, reason=reason)
        return task

    def cancel_all(self, reason: str = "cancelled by the user") -> int:
        with self._lock:
            ids = [t.id for t in self._tasks if t.status is TaskStatus.PENDING]
        for task_id in ids:
            try:
                self.cancel(task_id, reason)
            except TaskNotFound:
                continue
        return len(ids)

    def is_cancelled(self, task_id: str) -> bool:
        return task_id in self._cancelled

    def clear_cancellations(self) -> None:
        self._cancelled.clear()

    # ---- listeners ----
    def on_add(self, callback: Callable[[Task], None]) -> None:
        self._listeners.append(callback)

    def _emit(self, task: Task) -> None:
        for fn in self._listeners:
            try:
                fn(task)
            except Exception as e:
                log.debug("queue listener failed: %s", e)

    # ---- reporting ----
    def stats(self) -> dict:
        with self._lock:
            counts: dict[str, int] = {}
            for t in self._tasks:
                counts[t.status.value] = counts.get(t.status.value, 0) + 1
            return {
                "length": len(self._tasks),
                "by_status": counts,
                "by_priority": {
                    p.label: sum(1 for t in self._tasks if t.priority is p)
                    for p in TaskPriority
                },
                "path": str(self.path),
                "next": (self.next().id if self.next() else None),
            }

    def describe(self) -> str:
        from .task import describe

        return describe(self.list())


_default: TaskQueue | None = None


def get_queue(path: str | Path | None = None, *, load: bool = True) -> TaskQueue:
    """Process-wide queue, bound to the configured home on first use.

    Named `get_queue`, not `queue`, so `from ..core import queue` still yields
    this module rather than shadowing it with a callable.
    """
    global _default
    if _default is None:
        if path is None:
            from ..config import active

            path = active().storage.tasks_path
        _default = TaskQueue(path)
    if load:
        _default.load()
    return _default


def set_default(q: TaskQueue | None) -> None:
    global _default
    _default = q
