"""Category 46: corrections that change what the agent does next time.

`feedback` used to write a note into memory and stop. That is a diary, not a
correction: the user says "don't rename anything in Downloads", the note is
filed, and the next "tidy up my downloads" renames things exactly as before. The
user has been told their instruction was recorded.

So a correction becomes a **rule**, stored separately from memory and consulted
before every step:

* `never` — a veto. Checked *before* the guard, so it stops the action even if
  the guard would have allowed it. A standing instruction from the user outranks
  a risk score.
* `always` — a suggestion that this capability is proposed first. It never
  bypasses the guard and never runs anything by itself.
* `prefer` — a weak `always`, for a tie-break.

Two deliberate limits:

* `never` is matched on whole words against the action name and the user's own
  target text, never on a loose substring. A rule about "delete" must not fire
  on "deleted from my clipboard".
* A rule cannot widen what the guard allows. `always` only reorders; if the
  guard would say no, it still says no.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger(__name__)

KINDS = ("never", "always", "prefer")

#: Words that mean "not this one", when the user is correcting an action name.
_NEGATIONS = ("never", "don't", "dont", "do not", "stop", "no more", "not",
              "always", "please")

#: Filler in a rule subject. "never rename anything in Downloads" is a rule
#: about renaming in Downloads; demanding that the words "anything" and "in"
#: also appear is how a natural sentence ends up matching nothing at all.
_RULE_FILLER = frozenset({
    "anything", "any", "something", "ever", "the", "a", "an", "of", "in", "on",
    "at", "to", "for", "with", "and", "or", "my", "me", "i", "it", "them",
    "files", "file", "stuff", "things", "thing", "again", "too", "also", "just",
    "please", "you", "u", "are", "is", "be", "do", "does", "from", "that",
    "this", "those", "these", "there", "here", "all", "anymore", "more",
})

#: Tokens are alphanumeric only, so "file_delete" and a rule about "delete"
#: can match each other. Splitting on the underscore is what makes a rule written
#: in plain words apply to the capability that implements it.
_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass
class Rule:
    """One standing instruction from the user."""

    kind: str                       # never | always | prefer
    subject: str                    # what it is about, as the user typed it
    note: str = ""                  # why, in the user's words
    created: str = ""

    def tokens(self) -> set[str]:
        """The words this rule is really about, filler and negations removed.

        An empty result means the rule says nothing specific — "never do it" —
        and is refused rather than left to veto whatever comes next.
        """
        found = set(_TOKEN.findall(self.subject.lower()))
        return found - _RULE_FILLER - set(_NEGATIONS)

    def describe(self) -> str:
        body = f"{self.kind} {self.subject!r}"
        return f"{body}  ({self.note})" if self.note else body


@dataclass
class Rules:
    """The rule store. One per state directory."""

    path: Path
    _rules: list[Rule] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self):
        self._load()

    # -- reading --
    def all(self) -> list[Rule]:
        with self._lock:
            return list(self._rules)

    def as_dicts(self) -> list[dict]:
        return [asdict(r) for r in self.all()]

    def veto_for(self, capability: str, target: str = "", args: str = "") -> str:
        """Why this step is vetoed, or "" when no rule objects.

        Matched on whole words, and a rule must name at least one distinctive
        word: a rule about "delete" must not fire on "deleted from my clipboard",
        and a rule with no distinctive words at all would veto everything.
        """
        haystack = " ".join(x for x in (capability, target, args) if x).lower()
        words = set(_TOKEN.findall(haystack))
        if not words:
            return ""
        for rule in self.all():
            if rule.kind != "never":
                continue
            tokens = rule.tokens()
            if not tokens:
                continue
            if tokens <= words:
                return rule.describe()
        return ""

    def rank_for(self, capability: str) -> int:
        """How strongly a rule argues for this capability. Higher is earlier."""
        words = set(_TOKEN.findall(capability.lower()))
        best = 0
        for rule in self.all():
            if rule.kind == "never":
                continue
            tokens = rule.tokens()
            if not tokens or not tokens <= words:
                continue
            best = max(best, 2 if rule.kind == "always" else 1)
        return best

    def vetoed_capabilities(self) -> set[str]:
        """Capabilities a `never` rule names outright, for the guard to consult."""
        out = set()
        for rule in self.all():
            if rule.kind == "never":
                out.add(rule.subject.strip().lower())
        return out

    # -- writing --
    def add(self, kind: str, subject: str, note: str = "") -> Rule:
        kind = str(kind or "").strip().lower()
        if kind not in KINDS:
            raise ValueError(f"rule kind must be one of {KINDS}, not {kind!r}")
        subject = str(subject or "").strip()
        if not subject:
            raise ValueError("a rule needs something to be about")
        rule = Rule(kind=kind, subject=subject, note=str(note or "").strip())
        if not rule.tokens():
            # "never do it" is not a rule. Storing it would veto every action
            # that mentions an "it".
            raise ValueError(
                f"{subject!r} does not name anything specific. Say what the "
                f"rule is about, e.g. never rename files in Downloads")
        with self._lock:
            for existing in self._rules:
                if (existing.kind, existing.subject.lower()) == (kind, subject.lower()):
                    existing.note = note or existing.note
                    self._save()
                    return existing
            self._rules.append(rule)
            self._save()
        return rule

    def parse(self, text: str, note: str = "") -> Rule:
        """Read a correction in the user's words and store the right kind of rule.

        "don't rename files" is a veto. "always run the backup first" is a
        preference. A sentence with neither is refused rather than filed as a
        rule that will never match anything.
        """
        raw = " ".join(str(text or "").split())
        lowered = raw.lower()
        if any(lowered.startswith(w) for w in ("never", "don't", "dont",
                                               "do not", "no ", "stop ")):
            subject = _strip_negation(raw)
            return self.add("never", subject, note)
        if lowered.startswith("always") or lowered.startswith("prefer"):
            subject = re.sub(r"(?i)^\s*(always|prefer)\s+", "", raw)
            kind = "always" if lowered.startswith("always") else "prefer"
            return self.add(kind, subject, note)
        raise ValueError(
            "say it as a rule: start with 'never', 'don't' or 'always'. "
            "For example: never rename anything in Downloads")

    def remove(self, subject: str) -> bool:
        needle = str(subject or "").strip().lower()
        with self._lock:
            before = len(self._rules)
            self._rules = [r for r in self._rules
                           if r.subject.lower() != needle]
            removed = len(self._rules) < before
            if removed:
                self._save()
        return removed

    def clear(self) -> int:
        with self._lock:
            count = len(self._rules)
            self._rules = []
            self._save()
        return count

    # -- persistence --
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            if not isinstance(e, FileNotFoundError):
                log.debug("could not read the rules: %s", e)
            return
        rules = data.get("rules", []) if isinstance(data, dict) else []
        self._rules = [Rule(kind=r.get("kind", ""), subject=r.get("subject", ""),
                            note=r.get("note", ""), created=r.get("created", ""))
                       for r in rules if isinstance(r, dict)]

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"rules": self.as_dicts()}, indent=2), encoding="utf-8")
        except OSError as e:
            # Losing a rule is survivable; refusing to add one is not. Keep it in
            # memory and say nothing, so the session still obeys it.
            log.debug("could not write the rules: %s", e)


def _strip_negation(text: str) -> str:
    """The action a "don't X" is about, without the negation or the pronoun."""
    out = re.sub(r"(?i)^\s*(please\s+)?(never|don'?t|do not|no more|stop|not)\s+",
                 "", text)
    out = re.sub(r"(?i)\b(again|any ?more|please|for me)\b", "", out)
    return " ".join(out.split()).strip(" .!") or text.strip()


_rules: Rules | None = None
_lock = threading.RLock()


def get_rules() -> Rules:
    """The process-wide rule store."""
    global _rules
    with _lock:
        if _rules is None:
            from ..config import active

            _rules = Rules(Path(active().storage.home) / "rules.json")
        return _rules


def reset_for_tests(path: Path) -> Rules:
    global _rules
    with _lock:
        _rules = Rules(path)
    return _rules


__all__ = ["Rule", "Rules", "KINDS", "get_rules", "reset_for_tests"]
