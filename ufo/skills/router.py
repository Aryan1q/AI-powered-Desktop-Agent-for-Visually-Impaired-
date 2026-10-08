"""Decide whether an instruction is a known skill, and hand it over.

A sentence like "convert test.pdf to word, change 2024 to 2025, convert it back
to pdf" is a procedure this machine already knows how to do exactly. Sending it
to the model would mean asking a 4B model to improvise twenty browser steps, at
minutes per step, and it would probably fail halfway. So the router claims the
instruction, and the model is only consulted for the part it is actually good
at: working out which text the user meant.

The rule is deliberately narrow. When in doubt the instruction goes to the
agent, because a skill that fires on the wrong sentence edits the wrong
document.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .pdf_roundtrip import find_pdf, find_replacement

# Both halves must be present: a conversion *and* an edit.
CONVERT_WORDS = re.compile(
    r"\b(?:pdf\s*to\s*word|word\s*to\s*pdf|pdf\s*to\s*docx|"
    r"convert(?:er)?|conversion)\b", re.IGNORECASE)
EDIT_WORDS = re.compile(
    r"\b(?:edit|change|replace|rename|update|fix|correct)\b", re.IGNORECASE)
BACK_TO_PDF = re.compile(
    r"\b(?:back\s*to\s*(?:the\s*)?pdf|to\s*pdf|pdf\s*again|"
    r"convert\s*(?:it\s*)?(?:back)?\s*(?:to\s*)?pdf)\b", re.IGNORECASE)
ROUNDTRIP_WORDS = re.compile(
    r"\b(?:round\s*trip|roundtrip|pdf\s*to\s*word|word\s*to\s*pdf)\b",
    re.IGNORECASE)

# Only these folders are searched for a named PDF. Searching the whole disk for
# a file the user only described in a few words would be both slow and unsafe.
DEFAULT_SEARCH_DIRS = (
    Path.home() / "Downloads",
    Path.home() / "Documents",
    Path.home() / "Desktop",
)


@dataclass
class SkillInvocation:
    name: str
    instruction: str
    source: Path
    old: str
    new: str

    def describe(self) -> str:
        return (f"Running the PDF round trip on {self.source.name}: "
                f"changing {self.old!r} to {self.new!r}.")


def looks_like_roundtrip(instruction: str) -> bool:
    text = instruction or ""
    if not CONVERT_WORDS.search(text):
        return False
    if BACK_TO_PDF.search(text) and EDIT_WORDS.search(text):
        return True
    if ROUNDTRIP_WORDS.search(text) and EDIT_WORDS.search(text):
        return True
    # PDF to Word with an edit, and no way back, is still this skill: it
    # produces the Word file and stops there.
    return bool(EDIT_WORDS.search(text)
                and re.search(r"pdf\s*to\s*word|pdf\s*to\s*docx", text,
                              re.IGNORECASE))


def match_roundtrip(instruction: str,
                    search_dirs=DEFAULT_SEARCH_DIRS) -> SkillInvocation | None:
    """Build a round-trip invocation, or None if this is not one.

    Returning None is the normal case and always safe: the caller then hands
    the instruction to the agent exactly as before.
    """
    if not looks_like_roundtrip(instruction):
        return None

    replacement = find_replacement(instruction)
    source = find_pdf(instruction, search_dirs)
    if replacement is None or source is None:
        return None
    old, new = replacement
    return SkillInvocation(name="pdf_roundtrip", instruction=instruction,
                           source=source, old=old, new=new)


def run_roundtrip(invocation: SkillInvocation, out_dir=None, headless=True,
                  progress=None):
    from .pdf_roundtrip import roundtrip

    return roundtrip(invocation.source, invocation.old, invocation.new,
                     out_dir=out_dir, headless=headless, progress=progress)
