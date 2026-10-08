"""Task execution: turn a `Task` into a result.

One place knows how to actually run a task, so the CLI, the HTTP API, the
dashboard and the daemon all get identical behaviour — including the
confirmation plumbing, the per-task safety overrides, narration, notifications,
memory writes and history persistence.
"""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from ..agent.base import Outcome
from ..config import Config
from ..errors import (BudgetExceeded, EmergencyAbort, LLMError, NoLLMConfigured,
                      PolicyViolation, QuotaExceeded, UFOError, WindowNotFoundError)
from ..logging_setup import get_logger, record
from ..safety.emergency import emergency
from ..utils import text as textutil
from .memory import Episode, get_memory
from .task import Task, TaskResult, TaskStatus

log = get_logger(__name__)


@dataclass
class Runner:
    """Executes tasks. Inject `agent_factory` to test without a real desktop."""

    cfg: Config
    narrator: Any = None
    agent_factory: Optional[Callable[[Task, Config], Any]] = None
    should_continue: Optional[Callable[[], bool]] = None   # poll for cancellation

    def __post_init__(self) -> None:
        if self.narrator is None:
            from ..voice import Narrator

            self.narrator = Narrator(
                enabled=self.cfg.voice.enabled, rate=self.cfg.voice.rate,
                volume=self.cfg.voice.volume,
                console=self.cfg.voice.confirmation_style != "voice",
            )

    # ---- config for this task ----
    def config_for(self, task: Task) -> Config:
        """A per-task copy of the config, so one task's overrides never leak."""
        cfg = self.cfg.copy()
        if task.allow:
            cfg.safety.allowlist = list(dict.fromkeys(cfg.safety.allowlist + task.allow))
        if task.deny:
            cfg.safety.denylist = list(dict.fromkeys(cfg.safety.denylist + task.deny))
        if task.dry_run:
            cfg.safety.dry_run = True
        if task.confirm:
            cfg.safety.confirm_all = True
        if task.max_steps:
            cfg.automation.max_steps = int(task.max_steps)
        return cfg

    # ---- the run ----
    def run(self, task: Task) -> Task:
        cfg = self.config_for(task)
        emergency.reset()
        task.start()
        record("task_start", id=task.id, goal=task.goal[:160], mode=task.mode)

        try:
            result = self._dispatch(task, cfg)
        except EmergencyAbort:
            task.cancel("emergency stop was triggered")
            self._finish(task, cfg, "aborted by emergency stop")
            return task
        except BudgetExceeded as e:
            task.fail(str(e), "the run hit a safety or cost budget limit")
            self._finish(task, cfg, "budget exceeded")
            return task
        except QuotaExceeded as e:
            task.fail(str(e), "the language model quota ran out")
            self._finish(task, cfg, "quota exceeded")
            return task
        except (NoLLMConfigured, LLMError) as e:
            task.fail(str(e), "could not reach the language model")
            self._finish(task, cfg, "model error")
            return task
        except PolicyViolation as e:
            task.fail(str(e), "the safety policy blocked this task")
            self._finish(task, cfg, "policy blocked")
            return task
        except WindowNotFoundError as e:
            task.fail(str(e), "the target window was not found or is not allowlisted")
            self._finish(task, cfg, "window not found")
            return task
        except KeyboardInterrupt:
            task.cancel("the user interrupted the run")
            self._finish(task, cfg, "interrupted")
            raise
        except Exception as e:  # noqa: BLE001 - the CLI must never traceback at a user
            log.error("task %s crashed: %s\n%s", task.id, e, traceback.format_exc())
            task.fail(f"{type(e).__name__}: {e}", "unexpected error")
            self._finish(task, cfg, "crashed")
            return task

        self._apply(task, result, cfg)
        return task

    def _apply(self, task: Task, result: TaskResult, cfg: Config) -> None:
        task.result = result
        task.status = result.status
        if task.log_dir:
            task.log_dir = result.run_dir or task.log_dir
        self._finish(task, cfg, result.outcome)

    def _finish(self, task: Task, cfg: Config, outcome: str) -> None:
        record("task_end", id=task.id, outcome=outcome, status=task.status.value,
               actions=task.result.actions, seconds=round(task.result.duration, 1))
        if cfg.automation.persist_memory:
            try:
                get_memory(cfg.storage.memory_path).record(Episode(
                    task=task.goal, outcome=outcome,
                    success=task.status.successful,
                    steps=task.result.actions, duration=task.result.duration,
                    lesson=self._lesson(task),
                    app=task.app,
                ))
            except Exception as e:
                log.warning("could not write to memory: %s", e)
        try:
            from .task import save_history

            save_history([task], cfg.storage.history_path)
        except Exception as e:
            log.warning("could not append to history: %s", e)
        if task.notify and task.status.terminal and not task.status.successful:
            self._notify(task)

    def _lesson(self, task: Task) -> str:
        if task.status.successful:
            return ""
        if "policy" in (task.result.outcome or ""):
            return f"{task.goal} was blocked by the safety policy."
        if task.result.actions == 0:
            return f"{task.goal} made no progress; the target may not be open."
        return f"{task.goal} failed after {task.result.actions} action(s)."

    def _notify(self, task: Task) -> None:
        try:
            from ..voice import beep_pattern, notify

            beep_pattern("error")
            notify("Automation failed", textutil.truncate(task.result.summary(), 160))
        except Exception:
            pass

    # ---- dispatch by mode ----
    def _dispatch(self, task: Task, cfg: Config) -> TaskResult:
        mode = (task.mode or "computer").lower()
        if mode == "command":
            return self._run_command(task, cfg)
        if mode == "plan":
            return self._run_plan(task, cfg)
        if mode in ("app", "window"):
            return self._run_app(task, cfg)
        return self._run_computer(task, cfg)

    def _build(self, task: Task, cfg: Config, mode: str):
        if self.agent_factory is not None:
            return self.agent_factory(task, cfg)
        if mode == "app":
            from ..agent.app_agent import AppAgent
            from ..perception import uia

            info = uia.wait_for(task.app or ".*", timeout=cfg.automation.window_timeout,
                                allowed=cfg.safety.allowlist or None)
            return AppAgent(info.wrapper, task.goal, cfg=cfg,
                            narrator=self.narrator,
                            max_steps=cfg.automation.max_steps,
                            close_on_finish=cfg.automation.close_window_on_finish)
        from ..agent.computer_agent import ComputerAgent

        return ComputerAgent(task=task.goal, cfg=cfg, narrator=self.narrator,
                             max_steps=cfg.automation.max_steps)

    def _finalize(self, agent: Any, task: Task, cfg: Config,
                  observation: Any = None) -> TaskResult:
        from ..agent.verifier import Verifier, run_deterministic

        budget = agent.guard.active_budget if getattr(agent, "guard", None) else None
        detail, evidence = "", ""

        if cfg.automation.verifier_enabled and agent.outcome != Outcome.ANSWERED.value:
            # An ANSWERED run changed nothing, so asking "is the task done?"
            # would always fail and bury the answer under a bogus verdict.
            deterministic = run_deterministic(task.goal, agent.history)
            if deterministic is not None:
                detail, evidence = deterministic.evidence, deterministic.check
            else:
                verifier = Verifier(cfg=cfg, client=getattr(agent, "client", None))
                result = verifier.verify(
                    task.goal,
                    agent.describe_context() if observation is None else observation,
                    agent.history,
                    image_b64=getattr(observation, "b64", None) if observation else None,
                )
                detail = verifier.describe(result)
                evidence = result.verdict
                if result.verdict == "FAIL" and agent.outcome == "FINISH":
                    log.warning("the agent claimed success but verification failed: %s",
                                result.missing)

        return TaskResult(
            status=task_status_for(agent.outcome),
            outcome=agent.outcome,
            detail=detail or agent.outcome,
            evidence=evidence,
            actions=agent.actions_done,
            duration=time.time() - agent.started,
            cost_usd=budget.cost_usd if budget else 0.0,
            run_dir=str(getattr(agent, "run_dir", "") or ""),
        )

    # ---- modes ----
    def _run_computer(self, task: Task, cfg: Config) -> TaskResult:
        agent = self._build(task, cfg, "computer")
        self._bind_narration(agent)
        outcome = agent.run()
        observation = getattr(agent, "_last_observation", None)
        result = self._finalize(agent, task, cfg, observation)
        if task.status.terminal:
            task.status = result.status
        del outcome
        return result

    def _run_app(self, task: Task, cfg: Config) -> TaskResult:
        agent = self._build(task, cfg, "app")
        self._bind_narration(agent)
        agent.run()
        return self._finalize(agent, task, cfg, getattr(agent, "_last_observation", None))

    def _run_plan(self, task: Task, cfg: Config) -> TaskResult:
        from ..agent.planner import Planner

        planner = Planner(cfg=cfg)
        context = self._current_context()
        plan = planner.plan(task.goal, context)
        log.info("%s", plan.describe())

        if plan.needs_clarification:
            return TaskResult(status=TaskStatus.BLOCKED, outcome="BLOCKED",
                              detail=f"Needs an answer: {plan.clarification}")

        total_actions = 0
        total_cost = 0.0
        step_results: list[str] = []
        for step in plan.steps:
            if self.should_continue and not self.should_continue():
                return TaskResult(status=TaskStatus.CANCELLED, outcome="CANCELLED",
                                  detail=f"stopped before step {step.id}",
                                  actions=total_actions, cost_usd=total_cost)
            sub = Task(goal=step.goal, priority=task.priority, app=step.app or task.app,
                       allow=list(task.allow), dry_run=task.dry_run, notify=False)
            log.info("plan step %d/%d: %s", step.id, len(plan.steps), step.goal)
            agent = self._build(sub, cfg, "app" if step.app else "computer")
            self._bind_narration(agent)
            agent.run()
            sub_result = self._finalize(agent, sub, cfg,
                                        getattr(agent, "_last_observation", None))
            total_actions += sub_result.actions
            total_cost += sub_result.cost_usd
            mark = "ok" if sub_result.ok else sub_result.outcome
            step_results.append(f"{step.id}. {textutil.truncate(step.goal, 60)} -> {mark}")
            if not sub_result.ok and sub_result.status is TaskStatus.FAILED:
                return TaskResult(
                    status=TaskStatus.FAILED, outcome="FAILED",
                    detail=f"step {step.id} failed: {sub_result.detail}",
                    actions=total_actions, cost_usd=total_cost,
                    evidence="; ".join(step_results),
                )
        return TaskResult(status=TaskStatus.SUCCEEDED, outcome="FINISH",
                          detail=f"completed {len(plan.steps)} step(s)",
                          evidence="; ".join(step_results),
                          actions=total_actions, cost_usd=total_cost)

    def _run_command(self, task: Task, cfg: Config) -> TaskResult:
        from ..actions import shell
        from ..safety.guard import ActionGuard
        from ..safety.policy import ActionPolicy

        command = task.command or task.goal
        guard = ActionGuard(
            policy=ActionPolicy.from_config(cfg.safety), config=cfg.safety,
            narrate=self.narrator.say, confirm=self._confirm,
        )
        started = time.time()
        result = guard.guard(
            "run_command", "shell", command, kind="shell",
            execute=lambda: shell.run_powershell(command,
                                                timeout=cfg.automation.action_timeout),
        )
        if not result.executed:
            status = TaskStatus.PENDING if cfg.safety.dry_run else TaskStatus.FAILED
            return TaskResult(status=status, outcome=result.reason.upper() or "BLOCKED",
                              detail=result.reason, actions=0,
                              duration=time.time() - started)
        command_result = result.result
        return TaskResult(
            status=TaskStatus.SUCCEEDED if command_result.ok else TaskStatus.FAILED,
            outcome="EXIT_" + str(command_result.returncode),
            detail=str(command_result), actions=1,
            duration=time.time() - started,
            evidence=command_result.output[:400],
        )

    def _confirm(self, message: str, risk: Any) -> bool:
        return bool(self.narrator.say(message) and self._ask(message))

    def _ask(self, message: str) -> bool:
        try:
            answer = input("[CONFIRM] 'yes' / 'no' / 'stop': ")
        except (EOFError, KeyboardInterrupt):
            return False
        answer = textutil.collapse_ws(answer).lower()
        if answer in {"stop", "cancel", "abort"}:
            emergency.trip("panic phrase at a confirmation prompt")
            return False
        return answer in {"y", "yes"}

    # ---- helpers ----
    def _bind_narration(self, agent: Any) -> None:
        if self.narrator is not None and hasattr(agent, "narrate"):
            agent.narrate = self.narrator.say

    def _current_context(self) -> str:
        try:
            from ..perception import uia

            windows = uia.list_all_windows()
            return ", ".join(w.title for w in windows[:20])
        except Exception:
            return ""


def task_status_for(outcome: str) -> TaskStatus:
    mapping = {
        "FINISH": TaskStatus.SUCCEEDED,
        "ANSWERED": TaskStatus.SUCCEEDED,
        "FAIL": TaskStatus.FAILED,
        "MAX_STEPS": TaskStatus.FAILED,
        "USER_ABORTED": TaskStatus.CANCELLED,
        "EMERGENCY_ABORT": TaskStatus.CANCELLED,
        "BUDGET_EXCEEDED": TaskStatus.FAILED,
        "LLM_ERROR": TaskStatus.FAILED,
        "QUOTA_EXCEEDED": TaskStatus.FAILED,
        "OBSERVATION_ERROR": TaskStatus.FAILED,
        "ACTION_ERROR": TaskStatus.FAILED,
        "LOOP_DETECTED": TaskStatus.FAILED,
        "VERIFICATION_FAILED": TaskStatus.FAILED,
        "POLICY_BLOCKED": TaskStatus.FAILED,
    }
    return mapping.get(str(outcome).upper(), TaskStatus.FAILED)
