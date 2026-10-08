"""Voice instructions from a phone on the same Wi-Fi.

How it works, and why it works this way:

  Your phone opens a small page in its **own browser** at
  `http://<laptop-ip>:8766`. That page calls `getUserMedia` for the phone's
  microphone and transcribes the audio **on the phone** using the browser's
  built-in Web Speech API. It then POSTs the finished *text* to the laptop.
Consequences worth knowing:
  * No microphone permission for the laptop, no audio driver juggling, and no
    30 MB of audio crossing the network.
  * It works with **no API key at all** — useful when the LLM quota is gone.
  * Transcription quality is the phone's engine (Google's on Android, Safari's
    on iOS). Note that those engines are cloud services: the recording leaves
    the phone even though the transcript is what crosses the LAN. Set
`phone.transcription = "local"` to keep the audio on your own network —
     the page then records raw WAV and the laptop transcribes it with
     faster-whisper, which needs a key for nothing and no network at all.
     That is the default here, because this install is local-only: there is no
     server-side transcription mode, so no path can send your voice to a
     third party.

Security posture: the phone page is **unauthenticated by design** so a browser
can load it, but it can only *submit an instruction*. Submission never runs
anything on its own — the instruction is spoken back and the laptop waits for
your confirmation, unless you set `phone.auto_run`. The allowlist, the risk
classifier and the emergency stop all still apply. The listener binds to the
LAN, so it is reachable by other devices on your Wi-Fi; keep `auto_run` off on
an untrusted network.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from uuid import uuid4

from ..logging_setup import get_logger, record
from ..safety.secrets import redact
from .persona import Persona

log = get_logger(__name__)

MAX_BODY = 8_000_000  # ~5 min of 16 kHz mono PCM, base64'd


@dataclass
class Instruction:
    """One spoken instruction from the phone."""

    text: str
    source: str = "phone"          # phone | browser | local
    language: str = "en-US"
    received: float = field(default_factory=time.time)
    identifier: str = field(default_factory=lambda: uuid4().hex[:8])
    transcript_audio: str = ""     # base64 WAV, only when transcription=local
    sample_rate: int = 16_000

    def to_dict(self) -> dict:
        return {"id": self.identifier, "text": self.text, "source": self.source,
                "language": self.language, "received": self.received,
                "has_audio": bool(self.transcript_audio)}


# --------------------------------------------------------------------------
# the page the phone loads
# --------------------------------------------------------------------------
PHONE_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>UFO</title>
<style>
  :root { --bg:#0b0d12; --card:#151923; --ink:#e8ecf4; --dim:#8b93a7;
          --accent:#4da3ff; --live:#ff5a5a; --ok:#3ddc97; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin:0; font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
         background:var(--bg); color:var(--ink);
         padding: max(16px, env(safe-area-inset-top)) 16px calc(28px + env(safe-area-inset-bottom));
         display:flex; flex-direction:column; gap:14px; min-height:100dvh; }
  h1 { font-size:15px; letter-spacing:.22em; text-transform:uppercase;
       color:var(--dim); margin:0; font-weight:600; }
  #status { font-size:14px; color:var(--dim); min-height:1.5em; }
  .wave { height:56px; background:var(--card); border-radius:12px;
          display:flex; align-items:center; justify-content:center; gap:3px; overflow:hidden; }
  .wave i { width:3px; height:8px; background:var(--accent); border-radius:2px;
            display:block; transition:height .09s linear; }
  button { width:100%; padding:18px; border:0; border-radius:14px;
           background:var(--accent); color:#04121f; font:600 17px/1 inherit; }
  button:active { transform:scale(.985); }
  button[disabled] { opacity:.4; }
  #talk { background:var(--card); color:var(--ink); border:1px solid #232a38; }
  #talk.live { background:var(--live); color:#fff; border-color:var(--live); }
  textarea { width:100%; min-height:92px; resize:vertical; padding:13px;
             border-radius:12px; border:1px solid #232a38; background:var(--card);
             color:var(--ink); font:inherit; }
  #log { background:var(--card); border-radius:12px; padding:12px;
         font-size:13px; color:var(--dim); max-height:34vh; overflow:auto; }
  #log div { padding:5px 0; border-bottom:1px solid #1d2331; }
  #log div:last-child { border:0; }
  #log b { color:var(--ink); font-weight:600; }
  .ok { color:var(--ok) !important; }
</style>
</head>
<body>
<h1>UFO &middot; voice</h1>
<div id="status">Ready.</div>
<div class="wave" id="wave"></div>
<button id="talk">Hold to speak</button>
<textarea id="text" placeholder="...or type an instruction"></textarea>
<button id="send">Send instruction</button>
<div id="log"></div>

<script>
const LANG = __LANG__;
const MODE = __MODE__;          // "browser" | "local"
const $ = id => document.getElementById(id);
const logEl = $("log");
function log(msg, ok) {
  const d = document.createElement("div");
  d.innerHTML = msg;
  if (ok) d.className = "ok";
  logEl.prepend(d);
}

// --- level meter ---
const wave = $("wave");
for (let i = 0; i < 28; i++) wave.appendChild(document.createElement("i"));
const bars = [...wave.children];

// --- recognition ---
let rec = null, listening = false, finalText = "";
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;

function startRecognition() {
  if (!SR) { $("status").textContent = "This browser has no speech recognition."; return false; }
  rec = new SR();
  rec.lang = LANG; rec.interimResults = true; rec.continuous = false;
  rec.onresult = e => {
    let interim = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const r = e.results[i];
      if (r.isFinal) finalText += r[0].transcript + " ";
      else interim += r[0].transcript;
    }
    $("text").value = (finalText + interim).trim();
  };
  rec.onerror = e => { $("status").textContent = "Mic error: " + e.error; stop(); };
  rec.onend = () => { if (listening) { try { rec.start(); } catch (e) {} } };
  try { rec.start(); listening = true; return true; }
  catch (e) { $("status").textContent = "Could not start the mic: " + e; return false; }
}

function stop() {
  listening = false;
  if (rec) { try { rec.stop(); } catch (e) {} }
  $("talk").classList.remove("live");
  $("talk").textContent = "Hold to speak";
  bars.forEach(b => b.style.height = "8px");
  $("status").textContent = "Ready.";
}

// --- microphone level (works even when the laptop is transcribing) ---
let audioCtx = null, analyser = null, meterRaf = 0;
async function startMeter() {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const src = audioCtx.createMediaStreamSource(stream);
  analyser = audioCtx.createAnalyser();
  analyser.fftSize = 256;
  src.connect(analyser);
  const data = new Uint8Array(analyser.frequencyBinCount);
  const tick = () => {
    analyser.getByteFrequencyData(data);
    let i = 0;
    for (const b of bars) {
      const v = data[i++ % data.length] / 255;
      b.style.height = Math.max(8, v * 52) + "px";
    }
    meterRaf = requestAnimationFrame(tick);
  };
  tick();
  return stream;
}

async function recordWav(seconds) {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const src = ctx.createMediaStreamSource(stream);
  const proc = ctx.createScriptProcessor(4096, 1, 1);
  const chunks = [];
  proc.onaudioprocess = e => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  src.connect(proc); proc.connect(ctx.destination);
  await new Promise(r => setTimeout(r, seconds * 1000));
  proc.disconnect();
  let total = 0; chunks.forEach(c => total += c.length);
  const pcm = new Int16Array(total);
  let off = 0;
  for (const c of chunks) {
    for (let i = 0; i < c.length; i++) {
      const s = Math.max(-1, Math.min(1, c[i]));
      pcm[off++] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
  }
  const rate = ctx.sampleRate;
  ctx.close();
  stream.getTracks().forEach(t => t.stop());
  // 44-byte canonical WAV header
  const buf = new ArrayBuffer(44 + pcm.byteLength), v = new DataView(buf);
  const ws = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  ws(0, "RIFF"); v.setUint32(4, 36 + pcm.byteLength, true); ws(8, "WAVE");
  ws(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
  v.setUint16(22, 1, true); v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  ws(36, "data"); v.setUint32(40, pcm.byteLength, true);
  new Uint8Array(buf, 44).set(new Uint8Array(pcm.buffer));
  let bin = ""; const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.length; i += 0x8000)
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return { b64: btoa(bin), rate };
}

async function submit(text, audio) {
  text = (text || "").trim();
  if (!text && !audio) { log("Nothing to send."); return; }
  const body = { text: text, language: LANG };
  if (audio) { body.audio = audio.b64; body.sample_rate = audio.rate; }
  $("status").textContent = "Sending...";
  try {
    const r = await fetch("/instruct", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (j.understood && j.text) {
      $("text").value = j.text;
      log("<b>Heard:</b> " + escapeHtml(j.text), true);
    } else {
      log("<b>Queued:</b> " + escapeHtml(text || "(audio)"));
    }
    if (j.reply) log("<b>UFO:</b> " + escapeHtml(j.reply), true);
    $("status").textContent = "Ready.";
  } catch (e) {
    $("status").textContent = "Send failed: " + e;
    log("Send failed: " + e);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// --- push to talk ---
const talk = $("talk");
const down = async ev => {
  ev.preventDefault();
  if (talk.classList.contains("live")) return;
  talk.classList.add("live"); talk.textContent = "Listening...";
  finalText = ""; $("text").value = "";
  $("status").textContent = "Listening...";
  try { await startMeter(); } catch (e) { /* level meter is optional */ }
  if (MODE === "browser") startRecognition();
};
const up = async ev => {
  ev.preventDefault();
  if (!talk.classList.contains("live")) return;
  $("status").textContent = MODE === "local" ? "Transcribing on the laptop..." : "Sending...";
  const audio = (MODE === "local") ? await recordWav(6) : null;
  stop();
  cancelAnimationFrame(meterRaf);
  await submit($("text").value, audio);
};
talk.addEventListener("pointerdown", down);
talk.addEventListener("pointerup", up);
talk.addEventListener("pointercancel", up);
talk.addEventListener("pointerleave", (e) => { if (e.buttons) up(e); });

$("send").addEventListener("click", () => { const t = $("text").value; $("text").value = ""; submit(t, null); });

log("Connected to <b>" + location.host + "</b>. Hold the button and speak.");
</script>
</body>
</html>
"""


def local_transcription_available() -> bool:
    """Is there a local speech-to-text engine to transcribe with?

    Checked before startup rather than at the first utterance. A missing engine
    otherwise shows up as a failed instruction after the user has walked over to
    their phone, held a button and spoken into it — the worst moment to be told
    something cannot work. The import is the check: `faster-whisper` is heavy
    and optional, and importing it here is the only way to know.
    """
    import importlib.util

    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        return False


def render_page(language: str = "en-US", transcription: str = "local") -> str:
    import json as _json

    safe_lang = _json.dumps(str(language))
    # An unrecognised mode must not silently become "browser": that would put
    # the microphone back on the phone's cloud recogniser without anyone
    # asking for it.
    #
    # "gemini" is gone: this install is local-only, so there is no code path
    # that sends audio to a third party. "local" (faster-whisper on the laptop)
    # is the only mode that keeps the recording on your own network.
    mode = str(transcription or "local").strip().lower()
    if mode not in ("browser", "local"):
        mode = "local"
    safe_mode = _json.dumps(mode)
    return (PHONE_PAGE.replace("__LANG__", safe_lang)
                     .replace("__MODE__", safe_mode))


# --------------------------------------------------------------------------
@dataclass
class PhoneListenerConfig:
    host: str = "0.0.0.0"
    port: int = 8766
    language: str = "en-US"
    transcription: str = "local"
    auto_run: bool = False
    stt_model: str = "base"
    max_instruction_chars: int = 2000
    token: str = ""


class PhoneListener:
    """Tiny HTTP server the phone talks to, plus a queue of instructions."""

    def __init__(self, cfg: PhoneListenerConfig,
                 on_instruction: Optional[Callable[[Instruction], None]] = None):
        self.cfg = cfg
        self.on_instruction = on_instruction
        self.instructions: queue.Queue[Instruction] = queue.Queue()
        self.last: Instruction | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._persona = Persona()
        self._reply: Callable[[str], str] | None = None
        self.requests = 0
        self._lock = threading.Lock()

    # ---- callbacks the CLI wires up ----
    def set_persona(self, persona: Persona) -> None:
        self._persona = persona

    def set_replier(self, fn: Callable[[str], str]) -> None:
        """`fn(text) -> spoken reply`, used for the read-back confirmation."""
        self._reply = fn

    # ---- lifecycle ----
    def start(self) -> int:
        listener = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ufo-phone"
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                log.debug("phone %s", fmt % args)

            def _send(self, code: int, payload, ctype="application/json",
                      raw: bytes | None = None) -> None:
                body = raw if raw is not None else json.dumps(
                    payload, default=str).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                if path in ("/", "/index.html"):
                    self._send(200, None, "text/html; charset=utf-8",
                               render_page(listener.cfg.language,
                                           listener.cfg.transcription).encode("utf-8"))
                elif path == "/health":
                    self._send(200, {"ok": True, "requests": listener.requests,
                                     "queued": listener.instructions.qsize()})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):  # noqa: N802
                if urllib.parse.urlparse(self.path).path != "/instruct":
                    return self._send(404, {"error": "not found"})
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return self._send(400, {"error": "bad Content-Length"})
                if length > MAX_BODY:
                    return self._send(413, {"error": "recording too large"})
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    return self._send(400, {"error": f"bad JSON: {e}"})
                if not isinstance(body, dict):
                    return self._send(400, {"error": "body must be an object"})
                if listener.cfg.token:
                    supplied = (self.headers.get("X-UFO-Token") or "").strip()
                    if supplied != listener.cfg.token:
                        return self._send(401, {"error": "bad token"})
                try:
                    payload = listener.accept(body)
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                self._send(200, payload)

        try:
            self._server = ThreadingHTTPServer((self.cfg.host, self.cfg.port), Handler)
        except OSError as e:
            raise OSError(
                f"Could not listen on {self.cfg.host}:{self.cfg.port} ({e}). "
                f"Is another copy of UFO already running?"
            ) from e
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="ufo-phone", daemon=True)
        self._thread.start()
        return self.address()[1]

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def address(self) -> tuple[str, int]:
        if self._server is None:
            return (self.cfg.host, self.cfg.port)
        return self._server.server_address[:2]

    def url(self) -> str:
        host, port = self.address()
        if host in ("0.0.0.0", "::", ""):
            host = _lan_ip()
        return f"http://{host}:{port}/"

    @property
    def running(self) -> bool:
        return self._server is not None

    # ---- submission ----
    def accept(self, body: dict) -> dict:
        """Validate one submission and queue it."""
        text = str(body.get("text") or "").strip()
        audio = str(body.get("audio") or "")
        if not text and not audio:
            raise ValueError("send 'text' or 'audio'.")
        if len(text) > self.cfg.max_instruction_chars:
            raise ValueError(
                f"instruction is {len(text)} characters; the limit is "
                f"{self.cfg.max_instruction_chars}.")

        if audio and not text:
            text = self._transcribe(audio, body)

        mode = str(self.cfg.transcription or "browser").strip().lower()
        instruction = Instruction(
            text=text, source=(mode if audio else "phone"),
            language=str(body.get("language") or self.cfg.language),
            transcript_audio=audio,
            sample_rate=int(body.get("sample_rate") or 16_000),
        )
        with self._lock:
            self.requests += 1
            self.last = instruction
        self.instructions.put(instruction)
        record("phone_instruction", id=instruction.identifier,
               text=redact(text)[:160], source=instruction.source)
        log.info("phone instruction: %s", redact(text)[:120])

        reply = ""
        if self._reply is not None and text:
            try:
                reply = self._reply(text)
            except Exception as e:                      # never break the phone
                log.warning("phone reply failed: %s", e)
        return {"ok": True, "id": instruction.identifier, "reply": reply,
                "understood": bool(text), "text": text,
                "will_run_automatically": bool(self.cfg.auto_run)}

    def _transcribe(self, audio_b64: str, body: dict) -> str:
        import base64
        import binascii

        try:
            raw = base64.b64decode(audio_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValueError(f"audio was not valid base64: {e}") from e
        if not raw:
            raise ValueError("audio was empty.")
        if not raw.startswith(b"RIFF"):
            log.warning("phone audio is not a WAV; wrapping raw PCM")
            from .model_speech import _wav_from_pcm

            raw = _wav_from_pcm(raw, rate=int(body.get("sample_rate") or 16_000))
        language = str(body.get("language") or self.cfg.language)

        if str(self.cfg.transcription or "").strip().lower() == "local":
            from . import transcribe_wav_bytes

            try:
                return transcribe_wav_bytes(raw, language=language)
            except RuntimeError as e:
                raise ValueError(
                    f"local transcription is not available: {e}. Install it with "
                    f"'pip install faster-whisper', or set "
                    f"phone.transcription = 'browser' to transcribe on the phone."
                ) from e

        from . import model_speech

        try:
            text = model_speech.transcribe_wav(
                raw, model=self.cfg.stt_model, language=language)
        except model_speech.SpeechUnavailable as e:
            raise ValueError(
                f"could not transcribe the audio: {e}. Set "
                f"phone.transcription = 'local' to transcribe on the laptop "
                f"with no API key, or 'browser' to transcribe on the phone."
            ) from e
        return text

    def wait_for(self, timeout: float | None = None) -> Instruction | None:
        try:
            return self.instructions.get(timeout=timeout)
        except queue.Empty:
            return None


# --------------------------------------------------------------------------
def _lan_ip() -> str:
    """This machine's address on the LAN, for the URL the phone must open."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))          # no packets sent; just picks a route
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
