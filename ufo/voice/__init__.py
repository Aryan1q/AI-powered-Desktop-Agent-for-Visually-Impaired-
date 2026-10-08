"""Voice: text-to-speech, speech-to-text, global hotkeys, OS notifications.

Every backend is optional. If a package is missing the class still constructs
and degrades to a console/print no-op, so the agent never hard-fails just
because `pyttsx3` or `speech_recognition` is not installed.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import threading
from typing import Callable

from ..utils import text as textutil
from .persona import Persona, get_persona

# --------------------------------------------------------------------------
# TTS
# --------------------------------------------------------------------------
class TTSBackend:
    name = "none"
    available = False

    def speak(self, text: str) -> bool:  # pragma: no cover - interface
        return False

    def stop(self) -> None:
        pass

    def set_rate(self, rate: int) -> None:
        pass

    def set_volume(self, volume: float) -> None:
        pass


class ConsoleTTS(TTSBackend):
    """Always-available fallback: print what would be said."""

    name = "console"
    available = True

    def speak(self, text: str) -> bool:
        print(f"[SAY] {textutil.collapse_ws(text)}")
        return True


class Pyttsx3TTS(TTSBackend):
    name = "pyttsx3"
    _engine = None
    _lock = threading.Lock()

    def __init__(self, rate: int = 175, volume: float = 1.0):
        try:
            import pyttsx3  # type: ignore
        except ImportError:
            raise RuntimeError("pyttsx3 is not installed")
        self._rate, self._volume = rate, volume
        with Pyttsx3TTS._lock:
            if Pyttsx3TTS._engine is None:
                Pyttsx3TTS._engine = pyttsx3.init()
        self.available = True
        self.set_rate(rate)
        self.set_volume(volume)

    def speak(self, text: str) -> bool:
        text = textutil.collapse_ws(text)
        if not text:
            return False
        try:
            with Pyttsx3TTS._lock:
                engine = Pyttsx3TTS._engine
                if engine is None:
                    return False
                engine.setProperty("rate", self._rate)
                engine.say(text)
                engine.runAndWait()
            return True
        except Exception:
            return False

    def stop(self) -> None:
        try:
            with Pyttsx3TTS._lock:
                if Pyttsx3TTS._engine is not None:
                    Pyttsx3TTS._engine.stop()
        except Exception:
            pass


class PowerShellTTS(TTSBackend):
    """Uses the built-in Windows speech engine — no pip package needed."""

    name = "powershell"
    available = platform.system() == "Windows"

    def __init__(self, rate: int = 0, volume: int = 100):
        self.rate = max(-10, min(10, (rate - 175) // 15))
        self.volume = max(0, min(100, int(volume * 100)))

    def speak(self, text: str) -> bool:
        text = textutil.collapse_ws(text)
        if not text or platform.system() != "Windows":
            return False
        escaped = text.replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.Rate={self.rate}; $s.Volume={self.volume}; "
            f"$s.Speak('{escaped}')"
        )
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            return False


class GeminiTTS(TTSBackend):
    """Model-backed speech. Sounds far better than the local Windows engine,
    which is what makes a persona land. Needs GEMINI_API_KEY and network; the
    picker falls back automatically when either is missing."""

    name = "gemini"

    def __init__(self, model: str = "", voice: str = "jarvis",
                 rate: int = 175, volume: float = 1.0):
        self.model = model
        self.voice = voice
        self.rate = rate
        self.volume = volume
        self.available = bool(os.getenv("GEMINI_API_KEY")
                              or os.getenv("GOOGLE_API_KEY"))
        self._stop = threading.Event()

    def speak(self, text: str) -> bool:
        from . import model_speech

        text = textutil.collapse_ws(text)
        if not text or not self.available:
            return False
        # 175 wpm is a normal speaking rate; -1 means one notch slower.
        speed = max(0.5, min(2.0, 0.85 + (self.rate - 175) / 300.0))
        try:
            audio = model_speech.speak(text, voice=self.voice,
                                       model=self.model, speed=speed)
        except model_speech.SpeechUnavailable as e:
            log.warning("Gemini TTS unavailable: %s", e)
            self.available = False
            return False
        if not audio:
            return False
        ok = model_speech.play_wav(audio)
        self.last_audio = audio
        return ok

    def stop(self) -> None:
        self._stop.set()

    def set_rate(self, rate: int) -> None:
        self.rate = rate

    def set_volume(self, volume: float) -> None:
        self.volume = volume


class Narrator:
    """Serialised speech: one utterance at a time, never blocking the loop."""

    def __init__(self, backend: TTSBackend | None = None, enabled: bool = True,
                 rate: int = 175, volume: float = 1.0,
                 console: bool = True, max_chars: int = 400,
                 persona: "Persona | None" = None, chime: bool | None = None):
        self.console = console
        self.enabled = enabled
        self.max_chars = max_chars
        self.backend = backend or pick_tts(rate=rate, volume=volume)
        self._queue: list[str] = []
        self._lock = threading.Lock()
        self.spoken: list[str] = []
        self.persona = persona or Persona()
        if chime is not None:
            self.persona.chime = chime

    @property
    def backend_name(self) -> str:
        return getattr(self.backend, "name", "none")

    def say(self, text: str, *, force: bool = False, chime: bool = False) -> str:
        text = textutil.collapse_ws(text)[: self.max_chars]
        if not text:
            return text
        if self.console:
            print(f"[SAY] {text}")
        self.spoken.append(text)
        if chime and self.persona.enabled and self.persona.chime:
            beep_pattern("ok")
        if self.enabled and self.backend and self.backend.available:
            try:
                self.backend.speak(text)
            except Exception:
                pass
        return text

    def say_async(self, text: str) -> None:
        threading.Thread(target=self.say, args=(text,), daemon=True).start()

    def stop(self) -> None:
        if self.backend:
            try:
                self.backend.stop()
            except Exception:
                pass


def pick_tts(rate: int = 175, volume: float = 1.0,
             prefer_model: bool = False, model: str = "",
             voice: str = "jarvis") -> TTSBackend:
    """Best available speech backend.

    Local engines only. A model-backed voice would be nicer, but it means
    sending every spoken line to a hosted service, so it is skipped entirely
    while `llm.offline` is on — which is the default for this install. Anything
    that was going to leave the machine is better absent than merely unused.
    """
    from ..config import active

    if getattr(active().llm, "offline", True) and prefer_model:
        prefer_model = False
    if prefer_model:
        backend = GeminiTTS(model=model, voice=voice, rate=rate, volume=volume)
        if backend.available:
            return backend
    for factory in (Pyttsx3TTS, PowerShellTTS):
        try:
            backend = factory(rate=rate, volume=volume)  # type: ignore[call-arg]
        except Exception:
            continue
        if getattr(backend, "available", False):
            return backend
    if not getattr(active().llm, "offline", True) and \
            (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
        return GeminiTTS(model=model, voice=voice, rate=rate, volume=volume)
    return ConsoleTTS()


# --------------------------------------------------------------------------
# STT
# --------------------------------------------------------------------------
class STTBackend:
    name = "none"
    available = False

    def listen(self, timeout: float = 8.0) -> str:  # pragma: no cover - needs a mic
        return ""

    def listen_once(self, timeout: float = 8.0) -> str:
        return self.listen(timeout)


class UnavailableSTT(STTBackend):
    def __init__(self, reason: str = "no speech-to-text backend installed"):
        self.reason = reason

    def listen(self, timeout: float = 8.0) -> str:
        return ""


class SpeechRecognitionSTT(STTBackend):
    """Wraps `speech_recognition` using only local recognition.

    `recognize_google` is deliberately not offered: it uploads the microphone
    audio to Google's endpoint, which this local-only install must not do.
    `recognize_whisper` runs on this machine; with neither installed the
    picker falls back to faster-whisper.
    """

    name = "speech_recognition"
    _SR = None
    _recognizer = None

    def __init__(self):
        try:
            import speech_recognition as sr  # type: ignore
        except ImportError as e:
            raise RuntimeError("speech_recognition is not installed") from e
        SpeechRecognitionSTT._SR = sr
        if SpeechRecognitionSTT._recognizer is None:
            SpeechRecognitionSTT._recognizer = sr.Recognizer()
        self.available = True

    def listen(self, timeout: float = 8.0) -> str:
        sr = SpeechRecognitionSTT._SR
        r = SpeechRecognitionSTT._recognizer
        if sr is None or r is None:
            return ""
        with sr.Microphone() as source:
            r.pause_threshold = 0.8
            try:
                r.adjust_for_ambient_noise(source, duration=0.4)
            except Exception:
                pass
            try:
                audio = r.listen(source, timeout=timeout, phrase_time_limit=max(4.0, timeout))
            except Exception:
                return ""
        for method in ("recognize_whisper",):
            fn = getattr(r, method, None)
            if fn is None:
                continue
            try:
                return textutil.collapse_ws(fn(audio))
            except Exception:
                continue
        return ""


_WHISPER_LANGUAGES = {
    "af", "am", "ar", "as", "az", "ba", "be", "bg", "bn", "bo", "br", "bs", "ca",
    "cs", "cy", "da", "de", "el", "en", "es", "et", "eu", "fa", "fi", "fo", "fr",
    "gl", "gu", "ha", "haw", "he", "hi", "hr", "ht", "hu", "hy", "id", "is", "it",
    "ja", "jw", "ka", "kk", "km", "kn", "ko", "la", "lb", "ln", "lo", "lt", "lv",
    "mg", "mi", "mk", "ml", "mn", "mr", "ms", "mt", "my", "ne", "nl", "nn", "no",
    "oc", "pa", "pl", "ps", "pt", "ro", "ru", "sa", "sd", "si", "sk", "sl", "sn",
    "so", "sq", "sr", "su", "sv", "sw", "ta", "te", "tg", "th", "tk", "tl", "tr",
    "tt", "uk", "ur", "uz", "vi", "yi", "yo", "zh", "yue",
}


def _whisper_language(language: str) -> str | None:
    """Reduce a BCP-47 tag to a code faster-whisper accepts.

    The phone page and the config both speak `en-US`, but the model only takes
    `en`. Passing the tag straight through made every local-mode instruction
    fail with "'en-US' is not a valid language code", so a mis-spoken locale
    became a hard 400 rather than a recognised request.
    """
    tag = str(language or "").strip().lower().replace("_", "-")
    if not tag:
        return None
    if tag in _WHISPER_LANGUAGES:
        return tag
    base = tag.split("-", 1)[0]
    if base in _WHISPER_LANGUAGES:
        return base
    return None            # unknown: let the model detect the language


def _wav_to_float32(raw: bytes, target_rate: int = 16_000):
    """Decode a 16-bit PCM WAV to a mono float32 array at `target_rate`.

    faster-whisper's file path goes through PyAV, and the current PyAV and
    faster-whisper releases no longer agree on the container API, so the audio
    is decoded here instead. The `wave` and `audioop` modules are in the
    standard library, which keeps a hard dependency out of the offline path.
    """
    import io
    import wave

    import numpy as np

    with wave.open(io.BytesIO(raw), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())

    if width != 2:
        raise ValueError(f"only 16-bit PCM WAV is supported, got {width * 8}-bit")

    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)

    if rate != target_rate:
        try:
            import audioop

            resampled, _state = audioop.ratecv(
                (samples * 32768.0).astype("<i2").tobytes(), 2, 1,
                rate, target_rate, None)
            samples = np.frombuffer(resampled, dtype="<i2").astype(np.float32) / 32768.0
        except Exception:
            # audioop was removed in Python 3.13. Linear interpolation is
            # enough for speech fed to a transcription model.
            count = max(1, int(round(len(samples) * target_rate / float(rate))))
            positions = np.linspace(0.0, len(samples) - 1, num=count,
                                    dtype=np.float32)
            samples = np.interp(positions, np.arange(len(samples), dtype=np.float32),
                                samples).astype(np.float32)
    return samples


class WhisperSTT(STTBackend):
    """Local, offline transcription. No key, no network, no cloud.

    `faster-whisper` is preferred: it runs on CPU through CTranslate2 and does
    not drag in PyTorch, which matters on a laptop that is already running a
    local LLM. `openai-whisper` is accepted as a fallback.
    """

    name = "whisper"
    _model = None
    _model_name = "base"
    _engine = None

    def __init__(self, model_name: str = "base"):
        self._load(model_name)
        self.available = True

    def _load(self, model_name: str) -> None:
        if (WhisperSTT._model is not None
                and WhisperSTT._model_name == model_name
                and WhisperSTT._engine is not None):
            return
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError:
            WhisperSTT._engine = "openai"
            try:
                import whisper  # type: ignore
            except ImportError as e:
                raise RuntimeError(
                    "neither faster-whisper nor openai-whisper is installed") from e
            WhisperSTT._model = whisper.load_model(model_name)
            WhisperSTT._model_name = model_name
            return
        WhisperSTT._engine = "faster"
        WhisperSTT._model = WhisperModel(model_name, device="cpu",
                                        compute_type="int8")
        WhisperSTT._model_name = model_name

    def transcribe_wav(self, raw: bytes, language: str = "") -> str:
        """Transcribe a WAV byte string. No microphone and no key needed.

        This is the path the phone listener uses: the handset records the
        utterance, the laptop does the recognition, and no audio or text is
        ever handed to a third party.
        """
        import tempfile

        if not raw:
            return ""
        self._load(self._model_name)

        if WhisperSTT._engine == "faster":
            audio = _wav_to_float32(raw)
            segments, _info = WhisperSTT._model.transcribe(
                audio, language=_whisper_language(language), beam_size=1)
            return textutil.collapse_ws(
                " ".join(segment.text for segment in segments))

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            path = fh.name
            fh.write(raw)
        try:
            result = WhisperSTT._model.transcribe(path)
            return textutil.collapse_ws(result.get("text", ""))
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def listen(self, timeout: float = 8.0) -> str:
        import tempfile

        import speech_recognition as sr  # type: ignore

        r = sr.Recognizer()
        with sr.Microphone() as source:
            try:
                audio = r.listen(source, timeout=timeout, phrase_time_limit=max(4.0, timeout))
            except Exception:
                return ""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            path = fh.name
        try:
            with open(path, "wb") as out:
                out.write(audio.get_wav_data())
            return self.transcribe_wav(open(path, "rb").read())
        except Exception:
            return ""
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


def transcribe_wav_bytes(raw: bytes, model_name: str = "base",
                         language: str = "") -> str:
    """Transcribe audio locally, loading the model on first use.

    Raises RuntimeError when no local engine is installed. Callers must handle
    that: a missing speech-to-text model is a degraded feature, not a reason
    to refuse the instruction.
    """
    if not raw:
        return ""
    backend = WhisperSTT(model_name)
    return backend.transcribe_wav(raw, language=language)


def pick_stt(prefer: str = "") -> STTBackend:
    order = [prefer] if prefer else ["whisper", "speech_recognition"]
    for name in order:
        if not name:
            continue
        try:
            return {"whisper": WhisperSTT, "speech_recognition": SpeechRecognitionSTT}[name]()
        except Exception:
            continue
    return UnavailableSTT()


# --------------------------------------------------------------------------
# global hotkeys (background listener thread)
# --------------------------------------------------------------------------
class HotkeyListener:
    """Registers global hotkeys and calls back from a daemon thread.

    This is the user-facing control surface: emergency stop, pause/resume,
    "what are you doing", and a push-to-talk key for voice commands.
    """

    def __init__(self) -> None:
        self._bindings: dict[str, tuple[int, ...]] = {}
        self._callbacks: dict[str, Callable[[], None]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.enabled = True

    def register(self, combo: str, callback: Callable[[], None], name: str = "") -> str:
        key = textutil.describe_hotkey(combo)
        self._bindings[key] = self._keys(combo)
        self._callbacks[key] = callback
        return key

    @staticmethod
    def _keys(combo: str) -> tuple[int, ...]:
        from ..utils import win32

        out = []
        for token in textutil.parse_hotkey(combo):
            vk = win32.VK_MAP.get(textutil.normalize_key(token))
            if vk is not None:
                out.append(vk)
        return tuple(out)

    def start(self) -> bool:
        if not self.enabled or not self._bindings:
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ufo-hotkeys", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def _loop(self) -> None:  # pragma: no cover - needs a live desktop session
        import ctypes

        user32 = getattr(ctypes, "windll", None) and ctypes.windll.user32
        if not user32:
            return
        pressed: set[int] = set()
        while not self._stop.is_set():
            if not user32.GetForegroundWindow() and not self._callbacks:
                self._stop.wait(0.1)
                continue
            down = {vk for vk in win32_keys() if user32.GetAsyncKeyState(vk) & 0x8000}
            for combo, keys in self._bindings.items():
                if keys and set(keys).issubset(down):
                    cb = self._callbacks.get(combo)
                    if cb and keys not in {self._bindings[c] for c in pressed}:
                        threading.Thread(target=cb, daemon=True).start()
            pressed = {c for c, k in self._bindings.items() if set(k).issubset(down)}
            del pressed
            self._stop.wait(0.03)


def win32_keys() -> list[int]:
    from ..utils import win32

    return sorted(set(win32.VK_MAP.values()))


# --------------------------------------------------------------------------
# OS notifications
# --------------------------------------------------------------------------
def notify(title: str, message: str, *, sound: bool = True,
           icon: str = "", timeout: int = 10) -> bool:
    """A Windows toast via PowerShell, falling back to a message box."""
    if platform.system() != "Windows":
        print(f"[NOTIFY] {title}: {message}")
        return False
    safe_title = str(title).replace("'", "")
    safe_msg = str(message).replace("'", "")
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,"
        "ContentType=WindowsRuntime] > $null;"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode("
        f"'{safe_title}')) > $null;"
        "$t.GetElementsByTagName('text')[1].AppendChild($t.CreateTextNode("
        f"'{safe_msg}')) > $null;"
        "$n=[Windows.UI.Notifications.ToastNotification]::new($t);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        f"'{icon or sys.argv[0] or 'PowerShell'}').Show($n);"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if sound:
            beep()
        return True
    except Exception:
        try:
            ctypes_windll_messagebox(title, message)
            return True
        except Exception:
            return False


def ctypes_windll_messagebox(title: str, message: str, flags: int = 0x40) -> None:
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, str(message), str(title), flags)


def beep(frequency: int = 800, duration_ms: int = 180) -> None:
    """Audible feedback for events a blind user cannot see."""
    if platform.system() != "Windows":
        return
    try:
        import winsound

        winsound.Beep(int(frequency), int(duration_ms))
    except Exception:
        pass


def beep_pattern(kind: str = "ok") -> tuple[int, int]:
    """Distinct tones for ok / warning / error so status is audible."""
    patterns = {
        "ok": (880, 120),
        "done": (660, 120),
        "warn": (520, 180),
        "error": (330, 260),
        "confirm": (740, 90),
    }
    freq, dur = patterns.get(kind, (600, 120))
    beep(freq, dur)
    return freq, dur


_default_narrator: Narrator | None = None


def console_narrator(text: str) -> None:
    """Module-level narrator hook the safety guard uses by default."""
    global _default_narrator
    if _default_narrator is None:
        _default_narrator = Narrator(enabled=False, console=True)
    _default_narrator.say(text)


def set_default_narrator(narrator: Narrator | None) -> None:
    global _default_narrator
    _default_narrator = narrator


def get_default_narrator() -> Narrator:
    global _default_narrator
    if _default_narrator is None:
        _default_narrator = Narrator(enabled=_voice_enabled(),
                                     rate=_voice_rate(), volume=_voice_volume())
    return _default_narrator


def _voice_enabled() -> bool:
    from .. import config as config_mod

    return bool(getattr(config_mod.active().voice, "enabled", False))


def _voice_rate() -> int:
    from .. import config as config_mod

    return int(getattr(config_mod.active().voice, "rate", 175))


def _voice_volume() -> float:
    from .. import config as config_mod

    return float(getattr(config_mod.active().voice, "volume", 1.0))


def build_narrator(cfg, *, console: bool = True) -> Narrator:
    """A narrator configured from a Config, persona included."""
    return Narrator(enabled=bool(cfg.voice.enabled), rate=int(cfg.voice.rate),
                    volume=float(cfg.voice.volume), console=console,
                    persona=get_persona(cfg))
