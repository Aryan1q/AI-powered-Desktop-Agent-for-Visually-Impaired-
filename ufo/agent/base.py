"""The shared agent loop: validate -> gate -> act -> record -> verify.

`AppAgent` (one application) and `ComputerAgent` (the whole laptop) differ only
in how they observe and how they resolve a target into a concrete action.
Everything else — decision validation, safety gating, budgets, loop breaking,
logging, narration, and the FINISH/FAIL state machine — lives here so both
behave identically and there is one place to audit.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from ..config import AutomationConfig, Config
from ..errors import BudgetExceeded, EmergencyAbort, LLMError, QuotaExceeded, ValidationError
from ..logging_setup import get_logger, record
from ..safety.guard import ActionGuard, Risk
from ..safety.policy import RiskClassifier
from ..utils import text as textutil
from . import llm

log = get_logger(__name__)

# Action names both agents support. Subclasses extend via `extra_actions`.
BASE_ACTIONS = (
    "click", "double_click", "right_click", "type", "set_text", "hotkey",
    "wait", "screenshot", "read_text", "finish", "fail",
)
COORD_ACTIONS = ("click_at", "double_click_at", "right_click_at", "move", "scroll_at")
SYSTEM_ACTIONS = ("click", "double_click", "right_click", "hotkey", "type", "set_text",
                  "wait", "scroll_at", "click_at", "double_click_at", "right_click_at",
                  "move", "drag", "key", "screenshot", "read_text", "list_windows",
                  "focus_window", "run_command", "read_file", "write_file", "list_dir",
                  "search_files", "app", "power", "audio", "volume", "browser",
                  "finish", "fail")

VALID_STATUSES = ("CONTINUE", "FINISH", "FAIL")
MAX_WAIT_SECONDS = 30.0
MAX_ARG_CHARS = 4000

# Actions that end a run, and the keys a model might use to hand back the
# outcome. Different models pick different names, so accept the common ones
# rather than dropping the result on the floor.
TERMINAL_ACTIONS = ("finish", "fail")
TERMINAL_PAYLOAD_KEYS = (
    "answer", "result", "summary", "outcome", "reason", "message",
    "response", "output", "text", "error",
)

# Small local models keep inventing a name for "hand the answer back" instead
# of using `finish`, and a rejected action costs a whole step. These map onto an
# action that already exists, so the request still goes through the normal
# policy, risk and confirmation gate. Nothing here widens what may be done: it
# only renames a request the model was always allowed to make.
ACTION_ALIASES = {
    "return_value": "finish",
    "return_result": "finish",
    "return_text": "finish",
    "return": "finish",
    "respond": "finish",
    "reply": "finish",
    "answer": "finish",
    "done": "finish",
    "complete": "finish",
    "completed": "finish",
    "succeed": "finish",
    "success": "finish",
    "abort": "fail",
    "give_up": "fail",
    "error": "fail",
}


class Outcome(str, Enum):
    FINISH = "FINISH"
    ANSWERED = "ANSWERED"
    FAIL = "FAIL"
    MAX_STEPS = "MAX_STEPS"
    USER_ABORTED = "USER_ABORTED"
    EMERGENCY_ABORT = "EMERGENCY_ABORT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    LLM_ERROR = "LLM_ERROR"
    OBSERVATION_ERROR = "OBSERVATION_ERROR"
    ACTION_ERROR = "ACTION_ERROR"
    LOOP_DETECTED = "LOOP_DETECTED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    POLICY_BLOCKED = "POLICY_BLOCKED"


@dataclass
class Decision:
    """A validated, normalized model decision."""

    thought: str = ""
    action: str = "wait"
    target: str = ""
    args: str = ""
    status: str = "CONTINUE"
    raw: dict = field(default_factory=dict)

    def signature(self) -> str:
        return textutil.action_signature(self.action, self.target, self.args)

    def to_prompt(self) -> str:
        return (f"step {self.status}: {self.action} on {self.target!r} "
                f"args={textutil.truncate(self.args, 60)!r}")

    def to_dict(self) -> dict:
        return {"thought": self.thought, "action": self.action, "target": self.target,
                "args": self.args, "status": self.status}


def validate_decision(raw: Any, valid_actions: tuple[str, ...] | set[str],
                      *, max_args: int = MAX_ARG_CHARS) -> Decision:
    """Turn raw model output into a Decision, or raise ValidationError.

    Every rejection is explicit and specific, because the message is fed back
    to the model on the next turn — vague errors cause repeated failures.
    """
    if not isinstance(raw, dict):
        raise ValidationError(
            "Your reply must be a JSON object with keys: thought, action, target, "
            "args, status."
        )

    action = str(raw.get("action", "")).strip().lower()
    if not action:
        raise ValidationError("Missing 'action'. Valid actions: "
                              f"{', '.join(sorted(valid_actions))}.")
    if action not in valid_actions:
        alias = ACTION_ALIASES.get(action)
        if alias in valid_actions:
            action = alias
        else:
            close = _closest(action, valid_actions)
            raise ValidationError(
                f"Unknown action {action!r}. Valid actions: "
                f"{', '.join(sorted(valid_actions))}."
                + (f" Did you mean {close!r}?" if close else "")
            )

    status = str(raw.get("status", "CONTINUE")).strip().upper()
    if status not in VALID_STATUSES:
        raise ValidationError(
            f"status must be one of {', '.join(VALID_STATUSES)}; got {status!r}."
        )
    if action in TERMINAL_ACTIONS and status == "CONTINUE":
        # Asking to finish is finishing. Small models often write the
        # terminal action but leave status at its default, which would
        # otherwise run one more pointless step and discard the answer.
        status = "FINISH" if action == "finish" else "FAIL"

    target = raw.get("target", raw.get("control_id", ""))
    if isinstance(target, bool):
        target = ""
    elif isinstance(target, (int, float)):
        target = str(int(target))
    elif target is None:
        target = ""
    elif not isinstance(target, str):
        target = str(target)
    target = textutil.truncate(target.strip(), 200)

    args = raw.get("args", "")
    if args is None:
        args = ""
    if isinstance(args, (dict, list)):
        args = json.dumps(args, ensure_ascii=False)
    elif not isinstance(args, (str, int, float)):
        args = str(args)
    args = str(args)
    if len(args) > max_args:
        raise ValidationError(
            f"'args' is {len(args)} characters; the limit is {max_args}. "
            "Split long text into several steps."
        )

    thought = textutil.truncate(str(raw.get("thought", "")).strip(), 600)

    if not args and action in TERMINAL_ACTIONS:
        # `finish`/`fail` report the outcome under a dozen different names
        # depending on the model. Without this the task always ends with an
        # empty result, however good the model's answer actually was.
        for key in TERMINAL_PAYLOAD_KEYS:
            payload = raw.get(key)
            if isinstance(payload, (str, int, float)) and str(payload).strip():
                args = str(payload).strip()
                break
            if isinstance(payload, (dict, list)):
                args = json.dumps(payload, ensure_ascii=False)
                break
        if not args and target:
            # A terminal action does nothing to `target`, so a model that put
            # the answer there meant it as the answer. Answering a question
            # with an empty result is strictly worse than reading it here.
            args = target

    if action in ("wait",) and status == "CONTINUE":
        try:
            seconds = float(args or "1")
        except ValueError:
            raise ValidationError(
                f"'wait' needs args to be a number of seconds, got {args!r}."
            ) from None
        if not 0 <= seconds <= MAX_WAIT_SECONDS:
            raise ValidationError(
                f"'wait' accepts 0 to {MAX_WAIT_SECONDS:.0f} seconds; got {seconds}."
            )
        args = str(seconds)

    if action in ("click_at", "double_click_at", "right_click_at", "move",
                  "scroll_at", "drag"):
        if not target and not args:
            raise ValidationError(
                f"{action!r} needs screen coordinates: put 'x,y' in 'target'."
            )
    if action in ("type", "set_text") and not args:
        raise ValidationError(
            f"{action!r} needs the text to enter in 'args'."
        )
    if action == "hotkey" and not args.strip():
        raise ValidationError("'hotkey' needs a key string in 'args', e.g. 'ctrl+s'.")

    return Decision(thought=thought, action=action, target=target, args=args,
                    status=status, raw=raw)


def _closest(word: str, options: Iterable[str]) -> str | None:
    import difflib

    return difflib.get_close_matches(word, list(options), n=1, cutoff=0.7)


# --------------------------------------------------------------------------
@dataclass
class AgentLoop:
    """Base observe -> reason -> gate -> act loop.

    Subclasses must implement `observe()`, `describe_context()`,
    `perform(decision)`, and may set `valid_actions`.
    """

    task: str
    cfg: Config = field(default_factory=Config)
    client: Any = None
    guard: ActionGuard | None = None
    narrator: Callable[[str], None] | None = None
    log_root: str | Path = "logs"
    max_steps: int | None = None
    system_prompt: str = ""
    valid_actions: tuple[str, ...] = BASE_ACTIONS

    # runtime state
    history: list[str] = field(default_factory=list)
    consecutive_errors: int = 0
    consecutive_reasoning_errors: int = 0
    consecutive_action_errors: int = 0
    actions_done: int = 0
    finish_rejections: int = 0
    initial_signature: Any = None
    last_result: str = ""
    outcome: str = Outcome.FAIL.value
    run_dir: Path | None = None
    started: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.auto = self.cfg.automation
        if self.max_steps is None:
            self.max_steps = self.auto.max_steps
        if self.guard is None:
            from ..safety.guard import ActionGuard as _Guard
            from ..safety.policy import ActionPolicy

            self.guard = _Guard(
                policy=ActionPolicy.from_config(self.cfg.safety),
                config=self.cfg.safety,
                confirm=self._confirm,
                narrate=self.narrate,
            )
        self.guard.reset_budget()
        if self.client is None:
            from ..config import LLMConfig

            self.client = llm.Client(LLMConfig(
                provider=self.cfg.llm.provider, model=self.cfg.llm.model,
                temperature=self.cfg.llm.temperature,
                max_tokens=self.cfg.llm.max_tokens,
                timeout=self.cfg.llm.timeout,
                max_retries=self.cfg.llm.max_retries,
                base_url=self.cfg.llm.base_url,
                fallbacks=self.cfg.llm.fallbacks,
                redact_screenshots=self.cfg.llm.redact_screenshots,
                offline=self.cfg.llm.offline,
            ))
        run_id = time.strftime("run_%Y%m%d_%H%M%S")
        self.run_dir = Path(self.cfg.storage.log_root) / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._apply_persona()

    def _apply_persona(self) -> None:
        """Let the persona colour the model's own prose, not its actions.

        Only the instruction text is appended. The action vocabulary, the JSON
        contract and every rule in the prompt are untouched, so a persona can
        change how the agent sounds but never what it is permitted to do.
        """
        try:
            from ..voice.persona import get_persona

            addition = get_persona(self.cfg).system_prompt_addition()
        except Exception:
            return
        if addition and addition.strip() not in (self.system_prompt or ""):
            self.system_prompt = (self.system_prompt or "").rstrip() + "\n" + addition

    # ---- hooks for subclasses ----
    def observe(self, step: int):  # pragma: no cover - interface
        raise NotImplementedError

    def describe_context(self) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def perform(self, decision: Decision, observation) -> str:  # pragma: no cover
        raise NotImplementedError

    def signature(self, observation) -> Any:
        return None

    def image_for_model(self, observation) -> str | None:
        if self.cfg.vision.redact_screenshots or not self.cfg.vision.annotate:
            return None
        return observation.b64

    # ---- narration / confirmation ----
    def narrate(self, text: str) -> None:
        speaker = self.narrator
        if speaker is None:
            print(f"[SAY] {text}")
            return
        say = getattr(speaker, "say", None)
        if callable(say):
            say(text)
        elif callable(speaker):
            speaker(text)
        else:
            print(f"[SAY] {text}")

    def _confirm(self, message: str, risk: Risk) -> bool:
        """Console confirmation. A blind user relies on the spoken prompt, and
        a kill phrase stops the run outright."""
        from ..safety.emergency import emergency
        from ..voice import beep_pattern

        self.narrate(message)
        beep_pattern("confirm")
        try:
            answer = input("[CONFIRM] 'yes' to proceed, 'no' to skip, 'stop' to abort: ")
        except (EOFError, KeyboardInterrupt):
            return False
        answer = textutil.collapse_ws(answer).lower()
        if answer in {p.lower() for p in self.cfg.safety.panic_phrases}:
            emergency.trip("user said a panic phrase at a confirmation prompt")
            return False
        return answer in {"y", "yes", "do it", "go ahead"}

    # ---- the loop ----
    def run(self) -> str:
        try:
            return self._run()
        except EmergencyAbort:
            self.outcome = Outcome.EMERGENCY_ABORT.value
            self.narrate("Stopping immediately: emergency stop was triggered.")
            self._write_summary()
            return self.outcome
        except BudgetExceeded as e:
            self.outcome = Outcome.BUDGET_EXCEEDED.value
            self.narrate(f"Stopping: {e}")
            self._write_summary()
            return self.outcome
        except KeyboardInterrupt:
            self.outcome = Outcome.USER_ABORTED.value
            self.narrate("Stopping: you interrupted the run.")
            self._write_summary()
            return self.outcome

    def _run(self) -> str:
        for step in range(1, int(self.max_steps or 30) + 1):
            self.guard.emergency_switch.check("the next step")

            # 1. observe
            try:
                observation = self.observe(step)
            except Exception as e:
                self.consecutive_errors += 1
                message = f"Observation failed: {type(e).__name__}: {e}"
                log.warning("[step %s] %s", step, message)
                self.history.append(f"step {step}: {message}")
                self._log(step, {"error": message})
                if self.consecutive_errors >= self.auto.max_consecutive_errors:
                    self.outcome = Outcome.OBSERVATION_ERROR.value
                    self.narrate("Stopping: I kept failing to look at the screen.")
                    return self.outcome
                time.sleep(self.cfg.safety.idle_backoff_seconds)
                continue
            self.consecutive_errors = 0

            signature = self.signature(observation)
            if self.initial_signature is None:
                self.initial_signature = signature

            if self.cfg.vision.screenshot_on_every_step and observation.image is not None:
                from ..utils import image as imageutil

                imageutil.save(observation.image, self.run_dir / f"step_{step:02d}.png")

            # 2. reason
            try:
                decision = self._decide(step, observation)
            except QuotaExceeded as e:
                self.outcome = "QUOTA_EXCEEDED"
                self.narrate("Stopping: the language model quota ran out. "
                             "Change the model or key, then try again.")
                self._log(step, {"error": str(e)})
                return self.outcome
            except LLMError as e:
                # A fatal one — a model that is not on the server, a quota that
                # is spent — cannot become true by waiting. Retrying it five
                # times just burned the whole step budget on an outcome that was
                # already decided, and the reason never reached the narrator.
                if getattr(e, "fatal", False):
                    message = f"{type(e).__name__}: {e}"
                    log.error("[step %s] %s", step, message)
                    self.history.append(f"step {step}: {message}")
                    self._log(step, {"error": message})
                    self.outcome = Outcome.LLM_ERROR.value
                    self.narrate(f"Stopping: {e}")
                    return self.outcome

                self.consecutive_reasoning_errors += 1
                message = f"Reasoning failed: {type(e).__name__}: {e}"
                log.warning("[step %s] %s", step, message)
                self.history.append(f"step {step}: {message}; retrying")
                self._log(step, {"error": message})
                if self.consecutive_reasoning_errors >= self.auto.max_consecutive_errors:
                    self.outcome = Outcome.LLM_ERROR.value
                    self.narrate("Stopping: I could not reach the language model.")
                    return self.outcome
                time.sleep(self.cfg.safety.idle_backoff_seconds)
                continue
            except ValidationError as e:
                self.consecutive_reasoning_errors += 1
                message = f"Invalid reply: {e}"
                log.warning("[step %s] %s", step, message)
                self.history.append(
                    f"step {step}: your last reply was rejected - {e} "
                    "Reply again with valid JSON for one of the allowed actions."
                )
                self._log(step, {"error": message})
                if self.consecutive_reasoning_errors >= self.auto.max_consecutive_errors:
                    self.outcome = Outcome.LLM_ERROR.value
                    self.narrate("Stopping: I could not produce a valid action.")
                    return self.outcome
                continue
            self.consecutive_reasoning_errors = 0

            # 3. terminal states
            if decision.status == "FAIL":
                self._log(step, {"decision": decision.to_dict(), "result": "FAIL"})
                self.narrate("I was not able to complete this task.")
                self.outcome = Outcome.FAIL.value
                self._write_summary()
                return self.outcome

            if decision.status == "FINISH":
                if self._reject_finish(step, decision, signature, observation):
                    time.sleep(self.auto.step_delay)
                    continue
                if self._is_pure_answer(decision):
                    answer = textutil.truncate(decision.args, 500)
                    self._log(step, {"decision": decision.to_dict(),
                                     "result": "ANSWERED", "answer": answer})
                    self.narrate(f"Answer: {answer}")
                    self.last_result = answer
                    self.outcome = Outcome.ANSWERED.value
                    self._write_summary()
                    return self.outcome
                self._log(step, {"decision": decision.to_dict(), "result": "FINISH"})
                self.narrate("Done. The task is complete.")
                self.outcome = Outcome.FINISH.value
                self._write_summary()
                return self.outcome

            # 4. act
            try:
                result = self._perform_guarded(step, decision, observation)
            except EmergencyAbort:
                raise
            except Exception as e:
                result = f"ERROR: {type(e).__name__}: {e}"

            self.last_result = str(result)
            if not self.last_result.startswith("ERROR"):
                self.actions_done += 1
                self.consecutive_action_errors = 0
            else:
                self.consecutive_action_errors += 1
                self.narrate(f"That did not work: {textutil.truncate(self.last_result, 160)}")
                if self.consecutive_action_errors >= self.auto.max_consecutive_errors:
                    self.outcome = Outcome.ACTION_ERROR.value
                    self.narrate("Stopping: too many actions failed in a row.")
                    self._write_summary()
                    return self.outcome

            self.history.append(
                f"step {step}: {decision.action} on "
                f"{textutil.truncate(decision.target, 60)!r} "
                f"args={textutil.truncate(decision.args, 60)!r} -> {self.last_result}"
            )
            self._log(step, {"decision": decision.to_dict(), "result": self.last_result})
            log.info("[step %s] %s -> %s", step, decision.action,
                     textutil.truncate(self.last_result, 120))

            if self.guard.looping and decision.action not in ("wait",):
                self.outcome = Outcome.LOOP_DETECTED.value
                self.narrate(
                    "Stopping: I kept trying the same thing without it working, "
                    "so I did not want to keep clicking."
                )
                self._write_summary()
                return self.outcome

            time.sleep(self.auto.step_delay)

        self.outcome = Outcome.MAX_STEPS.value
        self.narrate("Stopping: I reached the step limit before finishing.")
        self._write_summary()
        return self.outcome

    # ---- reasoning ----
    def _decide(self, step: int, observation) -> Decision:
        user_text = self._build_prompt(step, observation)
        image = self.image_for_model(observation)
        response = self.client.complete(
            self.system_prompt or DEFAULT_SYSTEM_PROMPT, user_text, image,
            charge=self.guard.active_budget.charge_llm,
        )
        try:
            raw = llm.parse_json(response.text, allow_list=True)
        except Exception as e:
            raise ValidationError(str(e)) from e
        if isinstance(raw, list) and raw:
            raw = raw[0]
        return validate_decision(raw, self.valid_actions)

    def _build_prompt(self, step: int, observation) -> str:
        recent = "\n".join(self.history[-12:]) or "(nothing yet)"
        return (
            f"TASK: {self.task}\n\n"
            f"CURRENT STATE:\n{self.describe_context()}\n\n"
            f"WHAT YOU HAVE DONE SO FAR:\n{recent}\n\n"
            f"This is step {step} of at most {self.max_steps}. "
            "Pick the single next action that makes progress. If the current "
            "state already shows the task is done, return status FINISH. "
            "If the task cannot be done, return status FAIL with a reason."
        )

    def _is_pure_answer(self, decision: Decision) -> bool:
        """True when the model is reporting an answer, not completing work.

        Nothing has been changed on the machine, so the "no visible change"
        rule below has nothing to say: a question about the screen is answered
        by reading the screen, and reading it changes nothing. Reported as a
        distinct ANSWERED outcome so a lookup is never mistaken for a task
        that was carried out.
        """
        return self.actions_done == 0 and bool((decision.args or "").strip())

    def _reject_finish(self, step: int, decision: Decision, signature, observation) -> bool:
        """Refuse an early FINISH: no actions, or a screen that never changed."""
        if self._is_pure_answer(decision):
            return False
        if self.actions_done > 0 and signature != self.initial_signature:
            return False
        if self.finish_rejections >= 3:
            return False
        self.finish_rejections += 1
        reason = (
            "the screen has not changed since the run started, so nothing has "
            "actually been done yet"
            if self.actions_done == 0 else
            "no visible change has happened yet"
        )
        message = (
            f"step {step}: FINISH rejected — {reason}. Perform the actual work "
            "before declaring the task complete."
        )
        log.info("[step %s] %s", step, message)
        self.history.append(message)
        self._log(step, {"decision": decision.to_dict(), "result": "REJECTED_EARLY_FINISH"})
        return True

    # ---- acting ----
    def _perform_guarded(self, step: int, decision: Decision, observation) -> str:
        kind = self.kind_for(decision.action)
        risk = RiskClassifier().classify(decision.action, decision.target,
                                         decision.args, kind=kind)
        # The subject has to be resolved from the decision, not from whatever
        # was set last step: a stale value would gate this action against the
        # wrong window.
        subject = self.subject(observation, decision)
        self.guard.preflight(decision.action, decision.target, decision.args,
                             kind=kind, subject=subject)
        return self.guard.guard(
            decision.action, decision.target, decision.args, kind=kind,
            subject=subject,
            execute=lambda: self.perform(decision, observation),
        ).result or ""

    def kind_for(self, action: str) -> str:
        if action in ("click", "double_click", "right_click", "click_at",
                      "double_click_at", "right_click_at", "move", "drag"):
            return "mouse" if action.endswith("_at") or action in ("move", "drag") else "ui"
        if action in ("hotkey", "key", "type", "set_text"):
            return "keyboard"
        if action == "scroll_at":
            return "mouse"
        if action == "run_command":
            return "shell"
        if action in ("read_file", "write_file", "list_dir", "search_files"):
            return "fs"
        if action in ("app", "power", "audio", "volume", "focus_window", "list_windows"):
            return "system" if action in ("app", "power", "audio", "volume") else "window"
        if action == "browser":
            return "network"
        return "ui"

    def subject(self, observation, decision=None) -> str:
        return ""

    # ---- logging ----
    def _log(self, step: int, data: dict) -> None:
        if self.run_dir is None:
            return
        payload = {k: v for k, v in data.items()}
        payload["step"] = step
        payload["task"] = self.task
        payload["at"] = round(time.time() - self.started, 2)
        if self.cfg.storage.redact_logs:
            from ..safety.secrets import scrub_data

            payload = scrub_data(payload)
        try:
            (self.run_dir / f"step_{step:02d}.json").write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except OSError:
            pass
        record("step", step=step, result=str(payload.get("result", ""))[:120])

    def _write_summary(self) -> None:
        if self.run_dir is None:
            return
        summary = {
            "task": self.task,
            "outcome": self.outcome,
            "actions": self.actions_done,
            "steps": len(self.history),
            "duration_seconds": round(time.time() - self.started, 2),
            "budget": self.guard.active_budget.to_dict() if self.guard else {},
            "history": self.history,
        }
        try:
            (self.run_dir / "summary.json").write_text(
                json.dumps(summary, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except OSError:
            pass


DEFAULT_SYSTEM_PROMPT = """You are an automation agent controlling a Windows computer.

Each turn you receive: the task, what is currently on screen (window titles,
numbered interactive controls, an annotated screenshot, and any text read
from the screen), and everything you have already done.

Reply with EXACTLY one JSON object:
{
  "thought": "why this is the next action",
  "action": "<one action name from the list below>",
  "target": "<control id, window title, or x,y coordinates>",
  "args": "<text to type, key string, command, seconds, or empty>",
  "status": "CONTINUE" | "FINISH" | "FAIL"
}

Rules:
- "target" must come from the CURRENT observation. Control ids are renumbered
  every step; they are never valid across steps.
- Only use actions from the list you are given. Never invent one.
- Clicking a control is not the same as doing the task. To enter text you must
  use "type" or "set_text" with the text in "args".
- Never claim an action worked unless the next observation shows it did.
- Prefer the slowest, most reliable method when unsure: click the control,
  look at the screen, then act. Do not batch steps you cannot verify.
- Set status FINISH only when the current state clearly shows the task is
  complete. Set status FAIL when it cannot be done; explain in "thought".
- Do not retype the same action repeatedly. If something did not work twice,
  try a different approach.
"""
