"""Post-task verification — "did it actually work?"

An agent that clicks Save and declares victory is worse than useless: the user
finds out ten minutes later. `Verifier` asks a model to look at the *result*
(the screen after the last action, the app's own status text) and answer one
question with evidence, and it also offers cheap deterministic checks for the
common cases where the check does not need a model at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import Config
from ..logging_setup import get_logger, record
from ..utils import text as textutil
from . import llm

log = get_logger(__name__)

VERIFIER_PROMPT = """You check whether a task was actually completed.

You get: the task, the final state of the screen (controls, screenshot, text),
and a log of what was done.

Reply with EXACTLY one JSON object:
{
  "verdict": "PASS" | "FAIL" | "UNCERTAIN",
  "confidence": 0.0,
  "evidence": "the specific thing on screen or in the text that proves it",
  "what_is_missing": "" 
}

Rules:
- PASS only with concrete evidence in the current state. Never assume an
  action worked because it was issued.
- If the screen shows the result (a saved file, a sent message, a changed
  setting, a closed window), PASS and quote what you see.
- If nothing shows the result, FAIL and say exactly what is missing.
- Use UNCERTAIN when the evidence is genuinely ambiguous — for example the
  app gives no feedback either way. Do not guess PASS.
- An empty "what_is_missing" is fine on PASS.
"""


@dataclass
class VerificationResult:
    verdict: str = "UNCERTAIN"     # PASS | FAIL | UNCERTAIN
    confidence: float = 0.0
    evidence: str = ""
    missing: str = ""
    check: str = "model"            # model | deterministic | skipped

    @property
    def passed(self) -> bool:
        return self.verdict == "PASS"

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "confidence": self.confidence,
                "evidence": self.evidence, "what_is_missing": self.missing,
                "check": self.check}


class Verifier:
    def __init__(self, client: Any = None, cfg: Config | None = None):
        self.cfg = cfg or Config()
        self.client = client
        self.log = log

    def _client(self):
        if self.client is None:
            from ..config import LLMConfig

            self.client = llm.Client(LLMConfig(
                provider=self.cfg.llm.provider, model=self.cfg.llm.model,
                max_tokens=min(1200, self.cfg.llm.max_tokens),
                timeout=self.cfg.llm.timeout, base_url=self.cfg.llm.base_url,
                fallbacks=self.cfg.llm.fallbacks,
                offline=self.cfg.llm.offline,
            ))
        return self.client

    # -- deterministic checks, no model needed --
    def check_file_exists(self, path: str) -> VerificationResult:
        p = Path(str(path)).expanduser()
        if p.exists():
            size = p.stat().st_size if p.is_file() else 0
            return VerificationResult("PASS", 1.0, f"{p} exists"
                                      + (f" ({size} bytes)" if p.is_file() else ""),
                                      check="deterministic")
        return VerificationResult("FAIL", 1.0, "", f"{p} does not exist",
                                  check="deterministic")

    def check_file_contains(self, path: str, needle: str) -> VerificationResult:
        from ..actions import filesystem

        try:
            body = filesystem.read_text(path, max_bytes=4_000_000)
        except Exception as e:
            return VerificationResult("FAIL", 1.0, "", f"could not read {path}: {e}",
                                      check="deterministic")
        if textutil.similarity(needle, body[:20_000]) > 0.0 and needle in body:
            return VerificationResult("PASS", 1.0, f"{path} contains {needle!r}",
                                      check="deterministic")
        return VerificationResult("FAIL", 1.0, "",
                                  f"{path} does not contain {needle!r}",
                                  check="deterministic")

    def check_window_closed(self, title_fragment: str) -> VerificationResult:
        from ..perception import uia

        still_open = [w.title for w in uia.list_all_windows()
                      if title_fragment.lower() in w.title.lower()]
        if still_open:
            return VerificationResult("FAIL", 0.9, "",
                                      f"still open: {', '.join(still_open)}",
                                      check="deterministic")
        return VerificationResult("PASS", 1.0, f"no window matching "
                                  f"{title_fragment!r} is open", check="deterministic")

    def check_window_open(self, title_fragment: str) -> VerificationResult:
        from ..perception import uia

        for w in uia.list_all_windows():
            if title_fragment.lower() in w.title.lower():
                return VerificationResult("PASS", 1.0, f"{w.title!r} is open",
                                          check="deterministic")
        return VerificationResult("FAIL", 0.9, "",
                                  f"no window matching {title_fragment!r} is open",
                                  check="deterministic")

    def check_contains_text(self, haystack: str, needle: str) -> VerificationResult:
        if needle and needle.lower() in (haystack or "").lower():
            return VerificationResult("PASS", 1.0, f"found {needle!r}",
                                      check="deterministic")
        return VerificationResult("FAIL", 1.0, "", f"{needle!r} is not on screen",
                                  check="deterministic")

    # -- model check --
    def verify(self, task: str, state: str, actions: list[str],
               image_b64: str | None = None) -> VerificationResult:
        if not self.cfg.automation.verifier_enabled:
            return VerificationResult("UNCERTAIN", 0.0, "verification disabled",
                                      check="skipped")
        user_text = (
            f"TASK: {task}\n\n"
            f"FINAL STATE OF THE SCREEN:\n{textutil.truncate(state, 6000)}\n\n"
            f"WHAT WAS DONE:\n{textutil.truncate(chr(10).join(actions[-20:]) or '(nothing)', 3000)}\n\n"
            "Is the task complete? Judge only from the final state, not from "
            "the list of actions."
        )
        try:
            response = self._client().complete(VERIFIER_PROMPT, user_text, image_b64)
            raw = llm.parse_json(response.text)
            verdict = str(raw.get("verdict", "UNCERTAIN")).strip().upper()
            if verdict not in ("PASS", "FAIL", "UNCERTAIN"):
                verdict = "UNCERTAIN"
            try:
                confidence = float(raw.get("confidence", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0
            result = VerificationResult(
                verdict=verdict,
                confidence=max(0.0, min(1.0, confidence)),
                evidence=textutil.truncate(str(raw.get("evidence", "")), 600),
                missing=textutil.truncate(str(raw.get("what_is_missing", "")), 600),
            )
        except Exception as e:
            self.log.warning("verification failed (%s); treating as unverified.", e)
            result = VerificationResult("UNCERTAIN", 0.0, f"verification error: {e}",
                                        check="skipped")
        record("verify", verdict=result.verdict, confidence=result.confidence)
        return result

    def describe(self, result: VerificationResult) -> str:
        if result.passed:
            return f"Verified: {result.evidence}"
        if result.verdict == "FAIL":
            return f"Not done: {result.missing}"
        return f"Could not confirm: {result.evidence or result.missing}"


def run_deterministic(task: str, history: list[str]) -> VerificationResult | None:
    """Infer a check from the task and the action history, without a model.

    Handles the cases where the answer is knowable exactly: the task names a
    file, or an action closed/created a named window.
    """
    import re

    from ..actions import filesystem

    for entry in history:
        m = re.search(r"wrote ([^(]+) \(\d+ chars\)", entry) or \
            re.search(r"downloaded \d+ bytes to (\S+)", entry) or \
            re.search(r"created directory ([^ ]+)", entry)
        if m:
            path = m.group(1).strip()
            if filesystem.exists(path):
                return VerificationResult("PASS", 1.0, f"{path} was created and exists",
                                          check="deterministic")
    if re.search(r"\b(create|make|write|save|download)\b.*\.(txt|md|py|csv|json|log)\b", task,
                 re.IGNORECASE):
        names = re.findall(r"[\w\-.]+\.(?:txt|md|py|csv|json|log)\b", task)
        for name in names:
            if filesystem.exists(name):
                return VerificationResult("PASS", 1.0, f"{name} exists",
                                          check="deterministic")
    return None
