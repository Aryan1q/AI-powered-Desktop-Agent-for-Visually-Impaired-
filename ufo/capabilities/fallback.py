"""Category 48: what happens when nothing in the catalog fits.

The specification asks for a ladder, in this order:

1. generate a script or tool on the fly;
2. drive the UI with Windows UI Automation;
3. look at a screenshot and use vision;
4. ask one clarifying question;
5. do whatever part is possible;
6. report honestly what failed and why;
7. log the request as a missing capability.

Each rung is only tried when the one above it cannot help, and every rung is
recorded, so the user can see *which* attempt was made rather than being told
"unsupported" with no explanation.

The two rules that matter:

* **Nothing runs unattended.** Steps 1 to 3 are the ones that could change the
  machine, and a generated script in particular is code this agent wrote itself.
  They require a confirmer. With no confirmer wired — a pipe, a scheduled tick —
  the ladder stops at step 4 and asks, because a silent action is worse than a
  question.
* **A partial result is reported as partial.** `PARTIAL` is not dressed up as
  success, and the failed steps are named.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..logging_setup import get_logger
from .nlu import Plan, Step

log = get_logger(__name__)

#: The rungs, in order. `NEEDS_CONFIRMATION` is what stops step 1 and 2 from
#: ever running by themselves.
LADDER = (
    "script",
    "uia",
    "vision",
    "clarify",
    "partial",
    "report",
    "log",
)


@dataclass
class Attempt:
    """One rung of the ladder and what came of it.

    `ok` means the rung produced something usable — a draft script, or steps it
    can run. It does not mean the request was fulfilled; almost nothing here
    does. `skipped` marks a rung that was not worth entering, which is what
    makes `Fallback.reached` mean "how far did we get" rather than "did it work".
    """

    rung: str
    ok: bool
    detail: str = ""
    plan: Plan | None = None
    skipped: bool = False
    missing: str = ""

    def line(self) -> str:
        mark = "ok  " if self.ok else ("skip" if self.skipped else "--  ")
        return f"{mark} {self.rung:<8} {self.detail[:200]}"


@dataclass
class Fallback:
    """The whole ladder's worth of attempts for one request."""

    request: str
    attempts: list[Attempt] = field(default_factory=list)
    text: str = ""

    @property
    def reached(self) -> str:
        last = ""
        for attempt in self.attempts:
            if not attempt.skipped:
                last = attempt.rung
        return last or "none"

    @property
    def executed(self) -> Plan | None:
        for attempt in self.attempts:
            if attempt.ok and attempt.plan is not None:
                return attempt.plan
        return None

    def describe(self) -> str:
        head = f"{self.request!r}: "
        if self.text:
            return head + self.text
        return head + "\n".join(a.line() for a in self.attempts)


def _label(control) -> str:
    """A control's name is often empty, so show what it is instead of nothing."""
    name = (getattr(control, "name", "") or "").strip()
    if name:
        return name
    kind = (getattr(control, "type", "") or "").strip()
    ident = getattr(control, "id", "?")
    return f"(unnamed {kind or 'control'} #{ident})"


# --------------------------------------------------------------------------
# rung 1: write a script
# --------------------------------------------------------------------------
_SCRIPTABLE = re.compile(
    r"(?i)\b(batch|powershell|script|automate|every time|each time|"
    r"schedule|rename|copy|move|delete|convert|extract|list|sort|filter|"
    r"read|write|append|parse|format|calculate|sum|count|find)\b")

#: A generated script is a shell command this agent wrote itself. It is written
#: to a file for the user to read before anything runs it.
def _try_script(request: str, can_run: bool) -> Attempt:
    if not can_run:
        return Attempt("script", False,
                       "skipped: writing and running my own code needs your "
                       "approval, and no one is here to give it",
                       skipped=True)
    if not _SCRIPTABLE.search(request):
        return Attempt("script", False,
                       "skipped: nothing in this reads as a script I could write",
                       skipped=True)
    draft = _write_script_draft(request)
    if draft is None:
        return Attempt("script", False, "could not write a draft script")
    return Attempt("script", True,
                   f"wrote a draft at {draft}. I have not run it — read it first, "
                   f"then say: run the script at {draft}")


def _write_script_draft(request: str) -> Path | None:
    """Write a PowerShell draft that comments what it would do, and stop.

    It is deliberately inert: the body is a comment, because a generated script
    that runs on creation is how a helper becomes an accident.
    """
    from ..config import active

    root = Path(active().storage.home) / "generated"
    root.mkdir(parents=True, exist_ok=True)
    import time

    path = root / f"script-{time.strftime('%Y%m%d-%H%M%S')}.ps1"
    body = "\n".join([
        "# Generated by ufo. Review before running.",
        f"# Request: {request}",
        "#",
        "# The body is empty on purpose: ufo wrote a starting point, not a",
        "# finished job. Fill it in, or tell ufo what to change and it will",
        "# rewrite this file.",
        "",
        "Write-Output 'todo: implement the above'",
    ])
    try:
        path.write_text(body, encoding="utf-8")
    except OSError as e:
        log.debug("could not write the script draft: %s", e)
        return None
    return path


# --------------------------------------------------------------------------
# rung 2: UI Automation
# --------------------------------------------------------------------------
_UIA_WORDS = re.compile(
    r"(?i)\b(click|button|menu|toolbar|dialog|window|tab|field|textbox|"
    r"combobox|checkbox|toolbar|press|select|open|close|scroll|hover|"
    r"type|enter|submit)\b")


def _try_uia(request: str, can_run: bool) -> Attempt:
    from ..perception import uia

    try:
        windows = uia.list_all_windows()
    except Exception as e:
        return Attempt("uia", False, f"no UI Automation available: {e}")
    if not windows:
        return Attempt("uia", False, "no windows are open to look in")
    if not _UIA_WORDS.search(request):
        return Attempt("uia", False,
                       "skipped: this does not name a control I could look for",
                       skipped=True)
    if not can_run:
        return Attempt("uia", False,
                       "found " + ", ".join(w.title for w in windows[:4])
                       + " — but acting on a window needs your approval")

    from ..config import active
    from ..actions import window as windowmod

    allowed = [w for w in windows if uia.is_allowed(w, active().safety.allowlist)]
    if not allowed:
        return Attempt("uia", False,
                       "the open windows are not in the allowlist, so I will "
                       "not touch them")
    return Attempt("uia", False,
                   "windows available: " + ", ".join(w.title for w in allowed[:5])
                   + ". Say which one and what to do in it")


# --------------------------------------------------------------------------
# rung 3: vision
# --------------------------------------------------------------------------
def _try_vision(request: str, can_run: bool) -> Attempt:
    try:
        from ..perception import vision
    except Exception as e:
        return Attempt("vision", False, f"vision is not available: {e}")
    try:
        obs = vision.observe_screen()
    except Exception as e:
        return Attempt("vision", False, f"could not look at the screen: {e}")
    if not obs.controls:
        return Attempt("vision", False,
                       "the screen has no controls I can recognise")
    if not can_run:
        return Attempt("vision", False,
                       f"skipped: I can see {len(obs.controls)} control(s) on "
                       f"screen, but acting on what I see needs your approval",
                       skipped=True)
    return Attempt("vision", False,
                   "I can see " + ", ".join(_label(c) for c in obs.controls[:6])
                   + ". Say which one, and what to do with it.")


# --------------------------------------------------------------------------
# rung 4: one clarifying question
# --------------------------------------------------------------------------
def _clarifying_question(request: str, near: list,
                         missing: str = "") -> str:
    """Exactly one question, and a real one.

    When part of a compound routed, the open question is about the part that did
    not — suggesting the capability that already succeeded would be answering a
    question the user did not ask.
    """
    if missing:
        return (f"I do not know how to {missing!r} yet. What should I do there?")
    if near:
        names = ", ".join(cap.name for cap, _p, _s in near[:3])
        return (f"I do not know how to do {request!r} yet. Did you mean one of "
                f"these: {names}?")
    return (f"I do not know how to do {request!r} yet, and nothing close came up. "
            f"What should I do, in one sentence?")


# --------------------------------------------------------------------------
# rung 5: do whatever part is possible
# --------------------------------------------------------------------------
def _try_partial(request: str) -> Attempt | None:
    """Run the parts of a compound request that do route.

    "check my battery and then compress the heliosphere" runs the first and says
    plainly that the second is missing. That is better than refusing both, and
    better than pretending both worked.

    The plan is assembled from the parts that routed rather than by re-reading
    the joined sentence: with only one part surviving, the joined string is no
    longer a compound and `interpret_compound` correctly declines it.
    """
    from . import nlu

    parts = nlu.split_compound(request)
    if len(parts) < 2:
        return None
    steps, missing = [], []
    for part in parts:
        plan = nlu.interpret(part)
        if plan is not None:
            steps.extend(plan.steps)
        else:
            missing.append(part)
    if not steps or not missing:
        # Nothing missing means the compound was fully understood; nothing to be
        # partial about. Returning None lets the ordinary compound path own it.
        return None
    kept = nlu.Plan(text=request, steps=steps, confidence=1.0,
                    matched=["partial"])
    return Attempt("partial", True,
                   f"{len(steps)} step(s) understood; missing: "
                   f"{'; '.join(missing)}",
                   plan=kept,
                   missing="; ".join(missing))
