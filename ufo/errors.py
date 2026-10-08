"""Exception taxonomy for the whole-laptop agent.

Every failure the system can hit maps to one of these, so the loops, the
safety guard and the CLI can react by *class* instead of by string matching.
"""
from __future__ import annotations


class UFOError(Exception):
    """Base class for every error raised by this package."""

    retryable = False
    fatal = False


# ---- configuration / input ----
class ConfigError(UFOError):
    fatal = True


class ValidationError(UFOError):
    """A model decision or user request failed schema/semantic validation."""


# ---- perception ----
class PerceptionError(UFOError):
    retryable = True


class WindowNotFoundError(PerceptionError):
    pass


class WindowClosedError(PerceptionError):
    pass


class ScreenCaptureError(PerceptionError):
    pass


class OCRUnavailableError(PerceptionError):
    """No OCR engine is installed; caller should fall back to vision only."""


# ---- action ----
class ActionError(UFOError):
    retryable = True


class ControlNotFoundError(ActionError):
    pass


class ControlNotActionableError(ActionError):
    """Control exists but is disabled/offscreen/in the wrong state."""


class ClipboardError(ActionError):
    pass


class ShellCommandError(ActionError):
    def __init__(self, message: str, *, returncode: int = -1, stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FilesystemError(ActionError):
    pass


class ProcessError(ActionError):
    pass


# ---- safety ----
class PolicyViolation(UFOError):
    """An action was blocked by the safety policy. Never retryable."""

    fatal = False


class ConfirmationRequired(UFOError):
    """Raised when an action needs human approval that was not granted."""


class EmergencyAbort(UFOError):
    """The global kill switch was engaged. Unrecoverable by design."""

    fatal = True


class BudgetExceeded(UFOError):
    """Step, time, token or cost budget for this run has been exhausted."""

    fatal = True


# ---- llm ----
class LLMError(UFOError):
    retryable = True


class QuotaExceeded(LLMError):
    fatal = True


class LLMTimeout(LLMError):
    retryable = True


class LLMResponseError(LLMError):
    """Model replied, but the reply was unusable."""


class ModelNotAvailable(LLMError):
    """The model named in the config is not on the server.

    Fatal, and not merely because retrying is pointless. The usual cause is a
    model that was never pulled: `ollama pull gemma3:4b`. Waiting and trying
    again cannot install it, so the loop used to burn five steps and about forty
    seconds before admitting the task was never going to run — with a warning
    line that named neither the model nor the fix. It names both now.
    """

    fatal = True


class NoLLMConfigured(LLMError):
    fatal = True


# ---- runtime ----
class SchedulerError(UFOError):
    pass


class TaskNotFound(UFOError):
    pass


__all__ = [name for name in dir() if name.endswith(("Error", "Exceeded", "Abort", "Violation", "Required", "NotFound", "Closed", "Unreachable")) or name in {"UFOError"}]
