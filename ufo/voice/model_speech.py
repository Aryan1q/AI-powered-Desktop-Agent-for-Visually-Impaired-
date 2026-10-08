"""Model-backed speech: Gemini TTS for the voice, Gemini Transcribe for the ears.

Both are optional. When no key is configured, no network is available, or the
call fails, `pick_tts` / `pick_stt` fall back to the local engines — this
module never becomes a hard dependency of the voice layer.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
import wave
from typing import Any

from ..logging_setup import get_logger

log = get_logger(__name__)

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"

# Voices Google exposes for the TTS models, best JARVIS-ish first.
# "Puck" is bright, "Charon" is deep and measured, "Kore" is level-headed.
VOICES = {
    "jarvis": "Charon",
    "charon": "Charon",
    "puck": "Puck",
    "kore": "Kore",
    "fenrir": "Fenrir",
    "aoede": "Aoede",
    "default": "Kore",
}

TTS_MODELS = ("gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview",
              "gemini-2.5-flash-preview-tts")
TRANSCRIBE_MODELS = ("gemini-3.5-transcribe",)


class SpeechUnavailable(RuntimeError):
    """No usable model-backed speech path: missing key, no network, or refused."""


# --------------------------------------------------------------------------
def _api_key() -> str:
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        if os.getenv(var):
            return os.getenv(var, "")
    raise SpeechUnavailable("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set.")


def _post(model: str, payload: dict, timeout: float = 60.0) -> dict:
    url = f"{ENDPOINT}/{model}:generateContent"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"x-goog-api-key": _api_key(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        raise SpeechUnavailable(f"{model}: HTTP {e.code} {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SpeechUnavailable(f"{model}: {e}") from e


def _first_model(candidates: tuple[str, ...], probe) -> str:
    for model in candidates:
        try:
            if probe(model):
                return model
        except SpeechUnavailable:
            continue
    raise SpeechUnavailable(f"none of {', '.join(candidates)} is available")


# --------------------------------------------------------------------------
def speak(text: str, *, voice: str = "jarvis", model: str = "",
          speed: float = 1.0) -> bytes:
    """Synthesise `text` and return WAV bytes."""
    if not text.strip():
        return b""

    def probe(name: str) -> bool:
        _post(name, {"contents": [{"parts": [{"text": " "}]}],
                "generationConfig": {"responseModalities": ["AUDIO"]}},
              timeout=25.0)
        return True

    if not model:
        model = _first_model(TTS_MODELS, probe)
    voice_name = VOICES.get(voice.lower(), voice) if voice else VOICES["default"]

    payload = {
        "contents": [{"parts": [{"text": text.strip()}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice_name},
                },
            },
        },
    }
    if speed and speed != 1.0:
        payload["generationConfig"]["speechConfig"]["speechConfig"] = {
            "speed": max(0.25, min(4.0, float(speed))),
        }

    data = _post(model, payload, timeout=90.0)
    for part in (data.get("candidates") or [{}])[0].get("content", {}).get("parts", []):
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            return base64.b64decode(inline["data"])
    raise SpeechUnavailable(f"{model} returned no audio data.")


def play_wav(data: bytes) -> bool:
    """Play WAV bytes through the default Windows audio device."""
    if not data:
        return False
    try:
        import winsound  # type: ignore
    except ImportError:
        return False
    with wave.open(_BytesIO(data), "rb") as wav:
        winsound.PlaySound(wav.getnframes() * 2, wav.getframerate(),
                           winsound.SND_MEMORY)
    return True


def _BytesIO(data: bytes):  # noqa: N802 - tiny shim, avoids a top-level import
    import io

    return io.BytesIO(data)


def save_wav(data: bytes, path: str) -> str:
    with open(path, "wb") as fh:
        fh.write(data)
    return path


# --------------------------------------------------------------------------
def _wav_from_pcm(pcm: bytes, rate: int = 16_000, channels: int = 1,
                  width: int = 2) -> bytes:
    """Wrap raw PCM in a WAV header so anything can play it."""
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def transcribe_wav(wav_bytes: bytes, *, model: str = "", language: str = "en-US") -> str:
    """Transcribe a complete WAV recording."""
    if not wav_bytes:
        return ""

    def probe(name: str) -> bool:
        _post(name, {"contents": [{"parts": [{"text": "ping"}]}]}, timeout=25.0)
        return True

    if not model:
        model = _first_model(TRANSCRIBE_MODELS, probe)

    payload = {
        "contents": [{
            "parts": [{
                "inline_data": {
                    "mime_type": "audio/wav",
                    "data": base64.b64encode(wav_bytes).decode("ascii"),
                }
            }]
        }],
        "generationConfig": {"responseMimeType": "text/plain"},
    }
    if language:
        payload["systemInstruction"] = {
            "parts": [{"text":
                      f"Transcribe this audio verbatim in {language}. "
                      f"Return only the spoken words. No speaker labels, no "
                      f"punctuation commentary, no explanation. If the audio "
                      f"contains no speech, return an empty response."}]}
    data = _post(model, payload, timeout=120.0)
    chunks: list[str] = []
    for candidate in data.get("candidates") or []:
        for part in candidate.get("content", {}).get("parts", []):
            if "text" in part:
                chunks.append(part["text"])
    return re.sub(r"\s{2,}", " ", "".join(chunks)).strip()


def transcribe_pcm(pcm: bytes, rate: int = 16_000, **kwargs: Any) -> str:
    return transcribe_wav(_wav_from_pcm(pcm, rate=rate), **kwargs)
