"""OCR — the fallback for UIA-invisible pixels.

Terminals, games, video players, PDFs, remote desktops and Electron apps draw
their own glyphs; there is no accessibility tree for them. OCR recovers the
text so the agent can at least read state, and — combined with
`vision.ground_text` — click a specific word it found.

Engines are tried in order of "already installed", never auto-installed:
    1. tesseract   (needs the Tesseract binary on PATH)
    2. easyocr     (bundled torch weights, fully offline)
    3. rapidocr    (lightweight ONNX, fully offline)
    4. pytesseract-with-Windows-OCR fallback via PowerShell
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

from PIL import Image

from ..errors import OCRUnavailableError
from ..utils import image as imageutil

_MIN_CONFIDENCE = 35.0


@dataclass
class Word:
    text: str
    box: tuple[int, int, int, int]      # local image coords
    confidence: float = 100.0

    @property
    def center(self) -> tuple[int, int]:
        x0, y0, x1, y1 = self.box
        return ((x0 + x1) // 2, (y0 + y1) // 2)

    def to_screen(self, origin: tuple[int, int]) -> tuple[int, int]:
        return self.center[0] + origin[0], self.center[1] + origin[1]


@dataclass
class OCRResult:
    text: str
    words: list[Word] = field(default_factory=list)
    engine: str = ""
    confidence: float = 0.0

    def find(self, needle: str) -> Word | None:
        needle = re.escape((needle or "").strip().lower())
        if not needle:
            return None
        pattern = re.compile(rf"(?<!\w){needle}(?!\w)", re.IGNORECASE)
        for w in self.words:
            if pattern.search(w.text):
                return w
        # fall back to a fuzzy contains match
        for w in self.words:
            if needle.replace(r"\ ", " ").lower() in w.text.lower():
                return w
        return None

    def lines(self) -> list[str]:
        return [ln for ln in (l.strip() for l in self.text.splitlines()) if ln]


# --------------------------------------------------------------------------
# engine discovery
# --------------------------------------------------------------------------
@lru_cache(maxsize=1)
def engine_name() -> str:
    """Which OCR engine is usable, or '' when none is."""
    for name, probe in (("tesseract", _probe_tesseract),
                        ("easyocr", _probe_module("easyocr")),
                        ("rapidocr", _probe_module("rapidocr_onnxruntime")),
                        ("rapidocr", _probe_module("rapidocr")),
                        ("windows_ocr", _probe_windows_ocr)):
        try:
            if probe():
                return name
        except Exception:
            continue
    return ""


def available() -> bool:
    return bool(engine_name())


def _probe_tesseract() -> bool:
    if not shutil.which("tesseract"):
        return False
    try:
        out = subprocess.run(["tesseract", "--list-langs"], capture_output=True,
                             text=True, timeout=8)
        return "eng" in out.stdout
    except Exception:
        return False


def _probe_module(name: str):
    def probe() -> bool:
        __import__(name)
        return True
    return probe


def _probe_windows_ocr() -> bool:
    return os.name == "nt"


@lru_cache(maxsize=4)
def _load_module(name: str):
    return __import__(name)


# --------------------------------------------------------------------------
# engines
# --------------------------------------------------------------------------
def _ocr_tesseract(img: Image.Image) -> OCRResult:
    import pytesseract  # type: ignore

    from pytesseract import Output  # type: ignore

    prepared = _prepare(img)
    data = pytesseract.image_to_data(prepared, output_type=Output.DICT,
                                     config="--psm 11 --oem 3")
    words: list[Word] = []
    lines: dict[tuple, list[Word]] = {}
    n = len(data.get("text", []))
    for i in range(n):
        text = (data["text"][i] or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if not text or conf < _MIN_CONFIDENCE:
            continue
        x, y = int(data["left"][i]), int(data["top"][i])
        w, h = int(data["width"][i]), int(data["height"][i])
        word = Word(text=text, box=(x, y, x + w, y + h), confidence=conf)
        words.append(word)
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)

    def order(items: list[Word]) -> list[Word]:
        return sorted(items, key=lambda w: (w.box[1], w.box[0]))

    text = "\n".join(" ".join(w.text for w in order(v)) for _k, v in
                     sorted(lines.items(), key=lambda kv: order(kv[1])[0].box[1]))
    avg = sum(w.confidence for w in words) / len(words) if words else 0.0
    return OCRResult(text=text, words=words, engine="tesseract", confidence=avg)


def _ocr_easyocr(img: Image.Image) -> OCRResult:
    module = _load_module("easyocr")
    global _EASYOCR_READER
    try:
        reader = _EASYOCR_READER
    except NameError:
        reader = None
    if reader is None:
        reader = module.Reader(["en"], gpu=False, verbose=False)
        globals()["_EASYOCR_READER"] = reader
    prepared = _prepare(img)
    entries = reader.readtext(prepared, detail=1, paragraph=False)
    words = []
    for box, text, conf in entries:
        xs = [int(p[0]) for p in box]
        ys = [int(p[1]) for p in box]
        words.append(Word(text=str(text), box=(min(xs), min(ys), max(xs), max(ys)),
                          confidence=float(conf) * 100))
    words.sort(key=lambda w: (w.box[1], w.box[0]))
    text = "\n".join(w.text for w in words)
    avg = sum(w.confidence for w in words) / len(words) if words else 0.0
    return OCRResult(text=text, words=words, engine="easyocr", confidence=avg)


def _ocr_rapidocr(img: Image.Image) -> OCRResult:
    module = None
    for name in ("rapidocr_onnxruntime", "rapidocr"):
        try:
            module = _load_module(name)
            break
        except Exception:
            continue
    if module is None:
        raise OCRUnavailableError("rapidocr not installed")
    global _RAPIDOCR_ENGINE
    try:
        engine = _RAPIDOCR_ENGINE
    except NameError:
        engine = None
    if engine is None:
        ctor = getattr(module, "RapidOCR", None)
        if ctor is None:
            raise OCRUnavailableError("rapidocr has no RapidOCR entry point")
        engine = ctor()
        globals()["_RAPIDOCR_ENGINE"] = engine
    result, _elapse = engine(_prepare(img))
    words: list[Word] = []
    for entry in result or []:
        box, text, conf = entry[0], entry[1], entry[2]
        xs = [int(p[0]) for p in box]
        ys = [int(p[1]) for p in box]
        words.append(Word(text=str(text), box=(min(xs), min(ys), max(xs), max(ys)),
                          confidence=float(conf) * 100))
    words.sort(key=lambda w: (w.box[1], w.box[0]))
    avg = sum(w.confidence for w in words) / len(words) if words else 0.0
    return OCRResult(text="\n".join(w.text for w in words), words=words,
                     engine="rapidocr", confidence=avg)


def _ocr_windows_ocr(img: Image.Image) -> OCRResult:
    """Call the Windows.Media.Ocr WinRT API through a PowerShell script."""
    if os.name != "nt":
        raise OCRUnavailableError("Windows OCR is only available on Windows.")
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
        prepared = _prepare(img)
        prepared.save(fh.name)
        path = fh.name
    try:
        script = _WINDOWS_OCR_PS.replace("__PATH__", path.replace("'", "''"))
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        raise OCRUnavailableError(f"Windows OCR call failed: {e}") from e
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if out.returncode != 0:
        raise OCRUnavailableError(out.stderr.strip()[:300] or "Windows OCR failed")
    import json

    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError as e:
        raise OCRUnavailableError(f"Windows OCR returned non-JSON: {e}") from e
    words = [
        Word(text=str(w["t"]), box=tuple(int(v) for v in w["b"]), confidence=100.0)
        for w in data.get("words", [])
    ]
    return OCRResult(text=data.get("text", ""), words=words, engine="windows_ocr",
                     confidence=100.0 if words else 0.0)


_WINDOWS_OCR_PS = r"""
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $resultType) {
  $task = $asTaskGeneric.MakeGenericMethod($resultType).Invoke($null, @($op))
  $task.Wait(-1) | Out-Null
  $task.Result
}
[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine,Windows.Media.Ocr,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime] | Out-Null
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync('__PATH__')) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$words = @()
foreach ($line in $result.Lines) {
  foreach ($w in $line.Words) {
    $r = $w.BoundingRect
    $words += @{ t = $w.Text; b = @([int]$r.X, [int]$r.Y, [int]($r.X + $r.Width), [int]($r.Y + $r.Height)) }
  }
}
@{ text = $result.Text; words = $words } | ConvertTo-Json -Depth 4 -Compress
"""


_ENGINES = {
    "tesseract": _ocr_tesseract,
    "easyocr": _ocr_easyocr,
    "rapidocr": _ocr_rapidocr,
    "windows_ocr": _ocr_windows_ocr,
}


def _prepare(img: Image.Image) -> Image.Image:
    """Upscale small text and flatten transparency; OCR needs both."""
    out = img.convert("RGB")
    if out.width and out.width < 1600:
        out = imageutil.upscale(out, 1600 / out.width)
    return out


def read_text(img: Image.Image, engine: str = "", min_confidence: float = 0.0) -> OCRResult:
    """Run OCR. Raises OCRUnavailableError when no engine is installed."""
    chosen = engine or engine_name()
    if not chosen:
        raise OCRUnavailableError(
            "No OCR engine available. Install one of: "
            "  pip install easyocr          (offline, ~1GB with torch)\n"
            "  pip install rapidocr-onnxruntime\n"
            "  winget install UB-Mannheim.TesseractOCR  (needs 'eng' traineddata)\n"
            "Or disable it with vision.ocr_enabled = false."
        )
    fn = _ENGINES.get(chosen)
    if fn is None:
        raise OCRUnavailableError(f"Unknown OCR engine: {chosen}")
    result = fn(img)
    if min_confidence:
        result.words = [w for w in result.words if w.confidence >= min_confidence]
    return result


def try_read_text(img: Image.Image) -> Optional[OCRResult]:
    """OCR that never raises — returns None when nothing is available."""
    try:
        return read_text(img)
    except Exception:
        return None
