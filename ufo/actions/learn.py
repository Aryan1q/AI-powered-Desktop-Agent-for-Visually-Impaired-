"""Learning and study: summaries, flashcards, explanations, reading aloud.

Summarisation is extractive by default. It picks the sentences that carry the
most of the document's own vocabulary, which is a defensible summary and an
obviously mechanical one. It never claims to have understood the material.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..errors import ActionError
from ..logging_setup import get_logger

from . import filesystem, keyboard

log = get_logger(__name__)


def _store_path() -> Path:
    from ..config import active

    return Path(active().storage.home) / "learning.json"


def _load() -> dict:
    path = _store_path()
    if not path.is_file():
        return {"cards": {}, "schedule": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"cards": {}, "schedule": []}


def _save(data: dict) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _text_of(target: str, args: str) -> str:
    """Accept a path, or the text itself."""
    candidate = target if len(target) > 200 or "\\" in target else args
    if candidate and len(candidate) < 400 and Path(filesystem.expand(candidate)).is_file():
        from . import docs

        return docs.read(candidate)
    return f"{target} {args}".strip()


_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def summarize(target: str = "", args: str = "") -> str:
    """An extractive summary: the sentences with the most distinctive words."""
    text = _text_of(target, args)
    if not text:
        return "give me a file or some text to summarise"
    sentences = [s.strip() for s in _SENTENCE.split(text) if len(s.strip()) > 30]
    if len(sentences) <= 3:
        return "too short to summarise:\n" + text[:800]
    wanted = max(3, min(8, len(sentences) // 5))
    frequency: dict[str, int] = {}
    for sentence in sentences:
        for word in re.findall(r"[a-z]{4,}", sentence.lower()):
            frequency[word] = frequency.get(word, 0) + 1
    scored = sorted(
        sentences,
        key=lambda s: sum(frequency.get(w, 0) for w in re.findall(r"[a-z]{4,}", s.lower())),
        reverse=True)
    keep = sorted(scored[:wanted], key=lambda s: sentences.index(s))
    return ("\n".join(keep) +
            f"\n\n({wanted} of {len(sentences)} sentences, chosen by word "
            f"frequency — not a judgement of importance)")


def flashcards(target: str = "", args: str = "") -> str:
    """Make a question/answer set from a document and store it."""
    text = _text_of(target, args)
    if not text:
        return "give me a file to make cards from"
    sentences = [s.strip() for s in _SENTENCE.split(text) if len(s.strip()) > 40]
    if not sentences:
        return "no complete sentences found to turn into cards"
    cards = [{"q": s.rstrip(".")[:120], "a": "review this sentence"} for s in sentences[:20]]
    data = _load()
    name = args.strip() or Path(filesystem.expand(target)).stem or "cards"
    data["cards"][name] = cards
    _save(data)
    return (f"made {len(cards)} card(s) in {name!r}. Review with "
            f"`ufo do flashcards_review {name}`")


def review(target: str = "", args: str = "") -> str:
    """Show the next card from a set."""
    data = _load()
    name = (target or args).strip()
    if not name:
        return f"sets: {', '.join(data['cards'])}" if data["cards"] else "no card sets yet"
    cards = data["cards"].get(name)
    if not cards:
        return f"no card set called {name!r}"
    return "\n\n".join(f"Q: {c['q']}\nA: {c['a']}" for c in cards[:10])


def explain_simple(target: str = "", args: str = "") -> str:
    """Explain a term in plain words, from the encyclopaedia summary."""
    term = target.strip()
    if not term:
        return "give me a term or a file to explain"
    from . import network

    try:
        text = network.fetch_text(f"https://en.wikipedia.org/api/rest_v1/page/summary/"
                                  f"{term.replace(' ', '_')}")
        return text[:1200]
    except Exception as e:
        return f"could not look that up: {type(e).__name__}: {e}"


def read_aloud(target: str = "", args: str = "") -> str:
    """Speak some text through the narrator."""
    text = f"{target} {args}".strip()
    if not text:
        text = keyboard.get_clipboard()
    if not text:
        return "give me the text, or put it on the clipboard"
    try:
        import pyttsx3  # type: ignore

        engine = pyttsx3.init()
        engine.say(text[:2000])
        engine.runAndWait()
        return "read that aloud"
    except ImportError:
        raise ActionError("reading aloud needs pyttsx3: `pip install pyttsx3`")


def lecture_notes(target: str = "", args: str = "") -> str:
    """Turn notes into a cleaned outline."""
    text = _text_of(target, args)
    if not text:
        return "give me a file of notes"
    lines = [line.strip(" -*•\t") for line in text.splitlines()]
    kept = [line for line in lines if line]
    from . import productivity

    return productivity.journal("\n".join(kept)) + \
        f" ({len(kept)} point(s))"


def schedule(target: str = "", args: str = "") -> str:
    """Put a study session in the calendar."""
    from . import auto

    topic = target.strip() or "study session"
    return auto.schedule(f"study {topic}", args or "tomorrow 18:00")


def translate_lecture(target: str = "", args: str = "") -> str:
    """Show a vocabulary table for a document: its commonest long words."""
    text = _text_of(target, args)
    if not text:
        return "give me a file or some text"
    from collections import Counter

    words = re.findall(r"[A-Za-z][a-z]{4,}", text)
    if not words:
        return "not enough words to build a vocabulary from"
    common = Counter(w.lower() for w in words).most_common(20)
    return "\n".join(f"{word:<18}{count:>5} use(s)" for word, count in common)


def practice(target: str = "", args: str = "") -> str:
    """Show a few cards from a set for self-testing."""
    return review(target, args)


def maths(target: str = "", args: str = "") -> str:
    """Evaluate a calculation. SymPy is optional; arithmetic always works."""
    expression = f"{target} {args}".strip()
    if not expression:
        return "give me something to work out"
    try:
        import sympy  # type: ignore

        return str(sympy.simplify(expression))
    except ImportError:
        pass
    allowed = {"+", "-", "*", "/", "**", "(", ")", ".", " "}
    if not set(expression) <= allowed | set("0123456789"):
        return ("that is not plain arithmetic. Sympy is not installed for "
                "anything harder: `pip install sympy`")
    try:
        value = eval(expression, {"__builtins__": {}}, {})  # noqa: S307
    except (SyntaxError, ZeroDivisionError, TypeError) as e:
        return f"could not work that out: {type(e).__name__}: {e}"
    return f"{expression} = {value}"
