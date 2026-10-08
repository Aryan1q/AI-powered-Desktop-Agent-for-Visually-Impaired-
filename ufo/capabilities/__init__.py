"""What the agent can do, and how a request for it gets turned into work.

Four pieces, each with one job:

* `catalog`  — the 48 domains, the action in each, its risk floor, its policy
  kind, and its undo. The single source of truth about capability.
* `registry` — resolves an action name to real code and runs it.
* `nlu`      — turns a sentence (English, Hindi, Hinglish, or loose and vague)
  into a plan of actions, or returns None so the model can have it instead.
* `executor` — runs a plan through the ordinary safety guard: allowlist,
  denylist, risk, confirmation, dry run, budgets, emergency stop.
* `gaps`     — what to do when nothing matched: write it down and say so.
"""
from __future__ import annotations

from . import catalog, executor, gaps, nlu, phrases, registry
from .catalog import ALL as CAPABILITIES, Capability, CATEGORIES, categories
from .executor import Executor, PlanResult, StepResult
from .gaps import Gap, GapLog, log_gap
from .nlu import Plan, Step, interpret, try_interpret
from .registry import invoke, resolve, run

__all__ = [
    "ALL",
    "CAPABILITIES",
    "CATEGORIES",
    "Capability",
    "Executor",
    "Gap",
    "GapLog",
    "Plan",
    "PlanResult",
    "Step",
    "StepResult",
    "catalog",
    "categories",
    "executor",
    "gaps",
    "interpret",
    "invoke",
    "log_gap",
    "nlu",
    "phrases",
    "registry",
    "resolve",
    "run",
    "try_interpret",
]
