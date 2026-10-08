"""Task decomposition.

A long task ("clean up my downloads, then email the report") is far more
reliable as three verified steps than as one 200-step loop. The planner asks
the model for a short ordered list, then each step runs as its own scoped
AgentLoop with its own budget — so one bad step cannot burn the whole run, and
a failure is reported against a step the user can actually name.

Deliberately capped: more than ~8 steps, or a plan the model cannot justify,
means fall back to a single loop rather than trusting a bad decomposition.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Config
from ..errors import ValidationError
from ..logging_setup import get_logger, record
from ..utils import text as textutil
from . import llm

log = get_logger(__name__)

MAX_STEPS = 8
MIN_STEPS_TO_PLAN = 2

PLANNER_PROMPT = """You break a user's automation request into the smallest
number of ordered steps, each of which one automation agent can carry out and
verify on its own.

Reply with EXACTLY one JSON object:
{
  "reasoning": "one sentence on why this decomposition",
  "needs_clarification": false,
  "clarification": "",
  "steps": [
    {"id": 1, "goal": "what must be true after this step",
     "verify": "how to tell from the screen or the filesystem that it worked",
     "app": "the app to use, or empty"}
  ]
}

Rules:
- Prefer 1 step. Only split when the parts genuinely need different apps,
  different windows, or a decision made after seeing the first part's result.
- Each step must be independently checkable. If you cannot say how to verify
  it, it is not a step — fold it into the next one.
- Never invent information the user did not give. If a step depends on
  something unknown (a file name, a password, a choice only the user can make),
  set needs_clarification to true and put the question in "clarification".
- Never include signing in, entering credentials, or paying for anything.
- Use the user's own wording in "goal" so they can recognize it.
"""


@dataclass
class PlanStep:
    id: int
    goal: str
    verify: str = ""
    app: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "goal": self.goal, "verify": self.verify, "app": self.app}


@dataclass
class Plan:
    task: str
    steps: list[PlanStep] = field(default_factory=list)
    reasoning: str = ""
    needs_clarification: bool = False
    clarification: str = ""
    source: str = "model"          # model | single | fallback

    @property
    def is_trivial(self) -> bool:
        return len(self.steps) <= 1

    def to_dict(self) -> dict:
        return {
            "task": self.task, "reasoning": self.reasoning, "source": self.source,
            "needs_clarification": self.needs_clarification,
            "clarification": self.clarification,
            "steps": [s.to_dict() for s in self.steps],
        }

    def describe(self) -> str:
        lines = [f"Plan for {self.task!r} ({len(self.steps)} step(s)):"]
        for s in self.steps:
            line = f"  {s.id}. {s.goal}"
            if s.app:
                line += f"  (in {s.app})"
            if s.verify:
                line += f"  — verify: {s.verify}"
            lines.append(line)
        if self.needs_clarification:
            lines.append(f"NEEDS A CLARIFYING ANSWER: {self.clarification}")
        return "\n".join(lines)


class Planner:
    def __init__(self, client: Any = None, cfg: Config | None = None):
        self.cfg = cfg or Config()
        self.client = client
        self.log = log

    def _client(self):
        if self.client is None:
            from ..config import LLMConfig

            self.client = llm.Client(LLMConfig(
                provider=self.cfg.llm.provider, model=self.cfg.llm.model,
                max_tokens=self.cfg.llm.max_tokens, timeout=self.cfg.llm.timeout,
                base_url=self.cfg.llm.base_url, fallbacks=self.cfg.llm.fallbacks,
                offline=self.cfg.llm.offline,
            ))
        return self.client

    def plan(self, task: str, context: str = "") -> Plan:
        """Decompose `task`, falling back to a single step on any problem.

        A plan is an optimisation, never a prerequisite: if the model is
        unavailable or returns nonsense, the caller still gets a usable plan.
        """
        task = textutil.collapse_ws(task)
        if not task:
            return Plan(task, [], source="single")

        if not self.cfg.automation.planner_enabled:
            return self._single(task)

        user_text = f"TASK: {task}"
        if context:
            user_text += f"\n\nWHAT IS CURRENTLY OPEN:\n{textutil.truncate(context, 2000)}"

        try:
            response = self._client().complete(llm.require(PLANNER_PROMPT), user_text)
            raw = llm.parse_json(response.text)
            return self._build(task, raw)
        except Exception as e:
            self.log.warning("planning failed (%s); running the task as one unit.", e)
            return self._single(task)

    def _single(self, task: str) -> Plan:
        return Plan(task, [PlanStep(1, task, verify="the user sees the result")],
                    reasoning="Treated as a single step.", source="single")

    def _build(self, task: str, raw: Any) -> Plan:
        if not isinstance(raw, dict):
            raise ValidationError("Planner reply was not an object.")

        steps_raw = raw.get("steps") or []
        if isinstance(steps_raw, str):
            steps_raw = [{"goal": s} for s in re.split(r"\n+|(?<=;)\s*", steps_raw) if s.strip()]
        if not isinstance(steps_raw, list) or not steps_raw:
            raise ValidationError("Planner returned no steps.")

        steps: list[PlanStep] = []
        for index, item in enumerate(steps_raw[:MAX_STEPS], 1):
            if isinstance(item, str):
                item = {"goal": item}
            if not isinstance(item, dict):
                continue
            goal = textutil.collapse_ws(item.get("goal") or item.get("task") or "")
            if not goal:
                continue
            steps.append(PlanStep(
                id=index,
                goal=goal[:400],
                verify=textutil.collapse_ws(item.get("verify", ""))[:300],
                app=textutil.collapse_ws(item.get("app", ""))[:120],
            ))

        if not steps:
            raise ValidationError("No usable steps in the planner reply.")
        if len(steps) > MAX_STEPS:
            self.log.warning("planner returned %d steps; truncating to %d.",
                             len(steps), MAX_STEPS)

        plan = Plan(
            task=task,
            steps=steps,
            reasoning=textutil.truncate(str(raw.get("reasoning", "")), 400),
            needs_clarification=bool(raw.get("needs_clarification")),
            clarification=textutil.truncate(str(raw.get("clarification", "")), 400),
            source="model",
        )
        record("plan", steps=len(plan.steps), clarification=plan.needs_clarification)
        return plan


def heuristic_plan(task: str, max_steps: int = MAX_STEPS) -> Plan:
    """Split on 'then' / 'and then' / ';' without calling a model.

    Used in dry-run mode and by the scheduler, where a model call is either
    unwanted or unavailable.
    """
    parts = [p.strip(" .;") for p in re.split(r"\s*(?:;|\bthen\b|\band then\b|\.\.\.)\s*",
                                             task, flags=re.IGNORECASE) if p.strip(" .;")]
    if len(parts) < MIN_STEPS_TO_PLAN:
        return Plan(task, [PlanStep(1, task)], reasoning="No split points found.",
                    source="fallback")
    return Plan(
        task,
        [PlanStep(i, p, verify="the user sees the result") for i, p in enumerate(parts[:max_steps], 1)],
        reasoning="Split on 'then'/';' in the request.",
        source="fallback",
    )


def to_prompt(plan: Plan) -> str:
    return plan.describe()


def save(plan: Plan, path) -> str:
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return str(p)
