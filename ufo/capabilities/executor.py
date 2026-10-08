"""Running a plan of capabilities through the safety guard.

Every capability goes through the same gate the agent's own actions do —
emergency latch, budgets, loop detection, allowlist and denylist, risk scoring,
dry run, confirmation, then execution. A capability is not a back door around
the guard; it is a verb the guard already knows how to score, because the
catalog tells it the policy kind and the risk floor for each one.

`Executor` exists so the four callers (the CLI, the agent loop, the phone page
and the daemon) behave identically. The one thing they may differ on is who
answers a confirmation prompt.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from ..config import Config
from ..core.control import get_control
from ..core.rules import get_rules
from ..errors import UFOError
from ..logging_setup import get_logger, record
from ..safety.guard import ActionGuard
from ..safety.policy import (RISK_CRITICAL, ActionPolicy, Risk,
                             RiskClassifier)
from . import catalog, gaps, nlu, registry
from .nlu import Plan, Step

log = get_logger(__name__)

#: The verbs the user uses to drive the agent rather than the machine. They stay
#: available while paused, or nothing could ever resume.
_CONTROL_VERBS = frozenset({
    "ctl_pause", "ctl_resume", "ctl_stop", "ctl_status", "ctl_history",
    "ctl_undo", "ctl_redo", "ctl_undo_list", "ctl_capabilities", "ctl_retry",
    "ctl_remember", "meta_reset",
})


def _is_control_verb(capability: str) -> bool:
    return str(capability or "") in _CONTROL_VERBS


def _control_halt() -> str:
    """Why this step must not run, or "" when the agent is not paused."""
    try:
        control = get_control()
    except Exception as e:
        log.debug("no control state: %s", e)
        return ""
    if not control.paused:
        return ""
    return f"the agent is paused ({control.pause_reason}); say resume to continue"


def _rule_veto(capability: str, target: str = "", args: str = "") -> str:
    """Why a standing rule forbids this step, or "" when none does."""
    try:
        return get_rules().veto_for(capability, target, args)
    except Exception as e:
        log.debug("no rules available: %s", e)
        return ""


#: How to put a capability back, as a goal the executor can run.
#:
#: Deliberately short. Only toggles that are exact are listed, because "undo"
#: that restores the wrong state is worse than an undo that refuses: the user
#: believes the machine is as they left it. Anything not here is journalled as
#: irreversible, and `undo` names it and stops, rather than reversing the step
#: before it and leaving the user thinking it worked.
#:
#: Volume is *not* here. Un-muting does not restore the level, and a saved level
#: would have to be read before the change to be worth anything.
_TOGGLES = {
    "set_do_not_disturb": {"on": "turn do not disturb off",
                           "off": "turn do not disturb on"},
    "set_mute": {"mute": "unmute", "unmute": "mute", "toggle": "unmute"},
    "notifications": {"clear": "notifications list"},
}


def reversal_for(capability: str, args: str) -> str:
    """The goal that reverses this step, or "" when nothing honest can."""
    table = _TOGGLES.get(str(capability or ""))
    if not table:
        return ""
    return table.get(str(args or "").strip().lower(), "")


@dataclass
class StepResult:
    step: Step
    ok: bool
    output: str = ""
    reason: str = ""
    risk: str = ""
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return {"capability": self.step.capability, "target": self.step.target,
                "args": self.step.args, "ok": self.ok, "output": self.output,
                "reason": self.reason, "risk": self.risk,
                "seconds": round(self.seconds, 2)}


@dataclass
class PlanResult:
    request: str
    steps: list[StepResult] = field(default_factory=list)
    outcome: str = "FINISH"          # FINISH | BLOCKED | PARTIAL | FAILED | UNSUPPORTED
    text: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in ("FINISH", "PARTIAL")

    def describe(self) -> str:
        if not self.steps:
            return self.text or "(nothing to do)"
        lines = []
        for step in self.steps:
            mark = "ok  " if step.ok else "FAIL"
            body = step.output or step.reason
            lines.append(f"[{mark}] {step.step.spec}  {textutil_truncate(body, 160)}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"request": self.request, "outcome": self.outcome,
                "steps": [s.to_dict() for s in self.steps], "text": self.text}


def textutil_truncate(text: str, limit: int) -> str:
    from ..utils import text as textutil

    return textutil.truncate(text, limit)


class Executor:
    """Runs capabilities, one guard-gated step at a time."""

    def __init__(self, cfg: Config | None = None, guard: ActionGuard | None = None,
                 narrate: Callable[[str], None] | None = None,
                 confirm: Callable[[str, Risk], bool] | None = None,
                 record_gaps: bool = True):
        self.cfg = cfg or Config()
        self.narrate = narrate
        self.guard = guard or ActionGuard(
            policy=ActionPolicy.from_config(self.cfg.safety),
            config=self.cfg.safety,
            confirm=confirm,
            narrate=narrate,
        )
        self.record_gaps = record_gaps

    # -- one step --
    def _narrate(self, message: str) -> None:
        if self.narrate is not None:
            self.narrate(message)

    def run_step(self, step: Step) -> StepResult:
        cap = catalog.get(step.capability)
        if cap is None:
            result = StepResult(step, False,
                                reason=f"unknown capability {step.capability!r}")
            self._note_gap(step.capability or step.target, result.reason, cap)
            return result

        # "pause" has to mean paused. A pause that only prints a message would
        # leave the user believing the agent had stopped while it carried on.
        # The control verbs themselves are exempt, or nothing could ever resume.
        if not _is_control_verb(step.capability):
            halted = _control_halt()
            if halted:
                return StepResult(step, False, reason=halted)

        # A standing "never" from the user outranks a risk score, so this is
        # checked before the guard. Without this, a correction would be filed and
        # then ignored by the very next request it was meant to cover.
        vetoed = _rule_veto(cap.name, step.target, step.args)
        if vetoed:
            return StepResult(step, False, reason=f"you told me not to: {vetoed}")

        started = time.time()
        # The guard sees only what the user actually supplied. `cap.targets` and
        # `cap.args` are usage documentation ("list|add|remove"), and feeding
        # them to the risk classifier makes the word "remove" escalate a
        # read-only wishlist to high risk. So the hint is printed, not scored.
        if not step.target and cap.targets:
            self._narrate(f"  usage: {cap.name} {cap.targets}"
                          + (f" {cap.args}" if cap.args else ""))
        outcome = self.guard.guard(
            cap.name, step.target, step.args,
            kind=cap.kind,
            execute=lambda: registry.invoke(cap, step.target, step.args),
            floor=cap.risk,
        )
        seconds = time.time() - started
        if not outcome.executed:
            reason = outcome.reason or (
                "dry run: nothing was changed"
                if outcome.risk and self.cfg.safety.dry_run else "not executed")
            return StepResult(step, False, reason=reason,
                              risk=outcome.risk.name if outcome.risk else "",
                              seconds=seconds)
        value = outcome.result
        output = value if isinstance(value, str) else str(value)
        # Journalled only after success: an entry for something that was blocked
        # would make undo try to reverse something that never happened.
        self._journal_step(cap, step)
        return StepResult(step, True, output=output,
                          risk=outcome.risk.name if outcome.risk else "",
                          seconds=seconds)

    def _journal_step(self, cap: catalog.Capability, step: Step) -> None:
        """Record a step that changed something, so `undo` can speak about it.

        Read-only steps are not journalled: there is nothing to undo, and a
        stack full of observations would bury the one step that matters.

        A step that *did* change something but has no reversal is journalled
        anyway. Skipping it would make "undo" reach past the deletion and
        reverse whatever came before it, which is the failure mode this whole
        mechanism exists to avoid.
        """
        try:
            control = get_control()
        except Exception as e:      # a missing journal is not worth failing a run
            log.debug("no control journal: %s", e)
            return
        haystack = " ".join(x for x in (step.target, step.args) if x)
        if not RiskClassifier().changes_machine(cap.name, cap.kind, haystack):
            return
        undo = reversal_for(cap.name, step.args)
        control.record(cap.name, step.target, step.args, undo=undo,
                       reversible=bool(undo))

    # -- a whole plan --
    def run_plan(self, plan: Plan, stop_on_error: bool = False) -> PlanResult:
        results: list[StepResult] = []
        blocked = False
        for step in plan.steps:
            try:
                result = self.run_step(step)
            except UFOError as e:
                result = StepResult(step, False, reason=f"{type(e).__name__}: {e}")
            except Exception as e:      # a handler bug must not kill the run
                log.exception("capability %s failed", step.capability)
                result = StepResult(step, False,
                                    reason=f"{type(e).__name__}: {e}")
            results.append(result)
            if not result.ok:
                blocked = True
                if stop_on_error:
                    break
        if not results:
            return PlanResult(plan.text, results, "UNSUPPORTED",
                              "Nothing to do for that request.")
        if all(r.ok for r in results):
            outcome = "FINISH"
        elif blocked and all(not r.ok for r in results):
            outcome = "BLOCKED"
        else:
            outcome = "PARTIAL"
        return PlanResult(plan.text, results, outcome)

    # -- one shot from a sentence --
    def handle(self, request: str, compound: bool = True) -> PlanResult:
        """Route a sentence and run it, falling back through the ladder.

        1. A named or triggered capability runs directly.
        2. A vague goal with a known recipe expands and runs.
        3. A compound runs its parts in order, and only if all of them route.
        4. Otherwise the fallback ladder runs: write a script, look with UI
           Automation, look with vision, ask one question, do the part that is
           possible, report honestly, and log the gap. See `capabilities/fallback`.
        """
        text = str(request or "").strip()
        if not text:
            return PlanResult("", outcome="UNSUPPORTED", text="Say something first.")

        plan = self._route(text) if compound else nlu.interpret(text)
        if plan is None or not plan.steps:
            return self._fall_back(text)

        return self.run_plan(plan)

    @staticmethod
    def _route(text: str) -> nlu.Plan | None:
        """Read a sentence, as a compound if it says it is one.

        A sentence with a real sequence marker ("then", "after that", "aur phir")
        is routed all-or-nothing and is never re-read as a single sentence.
        Otherwise "check my battery and then compress the heliosphere" becomes a
        confident `hw_battery` that quietly drops the second half.

        A bare "and" does not earn that treatment: "find and replace" is one
        action, and forcing it to route as two parts would break it. So the
        compound is attempted first and the plain reading is still allowed to win.
        """
        if nlu.has_sequence_marker(text):
            return nlu.interpret_compound(text)
        return nlu.interpret_compound(text) or nlu.interpret(text)

    def _fall_back(self, text: str) -> PlanResult:
        """The category 48 ladder, for a request nothing in the catalog matches."""
        from . import fallback

        near = nlu.search(text, limit=3)
        hint = (" Closest: " + ", ".join(cap.name for cap, _p, _s in near)
                + ".") if near else ""
        can_run = self.guard.confirm is not None

        chain = fallback.Fallback(text)
        chain.attempts.append(fallback._try_script(text, can_run))
        chain.attempts.append(fallback._try_uia(text, can_run))
        chain.attempts.append(fallback._try_vision(text, can_run))

        # The partial rung runs the understood half. It is worth trying even when
        # a draft script exists, because the user asked for a result, not a
        # draft — but the steps it runs are only ever catalog capabilities.
        partial = fallback._try_partial(text)
        if partial is not None:
            chain.attempts.append(partial)

        question = fallback._clarifying_question(
            text, self._suggestions_besides(near, partial),
            missing=partial.missing if partial is not None else "")

        if partial is not None and partial.plan is not None:
            self._note_gap(
                text,
                f"partly understood; missing: {partial.detail}",
                None,
            )
            result = self.run_plan(partial.plan)
            # The steps that ran all succeeded, but the request as a whole did
            # not, and PARTIAL is the only honest label for that.
            if result.outcome == "FINISH":
                result.outcome = "PARTIAL"
            result.text = (question + "\n\n" + partial.detail
                           + "\n\nWhat I ran:\n" + result.describe())
            return result

        self._note_gap(
            text,
            "no capability matched" + hint + "; ladder reached: " + chain.reached,
            None,
        )
        chain.attempts.append(fallback.Attempt("log", True, "written to the gap log"))

        tried = [a for a in chain.attempts if a.rung in ("script", "uia", "vision")]
        chain.attempts.append(fallback.Attempt("clarify", True, question.strip()))
        chain.attempts.append(fallback.Attempt("report", True, "shown below"))
        chain.text = (question + "\n\nWhat I tried first:\n"
                      + "\n".join(a.line() for a in tried))
        return PlanResult(text, outcome="UNSUPPORTED", text=chain.text)

    @staticmethod
    def _suggestions_besides(near, partial):
        """Near matches, minus anything already run.

        Offering the user the capability that just succeeded is noise; the open
        question is about the part that did not route.
        """
        if partial is None or partial.plan is None:
            return near
        done = {step.capability for step in partial.plan.steps}
        rest = [(cap, p, s) for cap, p, s in near if cap.name not in done]
        return rest or near

    def preview(self, request: str) -> PlanResult:
        """What `handle` would do, without doing any of it."""
        plan = self._route(request)
        if plan is None or not plan.steps:
            return PlanResult(request, outcome="UNSUPPORTED",
                              text="No capability matched that sentence.")
        return PlanResult(request, [], "FINISH", plan.describe())

    # -- helpers --
    def _note_gap(self, request: str, reason: str, cap: catalog.Capability | None) -> None:
        if not self.record_gaps:
            return
        domain = (catalog.CATEGORIES.get(cap.category, "") if cap is not None
                  else self._closest_domain(request))
        try:
            gaps.get_log().add(request, reason=reason, domain=domain)
        except Exception as e:      # the log must never break a run
            log.debug("could not write the gap log: %s", e)

    @staticmethod
    def _closest_domain(request: str) -> str:
        """The category whose triggers share the most words with the request."""
        from ..utils import text as textutil

        words = set(nlu.normalise(request).split())
        if not words:
            return ""
        best, best_score = "", 0
        for number, title in catalog.CATEGORIES.items():
            score = sum(1 for word in words if word in title.lower().split())
            if score > best_score:
                best, best_score = title, score
        return best


# --------------------------------------------------------------------------
# used by the agent loop
# --------------------------------------------------------------------------
def capability_kind(action: str) -> str:
    """Policy kind for an action name, or "" when it is not a capability."""
    cap = catalog.get(action)
    return cap.kind if cap else ""


def capability_risk_floor(action: str) -> int | None:
    cap = catalog.get(action)
    return cap.risk if cap else None


def needs_confirmation(action: str) -> bool:
    cap = catalog.get(action)
    return bool(cap and cap.confirm)


def is_capability(action: str) -> bool:
    return catalog.get(action) is not None


def describe_all(categories: Iterable[int] | None = None) -> str:
    lines: list[str] = []
    caps = catalog.iter_capabilities(categories)
    by_cat: dict[int, list[catalog.Capability]] = {}
    for cap in caps:
        by_cat.setdefault(cap.category, []).append(cap)
    for number in sorted(by_cat):
        lines.append(f"{number:>2}. {catalog.CATEGORIES.get(number, '')}")
        for cap in by_cat[number]:
            risk_note = f" [risk {cap.risk}]" if cap.risk >= 3 else ""
            confirm_note = " (confirms first)" if cap.confirm else ""
            lines.append(f"      {cap.name:<20} {cap.summary}{risk_note}{confirm_note}")
    return "\n".join(lines)


__all__ = [
    "Executor", "PlanResult", "StepResult", "capability_kind",
    "capability_risk_floor", "describe_all", "is_capability", "needs_confirmation",
    "RISK_CRITICAL",
]
