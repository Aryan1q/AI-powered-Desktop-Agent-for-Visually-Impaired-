"""Category 21: information and assistance.

These were all wired to a single handler that fetched its argument as a URL, so
"what is the capital of France" tried to open a web page and "2 + 2 * 17" tried
to open a URL containing a space. The others returned nothing at all.

The split is deliberate and is the honest one:

* What can be *computed* — arithmetic, unit and currency conversion, dates — is
  computed here, exactly, with no model in the loop. A calculator that guesses is
  worse than no calculator.
* What needs a *data source* — weather, news, directions, exchange rates — calls
  a real public API and says plainly when it cannot reach one.
* What needs *understanding* — answer, explain, define, summarize, translate,
  read aloud — goes to the model, because that is the one thing it is for. When
  no model is configured it says so rather than inventing an answer.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from ..logging_setup import get_logger

log = get_logger(__name__)

#: How long a public API call may take before we admit it did not answer.
_TIMEOUT = 12.0


def _get_json(url: str, timeout: float = _TIMEOUT) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "ufo/2.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _get_text(url: str, timeout: float = _TIMEOUT) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "ufo/2.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


# --------------------------------------------------------------------------
# the model, used only for what genuinely needs understanding
# --------------------------------------------------------------------------
def _ask(system: str, question: str) -> str:
    """One model call. Says so plainly when no model is reachable.

    Uses the same LLMConfig the planner and agent use, so a capability answers
    with whichever model the rest of the agent is already configured for rather
    than a second, differently-configured one. Whether a key exists is the
    client's business — the key lives in the secrets vault, not the config — so
    it is asked to build one and its own `NoLLMConfigured` is the honest answer.
    """
    from ..agent import llm
    from ..config import LLMConfig, active
    from ..errors import LLMError, NoLLMConfigured

    cfg = active()
    try:
        client = llm.Client(LLMConfig(
            provider=cfg.llm.provider, model=cfg.llm.model,
            max_tokens=cfg.llm.max_tokens, timeout=cfg.llm.timeout,
            base_url=cfg.llm.base_url, fallbacks=cfg.llm.fallbacks,
            offline=cfg.llm.offline,
        ))
        return str(client.complete(system, question).text or "").strip()
    except NoLLMConfigured as e:
        return (f"no model is configured, so I cannot answer that and I will "
                f"not guess. Set one with: ufo config set llm.model ({e})")
    except LLMError as e:
        return (f"the model did not answer, so I will not guess: {e}. "
                f"Check it with: ufo config show llm")
    except Exception as e:                       # a provider error is not a crash
        log.debug("model call failed: %s", e)
        return f"the model did not answer, so I will not guess: {e}"


_ASK_SYSTEM = (
    "You answer precisely and briefly. If the answer depends on something you "
    "cannot know — today's weather, a private document, a current price — say "
    "so plainly instead of guessing. Never invent a fact."
)


# --------------------------------------------------------------------------
# computed here, exactly
# --------------------------------------------------------------------------
_SAFE_MATH = re.compile(r"^[\d\s+\-*/().,%^]+$")

_UNITS = {
    # length, to metres
    "m": 1.0, "meter": 1.0, "meters": 1.0, "metre": 1.0, "metres": 1.0,
    "km": 1000.0, "kilometer": 1000.0, "kilometers": 1000.0,
    "kilometre": 1000.0, "kilometres": 1000.0,
    "cm": 0.01, "centimeter": 0.01, "centimeters": 0.01,
    "mm": 0.001, "mile": 1609.344, "miles": 1609.344,
    "ft": 0.3048, "feet": 0.3048, "foot": 0.3048,
    "in": 0.0254, "inch": 0.0254, "inches": 0.0254,
    "yd": 0.9144, "yard": 0.9144, "yards": 0.9144,
    "mi": 1609.344,
    # mass, to kilograms
    "kg": 1.0, "kilogram": 1.0, "kilograms": 1.0,
    "g": 0.001, "gram": 0.001, "grams": 0.001,
    "mg": 1e-6, "lb": 0.45359237, "lbs": 0.45359237, "pound": 0.45359237,
    "oz": 0.028349523, "ounce": 0.028349523, "ounces": 0.028349523,
    "t": 1000.0, "tonne": 1000.0, "tonnes": 1000.0,
    "ton": 907.18474, "tons": 907.18474,
    # volume, to litres
    "l": 1.0, "liter": 1.0, "liters": 1.0, "litre": 1.0, "litres": 1.0,
    "ml": 0.001, "gal": 3.785411784, "gallon": 3.785411784,
    "gallons": 3.785411784,
    "cup": 0.236588, "cups": 0.236588, "pint": 0.473176, "pints": 0.473176,
    # time
    "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
    "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hr": 3600.0, "hrs": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "day": 86400.0, "days": 86400.0, "week": 604800.0, "weeks": 604800.0,
}

#: Which families may be converted into each other.
_FAMILIES = (
    {"m", "meter", "meters", "metre", "metres", "km", "kilometer", "kilometers",
     "kilometre", "kilometres", "cm", "centimeter", "centimeters", "mm",
     "mile", "miles", "mi", "ft", "feet", "foot", "in", "inch", "inches",
     "yd", "yard", "yards"},
    {"kg", "kilogram", "kilograms", "g", "gram", "grams", "mg", "lb", "lbs",
     "pound", "oz", "ounce", "ounces", "t", "tonne", "tonnes", "ton", "tons"},
    {"l", "liter", "liters", "litre", "litres", "ml", "gal", "gallon",
     "gallons", "cup", "cups", "pint", "pints"},
    {"s", "sec", "secs", "second", "seconds", "min", "mins", "minute",
     "minutes", "h", "hr", "hrs", "hour", "hours", "day", "days", "week",
     "weeks"},
)


def calculate(target: str = "", args: str = "") -> str:
    """Do arithmetic exactly, or convert between units. No model involved."""
    expression = f"{target} {args}".strip()
    if not expression:
        return ('say what to work out, e.g. "2 + 2 * 17" or "5 km in miles"')

    converted = convert_units(expression, announce=False)
    if converted is not None:
        return converted

    # "15% of 200" and "20 percent of 50": the two shapes people actually type.
    percent = re.fullmatch(r"([\d.,]+)\s*(?:%|percent)\s+of\s+([\d.,]+)",
                           expression, re.I)
    if percent:
        try:
            part = float(percent.group(1).replace(",", "").rstrip("%"))
            whole = float(percent.group(2).replace(",", ""))
        except ValueError:
            part = whole = None
        if part is not None and whole is not None:
            return f"{expression} = {part / 100 * whole:,.10g}"

    cleaned = expression.replace(",", "").replace("^", "**")
    cleaned = re.sub(r"(?<=\d)\s*°", "**", cleaned)      # 2^8 typed with a degree
    if not _SAFE_MATH.match(cleaned):
        return (f"{expression!r} is not arithmetic I can do exactly. I can "
                f"compute +, -, *, /, parentheses, ** and 'N% of M', or convert "
                f"units. Anything else goes to the model: ask me to 'work out' "
                f"it and I will put it to the model instead.")
    try:
        value = eval(cleaned, {"__builtins__": {}}, {})     # noqa: S307
    except Exception as e:
        return f"could not work out {expression!r}: {e}"
    if isinstance(value, float):
        shown = f"{value:,.10g}"
    else:
        shown = f"{value:,}"
    return f"{expression} = {shown}"


def convert_units(expression: str, announce: bool = True) -> str | None:
    """"5 km in miles", "100 f to c", "2 kg to grams". None when it does not parse."""
    text = str(expression or "").strip().lower()
    match = re.search(r"([\d.,]+)\s*([a-z/^°]+)\s*(?:in|to|as)\s*([a-z/^°]+)", text)
    if not match:
        return None
    amount_text, from_unit, to_unit = (g.strip() for g in match.groups())
    try:
        amount = float(amount_text.replace(",", ""))
    except ValueError:
        return None

    from_unit, to_unit = from_unit.rstrip("."), to_unit.rstrip(".")
    if from_unit == to_unit:
        return f"{amount_text} {from_unit} = {amount_text} {to_unit}"

    # temperature needs its own scale, not a linear factor
    if {from_unit, to_unit} & {"c", "f", "k", "celsius", "fahrenheit", "kelvin"}:
        return _convert_temperature(amount, from_unit, to_unit)

    if from_unit not in _UNITS or to_unit not in _UNITS:
        return None
    family = next((f for f in _FAMILIES
                   if from_unit in f and to_unit in f), None)
    if family is None:
        return (f"{from_unit} and {to_unit} measure different things, so there "
                f"is no conversion between them.")
    result = amount * _UNITS[from_unit] / _UNITS[to_unit]
    return f"{amount_text} {from_unit} = {result:,.6g} {to_unit}"


def _convert_temperature(amount: float, from_unit: str, to_unit: str) -> str:
    to_c = {"c": lambda v: v, "celsius": lambda v: v,
            "f": lambda v: (v - 32) * 5 / 9, "fahrenheit": lambda v: (v - 32) * 5 / 9,
            "k": lambda v: v - 273.15, "kelvin": lambda v: v - 273.15}[from_unit](amount)
    out = {"c": to_c, "celsius": to_c,
           "f": to_c * 9 / 5 + 32, "fahrenheit": to_c * 9 / 5 + 32,
           "k": to_c + 273.15, "kelvin": to_c + 273.15}[to_unit]
    return f"{amount:g} °{from_unit[0].upper()} = {out:,.6g} °{to_unit[0].upper()}"


# --------------------------------------------------------------------------
# live data: not available in this install
# --------------------------------------------------------------------------
# This install talks to Ollama on this machine and nothing else. Weather,
# headlines, driving directions and exchange rates all need a public service to
# ask, so they are gone rather than quietly reaching out to one. The handlers
# stay registered so the catalog keeps resolving: an unavailable capability has
# to say so out loud instead of vanishing mid-sentence, which reads as a bug.
_UNAVAILABLE = (
    "There is no local source for {what}: it comes from a public service, and "
    "this install is Ollama-only. Open a normal browser for {instead}, or ask "
    "Ollama for what it can work out offline."
)


def weather(target: str = "", args: str = "") -> str:
    """No local weather source exists. Says so instead of calling out."""
    return _UNAVAILABLE.format(what="Live weather",
                               instead="a weather site")


def news(target: str = "", args: str = "") -> str:
    """No local news feed exists. Says so instead of calling out."""
    return _UNAVAILABLE.format(what="News headlines",
                               instead="a news site")


def directions(target: str = "", args: str = "") -> str:
    """No local routing or geocoding service exists. Says so."""
    return _UNAVAILABLE.format(what="Driving directions and distance",
                               instead="a maps site")


def currency(target: str = "", args: str = "") -> str:
    """No local exchange-rate feed exists. Says so instead of calling out."""
    return _UNAVAILABLE.format(what="A live currency conversion",
                               instead="a currency site")

# --------------------------------------------------------------------------
# understanding: these are what the model is for
# --------------------------------------------------------------------------
def answer(target: str = "", args: str = "") -> str:
    """Answer a question, using context the user supplied if any."""
    question = str(target or "").strip()
    context = str(args or "").strip()
    if not question:
        return "what do you want to know?"
    prompt = question if not context else f"{question}\n\nContext:\n{context}"
    return _ask(_ASK_SYSTEM, prompt)


def explain(target: str = "", args: str = "") -> str:
    """Explain something simply, pitched at an audience if one is named."""
    subject = str(target or "").strip()
    audience = str(args or "").strip()
    if not subject:
        return "what should I explain?"
    if audience:
        prompt = (f"Explain {subject} for {audience}. Be clear and concrete, "
                  f"and use a short example.")
    else:
        prompt = f"Explain {subject} clearly and simply, with a short example."
    return _ask(_ASK_SYSTEM, prompt)


def define(target: str = "", args: str = "") -> str:
    """Give the meaning of a word or term."""
    term = f"{target} {args}".strip()
    if not term:
        return "what should I define?"
    return _ask(_ASK_SYSTEM,
                f"Define {term!r}: the meaning, plus one everyday example.")


def translate(target: str = "", args: str = "") -> str:
    """Translate text into a named language."""
    text = str(target or "").strip()
    language = str(args or "").strip()
    if not text:
        return "what should I translate?"
    if not language:
        # Without a target language, ask rather than guessing one.
        return ("into which language? e.g. \"translate good morning to French\"")
    return _ask(_ASK_SYSTEM,
                f"Translate this into {language}. Give only the translation, "
                f"nothing else.\n\n{text}")


def summarize_url(target: str = "", args: str = "") -> str:
    """Read a page and summarise it. Falls back to the page's own text."""
    url = f"{target} {args}".strip()
    if not url:
        return "give me the page to summarise"
    if not re.match(r"^https?://", url, re.I):
        url = f"https://{url}"
    from . import network

    try:
        raw = network.fetch_text(url)
    except Exception as e:
        return f"could not read {url}: {type(e).__name__}: {e}"
    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    if not body:
        return f"{url} had no readable text to summarise"
    heading = re.sub(r"\s+", " ", title.group(1)).strip() if title else url
    return _ask(
        "You summarise web pages faithfully and briefly. Keep the page's own "
        "claims; do not add facts it does not contain.",
        f"Summarise this page ({heading}) in a short paragraph.\n\n{body[:8000]}")


def web_lookup(target: str = "", args: str = "") -> str:
    """Fetch a page's text so it can be read without a browser."""
    from . import network

    url = (target or args).strip()
    if not url:
        return "give me a URL to read"
    if not re.match(r"^https?://", url, re.I):
        url = f"https://{url}"
    try:
        return network.fetch_text(url)[:4000]
    except Exception as e:
        return f"could not read {url}: {type(e).__name__}: {e}"


def read_aloud(target: str = "", args: str = "") -> str:
    """Read a file or some text out loud.

    Uses the same narrator the agent already speaks through, so it respects the
    configured voice, rate and the voice-enabled switch rather than reaching for
    a second speech path.
    """
    text = f"{target} {args}".strip()
    if not text:
        return "what should I read?"
    from pathlib import Path

    candidate = Path(text)
    if candidate.is_file():
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")[:20000]
        except OSError as e:
            return f"could not read {candidate}: {e}"

    from ..voice import get_default_narrator

    narrator = get_default_narrator()
    if not getattr(narrator.backend, "available", False):
        # No TTS backend: printing it is strictly better than claiming it was
        # read aloud.
        return (f"no speech voice is available on this machine, so here it is "
                f"instead:\n\n{text[:2000]}")
    narrator.say(text)
    return (f"read {len(text)} characters aloud using the "
            f"{getattr(narrator.backend, 'name', 'default')} voice")
