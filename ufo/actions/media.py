"""Media: playback control, playlists, conversion, trimming, audio extraction.

Video and audio work needs ffmpeg. Where it is missing this says so with the
install line rather than failing inside a subprocess.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from ..errors import ActionError
from ..logging_setup import get_logger

from . import filesystem, shell, system

log = get_logger(__name__)


def control(target: str = "", args: str = "") -> str:
    """play, pause, next, previous, stop."""
    verb = (target or args).strip().lower()
    if "play" in verb and "pause" not in verb:
        return system.media_play_pause()
    if "pause" in verb:
        return system.media_play_pause()
    if "next" in verb:
        return system.media_next()
    if "previous" in verb or "prev" in verb or "back" in verb:
        return system.media_previous()
    if "stop" in verb:
        return shell.run_powershell(
            "(New-Object -ComObject WScript.Shell).SendKeys('q')") and "stopped"
    return "say play, pause, next, previous or stop"


def playlist(target: str = "", args: str = "") -> str:
    """Add a folder to the current player, or list what is playing."""
    if not target:
        return str(shell.run_powershell(
            "Get-AudioDevice | Select-Object Name | Format-Table"))
    folder = Path(filesystem.expand(target))
    if not folder.is_dir():
        return f"no such folder: {folder}"
    playable = [p for p in folder.iterdir()
                if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".flac", ".mp4")]
    if not playable:
        return f"no playable media in {folder}"
    return str(shell.run_powershell(
        f"Start-Process {folder}")) and f"opened {folder} in the default player"


def _require_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise ActionError(
            "ffmpeg is not on PATH. Install it with `winget install "
            "Gyan.FFmpeg` or from https://ffmpeg.org/download.html")
    return found


def convert(target: str = "", args: str = "") -> str:
    """Convert a media file. target is the file, args the output extension."""
    path = Path(filesystem.expand(target or ""))
    suffix = (args or "").strip().lstrip(".") or "mp3"
    if not path.is_file():
        return f"no such file: {path}"
    ffmpeg = _require_ffmpeg()
    out = path.with_suffix(f".{suffix}")
    shell.run_cmd(f'"{ffmpeg}" -y -i "{path}" "{out}"')
    return f"converted to {out.name}"


def trim_merge(target: str = "", args: str = "") -> str:
    """Trim a clip. args is 'start:duration', e.g. '00:00:10 00:00:30'."""
    path = Path(filesystem.expand(target or ""))
    if not path.is_file():
        return f"no such file: {path}"
    parts = args.split()
    if len(parts) < 2:
        return 'give me a start and a duration, e.g. "00:00:10 00:00:30"'
    ffmpeg = _require_ffmpeg()
    out = path.with_name(f"{path.stem}_trim{path.suffix}")
    shell.run_cmd(f'"{ffmpeg}" -y -ss {parts[0]} -i "{path}" -t {parts[1]} -c copy "{out}"')
    return f"wrote {out.name}"


def extract_audio(target: str = "", args: str = "") -> str:
    """Pull the audio track out of a video."""
    path = Path(filesystem.expand(target or ""))
    if not path.is_file():
        return f"no such file: {path}"
    ffmpeg = _require_ffmpeg()
    out = path.with_suffix(".mp3")
    shell.run_cmd(f'"{ffmpeg}" -y -i "{path}" -vn -acodec libmp3lame "{out}"')
    return f"wrote {out.name}"


def compress(target: str = "", args: str = "") -> str:
    """Make a video smaller with a lower bitrate."""
    path = Path(filesystem.expand(target or ""))
    if not path.is_file():
        return f"no such file: {path}"
    ffmpeg = _require_ffmpeg()
    out = path.with_name(f"{path.stem}_small{path.suffix}")
    before = path.stat().st_size
    shell.run_cmd(f'"{ffmpeg}" -y -i "{path}" -crf 30 -preset slow "{out}"')
    after = out.stat().st_size if out.exists() else before
    return (f"{out.name}: {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB")


def subtitles(target: str = "", args: str = "") -> str:
    return ("whisper needs the local engine for transcription. "
            "`pip install faster-whisper`, then ask me to transcribe the file.")


# --------------------------------------------------------------------------
# images
#
# Image work needs Pillow; background removal needs rembg. Where the library is
# absent the handler names the install line rather than raising from inside it.
# --------------------------------------------------------------------------
def _image(target: str = "", args: str = "") -> Path:
    from . import filesystem

    path = Path(filesystem.expand(target or args or ""))
    if not path.is_file():
        raise ActionError(f"no such image: {path}")
    return path


def _pillow():
    try:
        from PIL import Image  # type: ignore

        return Image
    except ImportError:
        raise ActionError("image work needs Pillow: `pip install Pillow`")


def image_edit(target: str = "", args: str = "") -> str:
    """Crop, rotate or resize. args is 'WxH' or 'rotate 90' or 'crop x,y,w,h'."""
    Image = _pillow()
    path = _image(target, args)
    picture = Image.open(path)
    verb, _, value = args.partition(" ")
    verb, value = verb.lower(), value.strip()
    out = path.with_name(f"{path.stem}_edited{path.suffix}")
    if verb.startswith("rotate"):
        degrees = int(re.sub(r"\D", "", value) or 90)
        picture.rotate(-degrees, expand=True).save(out)
        return f"rotated {degrees} degrees -> {out.name}"
    if verb.startswith("crop") and "," in value:
        x, y, w, h = (int(v) for v in value.split(","))
        picture.crop((x, y, x + w, y + h)).save(out)
        return f"cropped -> {out.name}"
    if "x" in verb:
        width, _, height = verb.partition("x")
        picture.resize((int(width), int(height) if height else None)).save(out)
        return f"resized -> {out.name}"
    return "say 'WxH', 'rotate 90' or 'crop x,y,w,h'"


def watermark(target: str = "", args: str = "") -> str:
    """Stamp text onto an image."""
    Image = _pillow()
    path = _image(target, args)
    text = args.strip() or "draft"
    picture = Image.open(path).convert("RGBA")
    from PIL import ImageDraw  # type: ignore

    overlay = Image.new("RGBA", picture.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).text(
        (10, picture.height - 24), text, fill=(255, 255, 255, 200))
    out = path.with_name(f"{path.stem}_watermarked{path.suffix}")
    Image.alpha_composite(picture, overlay).convert("RGB").save(out)
    return f"watermarked -> {out.name}"


def image_batch(target: str = "", args: str = "") -> str:
    """Resize every image in a folder to one size."""
    from . import filesystem

    root = Path(filesystem.expand(target or "."))
    if not root.is_dir():
        return f"no such folder: {root}"
    size = next((w for w in args.split() if "x" in w), "1280")
    width, _, height = size.partition("x")
    Image = _pillow()
    done = 0
    for item in root.iterdir():
        if item.suffix.lower() not in (".jpg", ".jpeg", ".png", ".bmp"):
            continue
        try:
            picture = Image.open(item)
            picture.thumbnail((int(width), int(height) if height else 10 ** 6))
            picture.save(item.with_name(f"{item.stem}_{size}{item.suffix}"))
            done += 1
        except (OSError, ValueError):
            continue
    return f"resized {done} image(s) to fit {size}"


def creative(target: str = "", args: str = "") -> str:
    """A simple generated image: a gradient, a shape, or a colour field."""
    Image = _pillow()
    width, height = 1024, 768
    colour = args.strip() or "indigo"
    from PIL import ImageColor, ImageDraw  # type: ignore

    try:
        base = ImageColor.getrgb(colour)
    except ValueError:
        return f"{colour!r} is not a colour name or #rrggbb value"
    picture = Image.new("RGB", (width, height), base)
    draw = ImageDraw.Draw(picture)
    for y in range(height):                       # a vertical light falloff
        shade = tuple(int(c * (1 - 0.4 * y / height)) for c in base)
        draw.line([(0, y), (width, y)], fill=shade)
    out = Path.home() / "Pictures" / f"ufo-{colour.strip('#')}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    picture.save(out)
    return f"made {out} ({width}x{height})"


def generate_image(target: str = "", args: str = "") -> str:
    """Text-to-image needs a model. This says which, rather than guessing."""
    return ("generating a picture from a description needs a local diffusion "
            "model (Stable Diffusion) or an image API. Neither is installed. "
            "I can generate the gradient and shape images with `creative` "
            "instead.")


def remove_background(target: str = "", args: str = "") -> str:
    """Cut the subject out of an image."""
    try:
        from rembg import remove  # type: ignore
    except ImportError:
        raise ActionError("background removal needs rembg: `pip install rembg`")
    path = _image(target, args)
    out = path.with_name(f"{path.stem}_nobg{path.suffix}")
    out.write_bytes(remove(path.read_bytes()))
    return f"background removed -> {out.name}"


def enhance(target: str = "", args: str = "") -> str:
    """Sharpen and lift contrast."""
    from PIL import ImageEnhance  # type: ignore

    path = _image(target, args)
    picture = Image.open(path)
    picture = ImageEnhance.Sharpness(picture).enhance(2.0)
    picture = ImageEnhance.Contrast(picture).enhance(1.15)
    out = path.with_name(f"{path.stem}_enhanced{path.suffix}")
    picture.save(out)
    return f"enhanced -> {out.name}"


def palette(target: str = "", args: str = "") -> str:
    """The dominant colours in an image."""
    Image = _pillow()
    path = _image(target, args)
    picture = Image.open(path).convert("RGB").resize((64, 64))
    colours = picture.getcolors(64 * 64)
    if not colours:
        return "could not count the colours"
    colours.sort(reverse=True)
    return "\n".join(f"#{r:02X}{g:02X}{b:02X}  {count}" for count, (r, g, b) in colours[:10])


# --------------------------------------------------------------------------
# audio and voice
# --------------------------------------------------------------------------
def record(target: str = "", args: str = "") -> str:
    return _need_sounddevice("recording")


def record_voice(target: str = "", args: str = "") -> str:
    return _need_sounddevice("recording")


def _need_sounddevice(what: str) -> str:
    try:
        import sounddevice  # type: ignore # noqa: F401
    except ImportError:
        raise ActionError(f"{what} needs sounddevice: `pip install sounddevice`")
    return (f"recording is started from the app that owns the device. Open the "
            f"app and record there; I can transcribe the result afterwards.")


def audio_trim_speed(target: str = "", args: str = "") -> str:
    return trim_merge(target, args)


def denoise(target: str = "", args: str = "") -> str:
    return ("cleaning audio needs a tool such as Audacity or ffmpeg's "
            "afftdn filter. Give me the file and I will run: "
            "ffmpeg -i in.wav -af afftdn out.wav")


def audio_device(target: str = "", args: str = "") -> str:
    return str(shell.run_powershell(
        "Get-CimInstance Win32_SoundDevice | Select-Object Name, Status | "
        "Format-Table -AutoSize"))


def test_mic(target: str = "", args: str = "") -> str:
    return str(shell.run_powershell(
        "Get-CimInstance Win32_SoundDevice | Where-Object {$_.Name -like '*input*' "
        "or $_.Name -like '*microphone*'} | Select-Object Name, Status | "
        "Format-Table -AutoSize"))


def speak(target: str = "", args: str = "") -> str:
    """Say some text aloud."""
    text = f"{target} {args}".strip()
    if not text:
        from . import keyboard

        text = keyboard.get_clipboard()
    if not text:
        return "give me the text to say, or put it on the clipboard"
    try:
        import pyttsx3  # type: ignore

        engine = pyttsx3.init()
        engine.say(text[:1500])
        engine.runAndWait()
        return "said that"
    except ImportError:
        raise ActionError("speaking needs pyttsx3: `pip install pyttsx3`")


def transcribe(target: str = "", args: str = "") -> str:
    """Transcribe audio with the local whisper engine."""
    from . import filesystem

    path = Path(filesystem.expand(target or args or ""))
    if not path.is_file():
        return f"no such file: {path}"
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        raise ActionError(
            "transcription needs faster-whisper: `pip install faster-whisper`")
    model = WhisperModel("small")
    segments, _info = model.transcribe(str(path))
    return "\n".join(segment.text.strip() for segment in segments)


def voice_commands(target: str = "", args: str = "") -> str:
    return ("voice commands are the listen path: run `ufo listen` and speak. "
            "This does not hold the microphone open behind your back.")
