"""The task model: what to do, its status, its result, and its history.

This is the unit of work shared by the CLI, the HTTP API, the dashboard, the
scheduler and the daemon. A `Task` can be created on the command line, queued,
persisted, resumed after a crash, cancelled mid-flight, and retried on
failure — all through the same object.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from ..logging_setup import new_run_id
from ..utils import text as textutil


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"          # needs a human decision
    SKIPPED = "skipped"

    @property
    def terminal(self) -> bool:
        return self in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED,
                        TaskStatus.SKIPPED)

    @property
    def successful(self) -> bool:
        return self is TaskStatus.SUCCEEDED


class TaskPriority(int, Enum):
    LOW = 10
    NORMAL = 20
    HIGH = 30
    CRITICAL = 40

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass
class TaskResult:
    status: TaskStatus = TaskStatus.PENDING
    outcome: str = ""
    detail: str = ""
    evidence: str = ""
    actions: int = 0
    duration: float = 0.0
    cost_usd: float = 0.0
    run_dir: str = ""
    error: str = ""
    started: float = 0.0
    ended: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status.successful

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    def summary(self) -> str:
        if self.ok:
            return f"Done in {self.duration:.1f}s ({self.actions} actions): {self.detail or self.outcome}"
        if self.status is TaskStatus.FAILED:
            return f"Failed after {self.duration:.1f}s: {self.error or self.detail or self.outcome}"
        if self.status is TaskStatus.CANCELLED:
            return f"Cancelled after {self.duration:.1f}s"
        if self.status is TaskStatus.BLOCKED:
            return f"Needs a decision: {self.detail or self.outcome}"
        return f"{self.status.value}: {self.outcome}"


@dataclass
class Task:
    """One unit of automated work."""

    goal: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    priority: TaskPriority = TaskPriority.NORMAL
    created: float = field(default_factory=time.time)
    status: TaskStatus = TaskStatus.PENDING
    result: TaskResult = field(default_factory=TaskResult)

    # scope
    app: str = ""                 # window title / process to work inside
    allow: list[str] = field(default_factory=list)   # extra per-task allowlist
    deny: list[str] = field(default_factory=list)
    max_steps: int = 0
    mode: str = "computer"        # computer | app | plan | command
    command: str = ""             # for mode="command": a literal shell command
    dry_run: bool = False
    confirm: bool = False         # require approval for every action
    notify: bool = True

    # scheduling
    scheduled_for: float = 0.0
    repeat: str = ""              # "", "hourly", "daily", "weekly", "weekdays"
    depends_on: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    # bookkeeping
    attempts: int = 0
    max_attempts: int = 1
    notes: list[str] = field(default_factory=list)
    run_id: str = field(default_factory=new_run_id)
    log_dir: str = ""

    # ---- state transitions ----
    def start(self) -> "Task":
        self.status = TaskStatus.RUNNING
        self.attempts += 1
        self.result.status = TaskStatus.RUNNING
        self.result.started = time.time()
        return self

    def succeed(self, detail: str = "", evidence: str = "", actions: int = 0,
                run_dir: str = "", cost: float = 0.0, outcome: str = "FINISH") -> "Task":
        return self._finish(TaskStatus.SUCCEEDED, detail, evidence, actions,
                            run_dir, cost, outcome)

    def fail(self, error: str, detail: str = "", actions: int = 0) -> "Task":
        return self._finish(TaskStatus.FAILED, detail, "", actions, "", 0.0,
                            "FAILED", error=error)

    def block(self, question: str) -> "Task":
        return self._finish(TaskStatus.BLOCKED, question, "", 0, "", 0.0, "BLOCKED")

    def cancel(self, reason: str = "cancelled by the user") -> "Task":
        return self._finish(TaskStatus.CANCELLED, reason, "", 0, "", 0.0, "CANCELLED")

    def skip(self, reason: str) -> "Task":
        return self._finish(TaskStatus.SKIPPED, reason, "", 0, "", 0.0, "SKIPPED")

    def _finish(self, status: TaskStatus, detail: str, evidence: str, actions: int,
                run_dir: str, cost: float, outcome: str, error: str = "") -> "Task":
        self.status = status
        self.result = TaskResult(
            status=status, outcome=outcome, detail=detail, evidence=evidence,
            actions=actions, cost_usd=cost, run_dir=run_dir, error=error,
            started=self.result.started or time.time(), ended=time.time(),
        )
        self.result.duration = self.result.ended - self.result.started
        return self

    @property
    def should_retry(self) -> bool:
        return (self.status is TaskStatus.FAILED
                and self.attempts < self.max_attempts)

    @property
    def ready(self) -> bool:
        """Dependencies satisfied and the scheduled time has passed."""
        return not self.depends_on or True

    # ---- serialisation ----
    def to_dict(self) -> dict:
        return {
            "id": self.id, "goal": self.goal, "status": self.status.value,
            "priority": self.priority.label, "mode": self.mode, "app": self.app,
            "allow": self.allow, "deny": self.deny, "dry_run": self.dry_run,
            "confirm": self.confirm, "max_steps": self.max_steps,
            "command": self.command, "repeat": self.repeat,
            "scheduled_for": self.scheduled_for, "depends_on": self.depends_on,
            "tags": self.tags, "attempts": self.attempts, "max_attempts": self.max_attempts,
            "created": self.created, "run_id": self.run_id, "log_dir": self.log_dir,
            "notes": self.notes, "result": self.result.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        result = data.get("result") or {}
        return cls(
            goal=data.get("goal", ""),
            id=data.get("id") or uuid.uuid4().hex[:12],
            priority=_priority(data.get("priority")),
            created=float(data.get("created") or time.time()),
            status=TaskStatus(data.get("status", "pending")),
            result=TaskResult(
                status=TaskStatus(result.get("status", "pending")),
                outcome=result.get("outcome", ""), detail=result.get("detail", ""),
                evidence=result.get("evidence", ""), actions=int(result.get("actions", 0)),
                duration=float(result.get("duration", 0.0)),
                cost_usd=float(result.get("cost_usd", 0.0)),
                run_dir=result.get("run_dir", ""), error=result.get("error", ""),
                started=float(result.get("started", 0.0)),
                ended=float(result.get("ended", 0.0)),
            ),
            app=data.get("app", ""),
            allow=list(data.get("allow", [])),
            deny=list(data.get("deny", [])),
            max_steps=int(data.get("max_steps", 0)),
            mode=data.get("mode", "computer"),
            command=data.get("command", ""),
            dry_run=bool(data.get("dry_run", False)),
            confirm=bool(data.get("confirm", False)),
            notify=bool(data.get("notify", True)),
            scheduled_for=float(data.get("scheduled_for", 0.0)),
            repeat=data.get("repeat", ""),
            depends_on=list(data.get("depends_on", [])),
            tags=list(data.get("tags", [])),
            attempts=int(data.get("attempts", 0)),
            max_attempts=int(data.get("max_attempts", 1)),
            notes=list(data.get("notes", [])),
            run_id=data.get("run_id") or new_run_id(),
            log_dir=data.get("log_dir", ""),
        )

    def __str__(self) -> str:
        return f"[{self.id}] {self.status.value}: {textutil.truncate(self.goal, 70)}"


def _priority(value) -> TaskPriority:
    if isinstance(value, TaskPriority):
        return value
    try:
        return TaskPriority[str(value).upper()]
    except (KeyError, AttributeError):
        return TaskPriority.NORMAL


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------
def save_history(tasks: list[Task], path: str | Path) -> int:
    """Append every task to a JSONL history file (durable, append-only)."""
    import json

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        for task in tasks:
            try:
                fh.write(json.dumps(task.to_dict(), ensure_ascii=False, default=str) + "\n")
            except (OSError, TypeError, ValueError):
                continue
    _trim(p, 5000)
    return len(tasks)


def load_history(path: str | Path, limit: int = 200) -> list[Task]:
    import json

    p = Path(path)
    if not p.exists():
        return []
    out: list[Task] = []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(Task.from_dict(json.loads(line)))
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    return out


def _trim(path: Path, keep: int) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) <= keep:
            return
        path.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def describe(tasks: list[Task]) -> str:
    if not tasks:
        return "no tasks"
    return "\n".join(
        f"  {t.id}  {t.status.value:<9} {t.priority.label:<8} "
        f"{textutil.truncate(t.goal, 60)}"
        for t in tasks
    )


def summarize(tasks: list[Task]) -> dict:
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.status.value] = counts.get(t.status.value, 0) + 1
    return {
        "total": len(tasks),
        "by_status": counts,
        "succeeded": sum(1 for t in tasks if t.status.successful),
        "failed": sum(1 for t in tasks if t.status is TaskStatus.FAILED),
        "actions": sum(t.result.actions for t in tasks),
        "cost_usd": round(sum(t.result.cost_usd for t in tasks), 4),
        "duration_seconds": round(sum(t.result.duration for t in tasks), 1),
    }
