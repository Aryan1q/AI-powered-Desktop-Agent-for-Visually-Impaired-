"""One-command skills: long, known procedures that must not be improvised.

A skill is deterministic on purpose. These flows take minutes and touch real
documents, so the model is only ever asked for the one thing it is good at
(working out *which* text the user meant) and never for the twenty mechanical
steps in between.
"""
from .pdf_roundtrip import (RoundtripError, RoundtripResult, find_pdf,
                            find_replacement, roundtrip, verify_pdf)
from .router import (SkillInvocation, looks_like_roundtrip, match_roundtrip,
                     run_roundtrip)

__all__ = [
    "RoundtripError",
    "RoundtripResult",
    "SkillInvocation",
    "find_pdf",
    "find_replacement",
    "looks_like_roundtrip",
    "match_roundtrip",
    "roundtrip",
    "run_roundtrip",
    "verify_pdf",
]
