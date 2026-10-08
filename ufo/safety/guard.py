"""The action guard — the single choke point every action must pass through.

Order of checks, deliberately fail-closed:

    1. emergency latch      — has the user hit the panic hotkey?
    2. run budgets          — wall clock, step count, LLM calls, cost
    3. loop detection       — same action on the same target, over and over
    4. policy               — allowlist / denylist / risk block
    5. dry run              — describe, do not do
    6. confirmation         — narrate, then wait for a human when required
    7. execute               — the caller's actual function

`confirm` is injected (`Callable[[str, Risk], bool]`) so the same guard serves
console prompts, voice prompts, an HTTP request from the dashboard, or tests.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Protocol

from ..config import SafetyConfig
from ..errors import BudgetExceeded, ConfirmationRequired, EmergencyAbort
from ..logging_setup import get_logger, record
from ..utils import text as textutil
from .emergency import EmergencySwitch, emergency as default_emergency
from .policy import ActionPolicy, PolicyDecision, Risk, RiskClassifier, RiskLevel


class Confirmer(Protocol):
    def __call__(self, message: str, risk: Risk) -> bool:  # pragma: no cover
        ...


@dataclass
class RunBudget:
    """Consumable limits for one task run."""

    max_actions: int = 200
    max_wall_seconds: int = 3600
    max_llm_calls: int = 200
    max_cost_usd: float = 1.00

    started: float = field(default_factory=time.monotonic)
    actions: int = 0
    llm_calls: int = 0
    cost_usd: float = 0.0

    @classmethod
    def from_config(cls, cfg: SafetyConfig) -> "RunBudget":
        return cls(
            max_actions=cfg.max_actions_per_run,
            max_wall_seconds=cfg.max_wall_seconds_per_run,
            max_llm_calls=cfg.max_llm_calls_per_run,
            max_cost_usd=cfg.max_cost_usd_per_run,
        )

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def remaining_actions(self) -> int:
        return max(0, self.max_actions - self.actions)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.max_wall_seconds - self.elapsed)

    def to_dict(self) -> dict:
        return {
            "elapsed_seconds": round(self.elapsed, 1),
            "actions": f"{self.actions}/{self.max_actions}",
            "llm_calls": f"{self.llm_calls}/{self.max_llm_calls}",
            "cost_usd": round(self.cost_usd, 4),
            "max_cost_usd": self.max_cost_usd,
        }

    def check(self) -> None:
        if self.remaining_seconds <= 0:
            raise BudgetExceeded(
                f"Wall-clock budget of {self.max_wall_seconds}s exhausted.")
        if self.actions >= self.max_actions:
            raise BudgetExceeded(
                f"Action budget of {self.max_actions} exhausted.")

    def charge_action(self) -> None:
        # Check *before* incrementing: max_actions=2 must permit exactly two
        # actions, not one.
        self.check()
        self.actions += 1

    def charge_llm(self, cost_usd: float = 0.0) -> None:
        # Reject the call that *would* push the run over budget, rather than
        # spending first and complaining afterwards. A budget of $0.01 must
        # therefore still allow a single $0.01 call.
        cost_usd = max(0.0, cost_usd)
        if self.llm_calls + 1 > self.max_llm_calls:
            raise BudgetExceeded(
                f"LLM call budget of {self.max_llm_calls} exhausted.")
        if self.max_cost_usd > 0 and self.cost_usd + cost_usd > self.max_cost_usd:
            raise BudgetExceeded(
                f"Cost budget of ${self.max_cost_usd:.2f} exhausted "
                f"(${self.cost_usd:.2f} already spent).")
        self.llm_calls += 1
        self.cost_usd += cost_usd


@dataclass
class GuardResult:
    allowed: bool
    executed: bool = False
    result: object = None
    risk: Risk | None = None
    reason: str = ""


@dataclass
class ActionGuard:
    policy: ActionPolicy
    config: SafetyConfig
    budget: RunBudget | None = None
    emergency_switch: EmergencySwitch = field(default=None)  # type: ignore[assignment]
    confirm: Confirmer | None = None
    narrate: Callable[[str], None] | None = None
    classifier: RiskClassifier = field(default_factory=RiskClassifier)
    _history: Deque[tuple[str, int]] = field(default_factory=lambda: deque(maxlen=64), init=False)
    _suspicion: float = field(default=0.0, init=False)
    log: object = field(default_factory=lambda: get_logger(__name__), init=False, repr=False)

    def __post_init__(self) -> None:
        if self.emergency_switch is None:
            self.emergency_switch = default_emergency
        if self.narrate is None:
            from ..voice import console_narrator

            self.narrate = console_narrator

    # ---- budgets ----
    @property
    def active_budget(self) -> RunBudget:
        if self.budget is None:
            self.budget = RunBudget.from_config(self.config)
        return self.budget

    def reset_budget(self) -> RunBudget:
        self.budget = RunBudget.from_config(self.config)
        return self.budget

    # ---- loop detection ----
    def note_action(self, action: str, target: str = "", args: str = "") -> None:
        """Feed the loop detector.

        `config.loop_detection_threshold` is a count of *identical consecutive
        actions*: with a threshold of 4, doing the same thing four times in a
        row is treated as a loop. Different actions decay the suspicion, so an
        agent that alternates between two sensible actions is not punished.
        """
        sig = textutil.action_signature(action, target, args)
        if self._history and self._history[-1][0] == sig:
            self._suspicion += 1.0
        else:
            self._suspicion = 1.0
        self._history.append((sig, int(time.time())))
        if self._suspicion >= self.config.loop_detection_threshold:
            self.log.warning(
                "Loop suspected: %s repeated %.0f times in a row.",
                sig, self._suspicion,
            )

    @property
    def looping(self) -> bool:
        return self._suspicion >= self.config.loop_detection_threshold

    def reset_loop(self) -> None:
        self._history.clear()
        self._suspicion = 0.0

    # ---- the gate ----
    def evaluate(self, action: str, target: str = "", args: str = "",
                 kind: str = "ui", subject: str = "",
                 floor: int = 0) -> PolicyDecision:
        return self.policy.evaluate(action, target, args, kind=kind,
                                    subject=subject, floor=floor)

    def preflight(self, action: str, target: str = "", args: str = "",
                  kind: str = "ui", subject: str = "",
                  floor: int = 0) -> GuardResult:
        """Checks 1-4. Raises only for emergency/budget; returns a decision otherwise."""
        self.emergency_switch.check(f"{action} on {target!r}")

        budget = self.active_budget
        budget.check()

        decision = self.evaluate(action, target, args, kind=kind, subject=subject,
                                 floor=floor)
        if not decision.allowed:
            record("policy_block", action=action, target=textutil.truncate(target, 120),
                   reason=decision.reason, risk=decision.risk.name)
            self.log.warning("Blocked %r on %r: %s", action, target, decision.reason)
            return GuardResult(False, risk=decision.risk, reason=decision.reason)

        needs_confirm = self.config.confirm_all or (
            decision.risk.level >= self.config.confirm_risk_at_or_above
        )
        if self.looping and decision.risk.level == 0:
            needs_confirm = True  # a looping harmless action still needs a human

        return GuardResult(True, risk=decision.risk, reason="", result=None) if not needs_confirm \
            else GuardResult(True, risk=decision.risk, reason="", result="needs_confirmation")

    def request_confirmation(self, message: str, risk: Risk | None = None) -> bool:
        """Ask the injected confirmer. Missing confirmer == deny (fail safe)."""
        risk = risk or Risk(0, "none")
        self.active_budget.check()
        if self.confirm is None:
            self.log.error("Confirmation requested but no confirmer is wired; denying.")
            return False
        try:
            approved = bool(self.confirm(message, risk))
        except KeyboardInterrupt:
            self.emergency_switch.trip("user interrupted a confirmation prompt")
            raise
        except Exception as e:
            self.log.error("Confirmer failed (%s); denying the action.", e)
            return False
        record("confirmation", approved=approved, risk=risk.name, message=textutil.truncate(message, 160))
        return approved

    def guard(self, action: str, target: str = "", args: str = "",
              kind: str = "ui", subject: str = "",
              execute: Callable[[], object] | None = None,
              floor: int = 0) -> GuardResult:
        """Run the full pipeline. Executes `execute()` only if everything passes."""
        pre = self.preflight(action, target, args, kind=kind, subject=subject,
                             floor=floor)
        if not pre.allowed:
            return pre

        risk = pre.risk or Risk(0, "none")
        description = describe(action, target, args, kind=kind, risk=risk)

        if self.config.dry_run:
            self.narrate(f"[dry run] {description}")
            record("dry_run", action=action, target=textutil.truncate(target, 120), risk=risk.name)
            return GuardResult(True, executed=False, risk=risk, reason="dry_run")

        needs = pre.result == "needs_confirmation"
        if needs:
            if not self.request_confirmation(description, risk):
                self.narrate(f"Skipped: {description}")
                record("declined", action=action, target=textutil.truncate(target, 120))
                return GuardResult(False, executed=False, risk=risk, reason="declined_by_user")

        self.narrate(description)
        self.note_action(action, target, args)
        self.active_budget.charge_action()

        if execute is None:
            return GuardResult(True, executed=False, risk=risk, reason="no_executor")

        try:
            result = execute()
        except EmergencyAbort:
            raise
        except Exception as e:
            record("action_error", action=action, error=f"{type(e).__name__}: {e}")
            self.log.error("Action %r failed: %s", action, e)
            raise

        record("action", action=action, target=textutil.truncate(target, 120),
               kind=kind, risk=risk.name, result=textutil.truncate(str(result), 200))
        return GuardResult(True, executed=True, result=result, risk=risk)

    # ---- context manager ----
    def confirmation(self, message: str, risk: Risk | None = None) -> bool:
        return self.request_confirmation(message, risk)


def describe(action: str, target: str = "", args: str = "", kind: str = "ui",
             risk: Risk | None = None) -> str:
    """One spoken/console sentence describing what is about to happen."""
    target = textutil.truncate(target, 70)
    args = textutil.truncate(args, 70)
    if kind == "shell":
        base = f"run the command {args or target}"
    elif kind == "fs":
        base = f"{action} on file {target}" + (f" with {args}" if args else "")
    elif kind == "process":
        base = f"{action} process {target or args}"
    elif kind == "system":
        base = f"{action} system setting {target or args}"
    elif kind == "network":
        base = f"{action} {target or args}"
    elif kind == "window":
        base = f"{action} window {target or args}"
    elif kind == "mouse":
        base = f"{action} at {target or args}"
    elif kind == "keyboard":
        base = f"send keys {args or target}"
    elif action in ("type", "set_text", "paste"):
        base = f"type {args!r} into {target}"
    elif action == "hotkey":
        base = f"press {args or target}"
    else:
        base = f"{action} {target}".strip()

    if risk and risk.level >= 2 and risk.reasons:
        base += f" — flagged as {risk.name} risk: {risk.reasons[0]}"
    return base


def require_confirmation(message: str) -> None:
    raise ConfirmationRequired(message)
