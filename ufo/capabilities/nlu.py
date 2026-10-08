"""Turning a sentence into a plan of capabilities.

The point of this module is to answer the cheap half of an instruction without
a model call. "Mute the sound" is not a reasoning problem; it is a lookup.
Sending it to a 4B model means minutes of latency for an answer the catalog
already knows.

The rule is narrow on purpose. `interpret` only claims a sentence when it can
name every step it would run, and it reports how it matched. When it is not
sure it returns `None` and the caller hands the sentence to the agent loop
unchanged — a router that guesses is worse than no router, because a wrong
guess deletes the wrong file.

Three layers, cheapest first:

1. **Named capability** — the text contains ``mute`` and the catalog has a
   capability called ``set_volume`` with that trigger. Exact wins.
2. **Trigger phrase** — the text matches a stored phrase, in English, Hindi or
   Hinglish, by whole-word containment.
3. **Recipe** — a vague, goal-shaped instruction ("make my laptop faster")
   expands into a list of real capabilities.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from ..utils import text as textutil
from . import catalog, phrases
from .catalog import Capability


@dataclass
class Step:
    """One capability call, ready to run."""

    capability: str
    target: str = ""
    args: str = ""
    why: str = ""
    via: str = ""          # recipe name, "trigger", "named", ...

    @property
    def spec(self) -> str:
        parts = [self.capability]
        if self.target:
            parts.append(self.target)
        if self.args:
            parts.append(self.args)
        return ":".join(parts)

    def to_dict(self) -> dict:
        return {"capability": self.capability, "target": self.target,
                "args": self.args, "why": self.why, "via": self.via}


@dataclass
class Plan:
    """A claimed sentence, with the steps it would run."""

    text: str
    steps: list[Step] = field(default_factory=list)
    confidence: float = 0.0
    matched: list[str] = field(default_factory=list)
    language: str = "en"

    def __bool__(self) -> bool:
        return bool(self.steps)

    def specs(self) -> list[str]:
        return [s.spec for s in self.steps]

    def describe(self) -> str:
        lines = [f'"{textutil.truncate(self.text, 70)}" -> '
                 f"{len(self.steps)} step(s), confidence {self.confidence:.2f}"]
        for step in self.steps:
            lines.append(f"  {step.spec}" + (f"   ({step.why})" if step.why else ""))
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"text": self.text, "confidence": round(self.confidence, 3),
                "matched": list(self.matched), "language": self.language,
                "steps": [s.to_dict() for s in self.steps]}


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------
_SCRIPT_RE = re.compile(r"[ऀ-ॿ]")


def detect_language(text: str) -> str:
    """'hi' for Devanagari, 'en' otherwise. Hinglish is written in Latin script
    and is matched by the same English triggers plus its own phrases."""
    return "hi" if _SCRIPT_RE.search(str(text or "")) else "en"


def normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, keep the words."""
    value = str(text or "").lower()
    value = value.replace("ढ", "ढ")
    value = re.sub(r"[^\w\sऀ-ॿ]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


#: Hinglish verbs that mean the same thing as the English one.
_HINGLISH = {
    "kro": "do", "kar": "do", "kardo": "do", "kar do": "do", "karna": "do",
    "karte": "do", "karenge": "do", "karega": "do", "kare": "do",
    "chalao": "run", "chala": "run", "chalaye": "run", "chalo": "run",
    "banao": "make", "bana": "make", "banaye": "make", "bana do": "make",
    "bana do": "make", "banana": "make",
    "dekho": "show", "dekha": "show", "dikh": "show", "dikhawa": "show",
    "dikhawao": "show", "batayo": "tell", "bata": "tell", "batao": "tell",
    "bata do": "tell",
    "hatao": "delete", "hata": "delete", "hatado": "delete", "hatana": "delete",
    "mita": "delete", "mita do": "delete", "mitana": "delete",
    "rok": "stop", "roko": "stop", "ruk": "stop", "rukna": "stop",
    "kholo": "open", "khol": "open", "kholna": "open", "khul": "open",
    "band": "close", "bandh": "close", "band karo": "close",
    "badlo": "change", "badal": "change", "badalna": "change",
    "lagao": "set", "laga": "set", "lagana": "set",
    "nikalo": "get", "nikal": "get", "nikala": "get", "lao": "get",
    "dhundho": "find", "dhoondo": "find", "dhundhna": "find",
    "dhoondho": "find", "dhunde": "find",
    "sunao": "read", "suna": "read", "sunana": "read", "padho": "read",
    "padhna": "read", "bolo": "speak", "bolna": "speak",
    "bhejo": "send", "bhej": "send", "bhejna": "send",
    "likho": "write", "likh": "write", "likhna": "write",
    "banao": "make", "bana": "make",
    "jodo": "join", "jod": "join", "jodna": "join", "milao": "merge",
    "mila": "merge", "milana": "merge",
    "taiyar": "prepare", "tayyar": "prepare", "kar": "do",
    "chalu": "on", "chalu karo": "on", "on karo": "on", "band karo": "off",
    "off karo": "off", "shuru": "start", "shuru karo": "start",
    "khatam": "finish", "khatam karo": "finish", "pure": "complete",
    "complete karo": "complete", "tadka": "search", "talash": "search",
    "dekh": "see", "dekhiye": "see",
    "waqt": "time", "samay": "time",
}


def normalise_hinglish(text: str) -> str:
    """Rewrite Hinglish verbs into their English verb.

    Deliberately shallow. It is a lookup table over the twenty verbs people
    actually mix into English, not a transliterator: anything it does not know
    is left exactly as written, and the trigger phrases already cover the
    Devanagari spelling of the same sentence.
    """
    words = normalise(text).split()
    out = [w for w in words if w not in phrases_extra_stopwords()]
    return " ".join(_HINGLISH.get(w, w) for w in out)


def phrases_extra_stopwords() -> frozenset:
    return frozenset({"please", "pls", "kindly", "mere", "mera", "meri", "mujhe",
                      "mujhko", "karo", "kar", "kro", "kardo", "bhai", "yaar",
                      "abhi", "jaldi", "aur", "phir", "karke", "kar", "karna"})


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------
def _trigger_index() -> list[tuple[Capability, str, str]]:
    """(capability, verbatim phrase, loose phrase) for every trigger.

    Both forms are kept because they answer different questions. The verbatim
    form matches the sentence as written and is strong evidence. The loose form
    drops filler words ("empty the recycle bin" -> "empty recycle bin") and is
    what catches a sentence that happens to carry extra ones. Scoring them
    differently is what keeps "clear my clipboard" away from "clear cookies".
    """
    index: list[tuple[Capability, str, str]] = []
    for number, entries in phrases.PHRASES.items():
        for names, triggers in entries:
            cap = catalog.get(names[0])
            if cap is None:
                raise ValueError(
                    f"phrases.py mentions unknown capability {names[0]!r} "
                    f"(category {number})"
                )
            for entry in triggers:
                verbatim = normalise(entry)
                loose = catalog.normalise_phrase(entry)
                if not verbatim:
                    continue
                # A trigger whose loose form collapses to nothing is still worth
                # indexing on the verbatim form: "what can you do" is filler
                # once stop words go, but written out it is an exact match.
                if loose and not _usable(loose):
                    loose = ""
                if not loose and not _usable(verbatim):
                    continue
                index.append((cap, verbatim, loose))
    return index


#: Words that carry no routing signal on their own. A loose trigger made only
#: of these would claim almost every sentence, so it is dropped: "what is" is a
#: bad trigger, "unzip" is a good one.
_WEAK = frozenset({
    "what", "how", "why", "who", "when", "where", "which", "this", "that",
    "them", "there", "here", "tell", "show", "give", "take", "use", "now",
})


def _usable(loose: str) -> bool:
    words = loose.split()
    if len(words) >= 2:
        return True
    return len(words[0]) >= 4 and words[0] not in _WEAK


_TRIGGERS = _trigger_index()

#: Longest trigger first, so "empty the recycle bin" beats "empty".
_TRIGGERS.sort(key=lambda row: len(row[1].split()), reverse=True)


def canonical(text: str) -> str:
    """The sentence as written, normalised. Both trigger forms are compared
    against this, and this against them, so the comparison is symmetric."""
    return normalise(text)


def loose(text: str) -> str:
    return catalog.collapse_stopwords(normalise(text))


def _contains(haystack: str, needle: str) -> bool:
    return f" {needle} " in f" {haystack} "


def _tokens(text: str) -> set[str]:
    return set(text.split())


def _hit(token: str, words: set[str]) -> bool:
    """A token counts as present even when the sentence inflects it.

    "flights" contains "flight", "muting" contains "mute". Only prefixes of
    four characters or more are tried, so short words still need to be exact.
    """
    if token in words:
        return True
    if len(token) >= 4:
        return any(w.startswith(token) for w in words)
    return False


def match_capabilities(text: str, limit: int = 6) -> list[tuple[Capability, str, float]]:
    """Capabilities the sentence clearly names, best first."""
    base = canonical(text)
    base_loose = loose(text)
    words = _tokens(base_loose)
    found: list[tuple[Capability, str, float, bool]] = []
    seen: set[str] = set()

    # 1. an explicit action name, e.g. "set_volume" or "set-volume"
    #
    # The score is by coverage, not a flat 1.0. A one-word alias such as "click"
    # appears inside far longer sentences that mean something else — "double
    # click the file" and "right click on it" both contain the word, and giving
    # the alias a perfect score meant they both routed to plain click.
    for token in re.findall(r"[a-z][a-z0-9_]{2,}", base.replace("-", "_")):
        cap = catalog.get(token)
        if cap is None or cap.name in seen:
            continue
        seen.add(cap.name)
        found.append((cap, cap.name, _name_score(base_loose, token), True))

    # 2. trigger phrases. A capability keeps its best-scoring trigger, not its
    # first one seen: the index is ordered by phrase length, so a long but only
    # partially matching trigger would otherwise lock out the capability's own
    # exact wording.
    best: dict[str, tuple[Capability, str, float, bool]] = {}
    for cap, verbatim, loose_phrase in _TRIGGERS:
        if cap.name in seen:
            continue
        if _contains(base, verbatim):
            score, exact = _score(verbatim, words, exact=True), True
        elif loose_phrase and _contains(base_loose, loose_phrase):
            score, exact = _score(loose_phrase, words, exact=False), False
        elif loose_phrase:
            score, exact = _score(loose_phrase, words, exact=False), False
        else:
            continue
        if score <= 0:
            continue
        current = best.get(cap.name)
        if current is None or score > current[2]:
            best[cap.name] = (cap, verbatim, score, exact)
    found.extend(best.values())
    if not found:
        return []
    found.sort(key=lambda row: row[2], reverse=True)

    # What survives.
    #
    # A partial match is a guess, so only the best one is acted on: "open the tax
    # pdf and summarise it" half-matches five capabilities, and running all five
    # would open, convert, split and summarise the same file.
    #
    # Exact matches are evidence, but two of them in one fragment is nearly
    # always a misfire rather than a plan: "print this to pdf" contains both
    # "print this" and "print this to pdf", and honouring both would put the file
    # on paper *and* save a PDF. Genuine multi-step requests are split before they
    # get here, by `interpret_compound`. So exact matches must be within a very
    # small margin of the best one to survive together.
    top = found[0]
    floor = max(0.5, top[2] - 0.07)
    if top[3]:
        exact_floor = top[2] - 0.02
        kept = [row for row in found if row[3] and row[2] >= exact_floor]
    else:
        kept = [top]
    kept = [row for row in kept if row[2] >= floor][:limit]
    return [(cap, phrase, score) for cap, phrase, score, _exact in kept]


def _name_score(sentence_loose: str, name: str) -> float:
    """Score an action name or alias found inside the sentence.

    Scored by how much of the sentence the name accounts for. A request that is
    just the name scores 1.0; a name that is one word of a longer sentence scores
    in proportion, which keeps "click" from claiming "double click the file".
    """
    wanted = name.replace("_", " ").split()
    words = sentence_loose.split()
    if not words or not wanted:
        return 0.0
    if words == wanted:
        return 1.0
    for start in range(len(words) - len(wanted) + 1):
        if words[start:start + len(wanted)] == wanted:
            return round(0.55 + 0.45 * (len(wanted) / len(words)), 3)
    return 0.0


def _score(phrase: str, words: set[str], exact: bool) -> float:
    tokens = phrase.split()
    if not tokens:
        return 0.0
    if exact:
        # A trigger that appears in the sentence verbatim is a literal match and
        # outranks a name or alias that merely appears inside a longer sentence.
        return min(0.99, 0.88 + 0.04 * len(tokens))
    if len(tokens) == 1:
        # A one-word trigger has to appear exactly. Prefix matching here is too
        # eager: "download" would claim "rename every .txt in Downloads", and a
        # single word is not enough evidence to run anything on its own.
        return 0.62 if tokens[0] in words else 0.0
    hits = sum(1 for token in tokens if _hit(token, words))
    if hits == len(tokens):
        return min(0.99, 0.5 + 0.08 * len(tokens))
    # most of the phrase is there; the missing words cost a little
    if hits >= 2 and hits >= len(tokens) - 2:
        return 0.42 + 0.07 * hits - 0.02 * (len(tokens) - hits)
    return 0.0


# --------------------------------------------------------------------------
# recipes
# --------------------------------------------------------------------------
def match_recipe(text: str) -> tuple[str, list[str]] | None:
    """A vague goal and the capabilities it expands to."""
    base = catalog.fold_spelling(canonical(text))
    folded = catalog.fold_spelling(
        catalog.collapse_stopwords(normalise_hinglish(text)))
    best: tuple[float, str, list[str]] | None = None
    for triggers, steps in phrases.RECIPES:
        for trigger in triggers:
            norm = catalog.normalise_phrase(trigger)
            if not norm:
                continue
            # A recipe expands into several actions, so it needs more evidence
            # than a single capability does. Triggers collapse stopwords, and
            # "is everything ok" collapses to "everything" — which then matched
            # "select everything" and replaced it with a four-step health check.
            if len(norm.split()) < 2:
                continue
            if _contains(base, norm) or _contains(folded, norm):
                confidence = min(0.98, 0.6 + 0.1 * len(norm.split()))
                if best is None or confidence > best[0]:
                    best = (confidence, trigger, list(steps))
    if best is None:
        return None
    return best[1], best[2]


def routine_steps(name: str) -> list[str] | None:
    """The goals one mode runs, straight from the table that owns them.

    `actions.auto.ROUTINES` is the only definition; this used to be a second,
    drifting copy in `phrases.ROUTINE_STEPS` with different steps under the same
    name, so a mode could mean two different things depending on which layer
    answered.
    """
    from ..actions import auto

    return auto.ROUTINES.get(catalog.normalise_phrase(name), {}).get("on")


# --------------------------------------------------------------------------
# the entry point
# --------------------------------------------------------------------------
_CONFIDENT = 0.55


def interpret(text: str, threshold: float = _CONFIDENT) -> Plan | None:
    """Claim the sentence, or return None to let the agent have it.

    `None` is the common, correct answer. The caller falls back to the model
    loop, which is better at working out what a person meant.
    """
    plan = try_interpret(text, threshold)
    if plan is None:
        return None
    return plan


def try_interpret(text: str, threshold: float = _CONFIDENT) -> Plan | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    language = detect_language(raw)
    matched: list[str] = []
    steps: list[Step] = []

    # A named mode, routed to the `routine` capability rather than expanded here.
    # Expanding it in this layer turned "focus off" into the five *on* steps, and
    # bypassed the guard that `routine` is risk-scored and confirmed under.
    named = _match_routine(raw)
    if named is not None:
        return named

    # an explicit list: "step;step;step"
    if ";" in raw and all(_looks_like_spec(p) for p in raw.split(";") if p.strip()):
        specs = [p.strip() for p in raw.split(";") if p.strip()]
        return Plan(raw, [_step_from_spec(s, "explicit list", "list")
                          for s in specs], 0.99, ["explicit list"], language)

    # a vague goal with a known recipe
    recipe = match_recipe(raw)
    if recipe is not None:
        name, specs = recipe
        for spec in specs:
            steps.append(_step_from_spec(spec, f"recipe for {name!r}", "recipe"))
        matched.append(f"recipe:{name}")
        return Plan(raw, steps, 0.7, matched, language)

    # ordinary named capabilities
    found = match_capabilities(raw, limit=4)
    if not found:
        return None
    steps = []
    for cap, phrase, confidence in found:
        if confidence < threshold:
            continue
        step = _capability_step(cap, raw, phrase)
        # `routine` picks one of nine modes. Matched on the bare word "mode" it
        # arrives with no mode name, and listing the modes is not what "the
        # gaming mode is broken" asked for. `_match_routine` already ran above
        # and declined, so no mode was named — decline again here rather than
        # guess, and let the ladder ask which mode was meant.
        #
        # This is deliberately narrow. Requiring a subject of *every*
        # target-taking capability broke "mute the sound" and "find and replace
        # in this file", which both degrade gracefully without one.
        if cap.name == "routine" and not (step.target or step.args):
            continue
        steps.append(step)
        matched.append(phrase)
    if not steps:
        return None
    return Plan(raw, steps, max(0.0, found[0][2]), matched, language)


#: Words that introduce a routine by name. A routine is only claimed when the
#: request is essentially *just* the routine name, optionally behind one of these.
_ROUTINE_VERBS = frozenset({
    "start", "begin", "enter", "run", "activate", "switch", "launch", "setup",
    "set", "do", "open", "turn", "on", "off", "karo", "chalu", "shuru",
    "startup", "chaluo", "mode", "routine", "please", "now", "me", "my",
    # "leave focus mode", "stop work mode": the same request in reverse. Without
    # these the sentence matched no mode at all and fell through to unsupported.
    "leave", "exit", "stop", "end", "quit", "disable", "cancel", "get",
})

#: Filler that may follow the mode name without making the request about
#: something else: "start work mode please", "good morning". Note that "on" and
#: "off" are deliberately absent — they select a direction, not a mode, and
#: treating them as filler is what made "focus off" run the focus steps.
_ROUTINE_TAIL = frozenset({
    "mode", "routine", "please", "now", "up", "it", "chaluo",
    "kar", "karo", "shuru", "session", "time", "block", "today", "again",
})

#: The nine modes, spelled the way people say them. Keys match
#: `actions.auto.ROUTINES`, which is the single source of truth for what each
#: mode does; this table only decides that a sentence is naming one.
_ROUTINE_NAMES = {
    "end of day": ("end of day", "endday", "wind down", "wrap up", "sign off"),
    "morning": ("morning", "start of day", "startup", "wake up"),
    "work": ("work", "office", "working", "business"),
    "focus": ("focus", "deep work", "pomodoro", "concentration"),
    "meeting": ("meeting", "call", "standup"),
    "presentation": ("presentation", "presenting", "demo", "pitch"),
    "study": ("study", "revise", "revision"),
    "gaming": ("gaming", "game"),
    "travel": ("travel", "commute", "journey", "trip"),
}


def _match_routine(text: str) -> Plan | None:
    """A sentence that names one of the nine modes and little else.

    A mode changes system state, so it has to be *named* rather than merely
    mentioned. This used to be a substring test, which meant any sentence
    containing "work" fired the whole work mode — "work only in this folder"
    minimised every window on the machine.

    Three conditions, all required:
      1. the request collapses to the mode name, plus at most a leading mode
         verb and a small tail of filler ("start work mode please");
      2. every leftover word is filler, so nothing else is being asked for;
      3. no specific capability claimed the sentence outright.

    The plan returned is a single `routine` step. Letting `actions.auto` decide
    the steps keeps the guard in the path, and is what lets "focus off" reverse
    the mode instead of repeating it.
    """
    base = catalog.collapse_stopwords(normalise_hinglish(text))
    words = base.split()
    if not words:
        return None

    for name, aliases in sorted(_ROUTINE_NAMES.items(), key=lambda kv: -len(kv[0])):
        # Match on any alias, not just the canonical name: "deep work" and
        # "commute" have to reach `focus` and `travel`, or the user is told the
        # request is unsupported when the mode exists under another word.
        for alias in aliases:
            if not _contains(base, alias):
                continue
            if not _routine_is_the_whole_request(words, alias):
                continue
            found = match_capabilities(text, limit=1)
            if found and found[0][2] >= 0.9 and found[0][0].name != "routine":
                return None
            step = Step(capability="routine", target=name,
                        args=_direction_of(base), why=f"mode '{name}'",
                        via="routine")
            return Plan(text, [step], 0.85, [name], detect_language(text))
    return None


#: Words that mean "undo this", not "do this".
_OFF_WORDS = frozenset({"off", "stop", "end", "exit", "quit", "leave", "disable",
                        "cancel", "band", "khatam", "undo"})


def _direction_of(text: str) -> str:
    """The direction a mode request is asking for: "", "on" or "off"."""
    words = set(text.split())
    if words & _OFF_WORDS:
        return "off"
    if words & {"on", "start", "begin", "enter", "chalu", "shuru"}:
        return "on"
    return ""


def _routine_is_the_whole_request(words: list[str], name: str) -> bool:
    """True when the request is the mode name and nothing else of substance."""
    wanted = name.split()
    remaining = list(words)
    if not remaining:
        return False
    if remaining[:len(wanted)] == wanted:
        remaining = remaining[len(wanted):]
    elif remaining[0] in _ROUTINE_VERBS and remaining[1:1 + len(wanted)] == wanted:
        remaining = remaining[1 + len(wanted):]     # "start morning routine"
    else:
        return False
    return all(word in _ROUTINE_TAIL or word in _ROUTINE_VERBS
               for word in remaining)


def _looks_like_spec(fragment: str) -> bool:
    token = fragment.split(":")[0].split("=")[0].strip()
    return catalog.get(token) is not None


def _step_from_spec(spec: str, why: str, via: str) -> Step:
    capability, target, args = _parse_spec(spec)
    return Step(capability=capability, target=target, args=args, why=why, via=via)


def _parse_spec(spec: str) -> tuple[str, str, str]:
    raw = str(spec or "").strip()
    if "|" in raw:
        raw, _, args = raw.partition("|")
        args = args.strip()
    else:
        args = ""
    head, sep, rest = raw.partition(":")
    if not sep:
        return raw, "", args
    cap = catalog.get(head)
    if cap is None:
        return raw, "", args
    rest = rest.strip()
    if "::" in rest:
        rest, _, more = rest.partition("::")
        args = f"{more.strip()} {args}".strip()
    return cap.name, rest, args


def _capability_step(cap: Capability, text: str, phrase: str) -> Step:
    """Fill the target and args from the sentence around the matched phrase."""
    target, args = extract_arguments(cap, text, phrase)
    return Step(capability=cap.name, target=target, args=args,
                why=f"matched {phrase!r}", via="trigger")


# --------------------------------------------------------------------------
# pulling the arguments out
# --------------------------------------------------------------------------
_QUOTES = "\"'“”‘’`"
_IN = re.compile(
    r"(?:in|into|on|to|at|from|for|of|as|se|me|ko|par)\s+(?:the\s+|a\s+|an\s+)?"
    r"([^\"'“”‘’`]+?)(?:\s+(?:folder|file|directory|path|ko|mein|se)\b|[,.;]|$)",
    re.IGNORECASE,
)


def extract_arguments(cap: Capability, text: str, phrase: str = "") -> tuple[str, str]:
    """Best-effort target and args for a capability matched in a sentence.

    Never throws and never invents a path: a capability that needs a file and
    cannot see one in the sentence gets an empty target, and the handler says
    so in a message the user can act on.
    """
    if cap.targets in ("", "-"):
        return "", _args_hint(cap, text)
    quoted = _quoted(text)
    if quoted:
        return quoted, ""
    if cap.targets in ("0-100", "0-100%", "volume 0-100, or mute|unmute|toggle"):
        percent = re.search(r"\b(\d{1,3})\s*(?:%|percent)?\b", text)
        # No number means no number is invented. "mute the sound" must not
        # quietly set the volume to 50.
        if percent:
            return percent.group(1), ""
        return _verb_hint(text, ("unmute", "mute", "toggle", "off", "on",
                                 "बंद", "chup")), ""
    if cap.targets == "app name":
        return _app_name(text), ""
    if cap.targets == "keys":
        return _keys(text), ""
    match = _IN.search(text)
    target = textutil.collapse_ws(match.group(1)) if match else ""
    if target and len(target) > 80:
        target = target[:80]
    if target.lower() in _NOT_A_TARGET:
        target = ""
    return target, _args_hint(cap, text)


#: Words the preposition pattern swallows but that name nothing: "rename every
#: .txt to .md" is a pattern rewrite, and the target is the file type, not the
#: quantifier in front of it.
_NOT_A_TARGET = frozenset({
    "every", "all", "each", "it", "them", "this", "that", "these", "those",
    "same", "another", "one", "two", "few", "many", "some", "any",
    "everything", "anything", "nothing", "everyone", "anything at all",
    "here", "there", "again", "too", "also", "just", "only",
})


def _quoted(text: str) -> str:
    for quote in ('"', "'"):
        if quote in text:
            start = text.index(quote) + 1
            end = text.find(quote, start)
            if end > start:
                return text[start:end].strip()
    return ""


#: A chord written the way people write it: "ctrl+s", "ctrl-shift-p",
#: "win+shift+s". The whole chord is one capture group.
_CHORD = re.compile(
    r"(?i)\b((?:(?:ctrl|control|alt|shift|win|windows|meta|super)\s*[+\-]\s*)+"
    r"[a-z0-9]+(?:\s*[+\-]\s*[a-z0-9]+)*)\b")
#: The same, for a chord spelled as words: "ctrl shift p".
_SPACED_CHORD = re.compile(
    r"(?i)\b((?:(?:ctrl|control|alt|shift|win|windows|meta|super)\s+)+"
    r"[a-z0-9]+(?:\s+[a-z0-9]+)?)\b")
#: A bare key with no modifier, but only a name on this list. "press enter" is a
#: real instruction; "press the shortcut in the file name" is not, and would
#: otherwise turn the word "name" into a keystroke.
_BARE_KEYS = frozenset({
    "enter", "return", "tab", "escape", "esc", "space", "spacebar", "backspace",
    "delete", "del", "insert", "home", "end", "pageup", "pagedown", "up", "down",
    "left", "right", "printscreen", "capslock", "menu",
    *(f"f{n}" for n in range(1, 25)),
})

_KEY_ALIASES = {"control": "ctrl", "windows": "win", "meta": "win",
                "super": "win", "ctl": "ctrl", "return": "enter",
                "esc": "escape", "spacebar": "space", "del": "delete"}


def _keys(text: str) -> str:
    """The shortcut a sentence names, normalised to "ctrl+shift+p".

    Without this, "press ctrl+s" routes to `keyboard_hotkey` and arrives with no
    keys at all, so the handler can only ask the user what they meant — the
    capability was reachable by name and useless in practice.
    """
    match = _CHORD.search(text) or _SPACED_CHORD.search(text)
    if match:
        raw = re.split(r"[+\-\s]+", match.group(1).strip())
        parts = [_KEY_ALIASES.get(p.lower(), p.lower()) for p in raw if p]
        if len(parts) > 1:
            return "+".join(parts)

    for word in normalise(text).split():
        bare = _KEY_ALIASES.get(word, word)
        if bare in _BARE_KEYS:
            return bare
    return ""


def _verb_hint(text: str, candidates: Iterable[str]) -> str:
    """The first candidate word the sentence actually uses, if any.

    "unmute" is checked before "mute" so "unmute the sound" does not come back
    as "mute", which would do the exact opposite of what was asked.
    """
    words = normalise(text).split()
    for candidate in candidates:
        if candidate in words:
            return candidate
    for candidate in candidates:
        if any(w.startswith(candidate) for w in words if len(candidate) >= 4):
            return candidate
    return ""


def _args_hint(cap: Capability, text: str) -> str:
    """`action:on` is the only argument shape most capabilities accept."""
    if cap.args in ("", "-"):
        return ""
    head = cap.args.split("|")[0].split(",")[0].split(" ")[0].strip()
    if not head or " " in head:
        return ""
    lowered = f" {normalise(text)} "
    if f" {head} " in lowered or f" {head}s " in lowered:
        return head
    return ""


_APP_WORDS = re.compile(
    r"\b(notepad|calculator|calc|paint|photos|explorer|files|terminal|"
    r"powershell|cmd|command prompt|edge|chrome|firefox|browser|outlook|"
    r"settings|task manager|registry editor|vscode|code|spotify|word|excel|"
    r"powerpoint|teams|slack|discord|zoom|steam|obs|discord)\b",
    re.IGNORECASE,
)


def _app_name(text: str) -> str:
    match = _APP_WORDS.search(text)
    return match.group(1) if match else ""


# --------------------------------------------------------------------------
# compound requests
# --------------------------------------------------------------------------
_SPLIT_THEN = re.compile(
    r"\s*(?:\bthen\b|\bafter that\b|\bafterwards?\b|\baur phir\b|\bphir\b|"
    r"\biske baad\b|\bफिर\b|\bबाद\b|\bbadb)\s*", re.IGNORECASE)
_SPLIT_AND = re.compile(
    r"\s*(?:\band also\b|\band then\b|,\s*and\s+|\band\b|\baur\b|"
    r"\bऔर\b|\bbhi\b)\s*", re.IGNORECASE)


#: A part that is left dangling on a connective is not a request. "empty the bin
#: and then compress the heliosphere" splits into "empty the bin and" and
#: "compress the heliosphere"; the trailing "and" stops the first part routing.
_DANGLING = re.compile(
    r"(?i)\s*(?:\band\b|\bthen\b|\bafter that\b|\balso\b|\baur\b|"
    r"\bbhi\b|\bऔर\b|\bफिर\b|\bबाद\b)\s*$")
#: Punctuation left at the end of a split. "open the file, then rename it" must
#: not leave "open the file," with a comma the router has never seen.
_TRAILING_JUNK = re.compile(r"[\s,;:.!]+$")


def split_compound(text: str) -> list[str]:
    """Break "do A then B" into its parts.

    Only the connectives that clearly mean sequence. "and" is tried last and
    only if both halves still route, because "find and replace" is one action,
    not two.
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    parts = [p for p in _SPLIT_THEN.split(raw) if p and p.strip()]
    if len(parts) < 2:
        parts = [p for p in _SPLIT_AND.split(raw) if p and p.strip()]
    cleaned = [_TRAILING_JUNK.sub("", _DANGLING.sub("", p)).strip()
               for p in parts]
    return [p for p in cleaned if p]


def has_sequence_marker(text: str) -> bool:
    """True for "then" style connectors, false for a bare "and".

    "and" is ambiguous — "find and replace" is one action — while "then" is a
    promise of order and nothing else. Callers that need to decide whether a
    sentence is a genuine sequence must use this, not `len(split_compound(...))`,
    or they will break every phrase that happens to contain "and".
    """
    return bool(_SPLIT_THEN.search(str(text or "")))


def interpret_compound(text: str, threshold: float = _CONFIDENT) -> Plan | None:
    """Route a sequence of steps, keeping their order.

    Order is the whole point: "open the file, then rename it, then upload it"
    is a different outcome if the steps are reordered or one of them is dropped,
    so a compound is only claimed when *every* part routes.
    """
    parts = split_compound(text)
    if len(parts) < 2:
        return None
    plans: list[Plan] = []
    for part in parts:
        sub = try_interpret(part, threshold)
        if sub is None or not sub.steps:
            return None
        plans.append(sub)
    steps: list[Step] = []
    matched: list[str] = []
    for plan in plans:
        steps.extend(plan.steps)
        matched.extend(plan.matched)
    return Plan(text, steps, min(p.confidence for p in plans), matched,
                detect_language(text))


# --------------------------------------------------------------------------
# the search capability itself
# --------------------------------------------------------------------------
def search(query: str, limit: int = 20) -> list[tuple[Capability, str, float]]:
    """Rank capabilities against a query, for `ufo caps "..."`."""
    return match_capabilities(query, limit=limit)


def capability_index() -> Iterable[tuple[Capability, str]]:
    for cap in catalog.ALL:
        yield cap, cap.summary
