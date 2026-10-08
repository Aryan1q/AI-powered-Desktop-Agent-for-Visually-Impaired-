"""Persona: how the agent sounds, not what it does.

The intelligence is in `ufo.agent`; this module only decides the wording. It is
deliberately separate so you can turn it off without changing behaviour, and so
a persona can never accidentally widen what the agent is allowed to do.

    persona = Persona("jarvis", address="sir")
    persona.acknowledge()          # "Right away, sir."
    persona.blocked("that window is not on the allowlist")
    persona.repeat("open notepad") # "Very well, sir. Open notepad."

The rewrite rules are conservative on purpose. A persona that paraphrases
*commands* is a persona that can change what the agent does; here only
acknowledgements, status lines and refusals are styled, and the actual
instruction is always passed through untouched.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Iterable

from ..logging_setup import get_logger

log = get_logger(__name__)

PERSONAS = ("none", "jarvis", "butler", "terse")


@dataclass
class Persona:
    """A named voice for narration. `none` is a pass-through."""

    name: str = "none"
    address: str = ""
    cadence: str = "dry"
    chime: bool = True

    # Filled in by __post_init__.
    _openers: tuple[str, ...] = field(default=(), init=False, repr=False)
    _closers: tuple[str, ...] = field(default=(), init=False, repr=False)
    _hedges: tuple[str, ...] = field(default=(), init=False, repr=False)

    def __post_init__(self) -> None:
        if self.name not in PERSONAS:
            log.warning("Unknown persona %r; falling back to 'none'.", self.name)
            self.name = "none"
        self._openers, self._closers, self._hedges = _PHRASES.get(
            self.name, ((), (), ()))

    # ---- helpers ----
    @property
    def enabled(self) -> bool:
        return self.name != "none"

    def _tag(self) -> str:
        """The form of address, or nothing. A silent persona never addresses."""
        if not self.enabled or not self.address:
            return ""
        return f", {self.address}"

    def _pick(self, options: Iterable[str]) -> str:
        options = tuple(options)
        return random.choice(options) if options else ""

    def style(self, text: str) -> str:
        """Style a line that already has its content.

        Tidy up, never reword: contractions in, filler out, and nothing else.
        Multi-sentence text is left intact.
        """
        if not self.enabled or not text:
            return text
        return _tidy(text)

    # ---- lines ----
    def wake(self) -> str:
        return self.style(f"{self._pick(self._openers)}{self._tag()}.")

    def acknowledge(self, what: str = "") -> str:
        if not what:
            return self.wake()
        return self.style(f"{self._pick(self._openers)}, {what}.")

    def status(self, what: str) -> str:
        if not what:
            return ""
        return self.style(f"{what[0].upper() + what[1:]}{self._closer()}{self._tag()}.")

    def confirm(self, question: str) -> str:
        question = question.rstrip("?.! ")
        return self.style(f"{self._hedge()}{question}{self._tag()}?")

    def blocked(self, reason: str) -> str:
        lead = "I'm afraid I can't" if self.cadence == "dry" else "I'm sorry"
        head = f"{lead}{self._tag()}." if self.address else f"{lead}."
        return self.style(f"{head} {reason.rstrip('. ')}.")

    def emergency(self) -> str:
        return self.style(f"Standing down immediately{self._tag()}.")

    def done(self, summary: str) -> str:
        return self.style(f"{summary.rstrip('. ')}{self._closer()}{self._tag()}.")

    def repeat(self, instruction: str) -> str:
        """Read an instruction back before acting on it.

        The instruction is passed through byte for byte. Only the framing
        around it is styled: this is the line the user decides on, so rewriting
        the words inside it would change what they are approving. Collapsing
        whitespace is safe (the recogniser inserts it freely); tidying is not,
        because it would eat the space in "every .txt".
        """
        text = " ".join(instruction.split())
        if not text:
            return ""
        # Case is not meaningful to a recogniser, and capitalising the first
        # letter only makes the line read aloud properly. Nothing else is
        # touched: no punctuation, no spacing, no words.
        if text[0].islower():
            text = text[0].upper() + text[1:]
        if not self.enabled:
            return f"You said: {text}."
        head = self.style(
            f"{self._pick(('Understood', 'Very well', 'Certainly', 'Right away', 'So noted'))}"
            f"{self._tag()}.")
        return f"{head} {text}"

    def clarify(self, question: str) -> str:
        return self.style(f"{self._hedge()}{question.rstrip('?.! ')}{self._tag()}?")

    # ---- phrase slots ----

    def _closer(self) -> str:
        """A trailing flourish, already space-prefixed, or ''."""
        return self._pick(self._closers) if self.enabled else ""

    def _hedge(self) -> str:
        """A leading softening, already space-separated, or ''."""
        return self._pick(self._hedges) if self.enabled else ""

    def system_prompt_addition(self) -> str:
        """What to tell the model so its *own* prose matches the voice."""
        if not self.enabled:
            return ""
        if self.name == "jarvis":
            return (
                "\nVOICE: when you write a 'thought', write it the way a "
                "capable but discreet British butler would say it out loud — "
                "understated, exact, faintly dry, no filler, no exclamation "
                "marks. Never theatrical or comedic. Example: 'The window is "
                "behind another window; closing the obstruction.' "
                "Keep it under 25 words."
            )
        if self.name == "butler":
            return ("\nVOICE: write thoughts as a discreet butler would say "
                    "them — polite, exact, unfussy.")
        if self.name == "terse":
            return ("\nVOICE: write thoughts as a bare status line. No more "
                    "than eight words. No pleasantries.")
        return ""


# --------------------------------------------------------------------------
_PHRASES: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "jarvis": (
        ("Right away", "Consider it done", "At once", "Very good",
         "Right you are", "Of course"),
        # Closers are suffixes: each carries its own leading space.
        ("", " if that is your wish", " as you require", " with your permission"),
        # Hedges are prefixes: each carries its own trailing space.
        ("", "If I may, ", "If that seems wise, "),
    ),
    "butler": (
        ("Certainly", "Of course", "Very good", "Right away", "At once"),
        ("", " as you wish"),
        ("", "If I may, "),
    ),
    "terse": ((), (), ()),
}

_TIGHTEN = (
    (r"\bI am going to\b", "I'll"),
    (r"\bI will\b", "I'll"),
    (r"\bI would\b", "I'd"),
    (r"\bit is\b", "it's"),
    (r"\bthat is\b", "that's"),
    (r"\bdo not\b", "don't"),
    (r"\bdoes not\b", "doesn't"),
    (r"\bcannot\b", "can't"),
    (r"\bwill not\b", "won't"),
    # Skip a following pronoun: "I have it" must not become "I've it".
    (r"\bI have\b(?!\s+(?:it|him|her|them|me|one|this)\b)", "I've"),
    (r"\bwe have\b", "we've"),
    (r"\bI have not\b", "haven't"),
    (r"\bPlease note that\b", ""),
    (r"\bI think that\b", "I think"),
    (r"\bjust going to\b", "going to"),
    (r"\bactually\b", ""),
    (r"\bbasically\b", ""),
    (r"\bI would like to\b", "I'll"),
    (r"\s{2,}", " "),
)


def _tidy(text: str) -> str:
    for pattern, replacement in _TIGHTEN:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    # Drop a space before punctuation, but only when nothing alphanumeric follows
    # it. 'word .' tidies to 'word.', yet 'every .txt' must keep its space or a
    # spoken file name turns into a different file name.
    text = re.sub(r"\s+([,.!?])(?![A-Za-z0-9])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = text.strip()
    if not text:
        return text

    # Capitalise sentence starts. Without this a styled multi-sentence line
    # reads as "I'm afraid I can't, sir. it's not on your allowlist."
    text = re.sub(r"([.!?]\s+)([a-z])",
                  lambda m: m.group(1) + m.group(2).upper(), text)
    if text[0].islower():
        text = text[0].upper() + text[1:]
    return text


def get_persona(cfg=None) -> Persona:
    """Build a Persona from config, defaulting to a silent pass-through."""
    try:
        voice = cfg.voice
    except AttributeError:
        return Persona()
    return Persona(name=getattr(voice, "persona", "none"),
                   address=getattr(voice, "address", ""),
                   cadence=getattr(voice, "cadence", "dry"),
                   chime=getattr(voice, "chime", True))
