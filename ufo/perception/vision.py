"""The observation: UIA tree + annotated pixels + OCR, fused into one frame.

`Observation` is what an agent loop actually consumes. It knows the screen
origin, so a pixel coordinate the model picks can be converted back into a
real click position, and it knows what changed since the previous frame, so
the model is not asked to re-read a static screen.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image, ImageDraw

from ..config import Config, VisionConfig
from ..logging_setup import get_logger, record
from ..utils import image as imageutil
from ..utils import text as textutil
from . import ocr, screen, uia
from .uia import Control, WindowInfo

log = get_logger(__name__)


@dataclass
class Observation:
    """Everything the model gets to see for one step."""

    controls: list[Control] = field(default_factory=list)
    windows: list[WindowInfo] = field(default_factory=list)
    image: Optional[Image.Image] = None
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)
    ocr: Optional[ocr.OCRResult] = None
    focus_handle: int = 0
    cursor: tuple[int, int] = (0, 0)
    changed_since_last: float = 0.0
    text: str = ""
    captured_at: float = field(default_factory=time.time)
    _b64: str | None = field(default=None, repr=False)
    _b64_format: str = field(default="png", repr=False)

    # -- encoding --
    @property
    def b64(self) -> str:
        """Base64 PNG of the annotated frame, cached. Empty if there is no frame."""
        if self._b64 is None:
            self._b64 = (
                imageutil.encode(self.image, self._b64_format)
                if self.image is not None else ""
            )
        return self._b64

    def set_encoding(self, fmt: str, quality: int = 70) -> None:
        """Switch to JPEG when bandwidth/token cost matters more than crispness."""
        if fmt == self._b64_format:
            return
        self._b64_format = fmt
        self._b64 = imageutil.encode(self.image, fmt, quality) if self.image else None

    # -- geometry --
    @property
    def origin(self) -> tuple[int, int]:
        return self.bbox[0], self.bbox[1]

    def to_screen(self, x: int, y: int) -> tuple[int, int]:
        return x + self.bbox[0], y + self.bbox[1]

    def to_local(self, x: int, y: int) -> tuple[int, int]:
        return x - self.bbox[0], y - self.bbox[1]

    def contains(self, x: int, y: int) -> bool:
        """Is this **screen** point inside the region that was captured?

        Screen space in, screen space out — the same space `to_local` maps
        *from*. A bbox that was never set is `(0, 0, 0, 0)`, which contains
        nothing, so an unpopulated observation answers False rather than
        claiming the whole desktop.
        """
        left, top, right, bottom = self.bbox
        if right <= left or bottom <= top:
            return False
        return left <= x < right and top <= y < bottom

    def find_control(self, control_id: Optional[int]) -> Control | None:
        if control_id is None:
            return None
        return next((c for c in self.controls if c.id == control_id), None)

    def find_by_name(self, name: str) -> Control | None:
        needle = (name or "").strip().lower()
        if not needle:
            return None
        for c in self.controls:
            if needle in c.name.lower() or (c.automation_id and needle == c.automation_id.lower()):
                return c
        for c in self.controls:
            if needle in f"{c.type} {c.name}".lower():
                return c
        return None

    def foreground(self) -> WindowInfo | None:
        return next((w for w in self.windows if w.is_foreground), None)

    def window_titles(self) -> list[str]:
        return [w.title for w in self.windows]

    # -- prompt --
    def describe(self, verbose: bool = False, include_windows: bool = True) -> str:
        """The text half of the observation, ready for the model."""
        parts: list[str] = []

        fg = self.foreground()
        parts.append(f"SCREEN: {self.bbox[2] - self.bbox[0]}x{self.bbox[3] - self.bbox[1]} "
                     f"virtual desktop, origin ({self.bbox[0]},{self.bbox[1]}), "
                     f"cursor at {self.cursor}.")
        parts.append(f"FOCUSED WINDOW: {fg.title if fg else '(none)'}")

        if include_windows and self.windows:
            titles = ", ".join(
                f'"{w.title}"' + (" [front]" if w.is_foreground else "")
                for w in self.windows[:12]
            )
            extra = f" (+{len(self.windows) - 12} more)" if len(self.windows) > 12 else ""
            parts.append(f"OPEN WINDOWS: {titles}{extra}")

        if self.controls:
            listing = "\n".join(c.to_prompt(verbose) for c in self.controls)
            parts.append(f"CONTROLS ({len(self.controls)}):\n{listing}")
        else:
            parts.append("CONTROLS: (none — the app may draw its own UI; use the "
                         "screenshot and coordinate clicks)")

        if self.ocr and self.ocr.text:
            shown = "\n".join(self.ocr.lines()[:40])
            parts.append(f"SCREEN TEXT (OCR via {self.ocr.engine}, "
                         f"confidence {self.ocr.confidence:.0f}%):\n{shown}")

        if self.text:
            parts.append(f"READABLE CONTENT:\n{textutil.truncate(self.text, 1500)}")

        if self.changed_since_last:
            parts.append(
                f"NOTE: {self.changed_since_last * 100:.1f}% of the screen changed "
                "since the last step."
            )
        return "\n\n".join(parts)

    def to_dict(self) -> dict:
        return {
            "focused_window": (self.foreground().title if self.foreground() else None),
            "controls": [c.to_dict() for c in self.controls],
            "windows": [w.to_dict() for w in self.windows],
            "bbox": list(self.bbox),
            "cursor": list(self.cursor),
            "ocr_engine": self.ocr.engine if self.ocr else None,
            "changed": round(self.changed_since_last, 4),
        }


# --------------------------------------------------------------------------
# annotation
# --------------------------------------------------------------------------
class Annotator:
    """Set-of-Mark: numbered boxes drawn over the controls the model may pick.

    The numbers match `Control.id`, which is what makes a vision model's
    "[7] File" click resolvable to a real UIA element instead of a guess.
    """

    def __init__(self, cfg: VisionConfig | None = None):
        self.cfg = cfg or VisionConfig()
        self.font = imageutil.load_font(13)

    def draw(self, img: Image.Image, controls: list[Control], origin: tuple[int, int],
             highlight: Optional[list[tuple]] = None) -> Image.Image:
        if not self.cfg.annotate:
            return img
        out = img.copy()
        draw = ImageDraw.Draw(out)
        ox, oy = origin
        for c in controls:
            box = (c.rect[0] - ox, c.rect[1] - oy, c.rect[2] - ox, c.rect[3] - oy)
            if box[2] < 0 or box[0] > out.width or box[3] < 0 or box[1] > out.height:
                continue
            color = (220, 40, 40) if c.enabled else (140, 140, 140)
            imageutil.draw_box(draw, box, color=color, width=2,
                               label=str(c.id), font=self.font)
        for region in highlight or []:
            imageutil.draw_box(draw, (region[0] - ox, region[1] - oy,
                                      region[2] - ox, region[3] - oy),
                               color=(40, 160, 255), width=3, font=self.font)
        return out

    def draw_ocr_words(self, img: Image.Image, result: ocr.OCRResult,
                       origin: tuple[int, int], needle: str = "") -> Image.Image:
        """Outline OCR hits, marking the one matching `needle`."""
        if not result or not result.words:
            return img
        out = img.copy()
        draw = ImageDraw.Draw(out)
        ox, oy = origin
        match = result.find(needle) if needle else None
        for w in result.words:
            if match is not None and w is match:
                continue
            box = (w.box[0], w.box[1], w.box[2], w.box[3])
            imageutil.draw_box(draw, box, color=(90, 90, 90), width=1, font=self.font)
        if match is not None:
            imageutil.draw_box(draw, match.box, color=(30, 200, 90), width=3,
                               label="match", font=self.font)
        del ox, oy
        return out


def annotator(cfg: VisionConfig | None = None) -> Annotator:
    return Annotator(cfg)


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------
_last_frame: Image.Image | None = None


def reset_frame_tracking() -> None:
    global _last_frame
    _last_frame = None


def diff_since_last(current: Image.Image) -> float:
    """Fraction of the screen that changed since the previous call."""
    global _last_frame
    if _last_frame is None:
        _last_frame = current.copy()
        return 0.0
    ratio = imageutil.diff_ratio(_last_frame, current)
    _last_frame = current.copy()
    return ratio


def observe_window(window, cfg: Config | None = None,
                   include_ocr: bool = False) -> Observation:
    """Observe one application window."""
    vcfg = (cfg or Config()).vision
    if cfg and cfg.automation.auto_focus_window:
        uia.focus_window(window)

    controls = uia.list_controls(window, cfg=vcfg)
    try:
        cap = screen.capture_window(window)
    except Exception as e:
        log.warning("Window capture failed (%s); falling back to full screen.", e)
        cap = screen.capture_full()

    ocr_result = None
    if include_ocr or (vcfg.ocr_fallback and not controls):
        ocr_result = ocr.try_read_text(cap.image)

    changed = diff_since_last(cap.image) if vcfg.diff_detection else 0.0
    annotated = annotator(vcfg).draw(cap.image, controls, cap.origin)

    obs = Observation(
        controls=controls,
        windows=[w for w in uia.list_all_windows() if uia.is_allowed(w, cfg.safety.allowlist)] if cfg else [],
        image=annotated,
        bbox=cap.bbox,
        ocr=ocr_result,
        focus_handle=int(getattr(window, "handle", 0) or 0),
        cursor=_cursor(),
        changed_since_last=changed,
    )
    record("observe", scope="window", controls=len(controls),
           changed=round(changed, 4), ocr=bool(ocr_result))
    return obs


def observe_screen(cfg: Config | None = None, monitor: int = -1,
                   include_ocr: bool = False) -> Observation:
    """Observe the whole laptop: every monitor, every window, every control.

    This is the observation a whole-laptop agent uses. It is more expensive
    than a single window, so control enumeration is capped and the prompt
    only lists windows the policy allows.
    """
    from ..utils import win32

    vcfg = (cfg or Config()).vision
    cap = screen.capture_monitor(monitor) if monitor >= 0 else screen.capture_full()

    windows = uia.list_all_windows()
    allowed = [w for w in windows
               if (cfg is None or uia.is_allowed(w, cfg.safety.allowlist))]
    blocked = len(windows) - len(allowed)

    controls: list[Control] = []
    for info in allowed:
        if not info.wrapper:
            continue
        try:
            controls.extend(uia.list_controls(info.wrapper, cfg=vcfg,
                                              max_controls=vcfg.max_controls))
        except Exception:
            continue
        if len(controls) >= vcfg.max_controls:
            break
    controls = controls[: vcfg.max_controls]
    for index, c in enumerate(controls, 1):
        c.id = index

    ocr_result = None
    if include_ocr or (vcfg.ocr_fallback and not controls):
        ocr_result = ocr.try_read_text(cap.image)

    changed = diff_since_last(cap.image) if vcfg.diff_detection else 0.0
    annotated = annotator(vcfg).draw(cap.image, controls, cap.origin)

    obs = Observation(
        controls=controls,
        windows=windows,
        image=annotated,
        bbox=cap.bbox,
        ocr=ocr_result,
        focus_handle=win32.foreground_window_handle(),
        cursor=_cursor(),
        changed_since_last=changed,
    )
    if blocked:
        obs.text = (f"{blocked} window(s) are visible but NOT in the allowlist; "
                    "their contents are not listed and must not be touched.")
    record("observe", scope="screen", controls=len(controls), windows=len(windows),
           blocked=blocked, changed=round(changed, 4), ocr=bool(ocr_result))
    return obs


def _cursor() -> tuple[int, int]:
    from ..utils import win32

    return win32.cursor_pos()


def save_frame(obs: Observation, path) -> None:
    if obs.image is not None:
        imageutil.save(obs.image, path)
