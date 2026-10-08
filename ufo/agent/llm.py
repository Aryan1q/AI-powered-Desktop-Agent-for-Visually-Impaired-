"""The LLM layer: providers, retries, structured output, cost accounting.

One entry point — `complete()` — for Anthropic, OpenAI, Gemini, Ollama and any
OpenAI-compatible endpoint (LM Studio, vLLM, OpenRouter, Groq). Three things
make it more than a thin SDK wrapper:

  * **Never guess JSON.** `parse_json` repairs fences, prose wrappers, trailing
    commas and truncated objects rather than failing the step.
  * **Knows what a retry is worth.** 429 with a quota marker is fatal (stop and
    tell the user to change key or model); 429 without one is transient.
  * **Accountable.** Every call is charged to a `RunBudget`, so a loop cannot
    quietly spend money without hitting a ceiling.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..config import LLMConfig
from ..errors import (LLMError, LLMResponseError, LLMTimeout,
                      ModelNotAvailable, NoLLMConfigured, QuotaExceeded)
from ..logging_setup import get_logger, record
from ..utils import text as textutil

log = get_logger(__name__)

def discover_ollama_model(base_url: str = "", timeout: float = 4.0) -> str:
    """Pick a model from the ones Ollama actually has. "" when it cannot tell.

    A hardcoded default is a guess about one machine, and the guess is wrong on
    every machine but the one it was written on — the symptom being a run that
    accepts a spoken instruction, confirms it, and then dies on a 404. Asking
    the server removes the guess.

    Vision models are preferred: the agent reads a full screenshot every step,
    and a text-only model answers that with a confident guess. Falls back to
    whatever exists so a text-only setup still runs, and returns "" rather than
    raising so the caller can give one accurate message.
    """
    import json
    import urllib.request

    url = (base_url or _LOCAL_BASE_URLS.get("ollama", "")).rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    if not url:
        return ""
    try:
        with urllib.request.urlopen(f"{url}/api/tags", timeout=timeout) as resp:
            tags = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        log.debug("could not list Ollama models: %s", e)
        return ""

    entries = [e for e in (tags.get("models") or []) if e.get("name")]
    if not entries:
        return ""
    for entry in entries:
        if "vision" in (entry.get("capabilities") or []):
            return str(entry["name"])
    return str(entries[0]["name"])


DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-5",
    "openai": "gpt-4o",
    "gemini": "gemini-3.7-flash",
    # Ollama's default is resolved from what the server actually has, not a
    # fixed name. See `discover_ollama_model`. A hardcoded tag here is a guess
    # about one machine, and being wrong means every run ends in a 404.
    "ollama": "",
    "lmstudio": "local-model",
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "anthropic/claude-sonnet-4.5",
}

# (input $/Mtok, output $/Mtok) — used only for the run budget, in USD.
# These are approximate public list prices and exist to make the cost budget
# roughly right, not to be an invoice. Override per model with
# `llm.pricing` in your config if a provider disagrees with these numbers.
PRICING = {
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gemini-3.8-flash": (0.30, 2.50),
    "gemini-3.7-flash": (0.30, 2.50),
    "gemini-3.6-flash": (0.30, 2.50),
    "gemini-3.5-flash": (0.30, 2.50),
    "gemini-3.5-flash-lite": (0.075, 0.30),
    "gemini-3.1-flash-lite": (0.075, 0.30),
    "gemini-3.5-transcribe": (0.30, 0.30),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.075, 0.30),
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.0-flash": (0.10, 0.40),
    "default": (1.0, 3.0),
}

ENV_KEYS = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "groq": ("GROQ_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
}

_OPENAI_COMPATIBLE = {"openai", "ollama", "lmstudio", "groq", "openrouter", "custom"}

#: This install is Ollama-only. Nothing else gets to hold a conversation with
#: the user: not a cloud API, and not another local runtime either. A second
#: local backend is only a smaller version of the same mistake — a way for the
#: model to end up somewhere nobody chose for it. Ollama on this machine is the
#: only endpoint, so this is a set of one on purpose.
_LOCAL_PROVIDERS = {"ollama"}

# Local servers the OpenAI SDK would otherwise not know about. Without this,
# `provider: ollama` falls through to the SDK's own default of
# https://api.openai.com/v1 — so a screenshot intended for a model running on
# this machine would be POSTed to OpenAI instead, or fail with an auth error
# that looks like a misconfigured key.
_LOCAL_BASE_URLS = {
    "ollama": "http://127.0.0.1:11434/v1",
}

_QA_FENCE = re.compile(r"```(?:json|jsonc)?", re.IGNORECASE)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


# --------------------------------------------------------------------------
@dataclass
class Response:
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration: float = 0.0
    attempts: int = 1

    def json(self) -> Any:
        return parse_json(self.text)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider, "model": self.model,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6), "duration": round(self.duration, 2),
            "attempts": self.attempts,
        }


@dataclass
class Client:
    """A configured provider. Cheap to construct; call it in a loop."""

    config: LLMConfig
    api_key: str = ""
    base_url: str = ""
    _last_good: str = field(default="", init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        provider = (self.config.provider or "ollama").strip().lower()
        if provider not in DEFAULT_MODELS and provider not in _OPENAI_COMPATIBLE \
                and provider not in ENV_KEYS:
            raise NoLLMConfigured(
                f"Unknown LLM provider {provider!r}. Known: "
                f"{sorted(set(DEFAULT_MODELS) | set(_OPENAI_COMPATIBLE) | set(ENV_KEYS))}"
            )
        self.provider = provider
        self.base_url = (self.config.base_url or "").rstrip("/")
        if not self.base_url:
            self.base_url = _LOCAL_BASE_URLS.get(provider, "")
        if self.base_url and provider in _LOCAL_BASE_URLS:
            log.info("Using the local %s server at %s", provider, self.base_url)

        # The address has to be known before the model is resolved: an empty
        # Ollama model is discovered by asking the server what it has.
        self.model = (self.config.model or DEFAULT_MODELS.get(provider, "")).strip()
        if not self.model and provider == "ollama":
            self.model = discover_ollama_model(self.base_url)
        if not self.model:
            hint = (f"or LLM_MODEL (defaults: {DEFAULT_MODELS.get(provider)})."
                    if DEFAULT_MODELS.get(provider) else
                    "— for Ollama the model is taken from what the server has, "
                    "so the server has to be up and have a model pulled:  "
                    "ollama serve, then  ollama pull gemma3:4b")
            raise NoLLMConfigured(
                f"No model configured for provider {provider!r}. Set llm.model {hint}")
        if provider == "gemini":
            self.model = self.model.removeprefix("models/").strip()
        if not self.api_key:
            for var in ENV_KEYS.get(provider, ()):
                if os.getenv(var):
                    self.api_key = os.environ[var]
                    break
        self._require_ready()

    def _require_ready(self) -> None:
        # `offline` is the switch that keeps this install local. It is checked
        # before anything else so a cloud provider cannot be reached even by
        # accident: a config left over from an earlier cloud setup still gets
        # refused rather than quietly sending screenshots off the machine.
        if self.config.offline and self.provider not in _LOCAL_PROVIDERS:
            if self.provider in ("lmstudio", "custom"):
                why = ("it is not Ollama, and this install only talks to Ollama "
                       "on this machine")
            else:
                why = "it sends data to a remote service"
            raise NoLLMConfigured(
                f"llm.offline is on, so only Ollama is allowed, but the provider "
                f"is {self.provider!r}: {why}. Use "
                f"{sorted(_LOCAL_PROVIDERS)} — "
                f"e.g. `ufo config set llm.provider ollama` — or turn offline "
                f"off with `ufo config set llm.offline false`."
            )
        if self.provider in _OPENAI_COMPATIBLE and self.provider != "openai":
            return
        if self.provider == "openai" and self.api_key:
            return
        if self.provider in ENV_KEYS and self.api_key:
            return
        if self.provider == "gemini" and not self.api_key:
            raise NoLLMConfigured(
                "GEMINI_API_KEY (or GOOGLE_API_KEY) is not set. In PowerShell:\n"
                '  $env:GEMINI_API_KEY="your-key"'
            )
        if self.provider in ("anthropic", "openai") and not self.api_key:
            var = ENV_KEYS[self.provider][0]
            raise NoLLMConfigured(
                f"{var} is not set. In PowerShell:\n  ${var}=\"your-key\""
            )

    # -- helpers --
    def order(self) -> list[str]:
        """Models to try, most-likely-to-work first."""
        models = [self._last_good, self.model, *self.config.fallbacks]
        return [m for m in dict.fromkeys(models) if m and m.strip()]

    def with_model(self, model: str) -> "Client":
        clone = Client(self.config, self.api_key, self.base_url)
        clone.model = model
        return clone

    # -- the call --
    def complete(self, system: str, user_text: str,
                 image_b64: str | None = None,
                 charge: Callable[[float], None] | None = None) -> Response:
        """One model call, with fallback models, retries and cost accounting."""
        if self.config.redact_screenshots and image_b64:
            image_b64 = None

        delays = self._delays()
        last_error: Exception | None = None
        started = time.time()
        attempts = 0

        for model in self.order():
            for attempt in range(len(delays) + 1):
                attempts += 1
                try:
                    response = self._dispatch(model, system, user_text, image_b64)
                    with self._lock:
                        self._last_good = model
                    if charge:
                        charge(response.cost_usd)
                    record("llm", provider=self.provider, model=model,
                           in_tokens=response.input_tokens, out_tokens=response.output_tokens,
                           cost=round(response.cost_usd, 6), attempt=attempts)
                    return response
                except QuotaExceeded:
                    raise  # a key/model switch is the only fix; never retry
                except ModelNotAvailable:
                    # The model is not on the server. Waiting cannot install it,
                    # so this re-raises before the backoff rather than sitting
                    # through four sleeps for a reply that cannot change.
                    raise
                except (LLMTimeout, LLMError) as e:
                    last_error = e
                    msg = str(e)
                    if any(code in msg for code in ("HTTP 400", "HTTP 401", "HTTP 403",
                                                    "HTTP 404", "invalid_api_key",
                                                    "API key not valid")):
                        raise
                    if attempt < len(delays):
                        wait = delays[attempt]
                        log.warning("%s failed (%s); retrying in %.0fs", model,
                                    textutil.truncate(msg, 120), wait)
                        time.sleep(wait)
                        continue
                    break  # try the next model
        raise LLMError(
            f"All configured models failed for provider {self.provider!r}. "
            f"Last error: {last_error}"
        ) from last_error

    def _delays(self) -> list[float]:
        from ..utils import retry as retryutil

        return retryutil.backoff_delays(2.0, 2.0, 30.0, int(self.config.max_retries))

    def _dispatch(self, model: str, system: str, user_text: str,
                  image_b64: str | None) -> Response:
        started = time.time()
        if self.provider == "anthropic":
            return self._anthropic(model, system, user_text, image_b64, started)
        if self.provider == "gemini":
            return self._gemini(model, system, user_text, image_b64, started)
        if self.provider in _OPENAI_COMPATIBLE:
            return self._openai_compatible(model, system, user_text, image_b64, started)
        raise NoLLMConfigured(f"No transport for provider {self.provider!r}.")

    # -- providers --
    def _anthropic(self, model: str, system: str, user_text: str,
                   image_b64: str | None, started: float) -> Response:
        import anthropic  # type: ignore

        client = anthropic.Anthropic(api_key=self.api_key or None,
                                     timeout=self.config.timeout,
                                     max_retries=0)
        content: list[dict] = []
        if image_b64:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": image_b64}})
        content.append({"type": "text", "text": user_text})
        try:
            resp = client.messages.create(
                model=model, max_tokens=int(self.config.max_tokens), system=system,
                temperature=float(self.config.temperature), messages=[
                    {"role": "user", "content": content}],
            )
        except anthropic.APIStatusError as e:
            raise _status_error(e, e.status_code, _body(e)) from e
        except anthropic.APITimeoutError as e:
            raise LLMTimeout(str(e)) from e
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        usage = getattr(resp, "usage", None)
        return self._finish(text, model, started,
                            getattr(usage, "input_tokens", 0),
                            getattr(usage, "output_tokens", 0))

    def _openai_compatible(self, model: str, system: str, user_text: str,
                           image_b64: str | None, started: float) -> Response:
        from openai import OpenAI  # type: ignore

        kwargs: dict[str, Any] = {"timeout": self.config.timeout, "max_retries": 0}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        elif self.provider in _LOCAL_BASE_URLS:
            # The SDK refuses to construct without a key, even for a server
            # that ignores one. Ollama and LM Studio only check that the header
            # is present, so a placeholder unblocks the local path. It is a
            # constant, not a secret, and is never sent anywhere but localhost.
            kwargs["api_key"] = _LOCAL_BASE_URLS[self.provider].split("//", 1)[-1]
        if self.base_url:
            kwargs["base_url"] = self.base_url
        client = OpenAI(**kwargs)

        content: list[dict] = [{"type": "text", "text": user_text}]
        if image_b64:
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_b64}"}})
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": content}],
            "max_tokens": int(self.config.max_tokens),
        }
        if self.config.temperature is not None:
            payload["temperature"] = float(self.config.temperature)
        if self.provider in ("ollama", "lmstudio"):
            payload.pop("max_tokens", None)   # not all local servers accept it
            # `format` is Ollama's structured-output switch, not an OpenAI
            # parameter. Passing it as a top-level kwarg makes the SDK raise
            # TypeError before any request is sent, so it has to travel in
            # extra_body, which the SDK forwards verbatim.
            payload["extra_body"] = {"format": "json"}
        try:
            resp = client.chat.completions.create(**payload)
        except Exception as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code:
                raise _status_error(e, code, _body(e)) from e
            raise LLMError(f"{type(e).__name__}: {e}") from e
        text = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        return self._finish(text, model, started,
                            getattr(usage, "prompt_tokens", 0),
                            getattr(usage, "completion_tokens", 0))

    def _gemini(self, model: str, system: str, user_text: str,
                image_b64: str | None, started: float) -> Response:
        parts: list[dict] = []
        if image_b64:
            parts.append({"inline_data": {"mime_type": "image/png", "data": image_b64}})
        parts.append({"text": user_text})
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "maxOutputTokens": int(self.config.max_tokens),
                "temperature": float(self.config.temperature),
                "responseMimeType": "application/json",
            },
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        data = _post_json(url, payload, {"x-goog-api-key": self.api_key},
                          self.config.timeout, self._delays())

        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback", {})
            raise LLMResponseError(
                "Gemini returned no candidates: " + textutil.truncate(str(feedback or data), 300)
            )
        body = candidates[0].get("content") or {}
        text = "".join(p.get("text", "") for p in body.get("parts") or []
                       if isinstance(p, dict))
        if not text.strip():
            raise LLMResponseError("Gemini returned an empty response.")
        usage = data.get("usageMetadata") or {}
        return self._finish(text, model, started,
                            int(usage.get("promptTokenCount", 0)),
                            int(usage.get("candidatesTokenCount", 0)))

    def _finish(self, text: str, model: str, started: float,
                in_tokens: int, out_tokens: int) -> Response:
        return Response(
            text=text, model=model, provider=self.provider,
            input_tokens=int(in_tokens or 0), output_tokens=int(out_tokens or 0),
            cost_usd=estimate_cost(model, int(in_tokens or 0), int(out_tokens or 0)),
            duration=time.time() - started,
        )


# --------------------------------------------------------------------------
def _body(e: Exception) -> str:
    for attr in ("body", "message", "text"):
        value = getattr(e, attr, None)
        if value:
            return str(value)[:1000]
    return str(e)[:1000]


def _status_error(e: Exception, code: int, detail: str) -> LLMError:
    lower = detail.lower()
    quota_markers = ("quota", "resource_exhausted", "resource exhausted", "billing",
                     "limit: 0", "free_tier", "insufficient_quota", "rate limit")
    # A model the server does not have. Checked before the generic 404 case
    # because the fix is specific and the message has to carry it: the common
    # cause is a model that was never pulled, and no amount of retrying pulls
    # one.
    not_found_markers = ("model", "not found", "not_found", "try pulling")
    if code == 404 and "model" in lower and any(m in lower for m in not_found_markers):
        name = _missing_model_name(detail) or "the configured model"
        return ModelNotAvailable(
            f"HTTP 404: the model {name!r} is not on the Ollama server. "
            f"Pull it with:  ollama pull {name}   "
            f"(or pick an installed one:  ollama list)"
        )
    if code == 429 and any(m in lower for m in quota_markers):
        return QuotaExceeded(f"HTTP 429 quota/billing exhausted: {textutil.truncate(detail, 300)}")
    if code == 429:
        return LLMError(f"HTTP 429 rate limited: {textutil.truncate(detail, 200)}")
    if code in (408, 500, 502, 503, 504, 522, 524):
        return LLMError(f"HTTP {code}: {textutil.truncate(detail, 200)}")
    if code in (400, 401, 403, 404):
        return LLMError(f"HTTP {code}: {textutil.truncate(detail, 400)}")
    return LLMError(f"HTTP {code}: {textutil.truncate(detail, 400)}")


def _missing_model_name(detail: str) -> str:
    """Pull the model name out of `model 'gemma3:4b' not found`."""
    m = re.search(r"model\s+'([^']+)'", detail, re.IGNORECASE)
    return m.group(1) if m else ""


def _post_json(url: str, payload: dict, headers: dict, timeout: float,
               delays: list[float]) -> dict:
    import urllib.error
    import urllib.request

    body = json.dumps(payload).encode("utf-8")
    for attempt in range(len(delays) + 1):
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json", **headers},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:1000]
            if attempt >= len(delays):
                raise _status_error(e, e.code, detail) from e
            time.sleep(delays[attempt])
        except urllib.error.URLError as e:
            if attempt >= len(delays):
                raise LLMError(f"Network error: {e.reason}") from e
            time.sleep(delays[attempt])
        except json.JSONDecodeError as e:
            raise LLMResponseError(f"Server returned invalid JSON: {e}") from e
    raise LLMError("Request failed after all retries.")


def _pricing_for(model: str) -> tuple[float, float]:
    """Longest-prefix price lookup.

    Providers append suffixes (`gemini-2.5-flash-latest`, dated snapshots,
    `claude-sonnet-4-5-20250929`). Exact matching alone would send every one
    of those to the punitive default rate and trip the cost budget on a
    cheap model.
    """
    name = (model or "").strip().lower()
    if name in PRICING:
        return PRICING[name]
    best: tuple[str, tuple[float, float]] | None = None
    for key, price in PRICING.items():
        if key != "default" and name.startswith(key):
            if best is None or len(key) > len(best[0]):
                best = (key, price)
    return best[1] if best else PRICING["default"]


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    per_in, per_out = _pricing_for(model)
    return (input_tokens / 1e6) * per_in + (output_tokens / 1e6) * per_out


# --------------------------------------------------------------------------
# JSON extraction
# --------------------------------------------------------------------------
def parse_json(text: str, *, allow_list: bool = False) -> Any:
    """Pull a usable JSON object/array out of a model reply.

    Handles fenced blocks, leading prose, Python-style literals, trailing
    commas, and objects the provider truncated at max_tokens (by closing the
    open brackets). A model that wrote *almost* valid JSON still gets a
    chance; a model that wrote nothing usable raises.
    """
    if not isinstance(text, str) or not text.strip():
        raise LLMResponseError("The model returned an empty response.")

    cleaned = _QA_FENCE.sub("", text).replace("```", "").strip()

    for candidate in (cleaned, _extract_balanced(cleaned)):
        if candidate is None:
            continue
        for attempt in (_loads, _loads_relaxed, _loads_python, _loads_truncated):
            try:
                value = attempt(candidate)
            except Exception:
                continue
            if isinstance(value, (dict, list)) and (allow_list or isinstance(value, dict)):
                return value
    raise LLMResponseError(
        f"Could not find usable JSON in the model reply: {textutil.truncate(cleaned, 300)}"
    )


def _loads(text: str) -> Any:
    return json.loads(text)


def _loads_relaxed(text: str) -> Any:
    repaired = _TRAILING_COMMA.sub(r"\1", text)
    repaired = re.sub(r"\bNaN\b|\bInfinity\b|\b-Un?defined\b", "null", repaired)
    return json.loads(repaired)


def _loads_python(text: str) -> Any:
    """Last resort for Python-style literals: single quotes, True/False/None.

    `ast.literal_eval` only evaluates literals, so this cannot execute
    anything from a model reply.
    """
    import ast

    return ast.literal_eval(text)


def _loads_truncated(text: str) -> Any:
    """Close a JSON object the provider cut off mid-stream."""
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack and stack[-1] == ch:
                stack.pop()
    if not stack:
        raise ValueError("nothing to close")
    body = text.rstrip().rstrip(",")
    if in_string:
        body += '"'
    if body.endswith(":"):
        body = body[:-1]
    elif body.endswith(","):
        body = body[:-1]
    # Drop a dangling key with no value, e.g. {"thought": "x",
    body = re.sub(r'[,{]\s*"[^"]*"\s*:\s*(?=[,}])', "", body)
    body = re.sub(r",\s*$", "", body)
    return json.loads(body + "".join(reversed(stack)))


def _extract_balanced(text: str) -> str | None:
    """The first balanced {...} or [...] block, ignoring braces in strings."""
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        while start != -1:
            depth, in_string, escaped = 0, False, False
            for i in range(start, len(text)):
                ch = text[i]
                if in_string:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        return text[start:i + 1]
            start = text.find(opener, start + 1)
    return None


def extract_all_objects(text: str) -> list[dict]:
    """Every top-level JSON object in a reply — used for batched requests."""
    out: list[dict] = []
    i = 0
    while True:
        start = text.find("{", i)
        if start == -1:
            break
        block = _extract_balanced(text[start:])
        if not block:
            break
        try:
            value = json.loads(block)
            if isinstance(value, dict):
                out.append(value)
        except Exception:
            try:
                value = _loads_relaxed(block)
                if isinstance(value, dict):
                    out.append(value)
            except Exception:
                pass
        i = start + max(1, len(block))
    return out


def require(prompt: str) -> str:
    """Build a strict system prompt that demands one JSON object and nothing else."""
    return (
        f"{prompt}\n\n"
        "Reply with EXACTLY one JSON object and nothing else. No prose before "
        "or after it, no markdown fences, no trailing commentary. Every key must "
        "be present. Unknown keys are ignored, missing keys are an error."
    )


def as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [value]
    return [value]


def dedupe(items: Iterable[str]) -> list[str]:
    seen, out = set(), []
    for item in items:
        key = textutil.collapse_ws(str(item)).lower()
        if key and key not in seen:
            seen.add(key)
            out.append(str(item))
    return out
